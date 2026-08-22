"""
Phase 3.5b: a club authoring an event end to end through /dashboard, entirely
club-scoped, and the two ownership guarantees:

  * every authoring route is scoped by session club_id - club B cannot edit,
    publish, delete, or read the placement of club A's event (404, never 403);
  * the field editor re-homed under /dashboard reads the CLUB's CSV columns, and
    a saved multi-field config renders the club's certificate.

Run with: python tests/test_club_authoring.py
"""
import io
import json
import re
import sys

from PIL import Image

from _fixture import A, Results, csrf, post, setup_scratch_event, teardown_scratch


r = Results()
scratch = setup_scratch_event()

# Storage through a stub, so uploads land somewhere and reads find them.
A.SUPABASE_URL = "https://proj.supabase.co"
A.SUPABASE_SERVICE_KEY = "svc"
A.SUPABASE_BUCKET = "shared"
A._SUPABASE_BUCKET_READY = True
_store = {}


class FR:
    def __init__(self, b=b""):
        self.b = b
    def read(self):
        return self.b
    def __enter__(self):
        return self
    def __exit__(self, *e):
        return False


def fake_urlopen(request, timeout=None):
    url = request.full_url
    method = request.get_method()
    if "/object/list/" in url:
        return FR(json.dumps([]).encode())
    if "/object/" in url:
        key = url.split("/object/shared/", 1)[-1] if "/object/shared/" in url else url.split("/object/", 1)[-1]
        if method == "POST":
            _store[key] = request.data
            return FR()
        if method == "GET":
            if key in _store:
                return FR(_store[key])
            from urllib import error as urlerror
            raise urlerror.HTTPError(url, 404, "nf", {}, io.BytesIO(b""))
        if method == "DELETE":
            _store.pop(key, None)
            return FR()
    return FR()


A.urlrequest.urlopen = fake_urlopen


def png():
    b = io.BytesIO(); Image.new("RGB", (400, 300), (240, 235, 220)).save(b, format="PNG")
    return b.getvalue()


def club_client(slug, name="Club"):
    club = A._repository.create_club(slug, name, A.generate_password_hash("password1"))
    A._repository.set_club_status(club["id"], "approved")
    c = A.app.test_client()
    with c.session_transaction() as sess:
        sess["club_id"] = club["id"]
        sess[A.CSRF_FIELD_NAME] = "t"
    return club, c


def cpost(c, url, data=None, **kw):
    data = dict(data or {}); data["csrf_token"] = "t"
    return c.post(url, data=data, **kw)


try:
    club_a, a = club_client("acme", "Acme")
    club_b, b = club_client("beta", "Beta")

    # ── create -> upload template -> upload CSV -> place fields -> publish ────
    r.check("create event", cpost(a, "/dashboard/events", {"name": "Gala", "slug": "gala"}).status_code in (302, 303))
    up = cpost(a, "/dashboard/events/gala/template",
               data={"template_file": (io.BytesIO(png()), "t.png")}, content_type="multipart/form-data")
    r.check("template upload accepted", up.status_code in (200, 302), up.status_code)

    csv_text = "player,team,position\nAda,Nimbus,1st\nGrace,Nimbus,2nd\n"
    cpost(a, "/dashboard/events/gala/csv",
          data={"csv_file": (io.BytesIO(csv_text.encode()), "p.csv")}, content_type="multipart/form-data")
    r.check("the field editor sees the CLUB's columns",
            set(A.csv_headers("gala", "acme")) == {"player", "team", "position"}, A.csv_headers("gala", "acme"))

    # place a csv field via the re-homed editor endpoint
    fields = [
        {"id": "name", "source": "input", "x": 200, "y": 100, "font_size": 40},
        {"id": "position", "source": "csv", "column": "position", "x": 200, "y": 200, "font_size": 30},
    ]
    save = a.post("/dashboard/events/gala/fields", data=json.dumps({"fields": fields}),
                  content_type="application/json",
                  headers={"X-CSRF-Token": "t", "X-Requested-With": "XMLHttpRequest"})
    r.check("club saves fields", save.status_code == 200 and save.get_json().get("ok"), save.status_code)
    r.check("a csv field referencing a real club column is accepted",
            len(A._repository.get_event(club_a["id"], "gala")["config"]["fields"]) == 2)

    # settings + publish
    cpost(a, "/dashboard/events/gala/config", {"name": "Gala 2026", "validation_type": "player_team",
                                               "download_format": "jpeg"})
    r.check("settings persist", A._repository.get_event(club_a["id"], "gala")["name"] == "Gala 2026")
    r.check("publish", cpost(a, "/dashboard/events/gala/toggle", {}).status_code in (302, 303))
    r.check("event is now active", A._repository.get_event(club_a["id"], "gala")["active"] is True)

    # the public page and a real render now work, with the csv field resolved
    pub = A.app.test_client()
    with pub.session_transaction() as sess:
        sess[A.CSRF_FIELD_NAME] = "t"
    r.check("published event is public", pub.get("/c/acme/gala").status_code == 200)
    resp = cpost(pub, "/c/acme/gala/download", {"registration_name": "Ada", "team_name": "Nimbus", "cert_name": "Ada"})
    tok = re.search(r"/preview/([^/?]+)", resp.headers.get("Location", ""))
    r.check("a participant can generate a certificate", tok is not None, resp.status_code)
    if tok:
        _club, _slug, vals = A.read_cert_token(tok.group(1))
        r.check("the csv field resolved from the CLUB's roster", vals.get("position") == "1st", vals)

    # ── cross-club authoring isolation (IDOR): B cannot touch A's event ──────
    r.check("B cannot open A's event detail", b.get("/dashboard/events/gala").status_code == 404)
    r.check("B cannot open A's placement editor", b.get("/dashboard/events/gala/coordinates").status_code == 404)
    r.check("B cannot edit A's settings", cpost(b, "/dashboard/events/gala/config", {"name": "Hijacked"}).status_code == 404)
    r.check("B cannot toggle A's event", cpost(b, "/dashboard/events/gala/toggle", {}).status_code == 404)
    r.check("B cannot save fields on A's event",
            b.post("/dashboard/events/gala/fields", data=json.dumps({"fields": fields}),
                   content_type="application/json",
                   headers={"X-CSRF-Token": "t", "X-Requested-With": "XMLHttpRequest"}).status_code == 404)
    r.check("B cannot read A's template via the editor preview",
            b.get("/dashboard/events/gala/template-preview").status_code == 404)
    r.check("A's settings were not changed by B's attempt",
            A._repository.get_event(club_a["id"], "gala")["name"] == "Gala 2026")
    # And the 404 is indistinguishable from a truly missing event.
    r.check("B's own missing event is the same 404",
            b.get("/dashboard/events/gala").status_code == b.get("/dashboard/events/nope").status_code == 404)

    # ── delete, typed-confirmation, club-scoped ──────────────────────────────
    r.check("delete without the typed slug is refused",
            cpost(a, "/dashboard/events/gala/delete", {"confirm": "wrong"}).status_code == 400)
    r.check("B cannot delete A's event", cpost(b, "/dashboard/events/gala/delete", {"confirm": "gala"}).status_code == 404)
    r.check("A deletes its own event", cpost(a, "/dashboard/events/gala/delete", {"confirm": "gala"}).status_code in (302, 303))
    r.check("the event is gone", A._repository.get_event(club_a["id"], "gala") is None)
    r.check("its public page is now dark", A.app.test_client().get("/c/acme/gala").status_code == 404)
finally:
    teardown_scratch(scratch)

sys.exit(r.finish())
