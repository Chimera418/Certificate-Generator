from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest
from functools import wraps
from io import BytesIO
from uuid import uuid4

from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for
from PIL import Image, ImageDraw, ImageFont
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import yaml

load_dotenv()
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB upload limit

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCE_EVENTS_DIR = os.path.join(BASE_DIR, "events")


def _resolve_runtime_writable_dir() -> str:
	"""Use BASE_DIR when writable, otherwise fall back to OS temp directory."""
	probe_path = os.path.join(BASE_DIR, ".write_probe")
	try:
		with open(probe_path, "w", encoding="utf-8") as f:
			f.write("ok")
		os.remove(probe_path)
		return BASE_DIR
	except OSError:
		return tempfile.gettempdir()


# Serverless deployments may have a read-only code directory.
RUNTIME_WRITABLE_DIR = _resolve_runtime_writable_dir()
EVENTS_DIR = os.path.join(RUNTIME_WRITABLE_DIR, "events")
GENERATED_DIR = os.path.join(RUNTIME_WRITABLE_DIR, "generated_certificates")
FONT_PATH = os.path.join(BASE_DIR, "fonts", "Montserrat-Bold.ttf")
DEFAULT_FONT_KEY = "montserrat_bold"
FONT_OPTIONS = {
	"montserrat_bold": {
		"label": "Montserrat Bold",
		"filename": "Montserrat-Bold.ttf",
		"css_family": "Montserrat Bold",
		"css_weight": "700",
	}
}

EVENT_STATE_FILE = os.path.join(GENERATED_DIR, "event_states.json")
KV_REST_API_URL = os.environ.get("KV_REST_API_URL", "").strip()
KV_REST_API_TOKEN = os.environ.get("KV_REST_API_TOKEN", "").strip()
KV_EVENT_STATE_KEY = os.environ.get("KV_EVENT_STATE_KEY", "certificate_generator:event_states")
KV_EVENT_INDEX_KEY = os.environ.get("KV_EVENT_INDEX_KEY", "certificate_generator:event_index")
KV_EVENT_CONFIG_PREFIX = os.environ.get("KV_EVENT_CONFIG_PREFIX", "certificate_generator:event_config:")
KV_EVENT_CSV_PREFIX = os.environ.get("KV_EVENT_CSV_PREFIX", "certificate_generator:event_csv:")
_EVENT_STATE_CACHE: dict[str, dict] | None = None
_EVENT_STATE_CACHE_AT = 0.0
_EVENT_STATE_CACHE_TTL_SEC = 2.0

# Event config cache (per-slug) to avoid repeated KV API calls during preview/download
_EVENT_CONFIG_CACHE: dict[str, tuple[dict, float]] = {}  # {slug: (config, timestamp)}
_EVENT_CONFIG_CACHE_TTL_SEC = 30.0  # 30 second TTL for event configs

# Rendered certificate image cache (in-memory) to avoid reprocessing
_RENDERED_CERT_CACHE: dict[str, tuple[bytes, str]] = {}  # {cert_id: (png_bytes, etag)}
_RENDERED_CERT_CACHE_MAX_SIZE = 50  # Keep cache size reasonable

# Font cache (in-memory) to avoid disk I/O on every render
_FONT_CACHE: dict[str, ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}  # {size:font_key -> font}

# Template image cache (in-memory) to avoid repeated open/convert operations
_TEMPLATE_IMAGE_CACHE: dict[str, Image.Image] = {}  # {event_slug -> RGBA Image}

os.makedirs(EVENTS_DIR, exist_ok=True)
os.makedirs(GENERATED_DIR, exist_ok=True)

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
VALIDATION_TYPES = {"player_team", "name_only", "email", "badge_id", "custom", "none"}


def _kv_enabled() -> bool:
	return bool(
		KV_REST_API_TOKEN
		and KV_REST_API_URL
		and KV_REST_API_URL.lower().startswith(("http://", "https://"))
	)


def _read_event_states_from_file() -> dict[str, dict]:
	if not os.path.exists(EVENT_STATE_FILE):
		return {}
	try:
		with open(EVENT_STATE_FILE, encoding="utf-8") as f:
			loaded = json.load(f)
		if isinstance(loaded, dict):
			return {str(k): v for k, v in loaded.items() if isinstance(v, dict)}
	except Exception:
		return {}
	return {}


def _write_event_states_to_file(states: dict[str, dict]) -> None:
	os.makedirs(os.path.dirname(EVENT_STATE_FILE), exist_ok=True)
	with open(EVENT_STATE_FILE, "w", encoding="utf-8") as f:
		json.dump(states, f, indent=2)


def _kv_get_event_states() -> dict[str, dict]:
	url = f"{KV_REST_API_URL.rstrip('/')}/get/{urlparse.quote(KV_EVENT_STATE_KEY, safe='')}"
	req = urlrequest.Request(url, headers={"Authorization": f"Bearer {KV_REST_API_TOKEN}"})
	with urlrequest.urlopen(req, timeout=5) as response:
		payload = json.loads(response.read().decode("utf-8"))
	raw = payload.get("result")
	if raw in (None, ""):
		return {}
	if isinstance(raw, str):
		loaded = json.loads(raw)
	elif isinstance(raw, dict):
		loaded = raw
	else:
		return {}
	if not isinstance(loaded, dict):
		return {}
	return {str(k): v for k, v in loaded.items() if isinstance(v, dict)}


def _kv_set_event_states(states: dict[str, dict]) -> None:
	encoded_states = json.dumps(states, separators=(",", ":"))
	key = urlparse.quote(KV_EVENT_STATE_KEY, safe="")
	value = urlparse.quote(encoded_states, safe="")
	url = f"{KV_REST_API_URL.rstrip('/')}/set/{key}/{value}"
	req = urlrequest.Request(url, method="POST", headers={"Authorization": f"Bearer {KV_REST_API_TOKEN}"})
	with urlrequest.urlopen(req, timeout=5):
		return


def _kv_get_raw(key: str):
	url = f"{KV_REST_API_URL.rstrip('/')}/get/{urlparse.quote(key, safe='')}"
	req = urlrequest.Request(url, headers={"Authorization": f"Bearer {KV_REST_API_TOKEN}"})
	with urlrequest.urlopen(req, timeout=5) as response:
		payload = json.loads(response.read().decode("utf-8"))
	return payload.get("result")


def _kv_set_raw(key: str, value: str) -> None:
	encoded_value = urlparse.quote(value, safe="")
	url = f"{KV_REST_API_URL.rstrip('/')}/set/{urlparse.quote(key, safe='')}/{encoded_value}"
	req = urlrequest.Request(url, method="POST", headers={"Authorization": f"Bearer {KV_REST_API_TOKEN}"})
	with urlrequest.urlopen(req, timeout=5):
		return


def _kv_delete_key(key: str) -> None:
	url = f"{KV_REST_API_URL.rstrip('/')}/del/{urlparse.quote(key, safe='')}"
	req = urlrequest.Request(url, method="POST", headers={"Authorization": f"Bearer {KV_REST_API_TOKEN}"})
	with urlrequest.urlopen(req, timeout=5):
		return


