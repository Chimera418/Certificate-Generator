"""
Club and event storage for the multi-tenant platform.

One interface (`Repository`), two backends chosen explicitly by whether
`DATABASE_URL` is set:

  * `PostgresRepository` - the real store, over psycopg. Selected when
    DATABASE_URL is present. If Postgres is configured but unreachable it raises
    `DatabaseUnavailable`; the web layer turns that into a 503. It NEVER falls
    back to the in-memory store - a credential store that silently forgot every
    account, or authenticated against an empty table, is worse than a 503.

  * `InMemoryRepository` - a process-local dict store for `tests/` (which run
    with no network) and for local development. Selected only when DATABASE_URL
    is unset, and announced loudly at startup, because it does not persist: on a
    real host every club account would vanish on restart.

Every club-scoped read and write takes `club_id` as an argument. There is no
"get event by slug" that spans clubs - isolation is structural, not a WHERE
clause a caller might forget. A club asking for another club's event gets None,
which the web layer renders as 404 (never 403 - we do not confirm the other
club's event exists).
"""
from __future__ import annotations

import copy
import os
import threading
import uuid
from datetime import datetime, timezone

DEFAULT_QUOTA_BYTES = 100 * 1024 * 1024  # 100 MB
CLUB_STATUSES = ("pending", "approved", "suspended")


class DatabaseUnavailable(RuntimeError):
	"""Postgres is configured but could not be reached. The caller serves a 503."""


def _clean_config(config) -> dict:
	"""Drop keys the app derives at load time (anything underscore-prefixed, e.g. a
	resolved club slug) so they never get persisted into the jsonb column and then
	drift. The store holds only what the event actually is."""
	if not isinstance(config, dict):
		return {}
	return {k: v for k, v in config.items() if not str(k).startswith("_")}


def _now() -> str:
	return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
	return str(uuid.uuid4())


class Repository:
	"""Interface both backends implement. All club-scoped calls require club_id."""

	backend_name = "abstract"

	# ── clubs ────────────────────────────────────────────────────────────────
	def create_club(self, slug: str, name: str, password_hash: str) -> dict: ...
	def get_club_by_id(self, club_id: str) -> dict | None: ...
	def get_club_by_slug(self, slug: str) -> dict | None: ...
	def list_clubs(self) -> list[dict]: ...
	def set_club_status(self, club_id: str, status: str) -> bool: ...
	def set_club_password(self, club_id: str, password_hash: str) -> bool: ...
	def set_club_quota(self, club_id: str, quota_bytes: int) -> bool: ...
	def search_club_slugs(self, prefix: str, limit: int = 8) -> list[str]: ...

	# ── events ───────────────────────────────────────────────────────────────
	def create_event(self, club_id: str, slug: str, name: str, config: dict) -> dict | None: ...
	def get_event(self, club_id: str, slug: str) -> dict | None: ...
	def list_events(self, club_id: str) -> list[dict]: ...
	def update_event(self, club_id: str, slug: str, **changes) -> dict | None: ...
	def delete_event(self, club_id: str, slug: str) -> bool: ...
	def find_event_globally(self, slug: str) -> dict | None:
		"""Resolve an event by slug alone, across approved clubs.

		Safe ONLY while there is at most one approved club (Phases 1-2): with two
		clubs a bare slug is ambiguous, which is exactly why Phase 3 moves the
		participant path under /c/<club>/<event>. Returns the row only when the
		slug is unique among approved clubs, else None.
		"""
		...


