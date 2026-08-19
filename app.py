from __future__ import annotations

import copy
import csv
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import tempfile
import time
from collections import OrderedDict
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest
from functools import wraps
from io import BytesIO, StringIO
from uuid import uuid4

from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for
from PIL import Image, ImageDraw, ImageFont
from werkzeug.utils import secure_filename
from itsdangerous import BadSignature, URLSafeSerializer
from dotenv import load_dotenv
import yaml

load_dotenv()
app = Flask(__name__)

# Certificate links are signed with this key, so an unstable key does not just log
# admins out at random - it invalidates every outstanding certificate link too.
_SECRET_KEY = os.environ.get("SECRET_KEY", "").strip()
if not _SECRET_KEY:
	_SECRET_KEY = os.urandom(24).hex()
	print(
		"WARNING: SECRET_KEY is not set. A random key was generated for this process, "
		"so sessions and certificate links will break across restarts and will not "
		"work at all with more than one worker. Set SECRET_KEY in the environment."
	)
app.secret_key = _SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB upload limit
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes")

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
# Background jobs log here. It lives on the writable runtime dir because the code
# directory is read-only on some hosts.
LOG_DIR = os.path.join(RUNTIME_WRITABLE_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "system.log")
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

# Supabase Storage holds certificate templates so they survive redeploys and are
# visible to every worker. Local files stay as the dev / no-Supabase fallback.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
# One bucket for the club, one folder per event inside it. Supabase renders key
# prefixes as folders, so the dashboard shows <event>/participants and
# <event>/template. Bucket names cannot contain spaces, hence the slug form.
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET", "csi-aseb").strip()
_SUPABASE_BUCKET_READY = False


def _env_int(name: str, fallback: int) -> int:
	try:
		return max(1, int(os.environ.get(name, "").strip()))
	except (TypeError, ValueError):
		return fallback


# Event config cache (per-slug) to avoid repeated KV API calls during preview/download
_EVENT_CONFIG_CACHE: dict[str, tuple[dict, float]] = {}  # {slug: (config, timestamp)}
_EVENT_CONFIG_CACHE_TTL_SEC = 30.0  # 30 second TTL for event configs

# Participant CSVs are read several times per page (team list, each dropdown column,
# then again during validation). Without this, each of those was a separate KV
# round trip. Strings are immutable, so the cached value can be shared freely.
_EVENT_CSV_CACHE: dict[str, tuple[str | None, float]] = {}  # {slug: (csv_text, timestamp)}
_EVENT_CSV_CACHE_TTL_SEC = 30.0

# A decoded template is tens of megabytes (an A4 300 DPI page is 8.7 MP), so these
# caches stay small. Templates are held as RGB unless they really carry alpha,
# which is 25% less memory than RGBA and buys a couple more cache slots.
_TEMPLATE_CACHE_MAX = _env_int("TEMPLATE_CACHE_MAX", 6)
_RENDER_CACHE_MAX = _env_int("RENDER_CACHE_MAX", 6)
_FONT_CACHE_MAX = 20

# Encoding dominates render time: for an 8.7 MP page, drawing the name takes ~4 ms
# while the PNG encode takes ~320 ms at Pillow's default compression. So the screen
# preview is downscaled and sent as JPEG, and the download drops to a lighter PNG
# compression level - which, with the alpha channel gone, is still a smaller file.
PREVIEW_MAX_WIDTH = _env_int("PREVIEW_MAX_WIDTH", 1200)
PREVIEW_JPEG_QUALITY = _env_int("PREVIEW_JPEG_QUALITY", 90)
DOWNLOAD_PNG_COMPRESS_LEVEL = _env_int("DOWNLOAD_PNG_COMPRESS_LEVEL", 3)

# Every cache key embeds the template version and render settings, so a replaced
# template or an edited coordinate produces a new key instead of a stale hit.
_TEMPLATE_IMAGE_CACHE: "OrderedDict[str, Image.Image]" = OrderedDict()
_RENDERED_CERT_CACHE: "OrderedDict[str, tuple[bytes, str]]" = OrderedDict()
_FONT_CACHE: "OrderedDict[str, ImageFont.FreeTypeFont | ImageFont.ImageFont]" = OrderedDict()


def _cache_get(cache: OrderedDict, key: str):
	if key not in cache:
		return None
	cache.move_to_end(key)
	return cache[key]


def _cache_put(cache: OrderedDict, key: str, value, max_size: int) -> None:
	cache[key] = value
	cache.move_to_end(key)
	while len(cache) > max_size:
		cache.popitem(last=False)

os.makedirs(EVENTS_DIR, exist_ok=True)
os.makedirs(GENERATED_DIR, exist_ok=True)

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
TEMPLATE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
TEMPLATE_CONTENT_TYPES = {
	".png": "image/png",
	".jpg": "image/jpeg",
	".jpeg": "image/jpeg",
	".gif": "image/gif",
	".webp": "image/webp",
}
# Guards against decompression bombs: a 40 MP RGBA image is already ~160 MB decoded.
MAX_TEMPLATE_PIXELS = 40_000_000