def _load_event_states(force: bool = False) -> dict[str, dict]:
	global _EVENT_STATE_CACHE, _EVENT_STATE_CACHE_AT
	if not force and _EVENT_STATE_CACHE is not None and (time.time() - _EVENT_STATE_CACHE_AT) < _EVENT_STATE_CACHE_TTL_SEC:
		return _EVENT_STATE_CACHE
	states: dict[str, dict]
	if _kv_enabled():
		try:
			states = _kv_get_event_states()
		except (urlerror.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
			states = _read_event_states_from_file()
	else:
		states = _read_event_states_from_file()
	_EVENT_STATE_CACHE = states
	_EVENT_STATE_CACHE_AT = time.time()
	return states


def _save_event_states(states: dict[str, dict]) -> None:
	global _EVENT_STATE_CACHE, _EVENT_STATE_CACHE_AT
	if _kv_enabled():
		try:
			_kv_set_event_states(states)
		except (urlerror.URLError, TimeoutError, OSError, ValueError):
			# Keep local fallback for local dev / temporary outages.
			_write_event_states_to_file(states)
	else:
		_write_event_states_to_file(states)
	_EVENT_STATE_CACHE = states
	_EVENT_STATE_CACHE_AT = time.time()


def _event_state(slug: str, states: dict[str, dict] | None = None) -> dict:
	if states is None:
		states = _load_event_states()
	state = states.get(slug)
	return state if isinstance(state, dict) else {}


def _set_event_state(slug: str, **updates) -> None:
	states = _load_event_states()
	current = _event_state(slug, states)
	next_state = dict(current)
	next_state.update(updates)
	states[slug] = next_state
	_save_event_states(states)
	_ensure_event_slug_registered(slug)


def _bootstrap_runtime_events() -> None:
	"""Copy bundled events into the runtime-writable directory when needed."""
	if EVENTS_DIR == SOURCE_EVENTS_DIR or not os.path.isdir(SOURCE_EVENTS_DIR):
		return
	states = _load_event_states()
	for slug in os.listdir(SOURCE_EVENTS_DIR):
		source_path = os.path.join(SOURCE_EVENTS_DIR, slug)
		target_path = os.path.join(EVENTS_DIR, slug)
		if not os.path.isdir(source_path):
			continue
		if _event_state(slug, states).get("deleted", False):
			continue
		if os.path.exists(os.path.join(target_path, "config.json")):
			continue
		shutil.copytree(source_path, target_path, dirs_exist_ok=True)


# ─── First-run migration ──────────────────────────────────────────────────────

def _migrate_legacy_event() -> None:
	"""Copy certificate_template.png + data.csv into events/think-run-debug/ on first run."""
	slug = "think-run-debug"
	edir = os.path.join(EVENTS_DIR, slug)
	config_path = os.path.join(edir, "config.json")
	if os.path.exists(config_path):
		return
	os.makedirs(edir, exist_ok=True)
	legacy_template = os.path.join(BASE_DIR, "certificate_template.png")
	if os.path.exists(legacy_template):
		shutil.copy2(legacy_template, os.path.join(edir, "template.png"))
	legacy_csv = os.path.join(BASE_DIR, "data.csv")
	if os.path.exists(legacy_csv):
		shutil.copy2(legacy_csv, os.path.join(edir, "data.csv"))
	config = {
		"name": "Think, Run, Debug",
		"slug": slug,
		"active": True,
		"validation_type": "player_team",
		"text_x": 1789,
		"text_y": 1440,
		"font_size": 100,
		"font_color": [50, 34, 24],
		"font_key": DEFAULT_FONT_KEY,
	}
	with open(config_path, "w", encoding="utf-8") as f:
		json.dump(config, f, indent=2)

_bootstrap_runtime_events()
_migrate_legacy_event()


# ─── Event helpers ────────────────────────────────────────────────────────────

def safe_slug(slug: str) -> bool:
	return bool(_SLUG_RE.match(slug)) and ".." not in slug and len(slug) <= 80


def _event_dir(slug: str) -> str:
	return os.path.join(EVENTS_DIR, slug)


def _event_config_path(slug: str) -> str:
	return os.path.join(_event_dir(slug), "config.json")


def _event_config_key(slug: str) -> str:
	return f"{KV_EVENT_CONFIG_PREFIX}{slug}"


def _event_template_path(slug: str) -> str:
	"""Get template path. Returns first matching image file in event directory."""
	event_dir = _event_dir(slug)
	for ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp']:
		path = os.path.join(event_dir, f"template{ext}")
		if os.path.exists(path):
			return path
	return os.path.join(event_dir, "template.png")


def _event_csv_path(slug: str) -> str:
	return os.path.join(_event_dir(slug), "data.csv")


def _event_csv_key(slug: str) -> str:
	return f"{KV_EVENT_CSV_PREFIX}{slug}"


def available_font_options() -> list[dict[str, str]]:
	options: list[dict[str, str]] = []
	for key, meta in FONT_OPTIONS.items():
		path = os.path.join(BASE_DIR, "fonts", meta["filename"])
		if not os.path.exists(path):
			continue
		options.append(
			{
				"key": key,
				"label": meta["label"],
				"path": path,
				"css_family": meta["css_family"],
				"css_weight": meta["css_weight"],
			}
		)
	if options:
		return options
	return [
		{
			"key": DEFAULT_FONT_KEY,
			"label": "Default",
			"path": FONT_PATH,
			"css_family": "Arial",
			"css_weight": "700",
		}
	]


def normalize_font_key(value: str | None, fallback: str = DEFAULT_FONT_KEY) -> str:
	candidate = (value or "").strip().lower()
	available = {option["key"] for option in available_font_options()}
	if candidate in available:
		return candidate
	if fallback in available:
		return fallback
	return next(iter(available), DEFAULT_FONT_KEY)


def resolve_font_option(font_key: str | None) -> dict[str, str]:
	normalized_key = normalize_font_key(font_key)
	for option in available_font_options():
		if option["key"] == normalized_key:
			return option
	return available_font_options()[0]


def _normalize_event_style_config(config: dict) -> None:
	config["font_key"] = normalize_font_key(config.get("font_key"), DEFAULT_FONT_KEY)


def _read_event_config_from_file(slug: str) -> dict | None:
	path = _event_config_path(slug)
	if not os.path.exists(path):
		return None
	try:
		with open(path, encoding="utf-8") as f:
			loaded = json.load(f)
		if isinstance(loaded, dict):
			return loaded
	except Exception:
		return None
	return None


def _write_event_config_to_file(slug: str, config: dict) -> None:
	os.makedirs(_event_dir(slug), exist_ok=True)
	with open(_event_config_path(slug), "w", encoding="utf-8") as f:
		json.dump(config, f, indent=2)


def _load_event_config(slug: str) -> dict | None:
	"""Load event config with caching to minimize KV API calls during preview/download."""
	global _EVENT_CONFIG_CACHE
	
	# Check cache first within TTL
	if slug in _EVENT_CONFIG_CACHE:
		config, cached_at = _EVENT_CONFIG_CACHE[slug]
		if (time.time() - cached_at) < _EVENT_CONFIG_CACHE_TTL_SEC:
			return config
	
	# Cache miss or expired - reload from source
	config = None
	if _kv_enabled():
		try:
			raw = _kv_get_raw(_event_config_key(slug))
			if raw not in (None, ""):
				loaded = json.loads(raw) if isinstance(raw, str) else raw
				if isinstance(loaded, dict):
					config = loaded
					_ensure_event_slug_registered(slug)
		except (urlerror.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
			pass
	
	# Fallback to file if KV missed or on error
	if config is None:
		config = _read_event_config_from_file(slug)
	
	# Cache the result (even if None) to avoid hammering KV on missing configs
	_EVENT_CONFIG_CACHE[slug] = (config, time.time())
	
	# Limit cache size to prevent memory bloat
	if len(_EVENT_CONFIG_CACHE) > 100:
		_EVENT_CONFIG_CACHE.clear()
	
	return config


def _read_event_csv_from_file(slug: str) -> str | None:
	path = _event_csv_path(slug)
	if not os.path.exists(path):
		return None
	try:
		with open(path, newline="", encoding="utf-8") as f:
			return f.read()
	except OSError:
		return None


def load_event_csv_text(slug: str) -> str | None:
	if _kv_enabled():
		try:
			raw = _kv_get_raw(_event_csv_key(slug))
			if isinstance(raw, str):
				return raw
		except (urlerror.URLError, TimeoutError, OSError, ValueError):
			pass
	return _read_event_csv_from_file(slug)


def _write_event_csv_to_file(slug: str, content: str) -> None:
	os.makedirs(_event_dir(slug), exist_ok=True)
	with open(_event_csv_path(slug), "w", encoding="utf-8", newline="") as f:
		f.write(content)


def _load_kv_event_index() -> list[str]:
	if not _kv_enabled():
		return []
	try:
		raw = _kv_get_raw(KV_EVENT_INDEX_KEY)
		if raw in (None, ""):
			return []
		loaded = json.loads(raw) if isinstance(raw, str) else raw
		if not isinstance(loaded, list):
			return []
		seen: set[str] = set()
		result: list[str] = []
		for value in loaded:
			slug = str(value)
			if safe_slug(slug) and slug not in seen:
				seen.add(slug)
				result.append(slug)
		return result
	except (urlerror.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
		return []


def _save_kv_event_index(slugs: list[str]) -> None:
	if not _kv_enabled():
		return
	encoded = json.dumps(slugs, separators=(",", ":"))
	_kv_set_raw(KV_EVENT_INDEX_KEY, encoded)


def _register_event_slug(slug: str) -> None:
	if not _kv_enabled():
		return
	try:
		slugs = _load_kv_event_index()
		if slug not in slugs:
			slugs.append(slug)
			_save_kv_event_index(slugs)
	except (urlerror.URLError, TimeoutError, OSError, ValueError):
		return


def _ensure_event_slug_registered(slug: str) -> None:
	"""Best-effort index repair for cases where config exists but index is stale."""
	if not _kv_enabled() or not safe_slug(slug):
		return
	_register_event_slug(slug)


def _unregister_event_slug(slug: str) -> None:
	if not _kv_enabled():
		return
	try:
		slugs = [value for value in _load_kv_event_index() if value != slug]
		_save_kv_event_index(slugs)
	except (urlerror.URLError, TimeoutError, OSError, ValueError):
		return


def _event_exists(slug: str) -> bool:
	if not safe_slug(slug):
		return False
	# Slugs explicitly marked deleted are considered available for recreation.
	if _event_state(slug).get("deleted", False):
		return False
	return _load_event_config(slug) is not None


def _event_csv_exists(slug: str) -> bool:
	return load_event_csv_text(slug) is not None


def _all_event_slugs() -> list[str]:
	seen: set[str] = set()
	result: list[str] = []
	if os.path.isdir(EVENTS_DIR):
		for slug in os.listdir(EVENTS_DIR):
			if safe_slug(slug) and slug not in seen:
				seen.add(slug)
				result.append(slug)
	# Include event state keys so state/index drift does not hide valid events.
	for slug in _load_event_states().keys():
		if safe_slug(slug) and slug not in seen:
			seen.add(slug)
			result.append(slug)
	for slug in _load_kv_event_index():
		if slug not in seen:
			seen.add(slug)
			result.append(slug)
	return result


def _apply_profile_overrides(config: dict) -> None:
	mode = config.get("mode") or config.get("profile")
	if not mode:
		return
	
	profiles_path = os.path.join(BASE_DIR, "profiles.yaml")
	if not os.path.exists(profiles_path):
		return
	
	try:
		with open(profiles_path, "r", encoding="utf-8") as f:
			profiles_data = yaml.safe_load(f)
			if not isinstance(profiles_data, dict):
				return
			
			profiles = profiles_data.get("profiles", {})
			profile_config = profiles.get(mode)
			
			if isinstance(profile_config, dict):
				# Inherit keys if missing from the event config
				for key in ["text_x", "text_y", "font_size", "font_color", "font_key"]:
					if key in profile_config and key not in config:
						config[key] = profile_config[key]
	except Exception as e:
		print(f"Error loading profile {mode}: {e}")

def load_event(slug: str, states: dict[str, dict] | None = None) -> dict | None:
	if not safe_slug(slug):
		return None
	state = _event_state(slug, states)
	if state.get("deleted", False):
		return None
	config = _load_event_config(slug)
	if config is None:
		return None
	_apply_profile_overrides(config)
	_normalize_event_style_config(config)
	if "active" in state:
		config["active"] = bool(state.get("active"))
	return config


def save_event_config(slug: str, config: dict) -> None:
	_normalize_event_style_config(config)
	_write_event_config_to_file(slug, config)
	# Invalidate cache so next load gets the new config
	global _EVENT_CONFIG_CACHE
	_EVENT_CONFIG_CACHE.pop(slug, None)
	if _kv_enabled():
		try:
			_kv_set_raw(_event_config_key(slug), json.dumps(config, separators=(",", ":")))
			_register_event_slug(slug)
		except (urlerror.URLError, TimeoutError, OSError, ValueError):
			return


def save_event_csv(slug: str, content: str) -> None:
	_write_event_csv_to_file(slug, content)
	if _kv_enabled():
		try:
			_kv_set_raw(_event_csv_key(slug), content)
			_register_event_slug(slug)
		except (urlerror.URLError, TimeoutError, OSError, ValueError):
			return


def delete_event_storage(slug: str) -> None:
	if os.path.isdir(_event_dir(slug)):
		shutil.rmtree(_event_dir(slug))
	# Invalidate cache for deleted event
	global _EVENT_CONFIG_CACHE
	_EVENT_CONFIG_CACHE.pop(slug, None)
	if _kv_enabled():
		try:
			_kv_delete_key(_event_config_key(slug))
			_kv_delete_key(_event_csv_key(slug))
			_unregister_event_slug(slug)
		except (urlerror.URLError, TimeoutError, OSError, ValueError):
			return


def load_all_events(active_only: bool = False) -> list[dict]:
	events = []
	states = _load_event_states()
	for slug in _all_event_slugs():
		config = load_event(slug, states)
		if config is None:
			continue
		if active_only and not config.get("active", False):
			continue
		events.append(config)
	events.sort(key=lambda e: e.get("name", "").lower())
	return events


def normalize_value(value: str) -> str:
	return (value or "").strip().lower()


def parse_custom_fields(form_values: list[str]) -> list[str]:
	fields: list[str] = []
	seen: set[str] = set()
	for value in form_values:
		for token in (value or "").split(","):
			field = normalize_value(token)
			if field and field not in seen:
				seen.add(field)
				fields.append(field)
	return fields


def csv_headers(slug: str) -> list[str]:
	content = load_event_csv_text(slug)
	if content is None:
		return []
	reader = csv.DictReader(content.splitlines())
	return [normalize_value(h) for h in (reader.fieldnames or []) if normalize_value(h)]


def load_csv_rows(slug: str) -> list[dict[str, str]]:
	rows: list[dict[str, str]] = []
	content = load_event_csv_text(slug)
	if content is None:
		return rows
	reader = csv.DictReader(content.splitlines())
	for row in reader:
		normalized_row: dict[str, str] = {}
		for key, value in row.items():
			normalized_key = normalize_value(key)
			if normalized_key:
				normalized_row[normalized_key] = normalize_value(value or "")
		rows.append(normalized_row)
	return rows


def required_headers_for_validation(validation_type: str, custom_fields: list[str]) -> set[str]:
	if validation_type == "player_team":
		return {"player", "team"}
	if validation_type == "name_only":
		return {"name"}
	if validation_type == "email":
		return {"email"}
	if validation_type == "custom":
		return set(custom_fields)
	return set()


def build_custom_form_fields(slug: str, custom_fields: list[str], custom_dropdown_fields: list[str] | None = None) -> list[dict]:
	result: list[dict] = []
	dropdown_set = set(custom_dropdown_fields or [])
	for field in custom_fields:
		key = re.sub(r"[^a-z0-9]+", "_", field).strip("_")
		if not key:
			continue
		is_dropdown = field in dropdown_set
		result.append(
			{
				"column": field,
				"key": key,
				"label": field.replace("_", " ").title(),
				"is_dropdown": is_dropdown,
				"options": load_unique_column_values(slug, field) if is_dropdown else [],
			}
		)
	return result


def validation_prompt_for_type(validation_type: str) -> str:
	if validation_type == "email":
		return "Registration Email"
	if validation_type == "badge_id":
		return "Roll No"
	if validation_type == "name_only":
		return "Registration Name"
	return "Registration Name"


def event_form_context(config: dict, slug: str, error: str | None = None) -> dict:
	validation_type = config.get("validation_type", "player_team")
	custom_fields = config.get("custom_fields", [])
	custom_dropdown_fields = config.get("custom_dropdown_fields", [])
	validation_prompt = validation_prompt_for_type(validation_type)
	registration_placeholder = "BL.SC.U4AIExxxxx" if validation_type == "badge_id" else f"Enter {validation_prompt.lower()}"
	return {
		"event": config,
		"teams": load_team_names(slug) if validation_type == "player_team" else [],
		"custom_form_fields": build_custom_form_fields(slug, custom_fields, custom_dropdown_fields),
		"validation_prompt": validation_prompt,
		"registration_placeholder": registration_placeholder,
		"error": error,
	}


def load_valid_participants(slug: str) -> set[tuple[str, str]]:
	participants: set[tuple[str, str]] = set()
	content = load_event_csv_text(slug)
	if content is None:
		return participants
	reader = csv.DictReader(content.splitlines())
	for row in reader:
		player = normalize_value(row.get("player", ""))
		team = normalize_value(row.get("team", ""))
		if player and team:
			participants.add((player, team))
	return participants


def load_valid_names(slug: str) -> set[str]:
	names: set[str] = set()
	content = load_event_csv_text(slug)
	if content is None:
		return names
	reader = csv.DictReader(content.splitlines())
	for row in reader:
		name = normalize_value(row.get("name", ""))
		if name:
			names.add(name)
	return names


def load_team_names(slug: str) -> list[str]:
	content = load_event_csv_text(slug)
	if content is None:
		return []
	seen: set[str] = set()
	teams: list[str] = []
	reader = csv.DictReader(content.splitlines())
	for row in reader:
		team_raw = (row.get("team", "") or "").strip()
		key = normalize_value(team_raw)
		if team_raw and key not in seen:
			seen.add(key)
			teams.append(team_raw)
	return sorted(teams, key=lambda v: v.lower())


def load_unique_column_values(slug: str, column: str) -> list[str]:
	content = load_event_csv_text(slug)
	if content is None:
		return []
	reader = csv.DictReader(content.splitlines())
	target = normalize_value(column)
	seen: set[str] = set()
	values: list[str] = []
	for row in reader:
		for key, value in row.items():
			if normalize_value(key) != target:
				continue
			raw_value = (value or "").strip()
			normalized = normalize_value(raw_value)
			if raw_value and normalized not in seen:
				seen.add(normalized)
				values.append(raw_value)
			break
	return values


def validate_participant_submission(slug: str, config: dict, form_data) -> str | None:
	validation_type = config.get("validation_type", "player_team")
	custom_fields: list[str] = config.get("custom_fields", [])
	rows = load_csv_rows(slug)

	if validation_type == "none":
		return None

	if validation_type == "player_team":
		registration_name = normalize_value(form_data.get("registration_name", ""))
		team_name = normalize_value(form_data.get("team_name", ""))
		if not registration_name or not team_name:
			return "Please fill all fields."
		if (registration_name, team_name) not in load_valid_participants(slug):
			return "Invalid player or team name."
		return None

	if validation_type == "name_only":
		registration_name = normalize_value(form_data.get("registration_name", ""))
		if not registration_name:
			return "Please fill all fields."
		if registration_name not in load_valid_names(slug):
			return "Name not found in participant list."
		return None

	if validation_type == "email":
		registration_email = normalize_value(form_data.get("registration_name", ""))
		if not registration_email:
			return "Please fill all fields."
		if not any(row.get("email", "") == registration_email for row in rows):
			return "Email not found in participant list."
		return None

	if validation_type == "badge_id":
		registration_id = normalize_value(form_data.get("registration_name", ""))
		if not registration_id:
			return "Please fill all fields."
		for row in rows:
			if row.get("roll_no", "") == registration_id or row.get("id", "") == registration_id or row.get("badge_id", "") == registration_id or row.get("badge_number", "") == registration_id:
				return None
		return "Roll No not found in participant list."

	if validation_type == "custom":
		if not custom_fields:
			return "Custom validation fields are not configured by admin."
		form_fields = build_custom_form_fields(slug, custom_fields)
		expected: dict[str, str] = {}
		for field in form_fields:
			value = normalize_value(form_data.get(f"custom_{field['key']}", ""))
			if not value:
				return "Please fill all fields."
			expected[field["column"]] = value
		for row in rows:
			if all(row.get(col, "") == val for col, val in expected.items()):
				return None
		return "Details not found in participant list."

	return "Unsupported validation type configured for this event."


# ─── Certificate helpers ──────────────────────────────────────────────────────

def get_font(size: int, font_key: str | None = None) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
	"""Load font with caching to avoid repeated disk I/O on Render."""
	global _FONT_CACHE
	cache_key = f"{size}:{font_key or DEFAULT_FONT_KEY}"
	
	# Return cached font if available
	if cache_key in _FONT_CACHE:
		return _FONT_CACHE[cache_key]
	
	font_option = resolve_font_option(font_key)
	try:
		if os.path.exists(font_option["path"]):
			font = ImageFont.truetype(font_option["path"], size=size)
		else:
			font = ImageFont.truetype("arial.ttf", size=size)
	except Exception:
		font = ImageFont.load_default()
	
	# Cache the font
	if len(_FONT_CACHE) >= 20:  # Reasonable limit for font cache
		_FONT_CACHE.clear()
	_FONT_CACHE[cache_key] = font
	
	return font


def _cert_metadata_path(cert_id: str) -> str:
	return os.path.join(GENERATED_DIR, f"{cert_id}.json")


def _cert_image_path(cert_id: str) -> str:
	return os.path.join(GENERATED_DIR, f"{cert_id}.png")


def _get_cert_image_cached(cert_id: str) -> Image.Image | None:
	"""
	Load certificate PNG with caching to avoid repeated disk I/O on Render.
	Returns a new copy of the cached image for safe modification.
	"""
	cache_key = f"cert_{cert_id}"
	cert_path = _cert_image_path(cert_id)
	
	if not os.path.exists(cert_path):
		return None
	
	global _TEMPLATE_IMAGE_CACHE
	
	# Load and cache the image if not already cached
	if cache_key not in _TEMPLATE_IMAGE_CACHE:
		try:
			img = Image.open(cert_path).convert("RGBA")
			# Limit cache to avoid memory bloat
			if len(_TEMPLATE_IMAGE_CACHE) >= 100:
				_TEMPLATE_IMAGE_CACHE.clear()
			_TEMPLATE_IMAGE_CACHE[cache_key] = img
		except Exception:
			return None
	
	# Return a copy to avoid modifying the cached version
	if cache_key in _TEMPLATE_IMAGE_CACHE:
		return _TEMPLATE_IMAGE_CACHE[cache_key].copy()
	
	return None


def _get_event_template_cached(slug: str) -> Image.Image | None:
	"""
	Load event template image with caching to avoid repeated disk I/O.
	Returns a new copy of the cached image for safe modification.
	"""
	cache_key = f"template_{slug}"
	template_path = _event_template_path(slug)
	
	if not os.path.exists(template_path):
		return None
	
	global _TEMPLATE_IMAGE_CACHE
	
	# Load and cache the image if not already cached
	if cache_key not in _TEMPLATE_IMAGE_CACHE:
		try:
			img = Image.open(template_path).convert("RGBA")
			# Limit cache to avoid memory bloat
			if len(_TEMPLATE_IMAGE_CACHE) >= 100:
				_TEMPLATE_IMAGE_CACHE.clear()
			_TEMPLATE_IMAGE_CACHE[cache_key] = img
		except Exception:
			return None
	
	# Return a copy to avoid modifying the cached version
	if cache_key in _TEMPLATE_IMAGE_CACHE:
		return _TEMPLATE_IMAGE_CACHE[cache_key].copy()
	
	return None


def _render_certificate_to_bytes(cert_id: str, metadata: dict) -> tuple[bytes, str]:
	"""
	Render certificate image to PNG bytes with text overlay.
	Returns (png_bytes, etag) where etag is based on metadata.
	Caches result to avoid reprocessing on subsequent requests.
	"""
	global _RENDERED_CERT_CACHE
	
	# Check cache first
	if cert_id in _RENDERED_CERT_CACHE:
		return _RENDERED_CERT_CACHE[cert_id]
	
	# Generate cache key/etag based on metadata
	metadata_str = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
	etag = hashlib.md5(metadata_str.encode()).hexdigest()
	
	# Render the image (use cached version to avoid disk I/O)
	image = _get_cert_image_cached(cert_id)
	if image is None:
		# Fallback: try to load directly (shouldn't happen in normal flow)
		cert_path = _cert_image_path(cert_id)
		if not os.path.exists(cert_path):
			return (b"", "")
		image = Image.open(cert_path).convert("RGBA")
	
	draw_name_on_image(image, metadata)
	
	# Save to bytes (no optimize - caching is our bottleneck relief)
	output = BytesIO()
	image.save(output, format="PNG")
	png_bytes = output.getvalue()
	
	# Store in cache (with simple LRU by clearing if too large)
	if len(_RENDERED_CERT_CACHE) >= _RENDERED_CERT_CACHE_MAX_SIZE:
		_RENDERED_CERT_CACHE.clear()
	
	_RENDERED_CERT_CACHE[cert_id] = (png_bytes, etag)
	return png_bytes, etag


def generate_certificate_file(slug: str, cert_name: str, event_config: dict) -> str:
	# Use cached template to avoid repeated disk I/O on Render
	image = _get_event_template_cached(slug)
	if image is None:
		# Fallback to direct loading (shouldn't happen in normal flow)
		image = Image.open(_event_template_path(slug)).convert("RGBA")
	else:
		image = image.copy()  # Make a copy to avoid modifying the cache
	
	cert_id = uuid4().hex
	# Save PNG without optimization to keep POST response fast
	image.save(_cert_image_path(cert_id), format="PNG")
	metadata = {
		"event_slug": slug,
		"cert_name": cert_name,
		"text_x": event_config.get("text_x", 1789),
		"text_y": event_config.get("text_y", 1440),
		"font_size": event_config.get("font_size", 100),
		"font_color": event_config.get("font_color", [50, 34, 24]),
		"font_key": normalize_font_key(event_config.get("font_key"), DEFAULT_FONT_KEY),
	}
	with open(_cert_metadata_path(cert_id), "w", encoding="utf-8") as f:
		json.dump(metadata, f)
	return cert_id


def load_cert_metadata(cert_id: str) -> dict | None:
	path = _cert_metadata_path(cert_id)
	if not os.path.exists(path):
		return None
	with open(path, encoding="utf-8") as f:
		return json.load(f)


def build_render_metadata(cert_id: str) -> dict | None:
	metadata = load_cert_metadata(cert_id)
	if metadata is None:
		return None
	event_slug = metadata.get("event_slug", "")
	event_config = load_event(event_slug) if event_slug else None
	if event_config is None:
		return metadata
	render_metadata = dict(metadata)
	for key, fallback in (
		("text_x", 1789),
		("text_y", 1440),
		("font_size", 100),
		("font_color", [50, 34, 24]),
		("font_key", DEFAULT_FONT_KEY),
	):
		render_metadata[key] = event_config.get(key, metadata.get(key, fallback))
	render_metadata["font_key"] = normalize_font_key(render_metadata.get("font_key"), DEFAULT_FONT_KEY)
	return render_metadata


def draw_name_on_image(image: Image.Image, metadata: dict) -> None:
	draw = ImageDraw.Draw(image)
	font = get_font(metadata.get("font_size", 100), metadata.get("font_key"))
	color = tuple(metadata.get("font_color", [50, 34, 24]))
	draw.text(
		(metadata.get("text_x", 1789), metadata.get("text_y", 1440)),
		metadata.get("cert_name", ""),
		fill=color,
		font=font,
		anchor="mm",
	)


def safe_download_name(name: str, slug: str) -> str:
	cleaned = re.sub(r"[^A-Za-z0-9 _-]", "", (name or "").strip())
	cleaned = re.sub(r"\s+", "-", cleaned)
	cleaned = cleaned.strip("-")
	if not cleaned:
		cleaned = f"{slug}-certificate"
	return f"{cleaned}.png"


def _is_valid_image(stream, filename: str) -> bool:
	"""Validate that file is a valid image (PNG, JPG, GIF, WebP)."""
	header = stream.read(12)
	stream.seek(0)
	ext = os.path.splitext(filename)[1].lower()
	
	# PNG: 89 50 4E 47
	if ext == '.png' and header[:8] == b"\x89PNG\r\n\x1a\n":
		return True
	# JPEG: FF D8 FF
	if ext in ['.jpg', '.jpeg'] and header[:3] == b"\xff\xd8\xff":
		return True
	# GIF: 47 49 46
	if ext == '.gif' and header[:6] in [b"GIF87a", b"GIF89a"]:
		return True
	# WebP: RIFF ... WEBP
	if ext == '.webp' and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
		return True
	
	return False


def _parse_int(value: str | None, fallback: int) -> int:
	try:
		return max(0, int(value)) if value is not None else fallback
	except (TypeError, ValueError):
		return fallback


def _parse_color(value: str | None, fallback: list | None = None) -> list[int]:
	if fallback is None:
		fallback = [50, 34, 24]
	if not value:
		return fallback
	value = value.strip().lstrip("#")
	if len(value) == 6:
		try:
			return [int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)]
		except ValueError:
			pass
	return fallback


def build_preview_metadata(event_config: dict, cert_name: str | None = None) -> dict:
	return {
		"event_slug": event_config.get("slug", ""),
		"cert_name": (cert_name or "Sample Text").strip() or "Sample Text",
		"text_x": _parse_int(request.args.get("text_x"), event_config.get("text_x", 1789)),
		"text_y": _parse_int(request.args.get("text_y"), event_config.get("text_y", 1440)),
		"font_size": _parse_int(request.args.get("font_size"), event_config.get("font_size", 100)),
		"font_color": _parse_color(request.args.get("font_color"), event_config.get("font_color", [50, 34, 24])),
		"font_key": normalize_font_key(request.args.get("font_key"), event_config.get("font_key", DEFAULT_FONT_KEY)),
	}


@app.context_processor
def inject_style_context() -> dict:
	return {
		"font_options": available_font_options(),
		"default_font_key": normalize_font_key(DEFAULT_FONT_KEY),
	}


# ─── Admin auth ───────────────────────────────────────────────────────────────

def require_admin(f):
	@wraps(f)
	def decorated(*args, **kwargs):
		if not session.get("admin_logged_in"):
			return redirect(url_for("admin_login"))
		return f(*args, **kwargs)
	return decorated


# ─── Public routes ────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def home():
	return render_template("index.html", events=load_all_events(active_only=True))


@app.route("/events/<slug>", methods=["GET"])
def event_page(slug: str):
	if not safe_slug(slug):
		return redirect(url_for("home"))
	config = load_event(slug)
	if config is None or not config.get("active", False):
		return redirect(url_for("home"))
	return render_template("event.html", **event_form_context(config, slug, None))


@app.route("/events/<slug>/download", methods=["POST"])
def download_certificate(slug: str):
	if not safe_slug(slug):
		return redirect(url_for("home"))
	config = load_event(slug)
	if config is None or not config.get("active", False):
		return redirect(url_for("home"))
	validation_type = config.get("validation_type", "player_team")
	cert_name = (request.form.get("cert_name", "") or "").strip()
	if not cert_name:
		return render_template("event.html", **event_form_context(config, slug, "Please fill all fields.")), 400
	validation_error = validate_participant_submission(slug, config, request.form)
	if validation_error:
		return render_template("event.html", **event_form_context(config, slug, validation_error)), 400
	if not os.path.exists(_event_template_path(slug)):
		return render_template("event.html", **event_form_context(config, slug, "Certificate template not found on server.")), 500
	try:
		cert_id = generate_certificate_file(slug, cert_name, config)
	except Exception:
		return render_template("event.html", **event_form_context(config, slug, "Something went wrong generating your certificate.")), 500
	return redirect(url_for("preview_page", cert_id=cert_id))


@app.route("/preview/<cert_id>", methods=["GET"])
def preview_page(cert_id: str):
	if not re.match(r"^[a-f0-9]{32}$", cert_id):
		return redirect(url_for("home"))
	metadata = load_cert_metadata(cert_id)
	if metadata is None or not os.path.exists(_cert_image_path(cert_id)):
		return redirect(url_for("home"))
	event = load_event(metadata["event_slug"])
	return render_template("preview.html", cert_id=cert_id, cert_name=metadata["cert_name"], event=event)


@app.route("/preview-image/<cert_id>", methods=["GET"])
def preview_image(cert_id: str):
	if not re.match(r"^[a-f0-9]{32}$", cert_id):
		return ("Not found", 404)
	metadata = build_render_metadata(cert_id)
	if metadata is None or not os.path.exists(_cert_image_path(cert_id)):
		return ("Not found", 404)
	
	# Use cached rendering to avoid reprocessing image
	png_bytes, etag = _render_certificate_to_bytes(cert_id, metadata)
	
	# Add caching headers for browser optimization
	response = send_file(
		BytesIO(png_bytes),
		mimetype="image/png",
		etag=etag
	)
	# Cache for 1 year (immutable because cert_id won't change)
	response.cache_control.max_age = 31536000
	response.cache_control.public = True
	response.cache_control.immutable = True
	return response


@app.route("/download-file/<cert_id>", methods=["GET"])
def download_file(cert_id: str):
	if not re.match(r"^[a-f0-9]{32}$", cert_id):
		return ("Not found", 404)
	metadata = build_render_metadata(cert_id)
	if metadata is None or not os.path.exists(_cert_image_path(cert_id)):
		return ("Not found", 404)
	
	# Use cached rendering to avoid reprocessing image
	png_bytes, _ = _render_certificate_to_bytes(cert_id, metadata)
	
	return send_file(
		BytesIO(png_bytes),
		mimetype="image/png",
		as_attachment=True,
		download_name=safe_download_name(metadata["cert_name"], metadata.get("event_slug", "event")),
	)


# ─── Admin routes ─────────────────────────────────────────────────────────────

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
	error = None
	if request.method == "POST":
		password = request.form.get("password", "")
		if not ADMIN_PASSWORD:
			error = "ADMIN_PASSWORD environment variable is not set on this server."
		elif password == ADMIN_PASSWORD:
			session["admin_logged_in"] = True
			return redirect(url_for("admin_dashboard"))
		else:
			error = "Incorrect password."
	return render_template("admin/login.html", error=error)


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
	session.pop("admin_logged_in", None)
	return redirect(url_for("admin_login"))


@app.route("/admin", methods=["GET"])
@require_admin
def admin_dashboard():
	return render_template("admin/dashboard.html", events=load_all_events())


@app.route("/admin/events/new", methods=["GET"])
@require_admin
def admin_new_event():
	return render_template("admin/event_form.html", event=None, is_new=True, error=None)


@app.route("/admin/events/new", methods=["POST"])
@require_admin
def admin_create_event():
	name = (request.form.get("name", "") or "").strip()
	slug = (request.form.get("slug", "") or "").strip().lower()
	validation_type = request.form.get("validation_type", "player_team")
	if validation_type not in VALIDATION_TYPES:
		validation_type = "player_team"
	custom_fields = parse_custom_fields(request.form.getlist("custom_fields"))
	custom_dropdown_fields = [field for field in parse_custom_fields(request.form.getlist("custom_dropdown_fields")) if field in custom_fields]
	text_x = _parse_int(request.form.get("text_x"), 1789)
	text_y = _parse_int(request.form.get("text_y"), 1440)
	font_size = _parse_int(request.form.get("font_size"), 100)
	font_color = _parse_color(request.form.get("font_color", ""))
	font_key = normalize_font_key(request.form.get("font_key"), DEFAULT_FONT_KEY)
	form_data = {"name": name, "slug": slug, "validation_type": validation_type, "custom_fields": custom_fields,
				 "custom_dropdown_fields": custom_dropdown_fields,
				 "text_x": text_x, "text_y": text_y, "font_size": font_size, "font_color": font_color,
				 "font_key": font_key}
	if not name or not slug:
		return render_template("admin/event_form.html", event=form_data, is_new=True, error="Name and slug are required."), 400
	if not safe_slug(slug):
		return render_template("admin/event_form.html", event=form_data, is_new=True,
							   error="Slug must be lowercase letters, numbers, and hyphens only."), 400
	if _event_exists(slug):
		return render_template("admin/event_form.html", event=form_data, is_new=True,
							   error=f"An event with slug '{slug}' already exists."), 400
	os.makedirs(_event_dir(slug), exist_ok=True)
	config = {"name": name, "slug": slug, "active": False, "validation_type": validation_type, "custom_fields": custom_fields,
			  "custom_dropdown_fields": custom_dropdown_fields,
			  "text_x": text_x, "text_y": text_y, "font_size": font_size, "font_color": font_color, "font_key": font_key}
	save_event_config(slug, config)
	_set_event_state(slug, deleted=False, active=False)
	return redirect(url_for("admin_edit_event", slug=slug))


@app.route("/admin/events/<slug>", methods=["GET"])
@require_admin
def admin_edit_event(slug: str):
	if not safe_slug(slug):
		return redirect(url_for("admin_dashboard"))
	config = load_event(slug)
	if config is None:
		return redirect(url_for("admin_dashboard"))
	return render_template("admin/event_form.html", event=config, is_new=False, error=None,
						   has_template=os.path.exists(_event_template_path(slug)),
						   has_csv=_event_csv_exists(slug),
						   csv_columns=csv_headers(slug))


@app.route("/admin/events/<slug>/config", methods=["POST"])
@require_admin
def admin_update_config(slug: str):
	if not safe_slug(slug):
		return redirect(url_for("admin_dashboard"))
	config = load_event(slug)
	if config is None:
		return redirect(url_for("admin_dashboard"))
	config["name"] = (request.form.get("name", "") or config["name"]).strip()
	validation_type = request.form.get("validation_type", config.get("validation_type", "player_team"))
	if validation_type not in VALIDATION_TYPES:
		validation_type = "player_team"
	config["validation_type"] = validation_type
	parsed_custom_fields = parse_custom_fields(request.form.getlist("custom_fields"))
	parsed_custom_dropdown_fields = parse_custom_fields(request.form.getlist("custom_dropdown_fields"))
	allowed_dropdown_fields = [field for field in parsed_custom_dropdown_fields if field in parsed_custom_fields]
	if validation_type == "custom":
		config["custom_fields"] = parsed_custom_fields or config.get("custom_fields", [])
	else:
		config["custom_fields"] = parsed_custom_fields
	config["custom_dropdown_fields"] = [field for field in allowed_dropdown_fields if field in config.get("custom_fields", [])]
	config["text_x"] = _parse_int(request.form.get("text_x"), config.get("text_x", 1789))
	config["text_y"] = _parse_int(request.form.get("text_y"), config.get("text_y", 1440))
	config["font_size"] = _parse_int(request.form.get("font_size"), config.get("font_size", 100))
	config["font_color"] = _parse_color(request.form.get("font_color"), config.get("font_color", [50, 34, 24]))
	config["font_key"] = normalize_font_key(request.form.get("font_key"), config.get("font_key", DEFAULT_FONT_KEY))
	save_event_config(slug, config)
	if request.headers.get("X-Requested-With") == "XMLHttpRequest":
		return jsonify({"ok": True, "message": "Settings saved."})
	return render_template("admin/event_form.html", event=config, is_new=False, success="Settings saved.",
						   error=None, has_template=os.path.exists(_event_template_path(slug)),
						   has_csv=_event_csv_exists(slug),
						   csv_columns=csv_headers(slug))


@app.route("/admin/events/<slug>/upload-template", methods=["POST"])
@require_admin
def admin_upload_template(slug: str):
	if not safe_slug(slug):
		return redirect(url_for("admin_dashboard"))
	config = load_event(slug)
	if config is None:
		return redirect(url_for("admin_dashboard"))
	has_csv = _event_csv_exists(slug)
	has_template = os.path.exists(_event_template_path(slug))
	file = request.files.get("template_file")
	if not file or file.filename == "":
		return render_template("admin/event_form.html", event=config, is_new=False,
							   error="No file selected.", has_template=has_template, has_csv=has_csv,
							   csv_columns=csv_headers(slug)), 400
	
	filename = secure_filename(file.filename).lower()
	valid_exts = ['.png', '.jpg', '.jpeg', '.gif', '.webp']
	if not any(filename.endswith(ext) for ext in valid_exts):
		return render_template("admin/event_form.html", event=config, is_new=False,
						   error="Template must be PNG, JPG, GIF, or WebP.", has_template=has_template, has_csv=has_csv,
						   csv_columns=csv_headers(slug)), 400
	
	if not _is_valid_image(file.stream, filename):
		return render_template("admin/event_form.html", event=config, is_new=False,
						   error="File does not appear to be a valid image.", has_template=has_template, has_csv=has_csv,
						   csv_columns=csv_headers(slug)), 400
	
	# Delete existing template files with any extension
	os.makedirs(_event_dir(slug), exist_ok=True)
	for ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp']:
		old_path = os.path.join(_event_dir(slug), f"template{ext}")
		if os.path.exists(old_path):
			os.remove(old_path)
	
	# Save new template with its original extension
	file.stream.seek(0)
	ext = os.path.splitext(filename)[1].lower()
	new_path = os.path.join(_event_dir(slug), f"template{ext}")
	file.save(new_path)
	return render_template("admin/event_form.html", event=config, is_new=False,
						   success="Template uploaded successfully.", error=None, has_template=True, has_csv=has_csv,
						   csv_columns=csv_headers(slug))


@app.route("/admin/events/<slug>/upload-csv", methods=["POST"])
@require_admin
def admin_upload_csv(slug: str):
	if not safe_slug(slug):
		return redirect(url_for("admin_dashboard"))
	config = load_event(slug)
	if config is None:
		return redirect(url_for("admin_dashboard"))
	has_template = os.path.exists(_event_template_path(slug))
	has_csv = _event_csv_exists(slug)
	file = request.files.get("csv_file")
	if not file or file.filename == "":
		return render_template("admin/event_form.html", event=config, is_new=False,
							   error="No file selected.", has_template=has_template, has_csv=has_csv,
							   csv_columns=csv_headers(slug)), 400
	if not secure_filename(file.filename).lower().endswith(".csv"):
		return render_template("admin/event_form.html", event=config, is_new=False,
							   error="Participants file must be a .csv.", has_template=has_template, has_csv=has_csv,
							   csv_columns=csv_headers(slug)), 400
	content = file.stream.read().decode("utf-8", errors="replace")
	validation_type = config.get("validation_type", "player_team")
	custom_fields: list[str] = config.get("custom_fields", [])
	if validation_type == "custom" and not custom_fields:
		return render_template("admin/event_form.html", event=config, is_new=False,
							   error="Select at least one custom field in Event Settings before uploading CSV.",
							   has_template=has_template, has_csv=has_csv,
							   csv_columns=csv_headers(slug)), 400
	required_headers = required_headers_for_validation(validation_type, custom_fields)
	try:
		reader = csv.DictReader(content.splitlines())
		headers = {h.strip().lower() for h in (reader.fieldnames or [])}
		if validation_type == "badge_id" and not ({"roll_no", "id", "badge_id", "badge_number"} & headers):
			return render_template("admin/event_form.html", event=config, is_new=False,
								   error="CSV must include one of: roll_no, id, badge_id, badge_number.",
								   has_template=has_template, has_csv=has_csv,
								   csv_columns=sorted(headers)), 400
		if not required_headers.issubset(headers):
			missing = ", ".join(sorted(required_headers - headers))
			return render_template("admin/event_form.html", event=config, is_new=False,
								   error=f"CSV is missing required column(s): {missing}.",
							   has_template=has_template, has_csv=has_csv,
							   csv_columns=sorted(headers)), 400
	except Exception:
		return render_template("admin/event_form.html", event=config, is_new=False,
							   error="Could not parse CSV file.", has_template=has_template, has_csv=has_csv,
							   csv_columns=csv_headers(slug)), 400
	save_event_csv(slug, content)
	return render_template("admin/event_form.html", event=config, is_new=False,
						   success="Participants CSV uploaded.", error=None, has_template=has_template, has_csv=True,
						   csv_columns=csv_headers(slug))


@app.route("/admin/events/<slug>/toggle", methods=["POST"])
@require_admin
def admin_toggle_event(slug: str):
	if not safe_slug(slug):
		return redirect(url_for("admin_dashboard"))
	config = load_event(slug)
	if config is None:
		return redirect(url_for("admin_dashboard"))
	config["active"] = not config.get("active", False)
	save_event_config(slug, config)
	_set_event_state(slug, active=config["active"], deleted=False)
	return redirect(url_for("admin_dashboard"))


@app.route("/admin/events/<slug>/send_emails", methods=["GET", "POST"])
@require_admin
def admin_send_emails(slug: str):
	if not _event_exists(slug):
		return redirect(url_for("admin_dashboard"))
		
	event_config = load_event(slug)
	
	if request.method == "GET":
		return render_template(
			"admin/email_form.html",
			event=event_config
		)
	
	subject_template = request.form.get("subject_template")
	plain_body_template = request.form.get("plain_body_template")
	html_body_template = request.form.get("html_body_template")
	
	# Update coordinates if they were changed
	if request.form.get("text_x"):
		event_config["text_x"] = int(request.form.get("text_x"))
	if request.form.get("text_y"):
		event_config["text_y"] = int(request.form.get("text_y"))
	if request.form.get("font_size"):
		event_config["font_size"] = int(request.form.get("font_size"))
	if request.form.get("font_color"):
		color_hex = request.form.get("font_color").lstrip("#")
		event_config["font_color"] = [int(color_hex[0:2], 16), int(color_hex[2:4], 16), int(color_hex[4:6], 16)]
	if request.form.get("font_key"):
		event_config["font_key"] = normalize_font_key(request.form.get("font_key"))
	
	save_event_config(slug, event_config)
	
	import threading
	try:
		from manage import bulk_generate
		
		def background_task():
			try:
				bulk_generate(slug, send_emails=True, 
							  subject_template=subject_template,
							  plain_body_template=plain_body_template,
							  html_body_template=html_body_template)
			except Exception as e:
				print(f"Background email task failed: {e}")
				
		thread = threading.Thread(target=background_task)
		thread.daemon = True
		thread.start()
		
		return "<script>alert('Email sending has started in the background! Check server logs for progress.'); window.location.href='/admin/logs';</script>"
	except ImportError:
		return "manage.py module not found", 500


@app.route("/admin/logs", methods=["GET"])
@require_admin
def admin_logs():
	"""View the system.log file generated by background tasks."""
	log_path = os.path.join(BASE_DIR, "logs", "system.log")
	logs_content = ""
	if os.path.exists(log_path):
		try:
			with open(log_path, "r", encoding="utf-8") as f:
				logs_content = f.read()
		except Exception as e:
			logs_content = f"Error reading logs: {e}"
	return render_template("admin/logs.html", logs=logs_content)

@app.route("/admin/logs/clear", methods=["POST"])
@require_admin
def admin_clear_logs():
	"""Clear the system.log file."""
	log_path = os.path.join(BASE_DIR, "logs", "system.log")
	if os.path.exists(log_path):
		try:
			with open(log_path, "w", encoding="utf-8") as f:
				f.write("")
		except Exception as e:
			print(f"Error clearing logs: {e}")
	return redirect(url_for("admin_logs"))


@app.route("/admin/events/<slug>/delete", methods=["POST"])
@require_admin
def admin_delete_event(slug: str):
	if not safe_slug(slug):
		return redirect(url_for("admin_dashboard"))
	if request.form.get("confirm", "") != slug:
		return redirect(url_for("admin_dashboard"))
	delete_event_storage(slug)
	_set_event_state(slug, deleted=True, active=False)
	return redirect(url_for("admin_dashboard"))


@app.route("/admin/events/<slug>/coordinates", methods=["GET"])
@require_admin
def admin_coordinate_editor(slug: str):
	"""Full-screen coordinate editor for certificate text positioning."""
	if not safe_slug(slug):
		return redirect(url_for("admin_dashboard"))
	config = load_event(slug)
	if config is None:
		return redirect(url_for("admin_dashboard"))
	return render_template("admin/coordinate_editor.html", event=config)


@app.route("/admin/events/<slug>/template-preview", methods=["GET"])
@require_admin
def admin_template_preview(slug: str):
	"""Serve certificate template image for canvas preview in event editor."""
	if not safe_slug(slug):
		return "Not found", 404
	template_path = _event_template_path(slug)
	if not os.path.exists(template_path):
		return "Template not found", 404
	return send_file(template_path)


@app.route("/admin/events/<slug>/render-preview", methods=["GET"])
@require_admin
def admin_render_preview(slug: str):
	"""Render a preview with the same server-side Pillow path used for generated certificates."""
	if not safe_slug(slug):
		return "Not found", 404
	config = load_event(slug)
	if config is None:
		return "Not found", 404
	template_path = _event_template_path(slug)
	if not os.path.exists(template_path):
		return "Template not found", 404
	image = Image.open(template_path).convert("RGBA")
	metadata = build_preview_metadata(config, request.args.get("cert_name"))
	draw_name_on_image(image, metadata)
	output = BytesIO()
	image.save(output, format="PNG")
	output.seek(0)
	response = send_file(output, mimetype="image/png")
	# Short cache for admin previews (they may change as settings are edited)
	response.cache_control.max_age = 60
	response.cache_control.public = True
	return response


@app.route("/assets/fonts/<font_key>.ttf", methods=["GET"])
def font_asset(font_key: str):
	"""Serve event font files used by PIL so browser previews match generated files."""
	if normalize_font_key(font_key) != font_key:
		return "Font not found", 404
	font_option = resolve_font_option(font_key)
	if not os.path.exists(font_option["path"]):
		return "Font not found", 404
	response = send_file(font_option["path"], mimetype="font/ttf")
	# Cache fonts for 1 year (rarely change)
	response.cache_control.max_age = 31536000
	response.cache_control.public = True
	response.cache_control.immutable = True
	return response


@app.route("/assets/fonts/montserrat-bold.ttf", methods=["GET"])
def montserrat_bold_font():
	"""Backward-compatible route for existing CSS references."""
	return font_asset(DEFAULT_FONT_KEY)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
	app.run(debug=True)
