"""
Supabase Storage request shapes and the bucket layout, verified offline.

Real credentials are not available in tests, so urlopen is stubbed and the
requests the app would have sent are inspected instead: correct object paths,
auth headers, upsert on replace, and the fallbacks when storage is unreachable.

Layout under test:

    <bucket>/<event-slug>/template/template.<ext>
    <bucket>/<event-slug>/participants/data.csv
    <bucket>/<event-slug>/participants/source.xlsx   (only for workbook uploads)

Run with: python tests/test_supabase_storage.py
"""
import json
import sys
from urllib import error as urlerror

from _fixture import (
    TEST_SLUG,
    A,
    Results,
    make_template_bytes,
    make_xlsx_bytes,
    setup_scratch_event,
    teardown_scratch,
)

r = Results()
scratch = setup_scratch_event()

PROJECT = "https://example-project.supabase.co"
SERVICE_KEY = "service-role-key"
BUCKET = "csi-aseb"

A.SUPABASE_URL = PROJECT
A.SUPABASE_SERVICE_KEY = SERVICE_KEY
A.SUPABASE_BUCKET = BUCKET
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


def object_url(path):
    return f"{PROJECT}/storage/v1/object/{BUCKET}/{path}"


original_urlopen = A.urlrequest.urlopen
template_bytes = make_template_bytes()
CSV_TEXT = "name,email\nAda Lovelace,ada@example.com\n"