PARTICIPANT_EXTENSIONS = (".csv", ".xlsx")
PARTICIPANT_CONTENT_TYPES = {
	".csv": "text/csv",
	".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
VALIDATION_TYPES = {"player_team", "name_only", "email", "badge_id", "custom", "none"}
VALIDATION_TYPE_LABELS = {
	"player_team": "Player + Team",
	"name_only": "Name Only",
	"email": "Email",
	"badge_id": "Roll No",
	"custom": "Custom Fields",
	"none": "No Validation",
}


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


# ─── Supabase Storage ─────────────────────────────────────────────────────────

def _supabase_enabled() -> bool:
	return bool(
		SUPABASE_URL
		and SUPABASE_SERVICE_KEY
		and SUPABASE_URL.lower().startswith(("http://", "https://"))
	)


def _supabase_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
	headers = {
		"Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
		"apikey": SUPABASE_SERVICE_KEY,
	}
	if extra:
		headers.update(extra)
	return headers


def _supabase_object_url(object_path: str) -> str:
	bucket = urlparse.quote(SUPABASE_BUCKET, safe="")
	encoded_path = urlparse.quote(object_path, safe="/")
	return f"{SUPABASE_URL}/storage/v1/object/{bucket}/{encoded_path}"


def _supabase_ensure_bucket() -> None:
	"""Create the private storage bucket on first use. Safe to call repeatedly."""
	global _SUPABASE_BUCKET_READY
	if _SUPABASE_BUCKET_READY:
		return
	payload = json.dumps({"id": SUPABASE_BUCKET, "name": SUPABASE_BUCKET, "public": False}).encode("utf-8")
	req = urlrequest.Request(
		f"{SUPABASE_URL}/storage/v1/bucket",
		data=payload,
		method="POST",
		headers=_supabase_headers({"Content-Type": "application/json"}),
	)
	try:
		with urlrequest.urlopen(req, timeout=10):
			pass
	except urlerror.HTTPError as exc:
		# 400/409 is the steady state: the bucket already exists.
		if exc.code not in (400, 409):
			raise
	_SUPABASE_BUCKET_READY = True


def _supabase_ping() -> None:
	"""Round-trip to Supabase so a keep-alive cron also keeps the project awake."""
	bucket = urlparse.quote(SUPABASE_BUCKET, safe="")
	req = urlrequest.Request(f"{SUPABASE_URL}/storage/v1/bucket/{bucket}", headers=_supabase_headers())
	with urlrequest.urlopen(req, timeout=10):
		return


def _supabase_upload(object_path: str, data: bytes, content_type: str) -> None:
	_supabase_ensure_bucket()
	req = urlrequest.Request(
		_supabase_object_url(object_path),
		data=data,
		method="POST",
		headers=_supabase_headers({"Content-Type": content_type, "x-upsert": "true"}),
	)
	with urlrequest.urlopen(req, timeout=30):
		return


def _supabase_download(object_path: str) -> bytes | None:
	req = urlrequest.Request(_supabase_object_url(object_path), headers=_supabase_headers())
	try:
		with urlrequest.urlopen(req, timeout=20) as response:
			return response.read()
	except urlerror.HTTPError as exc:
		if exc.code == 404:
			return None
		raise


def _supabase_delete(object_path: str) -> None:
	req = urlrequest.Request(_supabase_object_url(object_path), method="DELETE", headers=_supabase_headers())
	try:
		with urlrequest.urlopen(req, timeout=15):
			return
	except urlerror.HTTPError as exc:
		if exc.code != 404:
			raise


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
	"""Local template path. Returns the first matching image file in the event directory."""
	event_dir = _event_dir(slug)
	for ext in TEMPLATE_EXTENSIONS:
		path = os.path.join(event_dir, f"template{ext}")
		if os.path.exists(path):
			return path
	return os.path.join(event_dir, "template.png")


def _event_csv_path(slug: str) -> str:
	return os.path.join(_event_dir(slug), "data.csv")


def _event_csv_key(slug: str) -> str:
	return f"{KV_EVENT_CSV_PREFIX}{slug}"


# ─── Certificate template storage ─────────────────────────────────────────────

def _template_object_path(slug: str, ext: str) -> str:
	return f"{slug}/template/template{ext}"


def _legacy_template_object_path(slug: str, ext: str) -> str:
	"""Where templates lived before the per-event folder layout."""
	return f"events/{slug}/template{ext}"


def _participants_object_path(slug: str, filename: str = "data.csv") -> str:
	return f"{slug}/participants/{filename}"


def template_ext_for(slug: str, config: dict | None = None) -> str:
	"""Extension recorded on the config, falling back to whatever is on local disk."""
	recorded = (config or {}).get("template_ext")
	if isinstance(recorded, str) and recorded.lower() in TEMPLATE_EXTENSIONS:
		return recorded.lower()
	for ext in TEMPLATE_EXTENSIONS:
		if os.path.exists(os.path.join(_event_dir(slug), f"template{ext}")):
			return ext
	return ".png"


def template_version_for(slug: str, config: dict | None = None) -> str:
	"""Cache-busting token for a template. Changes whenever the template changes."""
	version = (config or {}).get("template_version")
	if isinstance(version, str) and version:
		return version
	# Events created before versioning fall back to the local file mtime.
	try:
		return str(int(os.path.getmtime(_event_template_path(slug))))
	except OSError:
		return "none"


def has_template(slug: str, config: dict | None = None) -> bool:
	if (config or {}).get("template_version"):
		return True
	return os.path.exists(_event_template_path(slug))


def load_template_bytes(slug: str, config: dict | None = None) -> bytes | None:
	ext = template_ext_for(slug, config)
	if _supabase_enabled():
		for object_path in (_template_object_path(slug, ext),
							_legacy_template_object_path(slug, ext)):
			try:
				data = _supabase_download(object_path)
				if data:
					return data
			except (urlerror.URLError, TimeoutError, OSError, ValueError):
				break
	path = os.path.join(_event_dir(slug), f"template{ext}")
	if not os.path.exists(path):
		path = _event_template_path(slug)
	try:
		with open(path, "rb") as f:
			return f.read()
	except OSError:
		return None


def save_template_bytes(slug: str, data: bytes, ext: str) -> str:
	"""Persist a template everywhere and return its new version token."""
	if _supabase_enabled():
		_supabase_upload(
			_template_object_path(slug, ext),
			data,
			TEMPLATE_CONTENT_TYPES.get(ext, "application/octet-stream"),
		)
	# Keep a local copy too: it is the fallback when Supabase is not configured.
	try:
		os.makedirs(_event_dir(slug), exist_ok=True)
		for stale_ext in TEMPLATE_EXTENSIONS:
			if stale_ext == ext:
				continue
			stale_path = os.path.join(_event_dir(slug), f"template{stale_ext}")
			if os.path.exists(stale_path):
				os.remove(stale_path)
		with open(os.path.join(_event_dir(slug), f"template{ext}"), "wb") as f:
			f.write(data)
	except OSError:
		# A read-only filesystem is fine as long as Supabase accepted the upload.
		if not _supabase_enabled():
			raise
	return hashlib.sha256(data).hexdigest()[:16]


def delete_event_objects(slug: str) -> None:
	"""Remove an event's whole folder: template, participant files, and legacy keys."""
	if not _supabase_enabled():
		return
	paths = [_participants_object_path(slug)]
	for ext in TEMPLATE_EXTENSIONS:
		paths.append(_template_object_path(slug, ext))
		paths.append(_legacy_template_object_path(slug, ext))
	for ext in PARTICIPANT_EXTENSIONS:
		paths.append(_participants_object_path(slug, f"source{ext}"))
	for object_path in paths:
		try:
			_supabase_delete(object_path)
		except (urlerror.URLError, TimeoutError, OSError, ValueError):
			continue


def decode_template(data: bytes) -> Image.Image:
	"""
	Decode a template, keeping alpha only when the file actually has it.

	Certificate templates are almost always opaque. Forcing them to RGBA costs 25%
	more memory to hold and 25% more bytes to compress on every single render.
	"""
	image = Image.open(BytesIO(data))
	if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
		return image.convert("RGBA")
	return image.convert("RGB")


def get_template_image(slug: str, config: dict | None = None) -> Image.Image | None:
	"""Decoded template, cached per (slug, template version). Returns a copy."""
	cache_key = f"{slug}@{template_version_for(slug, config)}"
	cached = _cache_get(_TEMPLATE_IMAGE_CACHE, cache_key)
	if cached is None:
		data = load_template_bytes(slug, config)
		if data is None:
			return None
		try:
			cached = decode_template(data)
		except Exception:
			return None
		_cache_put(_TEMPLATE_IMAGE_CACHE, cache_key, cached, _TEMPLATE_CACHE_MAX)
	return cached.copy()


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
			# Copy: callers mutate what they get back, and the cache is shared
			# between requests and between bulk-generation threads.
			return copy.deepcopy(config)

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
	_EVENT_CONFIG_CACHE[slug] = (copy.deepcopy(config), time.time())

	# Limit cache size to prevent memory bloat
	if len(_EVENT_CONFIG_CACHE) > 100:
		_EVENT_CONFIG_CACHE.clear()

	return config


def xlsx_to_csv_text(data: bytes) -> str:
	"""
	Flatten the first worksheet of an .xlsx file to CSV text.

	Everything downstream (validation, dropdowns, bulk generation) already speaks
	CSV, so an upload is converted once here rather than teaching the rest of the
	app about workbooks. The header row sets the column count; blank rows are
	dropped and ragged rows are padded or trimmed to match.
	"""
	from openpyxl import load_workbook

	workbook = load_workbook(BytesIO(data), read_only=True, data_only=True)
	try:
		worksheet = workbook.worksheets[0]
		rows: list[list[str]] = []
		for raw_row in worksheet.iter_rows(values_only=True):
			cells = ["" if value is None else str(value).strip() for value in raw_row]
			while cells and cells[-1] == "":
				cells.pop()
			if not cells:
				continue
			rows.append(cells)
	finally:
		workbook.close()

	if not rows:
		return ""

	width = len(rows[0])
	output = StringIO()
	writer = csv.writer(output)
	for row in rows:
		writer.writerow((row + [""] * width)[:width])
	return output.getvalue()


def participant_text_from_upload(data: bytes, ext: str) -> tuple[str | None, str | None]:
	"""Return (csv_text, error). Accepts a .csv or .xlsx upload."""
	if ext == ".xlsx":
		# .xlsx is a zip container; anything else with that extension is a lie.
		if data[:4] != b"PK\x03\x04":
			return None, "That does not look like a valid .xlsx workbook."
		try:
			text = xlsx_to_csv_text(data)
		except Exception:
			return None, "Could not read that workbook. Try re-saving it, or upload a .csv."
		if not text.strip():
			return None, "The first sheet of that workbook is empty."
		return text, None

	# utf-8-sig strips the BOM that Excel writes when it exports CSV.
	return data.decode("utf-8-sig", errors="replace"), None


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
	"""
	Participant CSV text, cached briefly because one page view reads it repeatedly.

	Supabase is the store; KV is read only so events uploaded before the move keep
	working, and the local file is the no-storage-configured fallback.
	"""
	cached = _EVENT_CSV_CACHE.get(slug)
	if cached is not None and (time.time() - cached[1]) < _EVENT_CSV_CACHE_TTL_SEC:
		return cached[0]

	content: str | None = None
	if _supabase_enabled():
		try:
			raw_bytes = _supabase_download(_participants_object_path(slug))
			if raw_bytes:
				content = raw_bytes.decode("utf-8-sig", errors="replace")
		except (urlerror.URLError, TimeoutError, OSError, ValueError):
			pass
	if content is None and _kv_enabled():
		try:
			raw = _kv_get_raw(_event_csv_key(slug))
			if isinstance(raw, str):
				content = raw
		except (urlerror.URLError, TimeoutError, OSError, ValueError):
			pass
	if content is None:
		content = _read_event_csv_from_file(slug)

	_EVENT_CSV_CACHE[slug] = (content, time.time())
	if len(_EVENT_CSV_CACHE) > 100:
		_EVENT_CSV_CACHE.clear()
	return content


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


def save_event_csv(slug: str, content: str, source: tuple[bytes, str] | None = None) -> None:
	"""
	Persist the participant list.

	`source` is the original upload as (bytes, extension); when it is a workbook
	it is kept alongside the derived CSV so the organiser can download exactly
	what they uploaded.
	"""
	try:
		_write_event_csv_to_file(slug, content)
	except OSError:
		if not (_supabase_enabled() or _kv_enabled()):
			raise
	_EVENT_CSV_CACHE.pop(slug, None)

	if _supabase_enabled():
		_supabase_upload(
			_participants_object_path(slug),
			content.encode("utf-8"),
			PARTICIPANT_CONTENT_TYPES[".csv"],
		)
		if source is not None and source[1] != ".csv":
			raw_bytes, ext = source
			try:
				_supabase_upload(
					_participants_object_path(slug, f"source{ext}"),
					raw_bytes,
					PARTICIPANT_CONTENT_TYPES.get(ext, "application/octet-stream"),
				)
			except (urlerror.URLError, TimeoutError, OSError, ValueError):
				# Keeping the original is a convenience, not a requirement.
				pass
		_register_event_slug(slug)
		return

	# No Supabase configured: fall back to KV so existing deployments still work.
	if _kv_enabled():
		try:
			_kv_set_raw(_event_csv_key(slug), content)
			_register_event_slug(slug)
		except (urlerror.URLError, TimeoutError, OSError, ValueError):
			return


def delete_event_storage(slug: str) -> None:
	if os.path.isdir(_event_dir(slug)):
		shutil.rmtree(_event_dir(slug))
	delete_event_objects(slug)
	# Invalidate caches for the deleted event
	global _EVENT_CONFIG_CACHE
	_EVENT_CONFIG_CACHE.pop(slug, None)
	_EVENT_CSV_CACHE.pop(slug, None)
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
		return "Registration email"
	if validation_type == "badge_id":
		return "Roll number"
	return "Registration name"


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

	if validation_type == "none":
		return None

	# Parsed lazily: player_team and name_only use their own lookups below.
	rows: list[dict[str, str]] | None = None

	def csv_rows() -> list[dict[str, str]]:
		nonlocal rows
		if rows is None:
			rows = load_csv_rows(slug)
		return rows

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
		if not any(row.get("email", "") == registration_email for row in csv_rows()):
			return "Email not found in participant list."
		return None

	if validation_type == "badge_id":
		registration_id = normalize_value(form_data.get("registration_name", ""))
		if not registration_id:
			return "Please fill all fields."
		for row in csv_rows():
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
		for row in csv_rows():
			if all(row.get(col, "") == val for col, val in expected.items()):
				return None
		return "Details not found in participant list."

	return "Unsupported validation type configured for this event."


# ─── Certificate helpers ──────────────────────────────────────────────────────

def get_font(size: int, font_key: str | None = None) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
	"""Load font with caching to avoid repeated disk I/O on every render."""
	cache_key = f"{size}:{font_key or DEFAULT_FONT_KEY}"
	cached = _cache_get(_FONT_CACHE, cache_key)
	if cached is not None:
		return cached

	font_option = resolve_font_option(font_key)
	try:
		if os.path.exists(font_option["path"]):
			font = ImageFont.truetype(font_option["path"], size=size)
		else:
			font = ImageFont.truetype("arial.ttf", size=size)
	except Exception:
		font = ImageFont.load_default()

	_cache_put(_FONT_CACHE, cache_key, font, _FONT_CACHE_MAX)
	return font


def _cert_metadata_path(cert_id: str) -> str:
	"""Location of a pre-token certificate record. Retained for legacy links only."""
	return os.path.join(GENERATED_DIR, f"{cert_id}.json")


def certificate_render_settings(config: dict) -> dict:
	"""The subset of an event config that actually affects the rendered image."""
	return {
		"text_x": config.get("text_x", 1789),
		"text_y": config.get("text_y", 1440),
		"font_size": config.get("font_size", 100),
		"font_color": config.get("font_color", [50, 34, 24]),
		"font_key": normalize_font_key(config.get("font_key"), DEFAULT_FONT_KEY),
	}


def encode_certificate(image: Image.Image, variant: str = "download") -> tuple[bytes, str]:
	"""
	Encode a rendered certificate. Returns (bytes, mimetype).

	"preview" is what the browser shows in an <img>: downscaled and JPEG, because
	nobody looks at 3508 px on a phone and the full-size PNG is ~2.5 MB.
	"download" is the real artifact: full resolution PNG.
	"""
	has_alpha = image.mode in ("RGBA", "LA")
	if variant == "preview":
		if image.width > PREVIEW_MAX_WIDTH:
			# BOX is the cheapest filter that still looks clean at a ~3x reduction.
			image.thumbnail((PREVIEW_MAX_WIDTH, PREVIEW_MAX_WIDTH), Image.BOX)
		if not has_alpha:
			output = BytesIO()
			image.save(output, format="JPEG", quality=PREVIEW_JPEG_QUALITY)
			return output.getvalue(), "image/jpeg"
	output = BytesIO()
	image.save(output, format="PNG", compress_level=DOWNLOAD_PNG_COMPRESS_LEVEL)
	return output.getvalue(), "image/png"


def render_certificate(slug: str, cert_name: str, config: dict,
					   variant: str = "download") -> tuple[bytes, str, str] | None:
	"""
	Render a certificate on demand and return (image_bytes, etag, mimetype).

	Nothing is written to disk: the template plus the event config plus the name is
	everything needed to reproduce the image, so any worker can serve any link.
	The etag covers the template version, every render setting, and the variant, so
	replacing a template or moving the text produces a new etag instead of a stale
	cache hit.
	"""
	settings = certificate_render_settings(config)
	fingerprint = json.dumps(
		{
			"slug": slug,
			"name": cert_name,
			"template": template_version_for(slug, config),
			"settings": settings,
			"variant": variant,
		},
		sort_keys=True,
		separators=(",", ":"),
	)
	etag = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()

	cached = _cache_get(_RENDERED_CERT_CACHE, etag)
	if cached is not None:
		return cached

	image = get_template_image(slug, config)
	if image is None:
		return None

	metadata = dict(settings)
	metadata["cert_name"] = cert_name
	draw_name_on_image(image, metadata)

	image_bytes, mimetype = encode_certificate(image, variant)
	result = (image_bytes, etag, mimetype)
	_cache_put(_RENDERED_CERT_CACHE, etag, result, _RENDER_CACHE_MAX)
	return result


# ─── Certificate links ────────────────────────────────────────────────────────
#
# A certificate link is a signed token carrying the event slug and the printed
# name, not a pointer to a stored file. Links are unguessable without SECRET_KEY,
# survive restarts and redeploys, and work on any worker.

_CERT_TOKEN_SALT = "certificate-link-v1"


def _cert_serializer() -> URLSafeSerializer:
	return URLSafeSerializer(app.secret_key, salt=_CERT_TOKEN_SALT)


def make_cert_token(slug: str, cert_name: str) -> str:
	return _cert_serializer().dumps({"s": slug, "n": cert_name})


def read_cert_token(token: str) -> tuple[str, str] | None:
	try:
		payload = _cert_serializer().loads(token)
	except BadSignature:
		return None
	if not isinstance(payload, dict):
		return None
	slug = str(payload.get("s", ""))
	cert_name = str(payload.get("n", ""))
	if not safe_slug(slug) or not cert_name:
		return None
	return slug, cert_name


def _legacy_cert_record(cert_id: str) -> tuple[str, str] | None:
	"""Resolve a pre-token certificate id from its on-disk metadata file."""
	path = _cert_metadata_path(cert_id)
	if not os.path.exists(path):
		return None
	try:
		with open(path, encoding="utf-8") as f:
			metadata = json.load(f)
	except (OSError, json.JSONDecodeError):
		return None
	if not isinstance(metadata, dict):
		return None
	slug = str(metadata.get("event_slug", ""))
	cert_name = str(metadata.get("cert_name", ""))
	if not safe_slug(slug) or not cert_name:
		return None
	return slug, cert_name


def resolve_cert_token(token: str) -> tuple[str, str] | None:
	"""Accept a signed token, or a legacy 32-hex id still sitting on local disk."""
	resolved = read_cert_token(token)
	if resolved is not None:
		return resolved
	if re.match(r"^[a-f0-9]{32}$", token):
		return _legacy_cert_record(token)
	return None


def draw_name_on_image(image: Image.Image, metadata: dict) -> None:
	draw = ImageDraw.Draw(image)
	font = get_font(metadata.get("font_size", 100), metadata.get("font_key"))
	raw_color = metadata.get("font_color", [50, 34, 24])
	try:
		color = tuple(int(channel) for channel in raw_color)[:3]
	except (TypeError, ValueError):
		color = (50, 34, 24)
	position = (metadata.get("text_x", 1789), metadata.get("text_y", 1440))
	text = metadata.get("cert_name", "")
	# anchor= is only supported by truetype fonts; load_default() is the fallback
	# when the bundled TTF is missing, and would raise instead of degrading.
	if isinstance(font, ImageFont.FreeTypeFont):
		draw.text(position, text, fill=color, font=font, anchor="mm")
	else:
		draw.text(position, text, fill=color, font=font)


def safe_download_name(name: str, slug: str) -> str:
	cleaned = re.sub(r"[^A-Za-z0-9 _-]", "", (name or "").strip())
	cleaned = re.sub(r"\s+", "-", cleaned)
	cleaned = cleaned.strip("-")
	if not cleaned:
		cleaned = f"{slug}-certificate"
	return f"{cleaned}.png"


def _has_image_magic(data: bytes, ext: str) -> bool:
	"""Check that the file content matches the extension it claims."""
	header = data[:12]
	if ext == ".png":
		return header[:8] == b"\x89PNG\r\n\x1a\n"
	if ext in (".jpg", ".jpeg"):
		return header[:3] == b"\xff\xd8\xff"
	if ext == ".gif":
		return header[:6] in (b"GIF87a", b"GIF89a")
	if ext == ".webp":
		return header[:4] == b"RIFF" and header[8:12] == b"WEBP"
	return False


def validate_template_upload(data: bytes, ext: str) -> str | None:
	"""Return an error message for a rejected template, or None when it is usable."""
	if not _has_image_magic(data, ext):
		return "File does not appear to be a valid image."
	try:
		with Image.open(BytesIO(data)) as probe:
			probe.verify()
		with Image.open(BytesIO(data)) as probe:
			width, height = probe.size
	except Exception:
		return "That image could not be decoded. Try re-exporting it."
	if width * height > MAX_TEMPLATE_PIXELS:
		megapixels = MAX_TEMPLATE_PIXELS // 1_000_000
		return f"Template is too large ({width}x{height}). The maximum is {megapixels} megapixels."
	return None


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
		# A callable, so only templates that actually render a form mint a token.
		"csrf_token": csrf_token,
		"validation_type_labels": VALIDATION_TYPE_LABELS,
	}


