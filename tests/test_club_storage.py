"""
Phase 2: club-prefixed storage and per-club quota.

The checks that matter:
  * a club's uploads land under <club>/<event>/... and are invisible under another
    club's prefix (isolation is in the path, not just a query);
  * a legacy csi-aseb template still resolves at its bare <event>/... path,
    byte-identical;
  * an upload that would cross the quota is refused with a message and stores
    NOTHING (over-quota blocks, never deletes);
  * a non-image is rejected by magic bytes even with a .png name;
  * an event config round-trips through the store without gaining derived keys.

Storage is exercised the way test_supabase_storage does it: urlopen is stubbed and
the requests the app would have sent are inspected. No network.

Run with: python tests/test_club_storage.py
"""
import io
import json
import sys

from _fixture import A, Results, make_template_bytes, setup_scratch_event, teardown_scratch

r = Results()
scratch = setup_scratch_event()

PROJECT = "https://example-project.supabase.co"
BUCKET = "shared-bucket"
A.SUPABASE_URL = PROJECT
A.SUPABASE_SERVICE_KEY = "svc"
A.SUPABASE_BUCKET = BUCKET
A._SUPABASE_BUCKET_READY = True  # skip the bucket-create call in these tests

sent = []
# Per-prefix object listings the fake storage reports (for quota sums).
listing = {}


class FakeResponse:
    def __init__(self, body=b""):
        self._body = body
    def read(self):
        return self._body
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False


def fake_urlopen(request, timeout=None):
    url = request.full_url
    method = request.get_method()
    rec = {"url": url, "method": method, "body": request.data}
    sent.append(rec)
    if "/storage/v1/object/list/" in url:
        prefix = json.loads(request.data)["prefix"]
        return FakeResponse(json.dumps(listing.get(prefix, [])).encode("utf-8"))
    return FakeResponse()


A.urlrequest.urlopen = fake_urlopen


def obj_uploads():
    return [c for c in sent if "/object/" in c["url"] and "/list/" not in c["url"] and c["method"] == "POST"]