class InMemoryRepository(Repository):
	"""Dict-backed store for tests and local dev. Not durable; not for production."""

	backend_name = "in-memory"

	def __init__(self):
		self._lock = threading.RLock()
		self._clubs: dict[str, dict] = {}          # id -> club
		self._events: dict[tuple[str, str], dict] = {}  # (club_id, slug) -> event

	def reset(self) -> None:
		with self._lock:
			self._clubs.clear()
			self._events.clear()

	# ── clubs ────────────────────────────────────────────────────────────────
	def create_club(self, slug: str, name: str, password_hash: str) -> dict:
		with self._lock:
			if any(c["slug"] == slug for c in self._clubs.values()):
				raise ValueError("slug already exists")
			club = {
				"id": _new_id(), "slug": slug, "name": name,
				"password_hash": password_hash, "status": "pending",
				"quota_bytes": DEFAULT_QUOTA_BYTES, "created_at": _now(),
			}
			self._clubs[club["id"]] = club
			return copy.deepcopy(club)

	def get_club_by_id(self, club_id: str) -> dict | None:
		with self._lock:
			c = self._clubs.get(club_id)
			return copy.deepcopy(c) if c else None

	def get_club_by_slug(self, slug: str) -> dict | None:
		with self._lock:
			for c in self._clubs.values():
				if c["slug"] == slug:
					return copy.deepcopy(c)
			return None

	def list_clubs(self) -> list[dict]:
		with self._lock:
			return [copy.deepcopy(c) for c in
					sorted(self._clubs.values(), key=lambda c: c["created_at"])]

	def set_club_status(self, club_id: str, status: str) -> bool:
		if status not in CLUB_STATUSES:
			raise ValueError("invalid status")
		with self._lock:
			c = self._clubs.get(club_id)
			if not c:
				return False
			c["status"] = status
			return True

	def set_club_password(self, club_id: str, password_hash: str) -> bool:
		with self._lock:
			c = self._clubs.get(club_id)
			if not c:
				return False
			c["password_hash"] = password_hash
			return True

	def set_club_quota(self, club_id: str, quota_bytes: int) -> bool:
		with self._lock:
			c = self._clubs.get(club_id)
			if not c:
				return False
			c["quota_bytes"] = int(quota_bytes)
			return True

	def search_club_slugs(self, prefix: str, limit: int = 8) -> list[str]:
		prefix = (prefix or "").strip().lower()
		if len(prefix) < 3:
			return []
		with self._lock:
			slugs = sorted(c["slug"] for c in self._clubs.values()
						   if c["slug"].startswith(prefix))
			return slugs[:limit]

	# ── events ───────────────────────────────────────────────────────────────
	def create_event(self, club_id: str, slug: str, name: str, config: dict) -> dict | None:
		with self._lock:
			if club_id not in self._clubs:
				return None
			if (club_id, slug) in self._events:
				raise ValueError("event slug already exists for this club")
			event = {
				"id": _new_id(), "club_id": club_id, "slug": slug, "name": name,
				"config": _clean_config(copy.deepcopy(config)), "active": False,
				"template_ext": None, "template_version": None, "created_at": _now(),
			}
			self._events[(club_id, slug)] = event
			return copy.deepcopy(event)

	def get_event(self, club_id: str, slug: str) -> dict | None:
		with self._lock:
			e = self._events.get((club_id, slug))
			return copy.deepcopy(e) if e else None

	def list_events(self, club_id: str) -> list[dict]:
		with self._lock:
			events = [e for e in self._events.values() if e["club_id"] == club_id]
			return [copy.deepcopy(e) for e in
					sorted(events, key=lambda e: e["created_at"], reverse=True)]

	def update_event(self, club_id: str, slug: str, **changes) -> dict | None:
		allowed = {"name", "config", "active", "template_ext", "template_version"}
		with self._lock:
			e = self._events.get((club_id, slug))
			if not e:
				return None
			for key, value in changes.items():
				if key in allowed:
					e[key] = _clean_config(copy.deepcopy(value)) if key == "config" else value
			return copy.deepcopy(e)

	def delete_event(self, club_id: str, slug: str) -> bool:
		with self._lock:
			return self._events.pop((club_id, slug), None) is not None

	def find_event_globally(self, slug: str) -> dict | None:
		with self._lock:
			approved = {c["id"] for c in self._clubs.values() if c["status"] == "approved"}
			matches = [e for e in self._events.values()
					   if e["slug"] == slug and e["club_id"] in approved]
			return copy.deepcopy(matches[0]) if len(matches) == 1 else None