# ─── CSRF ─────────────────────────────────────────────────────────────────────
#
# Every state-changing request must carry a token tied to the caller's session.
# Enforcement is fail-closed and applies to all unsafe methods by default, so a
# route added later is protected without anyone remembering to protect it.

CSRF_FIELD_NAME = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"
CSRF_UNSAFE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
_CSRF_EXEMPT_ENDPOINTS: set[str] = set()


def csrf_exempt(view):
	"""Opt a route out of CSRF validation. Nothing needs this today."""
	_CSRF_EXEMPT_ENDPOINTS.add(view.__name__)
	return view


def csrf_token() -> str:
	"""Current session token, minted on first use. Exposed to templates."""
	token = session.get(CSRF_FIELD_NAME)
	if not isinstance(token, str) or not token:
		token = secrets.token_urlsafe(32)
		session[CSRF_FIELD_NAME] = token
	return token


def _submitted_csrf_token() -> str:
	form_value = request.form.get(CSRF_FIELD_NAME, "")
	if form_value:
		return form_value
	return request.headers.get(CSRF_HEADER_NAME, "")


@app.before_request
def _enforce_csrf():
	if request.method not in CSRF_UNSAFE_METHODS:
		return None
	if request.endpoint in _CSRF_EXEMPT_ENDPOINTS:
		return None

	expected = session.get(CSRF_FIELD_NAME, "")
	submitted = _submitted_csrf_token()
	if expected and submitted and hmac.compare_digest(str(expected), submitted):
		return None

	if request.headers.get("X-Requested-With") == "XMLHttpRequest":
		return jsonify({"ok": False, "error": "Session expired. Reload the page and try again."}), 400
	return render_template("csrf_error.html"), 400


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
	if not has_template(slug, config):
		return render_template("event.html", **event_form_context(config, slug, "Certificate template not found on server.")), 500
	# The image is rendered on demand from the token, so this POST stays cheap.
	return redirect(url_for("preview_page", token=make_cert_token(slug, cert_name)))