try:
    # ── path helpers carry the club prefix ───────────────────────────────────
    r.check("club template path is <club>/<event>/template/...",
            A._template_object_path("gala", ".png", club_slug="acme")
            == "acme/gala/template/template.png",
            A._template_object_path("gala", ".png", club_slug="acme"))
    r.check("legacy template path stays bare <event>/template/...",
            A._template_object_path("gala", ".png") == "gala/template/template.png")
    r.check("club participants path is club-prefixed",
            A._participants_object_path("gala", club_slug="acme") == "acme/gala/participants/data.csv")

    # ── a club upload lands only under its own prefix ────────────────────────
    sent.clear()
    tpl = make_template_bytes()
    A.save_template_bytes("gala", tpl, ".png", club_slug="acme")
    ups = obj_uploads()
    r.check("club template uploaded exactly once", len(ups) == 1, len(ups))
    r.check("club template lands under acme/gala/", ups and ups[0]["url"].endswith("/acme/gala/template/template.png"),
            ups and ups[0]["url"])
    r.check("nothing was written under another club's prefix",
            not any("/beta/" in c["url"] for c in ups))

    sent.clear()
    A.save_event_csv("gala", "name\nAda\n", source=("name\nAda\n".encode(), ".csv"), club_slug="acme")
    csv_ups = obj_uploads()
    r.check("club csv lands under acme/gala/participants/",
            csv_ups and csv_ups[0]["url"].endswith("/acme/gala/participants/data.csv"),
            csv_ups and csv_ups[0]["url"])

    # ── legacy csi-aseb template resolves at its bare path, byte-identical ────
    legacy_bytes = make_template_bytes(color=(9, 9, 9))
    served = {}

    def legacy_urlopen(request, timeout=None):
        url = request.full_url
        sent.append({"url": url, "method": request.get_method(), "body": request.data})
        # Only the bare per-event path has the object; the club path 404s.
        if url.endswith("/csi-legacy/template/template.png"):
            return FakeResponse(legacy_bytes)
        from urllib import error as urlerror
        raise urlerror.HTTPError(url, 404, "not found", {}, io.BytesIO(b""))

    A.urlrequest.urlopen = legacy_urlopen
    got = A.load_template_bytes("csi-legacy", {"template_ext": ".png", "template_version": "v1"})
    r.check("a legacy event reads from its bare <event>/ path", got == legacy_bytes,
            None if got == legacy_bytes else "mismatch")
    A.urlrequest.urlopen = fake_urlopen

    # A club event reads ONLY from the club path - no bare-path fallback, no wasted probe.
    sent.clear()
    club_bytes = make_template_bytes(color=(1, 2, 3))

    def club_read_urlopen(request, timeout=None):
        url = request.full_url
        sent.append({"url": url, "method": request.get_method()})
        if url.endswith("/acme/gala/template/template.png"):
            return FakeResponse(club_bytes)
        from urllib import error as urlerror
        raise urlerror.HTTPError(url, 404, "nf", {}, io.BytesIO(b""))

    A.urlrequest.urlopen = club_read_urlopen
    got = A.load_template_bytes("gala", {"template_ext": ".png", "template_version": "v1"}, club_slug="acme")
    r.check("a club event reads from its club path", got == club_bytes)
    r.check("a club read probes only the club path (no legacy fallback)",
            all("/acme/gala/" in c["url"] for c in sent if "/object/" in c["url"]),
            [c["url"] for c in sent])
    A.urlrequest.urlopen = fake_urlopen

    # ── quota: sum the club's objects, gate the upload ───────────────────────
    club = {"slug": "acme", "quota_bytes": 100 * 1024 * 1024}
    listing.clear()
    # acme/ -> one event folder; acme/gala/ -> template + participants folders; each holds one sized file.
    listing["acme/"] = [{"name": "gala", "metadata": None}]
    listing["acme/gala/"] = [{"name": "template", "metadata": None},
                             {"name": "participants", "metadata": None}]
    listing["acme/gala/template/"] = [{"name": "template.png", "metadata": {"size": 40 * 1024 * 1024}}]
    listing["acme/gala/participants/"] = [{"name": "data.csv", "metadata": {"size": 10 * 1024 * 1024}}]
    used = A.club_storage_bytes("acme")
    r.check("club_storage_bytes sums recursively across folders", used == 50 * 1024 * 1024,
            used // (1024 * 1024))

    ok, msg = A.quota_status(club, 40 * 1024 * 1024)
    r.check("an upload that fits is allowed", ok and msg is None, msg)
    ok, msg = A.quota_status(club, 60 * 1024 * 1024)
    r.check("an upload that would cross the cap is refused", not ok and msg, msg)
    r.check("the refusal message says nothing was uploaded", msg and "Nothing was uploaded" in msg)

    # ── replacement netting: re-uploading over an existing object frees its bytes ─
    tight = {"slug": "acme", "quota_bytes": 60 * 1024 * 1024}  # used is 50 MB
    replace_path = "acme/gala/template/template.png"           # the 40 MB template
    ok_new, _ = A.quota_status(tight, 40 * 1024 * 1024)        # as a NEW object
    r.check("a 40 MB new upload near the cap is refused (50+40 > 60)", not ok_new)
    ok_repl, _ = A.quota_status(tight, 40 * 1024 * 1024, replacing_path=replace_path)
    r.check("the same 40 MB as a REPLACEMENT is allowed ((50-40)+40 <= 60)", ok_repl)

    # ── fail closed: a listing outage must NOT admit the upload as 'used == 0' ────
    def listing_down(request, timeout=None):
        if "/storage/v1/object/list/" in request.full_url:
            from urllib import error as urlerror
            raise urlerror.HTTPError(request.full_url, 500, "boom", {}, io.BytesIO(b""))
        return FakeResponse()

    A.urlrequest.urlopen = listing_down
    r.check("lenient club_storage_bytes swallows the error (partial figure for display)",
            A.club_storage_bytes("acme") == 0)
    down_ok, down_msg = A.quota_status(club, 5 * 1024 * 1024)
    r.check("quota FAILS CLOSED when usage cannot be verified", not down_ok and down_msg)
    r.check("the fail-closed message asks to retry, not 'exceeds limit'",
            down_msg and "verify" in down_msg.lower())
    A.urlrequest.urlopen = fake_urlopen

    # And the refusal path stores nothing: drive the real route with a would-be-huge quota.
    A._repository = A.db.InMemoryRepository()
    c = A._repository.create_club("acme", "Acme", A.generate_password_hash("password1"))
    A._repository.set_club_status(c["id"], "approved")
    A._repository.set_club_quota(c["id"], 1)  # 1 byte quota -> any upload is over
    A._repository.create_event(c["id"], "gala", "Gala", {})

    web = A.app.test_client()
    with web.session_transaction() as sess:
        sess["club_id"] = c["id"]
        sess[A.CSRF_FIELD_NAME] = "tok"
    listing.clear()  # store currently empty
    sent.clear()
    resp = web.post("/dashboard/events/gala/template",
                    data={"csrf_token": "tok", "template_file": (io.BytesIO(tpl), "t.png")},
                    content_type="multipart/form-data")
    r.check("an over-quota upload route returns 400", resp.status_code == 400, resp.status_code)
    r.check("an over-quota upload uploaded no object", len(obj_uploads()) == 0, len(obj_uploads()))
    r.check("an over-quota upload recorded no template on the event",
            not A._repository.get_event(c["id"], "gala")["config"].get("template_version"))

    # ── magic bytes: a .png that is not a PNG is rejected ────────────────────
    A._repository.set_club_quota(c["id"], 100 * 1024 * 1024)
    sent.clear()
    bad = web.post("/dashboard/events/gala/template",
                   data={"csrf_token": "tok", "template_file": (io.BytesIO(b"not a real png"), "evil.png")},
                   content_type="multipart/form-data")
    r.check("a fake .png is rejected by magic-byte validation", bad.status_code == 400, bad.status_code)
    r.check("the rejected fake uploaded nothing", len(obj_uploads()) == 0)

    # A real upload under quota succeeds and records the template.
    sent.clear()
    good = web.post("/dashboard/events/gala/template",
                    data={"csrf_token": "tok", "template_file": (io.BytesIO(tpl), "real.png")},
                    content_type="multipart/form-data")
    r.check("a valid under-quota upload succeeds", good.status_code == 200, good.status_code)
    r.check("the valid upload wrote one object under the club prefix",
            len(obj_uploads()) == 1 and obj_uploads()[0]["url"].endswith("/acme/gala/template/template.png"))
    ev = A._repository.get_event(c["id"], "gala")
    r.check("the event records the template on its config", ev["config"].get("template_version"))

    # ── config never gains derived keys through the store ────────────────────
    A._repository.update_event(c["id"], "gala", config={"validation_type": "none",
                                                        "_club_slug": "acme", "_derived": 1, "fields": []})
    stored = A._repository.get_event(c["id"], "gala")["config"]
    r.check("a round-tripped config carries no underscore-prefixed keys",
            not any(str(k).startswith("_") for k in stored), list(stored))
    r.check("the real config keys survive", "validation_type" in stored and "fields" in stored)

    # ── cross-club prefix isolation, end to end ──────────────────────────────
    beta = A._repository.create_club("beta", "Beta", A.generate_password_hash("password1"))
    A._repository.set_club_status(beta["id"], "approved")
    A._repository.create_event(beta["id"], "gala", "Beta Gala", {})  # same slug, different club
    beta_web = A.app.test_client()
    with beta_web.session_transaction() as sess:
        sess["club_id"] = beta["id"]
        sess[A.CSRF_FIELD_NAME] = "tok"
    sent.clear()
    beta_web.post("/dashboard/events/gala/template",
                  data={"csrf_token": "tok", "template_file": (io.BytesIO(tpl), "b.png")},
                  content_type="multipart/form-data")
    beta_ups = obj_uploads()
    r.check("beta's upload lands under beta/, not acme/",
            beta_ups and beta_ups[0]["url"].endswith("/beta/gala/template/template.png"),
            beta_ups and beta_ups[0]["url"])
    r.check("beta cannot see acme's event through the repository",
            A._repository.get_event(beta["id"], "gala")["name"] == "Beta Gala")
finally:
    teardown_scratch(scratch)

sys.exit(r.finish())