class PostgresRepository(Repository):
	"""psycopg-backed store. Raises DatabaseUnavailable when Postgres is unreachable."""

	backend_name = "postgres"

	def __init__(self, dsn: str):
		self._dsn = dsn
		self._pool = None
		self._lock = threading.Lock()

	# psycopg is imported lazily so the package is only required in production,
	# where DATABASE_URL is set. tests/ and local dev never import it.
	def _get_pool(self):
		if self._pool is not None:
			return self._pool
		with self._lock:
			if self._pool is None:
				try:
					from psycopg_pool import ConnectionPool
					self._pool = ConnectionPool(self._dsn, min_size=1, max_size=8,
												open=True, timeout=10)
				except Exception as exc:  # import error or connect failure
					raise DatabaseUnavailable(str(exc)) from exc
			return self._pool

	def _connection(self):
		pool = self._get_pool()
		try:
			return pool.connection()
		except Exception as exc:
			raise DatabaseUnavailable(str(exc)) from exc

	@staticmethod
	def _club_row(row) -> dict:
		return {"id": str(row[0]), "slug": row[1], "name": row[2], "password_hash": row[3],
				"status": row[4], "quota_bytes": int(row[5]), "created_at": row[6].isoformat()}

	@staticmethod
	def _event_row(row) -> dict:
		return {"id": str(row[0]), "club_id": str(row[1]), "slug": row[2], "name": row[3],
				"config": row[4] or {}, "active": bool(row[5]), "template_ext": row[6],
				"template_version": row[7], "created_at": row[8].isoformat()}

	def _run(self, fn):
		try:
			with self._connection() as conn:
				return fn(conn)
		except (DatabaseUnavailable, ValueError):
			# ValueError is a domain error (e.g. duplicate slug) the caller handles
			# as a 400 - it must not be masked as an unreachable-database 503.
			raise
		except Exception as exc:
			raise DatabaseUnavailable(str(exc)) from exc

	# ── clubs ────────────────────────────────────────────────────────────────
	def create_club(self, slug: str, name: str, password_hash: str) -> dict:
		import psycopg

		def op(conn):
			with conn.cursor() as cur:
				try:
					cur.execute(
						"insert into clubs (slug, name, password_hash) values (%s, %s, %s) "
						"returning id, slug, name, password_hash, status, quota_bytes, created_at",
						(slug, name, password_hash))
				except psycopg.errors.UniqueViolation as exc:
					conn.rollback()
					raise ValueError("slug already exists") from exc
				return self._club_row(cur.fetchone())
		return self._run(op)

	def get_club_by_id(self, club_id: str) -> dict | None:
		return self._run(lambda conn: self._one_club(conn, "id", club_id))

	def get_club_by_slug(self, slug: str) -> dict | None:
		return self._run(lambda conn: self._one_club(conn, "slug", slug))

	def _one_club(self, conn, column: str, value) -> dict | None:
		with conn.cursor() as cur:
			cur.execute(
				f"select id, slug, name, password_hash, status, quota_bytes, created_at "
				f"from clubs where {column} = %s", (value,))
			row = cur.fetchone()
			return self._club_row(row) if row else None

	def list_clubs(self) -> list[dict]:
		def op(conn):
			with conn.cursor() as cur:
				cur.execute("select id, slug, name, password_hash, status, quota_bytes, "
							"created_at from clubs order by created_at")
				return [self._club_row(r) for r in cur.fetchall()]
		return self._run(op)

	def set_club_status(self, club_id: str, status: str) -> bool:
		if status not in CLUB_STATUSES:
			raise ValueError("invalid status")
		return self._update_club(club_id, "status", status)

	def set_club_password(self, club_id: str, password_hash: str) -> bool:
		return self._update_club(club_id, "password_hash", password_hash)

	def set_club_quota(self, club_id: str, quota_bytes: int) -> bool:
		return self._update_club(club_id, "quota_bytes", int(quota_bytes))

	def _update_club(self, club_id: str, column: str, value) -> bool:
		def op(conn):
			with conn.cursor() as cur:
				cur.execute(f"update clubs set {column} = %s where id = %s", (value, club_id))
				return cur.rowcount > 0
		return self._run(op)

	def search_club_slugs(self, prefix: str, limit: int = 8) -> list[str]:
		prefix = (prefix or "").strip().lower()
		if len(prefix) < 3:
			return []

		def op(conn):
			with conn.cursor() as cur:
				cur.execute("select slug from clubs where slug like %s order by slug limit %s",
							(prefix.replace("%", "").replace("_", "") + "%", limit))
				return [r[0] for r in cur.fetchall()]
		return self._run(op)

	# ── events ───────────────────────────────────────────────────────────────
	def create_event(self, club_id: str, slug: str, name: str, config: dict) -> dict | None:
		import json as _json
		import psycopg

		def op(conn):
			with conn.cursor() as cur:
				cur.execute("select 1 from clubs where id = %s", (club_id,))
				if cur.fetchone() is None:
					return None
				try:
					cur.execute(
						"insert into events (club_id, slug, name, config) values (%s, %s, %s, %s) "
						"returning id, club_id, slug, name, config, active, template_ext, "
						"template_version, created_at",
						(club_id, slug, name, _json.dumps(_clean_config(config))))
				except psycopg.errors.UniqueViolation as exc:
					conn.rollback()
					raise ValueError("event slug already exists for this club") from exc
				return self._event_row(cur.fetchone())
		return self._run(op)

	def get_event(self, club_id: str, slug: str) -> dict | None:
		def op(conn):
			with conn.cursor() as cur:
				cur.execute(
					"select id, club_id, slug, name, config, active, template_ext, "
					"template_version, created_at from events where club_id = %s and slug = %s",
					(club_id, slug))
				row = cur.fetchone()
				return self._event_row(row) if row else None
		return self._run(op)

	def list_events(self, club_id: str) -> list[dict]:
		def op(conn):
			with conn.cursor() as cur:
				cur.execute(
					"select id, club_id, slug, name, config, active, template_ext, "
					"template_version, created_at from events where club_id = %s "
					"order by created_at desc", (club_id,))
				return [self._event_row(r) for r in cur.fetchall()]
		return self._run(op)

	def update_event(self, club_id: str, slug: str, **changes) -> dict | None:
		import json as _json
		allowed = {"name", "config", "active", "template_ext", "template_version"}
		sets, params = [], []
		for key, value in changes.items():
			if key not in allowed:
				continue
			sets.append(f"{key} = %s")
			params.append(_json.dumps(_clean_config(value)) if key == "config" else value)
		if not sets:
			return self.get_event(club_id, slug)
		params.extend([club_id, slug])

		def op(conn):
			with conn.cursor() as cur:
				cur.execute(
					f"update events set {', '.join(sets)} where club_id = %s and slug = %s "
					"returning id, club_id, slug, name, config, active, template_ext, "
					"template_version, created_at", params)
				row = cur.fetchone()
				return self._event_row(row) if row else None
		return self._run(op)

	def delete_event(self, club_id: str, slug: str) -> bool:
		def op(conn):
			with conn.cursor() as cur:
				cur.execute("delete from events where club_id = %s and slug = %s", (club_id, slug))
				return cur.rowcount > 0
		return self._run(op)

	def find_event_globally(self, slug: str) -> dict | None:
		def op(conn):
			with conn.cursor() as cur:
				cur.execute(
					"select e.id, e.club_id, e.slug, e.name, e.config, e.active, "
					"e.template_ext, e.template_version, e.created_at from events e "
					"join clubs c on c.id = e.club_id where e.slug = %s and c.status = 'approved' "
					"limit 2", (slug,))
				rows = cur.fetchall()
				return self._event_row(rows[0]) if len(rows) == 1 else None
		return self._run(op)


def make_repository() -> Repository:
	"""Select a backend from the environment. Explicit, never a runtime fallback."""
	dsn = os.environ.get("DATABASE_URL", "").strip()
	if dsn:
		print("Club store: Postgres (DATABASE_URL is set).")
		return PostgresRepository(dsn)
	print(
		"WARNING: DATABASE_URL is not set. Using an in-memory club store that does "
		"NOT persist - every club account is lost on restart and is invisible to "
		"other workers. This is fine for tests and local development and wrong on "
		"any real deployment. Set DATABASE_URL to use Postgres."
	)
	return InMemoryRepository()