def _certificate_from_token(token: str) -> tuple[str, str, dict] | None:
	"""Resolve a certificate link to (slug, printed name, live event config)."""
	resolved = resolve_cert_token(token)
	if resolved is None:
		return None
	slug, cert_name = resolved
	config = load_event(slug)
	if config is None:
		return None
	return slug, cert_name, config


@app.route("/preview/<token>", methods=["GET"])
def preview_page(token: str):
	resolved = _certificate_from_token(token)
	if resolved is None:
		return redirect(url_for("home"))
	_, cert_name, config = resolved
	return render_template("preview.html", cert_token=token, cert_name=cert_name, event=config)


@app.route("/preview-image/<token>", methods=["GET"])
def preview_image(token: str):
	resolved = _certificate_from_token(token)
	if resolved is None:
		return ("Not found", 404)
	slug, cert_name, config = resolved
	rendered = render_certificate(slug, cert_name, config, variant="preview")
	if rendered is None:
		return ("Not found", 404)
	image_bytes, etag, mimetype = rendered

	response = send_file(BytesIO(image_bytes), mimetype=mimetype, etag=etag)
	# The image carries a participant's name, and it changes whenever an admin
	# edits the template or coordinates, so: private, revalidated, never immutable.
	response.cache_control.private = True
	response.cache_control.max_age = 300
	response.cache_control.must_revalidate = True
	return response


