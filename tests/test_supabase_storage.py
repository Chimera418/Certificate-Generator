"""
Supabase Storage request shapes, verified offline.

Real credentials are not available in tests, so urlopen is stubbed and the
requests the app would have sent are inspected instead: correct URLs, auth
headers, upsert on replace, and the local-file fallback when storage is down.

Run with: python tests/test_supabase_storage.py
"""
import io
import json
import sys
from urllib import error as urlerror

from _fixture import (
    TEST_SLUG,
    A,
    Results,
    make_template_bytes,
    setup_scratch_event,
    teardown_scratch,
)

r = Results()
scratch = setup_scratch_event()

PROJECT = "https://example-project.supabase.co"
SERVICE_KEY = "service-role-key"

# Turn Supabase on for this module only.
A.SUPABASE_URL = PROJECT
A.SUPABASE_SERVICE_KEY = SERVICE_KEY
A.SUPABASE_BUCKET = "certificate-templates"
A._SUPABASE_BUCKET_READY = False

sent = []


class FakeResponse:
    def __init__(self, body=b""):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def make_urlopen(handler):
    def fake_urlopen(request, timeout=None):
        record = {
            "url": request.full_url,
            "method": request.get_method(),
            "headers": {k.lower(): v for k, v in request.header_items()},
            "body": request.data,
        }
        sent.append(record)
        return handler(record)
    return fake_urlopen


original_urlopen = A.urlrequest.urlopen
template_bytes = make_template_bytes()

try:
    r.check("supabase is enabled with url + key", A._supabase_enabled())

    # ── upload ───────────────────────────────────────────────────────────────
    A.urlrequest.urlopen = make_urlopen(lambda record: FakeResponse())
    sent.clear()
    version = A.save_template_bytes(TEST_SLUG, template_bytes, ".png")

    bucket_calls = [c for c in sent if c["url"].endswith("/storage/v1/bucket")]
    r.check("bucket is created on first use", len(bucket_calls) == 1, len(bucket_calls))
    if bucket_calls:
        payload = json.loads(bucket_calls[0]["body"].decode())
        r.check("bucket is created private", payload.get("public") is False, payload)

    uploads = [c for c in sent if "/object/" in c["url"]]
    r.check("exactly one object upload", len(uploads) == 1, len(uploads))
    upload = uploads[0] if uploads else None
    if upload:
        expected = f"{PROJECT}/storage/v1/object/certificate-templates/events/{TEST_SLUG}/template.png"
        r.check("upload targets the versioned object path", upload["url"] == expected, upload["url"])
        r.check("upload is a POST", upload["method"] == "POST", upload["method"])
        r.check("upload sends a bearer token",
                upload["headers"].get("Authorization".lower()) == f"Bearer {SERVICE_KEY}",
                upload["headers"])
        r.check("upload sends the apikey header",
                upload["headers"].get("apikey") == SERVICE_KEY)
        r.check("upload sets the png content type",
                upload["headers"].get("content-type") == "image/png",
                upload["headers"].get("content-type"))
        r.check("upload upserts so replacing a template works",
                upload["headers"].get("x-upsert") == "true",
                upload["headers"].get("x-upsert"))
        r.check("upload sends the file bytes", upload["body"] == template_bytes)

    r.check("save returns a content-derived version", bool(version) and len(version) == 16, version)
    r.check("identical bytes produce the same version",
            A.save_template_bytes(TEST_SLUG, template_bytes, ".png") == version)
    r.check("different bytes produce a different version",
            A.save_template_bytes(TEST_SLUG, make_template_bytes(color=(1, 2, 3)), ".png") != version)

    # ── download ─────────────────────────────────────────────────────────────
    A.urlrequest.urlopen = make_urlopen(lambda record: FakeResponse(template_bytes))
    sent.clear()
    fetched = A.load_template_bytes(TEST_SLUG, {"template_ext": ".png"})
    r.check("download returns the stored bytes", fetched == template_bytes)
    r.check("download is a GET", sent and sent[0]["method"] == "GET", sent[0]["method"] if sent else None)

    # ── missing object falls through to the local file ───────────────────────
    def not_found(record):
        raise urlerror.HTTPError(record["url"], 404, "Not Found", None, None)

    A.urlrequest.urlopen = make_urlopen(not_found)
    local = A.load_template_bytes(TEST_SLUG, {"template_ext": ".png"})
    r.check("a 404 falls back to the local copy", local is not None and len(local) > 0)

    # ── storage outage falls through too, rather than 500ing ─────────────────
    def unreachable(record):
        raise urlerror.URLError("connection refused")

    A.urlrequest.urlopen = make_urlopen(unreachable)
    resilient = A.load_template_bytes(TEST_SLUG, {"template_ext": ".png"})
    r.check("an outage falls back to the local copy", resilient is not None and len(resilient) > 0)

    # ── delete removes every extension variant ───────────────────────────────
    A.urlrequest.urlopen = make_urlopen(lambda record: FakeResponse())
    sent.clear()
    A.delete_template_storage(TEST_SLUG)
    deletes = [c for c in sent if c["method"] == "DELETE"]
    r.check("delete covers all template extensions",
            len(deletes) == len(A.TEMPLATE_EXTENSIONS), len(deletes))

    # ── healthz reports storage state ────────────────────────────────────────
    A.urlrequest.urlopen = make_urlopen(lambda record: FakeResponse(b"{}"))
    body = A.app.test_client().get("/healthz").get_json()
    r.check("healthz reports ok when storage answers", body.get("storage") == "ok", body)

    A.urlrequest.urlopen = make_urlopen(unreachable)
    body = A.app.test_client().get("/healthz").get_json()
    r.check("healthz reports error when storage is down", body.get("storage") == "error", body)
finally:
    A.urlrequest.urlopen = original_urlopen
    teardown_scratch(scratch)

sys.exit(r.finish())