try:
    r.check("supabase is enabled with url + key", A._supabase_enabled())
    r.check("bucket defaults to the club bucket", A.SUPABASE_BUCKET == BUCKET)

    # ── object paths follow the per-event folder layout ──────────────────────
    r.check("template path is <slug>/template/template.png",
            A._template_object_path(TEST_SLUG, ".png") == f"{TEST_SLUG}/template/template.png",
            A._template_object_path(TEST_SLUG, ".png"))
    r.check("participants path is <slug>/participants/data.csv",
            A._participants_object_path(TEST_SLUG) == f"{TEST_SLUG}/participants/data.csv",
            A._participants_object_path(TEST_SLUG))
    r.check("workbook path sits beside the csv",
            A._participants_object_path(TEST_SLUG, "source.xlsx")
            == f"{TEST_SLUG}/participants/source.xlsx")

    # ── template upload ─────────────────────────────────────────────────────
    A.urlrequest.urlopen = make_urlopen(lambda record: FakeResponse())
    sent.clear()
    version = A.save_template_bytes(TEST_SLUG, template_bytes, ".png")

    bucket_calls = [c for c in sent if c["url"].endswith("/storage/v1/bucket")]
    r.check("bucket is created on first use", len(bucket_calls) == 1, len(bucket_calls))
    if bucket_calls:
        payload = json.loads(bucket_calls[0]["body"].decode())
        r.check("bucket is created private", payload.get("public") is False, payload)
        r.check("bucket is named for the club", payload.get("name") == BUCKET, payload)

    uploads = [c for c in sent if "/object/" in c["url"]]
    r.check("exactly one object upload", len(uploads) == 1, len(uploads))
    upload = uploads[0] if uploads else None
    if upload:
        r.check("template lands in the event's template folder",
                upload["url"] == object_url(f"{TEST_SLUG}/template/template.png"), upload["url"])
        r.check("upload is a POST", upload["method"] == "POST", upload["method"])
        r.check("upload sends a bearer token",
                upload["headers"].get("authorization") == f"Bearer {SERVICE_KEY}")
        r.check("upload sends the apikey header", upload["headers"].get("apikey") == SERVICE_KEY)
        r.check("upload sets the png content type",
                upload["headers"].get("content-type") == "image/png")
        r.check("upload upserts so replacing a template works",
                upload["headers"].get("x-upsert") == "true")
        r.check("upload sends the file bytes", upload["body"] == template_bytes)

    r.check("save returns a content-derived version", bool(version) and len(version) == 16, version)
    r.check("identical bytes produce the same version",
            A.save_template_bytes(TEST_SLUG, template_bytes, ".png") == version)
    r.check("different bytes produce a different version",
            A.save_template_bytes(TEST_SLUG, make_template_bytes(color=(1, 2, 3)), ".png") != version)

    # ── participant list upload ─────────────────────────────────────────────
    sent.clear()
    A.save_event_csv(TEST_SLUG, CSV_TEXT, source=(CSV_TEXT.encode("utf-8"), ".csv"))
    csv_uploads = [c for c in sent if "/object/" in c["url"]]
    r.check("a csv upload writes exactly one object", len(csv_uploads) == 1, len(csv_uploads))
    if csv_uploads:
        r.check("csv lands in the event's participants folder",
                csv_uploads[0]["url"] == object_url(f"{TEST_SLUG}/participants/data.csv"),
                csv_uploads[0]["url"])
        r.check("csv is sent as text/csv",
                csv_uploads[0]["headers"].get("content-type") == "text/csv")
        r.check("csv body is the participant text",
                csv_uploads[0]["body"] == CSV_TEXT.encode("utf-8"))

    sent.clear()
    workbook = make_xlsx_bytes()
    A.save_event_csv(TEST_SLUG, CSV_TEXT, source=(workbook, ".xlsx"))
    xlsx_uploads = [c for c in sent if "/object/" in c["url"]]
    r.check("a workbook upload writes both the csv and the original",
            len(xlsx_uploads) == 2, len(xlsx_uploads))
    urls = [c["url"] for c in xlsx_uploads]
    r.check("derived csv is written", object_url(f"{TEST_SLUG}/participants/data.csv") in urls, urls)
    r.check("original workbook is kept alongside",
            object_url(f"{TEST_SLUG}/participants/source.xlsx") in urls, urls)
    original = next((c for c in xlsx_uploads if c["url"].endswith("source.xlsx")), None)
    if original:
        r.check("workbook keeps its spreadsheet content type",
                original["headers"].get("content-type", "").endswith("spreadsheetml.sheet"),
                original["headers"].get("content-type"))
        r.check("workbook bytes are stored verbatim", original["body"] == workbook)

    # ── reads ───────────────────────────────────────────────────────────────
    A.urlrequest.urlopen = make_urlopen(lambda record: FakeResponse(template_bytes))
    sent.clear()
    r.check("template download returns the stored bytes",
            A.load_template_bytes(TEST_SLUG, {"template_ext": ".png"}) == template_bytes)
    r.check("template download is a GET", sent and sent[0]["method"] == "GET")

    A._EVENT_CSV_CACHE.clear()
    A.urlrequest.urlopen = make_urlopen(lambda record: FakeResponse(CSV_TEXT.encode("utf-8")))
    sent.clear()
    r.check("participant list is read from the bucket",
            A.load_event_csv_text(TEST_SLUG) == CSV_TEXT)
    r.check("participant read hits the participants folder",
            sent and sent[0]["url"] == object_url(f"{TEST_SLUG}/participants/data.csv"),
            sent[0]["url"] if sent else None)

    # ── fallbacks ───────────────────────────────────────────────────────────
    def not_found(record):
        raise urlerror.HTTPError(record["url"], 404, "Not Found", None, None)

    A.urlrequest.urlopen = make_urlopen(not_found)
    r.check("a missing template falls back to the local copy",
            bool(A.load_template_bytes(TEST_SLUG, {"template_ext": ".png"})))
    A._EVENT_CSV_CACHE.clear()
    r.check("a missing participant list falls back to the local copy",
            bool(A.load_event_csv_text(TEST_SLUG)))

    def unreachable(record):
        raise urlerror.URLError("connection refused")

    A.urlrequest.urlopen = make_urlopen(unreachable)
    r.check("a storage outage falls back to the local template",
            bool(A.load_template_bytes(TEST_SLUG, {"template_ext": ".png"})))
    A._EVENT_CSV_CACHE.clear()
    r.check("a storage outage falls back to the local participant list",
            bool(A.load_event_csv_text(TEST_SLUG)))

    # ── deleting an event clears its whole folder ───────────────────────────
    A.urlrequest.urlopen = make_urlopen(lambda record: FakeResponse())
    sent.clear()
    A.delete_event_objects(TEST_SLUG)
    deletes = [c["url"] for c in sent if c["method"] == "DELETE"]
    r.check("delete removes the participant csv",
            object_url(f"{TEST_SLUG}/participants/data.csv") in deletes)
    r.check("delete removes the stored workbook",
            object_url(f"{TEST_SLUG}/participants/source.xlsx") in deletes)
    r.check("delete removes every template extension",
            all(object_url(f"{TEST_SLUG}/template/template{e}") in deletes
                for e in A.TEMPLATE_EXTENSIONS), deletes)
    r.check("delete also clears the pre-folder template keys",
            all(object_url(f"events/{TEST_SLUG}/template{e}") in deletes
                for e in A.TEMPLATE_EXTENSIONS))

    # ── healthz ─────────────────────────────────────────────────────────────
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