@app.route("/download-file/<token>", methods=["GET"])
def download_file(token: str):
	resolved = _certificate_from_token(token)
	if resolved is None:
		return ("Not found", 404)
	slug, cert_name, config = resolved
	rendered = render_certificate(slug, cert_name, config, variant="download")
	if rendered is None:
		return ("Not found", 404)
	image_bytes, _, mimetype = rendered

	return send_file(
		BytesIO(image_bytes),
		mimetype=mimetype,
		as_attachment=True,
		download_name=safe_download_name(cert_name, slug),
	)


# ─── Admin routes ─────────────────────────────────────────────────────────────

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
	error = None
	if request.method == "POST":
		password = request.form.get("password", "")
		if not ADMIN_PASSWORD:
			error = "ADMIN_PASSWORD environment variable is not set on this server."
		elif hmac.compare_digest(password, ADMIN_PASSWORD):
			# New token for the newly privileged session, so a token an attacker
			# may have observed pre-login cannot be replayed against admin routes.
			session.clear()
			session["admin_logged_in"] = True
			csrf_token()
			return redirect(url_for("admin_dashboard"))
		else:
			error = "Incorrect password."
	return render_template("admin/login.html", error=error)


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
	session.clear()
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
						   has_template=has_template(slug, config),
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
						   error=None, has_template=has_template(slug, config),
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
	template_present = has_template(slug, config)
	file = request.files.get("template_file")
	if not file or file.filename == "":
		return render_template("admin/event_form.html", event=config, is_new=False,
							   error="No file selected.", has_template=template_present, has_csv=has_csv,
							   csv_columns=csv_headers(slug)), 400

	filename = secure_filename(file.filename).lower()
	ext = os.path.splitext(filename)[1].lower()
	if ext not in TEMPLATE_EXTENSIONS:
		return render_template("admin/event_form.html", event=config, is_new=False,
				error="Template must be PNG, JPG, GIF, or WebP.", has_template=template_present, has_csv=has_csv,
				csv_columns=csv_headers(slug)), 400

	file.stream.seek(0)
	data = file.stream.read()
	validation_error = validate_template_upload(data, ext)
	if validation_error:
		return render_template("admin/event_form.html", event=config, is_new=False,
				error=validation_error, has_template=template_present, has_csv=has_csv,
				csv_columns=csv_headers(slug)), 400

	try:
		version = save_template_bytes(slug, data, ext)
	except Exception:
		return render_template("admin/event_form.html", event=config, is_new=False,
				error="Could not save the template to storage. Check the Supabase settings and try again.",
				has_template=template_present, has_csv=has_csv,
				csv_columns=csv_headers(slug)), 502

	# Recording the version on the config is what invalidates every image cache:
	# render keys embed it, so a replaced template can never be served stale.
	config["template_ext"] = ext
	config["template_version"] = version
	save_event_config(slug, config)
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
	template_present = has_template(slug, config)
	has_csv = _event_csv_exists(slug)
	file = request.files.get("csv_file")
	if not file or file.filename == "":
		return render_template("admin/event_form.html", event=config, is_new=False,
							   error="No file selected.", has_template=template_present, has_csv=has_csv,
							   csv_columns=csv_headers(slug)), 400
	filename = secure_filename(file.filename).lower()
	ext = os.path.splitext(filename)[1]
	if ext not in PARTICIPANT_EXTENSIONS:
		return render_template("admin/event_form.html", event=config, is_new=False,
							   error="Participants file must be a .csv or .xlsx.",
							   has_template=template_present, has_csv=has_csv,
							   csv_columns=csv_headers(slug)), 400

	raw_upload = file.stream.read()
	content, upload_error = participant_text_from_upload(raw_upload, ext)
	if upload_error:
		return render_template("admin/event_form.html", event=config, is_new=False,
							   error=upload_error, has_template=template_present, has_csv=has_csv,
							   csv_columns=csv_headers(slug)), 400

	validation_type = config.get("validation_type", "player_team")
	custom_fields: list[str] = config.get("custom_fields", [])
	if validation_type == "custom" and not custom_fields:
		return render_template("admin/event_form.html", event=config, is_new=False,
							   error="Pick at least one column to match on before uploading a participant list.",
							   has_template=template_present, has_csv=has_csv,
							   csv_columns=csv_headers(slug)), 400
	required_headers = required_headers_for_validation(validation_type, custom_fields)
	try:
		reader = csv.DictReader(content.splitlines())
		headers = {h.strip().lower() for h in (reader.fieldnames or [])}
		if validation_type == "badge_id" and not ({"roll_no", "id", "badge_id", "badge_number"} & headers):
			return render_template("admin/event_form.html", event=config, is_new=False,
								   error="The file must include one of: roll_no, id, badge_id, badge_number.",
								   has_template=template_present, has_csv=has_csv,
								   csv_columns=sorted(headers)), 400
		if not required_headers.issubset(headers):
			missing = ", ".join(sorted(required_headers - headers))
			return render_template("admin/event_form.html", event=config, is_new=False,
								   error=f"The file is missing required column(s): {missing}.",
								   has_template=template_present, has_csv=has_csv,
								   csv_columns=sorted(headers)), 400
	except Exception:
		return render_template("admin/event_form.html", event=config, is_new=False,
							   error="Could not read that participant list.",
							   has_template=template_present, has_csv=has_csv,
							   csv_columns=csv_headers(slug)), 400

	try:
		save_event_csv(slug, content, source=(raw_upload, ext))
	except Exception:
		return render_template("admin/event_form.html", event=config, is_new=False,
							   error="Could not save the participant list to storage. Check the Supabase settings and try again.",
							   has_template=template_present, has_csv=has_csv,
							   csv_columns=csv_headers(slug)), 502

	converted = " Converted from the first sheet of the workbook." if ext == ".xlsx" else ""
	return render_template("admin/event_form.html", event=config, is_new=False,
						   success=f"Participant list uploaded.{converted}", error=None,
						   has_template=template_present, has_csv=True,
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
	logs_content = ""
	if os.path.exists(LOG_FILE):
		try:
			# Only the tail is useful, and the file is never rotated.
			with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
				logs_content = "".join(f.readlines()[-2000:])
		except Exception as e:
			logs_content = f"Error reading logs: {e}"
	return render_template("admin/logs.html", logs=logs_content)

@app.route("/admin/logs/clear", methods=["POST"])
@require_admin
def admin_clear_logs():
	"""Clear the system.log file."""
	if os.path.exists(LOG_FILE):
		try:
			with open(LOG_FILE, "w", encoding="utf-8") as f:
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
	config = load_event(slug)
	if config is None:
		return "Not found", 404
	data = load_template_bytes(slug, config)
	if data is None:
		return "Template not found", 404
	ext = template_ext_for(slug, config)
	response = send_file(
		BytesIO(data),
		mimetype=TEMPLATE_CONTENT_TYPES.get(ext, "application/octet-stream"),
		etag=template_version_for(slug, config),
	)
	response.cache_control.private = True
	response.cache_control.max_age = 60
	return response


@app.route("/admin/events/<slug>/render-preview", methods=["GET"])
@require_admin
def admin_render_preview(slug: str):
	"""Render a preview with the same server-side Pillow path used for generated certificates."""
	if not safe_slug(slug):
		return "Not found", 404
	config = load_event(slug)
	if config is None:
		return "Not found", 404
	image = get_template_image(slug, config)
	if image is None:
		return "Template not found", 404
	metadata = build_preview_metadata(config, request.args.get("cert_name"))
	draw_name_on_image(image, metadata)
	image_bytes, mimetype = encode_certificate(image, variant="preview")
	response = send_file(BytesIO(image_bytes), mimetype=mimetype)
	# Short cache for admin previews (they may change as settings are edited)
	response.cache_control.max_age = 60
	response.cache_control.public = True
	return response


@app.route("/healthz", methods=["GET"])
def healthz():
	"""
	Keep-alive endpoint for the warm-up cron.

	It deliberately touches Supabase: free Supabase projects pause after about a
	week of inactivity, and the app only reads storage on a cache miss, so pinging
	Flask alone would not be enough to keep the storage project awake.
	"""
	storage = "disabled"
	if _supabase_enabled():
		try:
			_supabase_ping()
			storage = "ok"
		except Exception:
			storage = "error"
	return jsonify({"ok": True, "storage": storage})


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
