"""
Phase 3: club-scoped participant URLs, the club-in-token binding, and the guarded
/events redirect.

The two that matter most (and the whole reason the club is signed into the token,
not taken from the URL):
  * a token minted under club A is REJECTED when club B has an identically-slugged
    event - the club travels in the signed token, so it cannot be swapped;
  * /events/<slug> 301s only when csi-aseb really has that event; an unknown slug
    404s directly, never a 301 into a 404.

Plus the IDOR surface Phase 3 opens: cross-club access by URL manipulation, all of
which must 404 (never 403 - a probe must not learn another club's event exists).

Run with: python tests/test_url_scoping.py
"""
import io
import json
import re
import sys

from PIL import Image

from _fixture import A, Results, post, setup_scratch_event, teardown_scratch

r = Results()
scratch = setup_scratch_event()

# Route storage through a stub so club uploads have somewhere to go.
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
    if "/object/list/" in url:
        return FR(json.dumps([]).encode())  # always report empty -> under quota
    if request.get_method() == "POST" and "/object/" in url:
        key = url.split("/object/")[1].split("shared/", 1)[-1]
        _store[key] = request.data
        return FR()
    if request.get_method() == "GET" and "/object/" in url:
        key = url.split("/object/shared/", 1)[-1]
        if key in _store:
            return FR(_store[key])
        from urllib import error as urlerror
        raise urlerror.HTTPError(url, 404, "nf", {}, io.BytesIO(b""))
    return FR()


A.urlrequest.urlopen = fake_urlopen


def png(color=(240, 235, 220)):
    b = io.BytesIO()
    Image.new("RGB", (400, 300), color).save(b, format="PNG")
    return b.getvalue()


def make_club_event(slug, club_slug, name, color, active=True):
    club = A._repository.get_club_by_slug(club_slug)
    if club is None:
        club = A._repository.create_club(club_slug, club_slug, A.generate_password_hash("password1"))
        A._repository.set_club_status(club["id"], "approved")
    A._repository.create_event(club["id"], slug, name, {"validation_type": "none"})
    # upload a template through the club route so it lands under the club prefix
    c = A.app.test_client()
    with c.session_transaction() as sess:
        sess["club_id"] = club["id"]
        sess[A.CSRF_FIELD_NAME] = "t"
    c.post(f"/dashboard/events/{slug}/template",
           data={"csrf_token": "t", "template_file": (io.BytesIO(png(color)), "t.png")},
           content_type="multipart/form-data")
    A._repository.update_event(club["id"], slug, active=active)
    return club


def mint_via_route(club_slug, slug, name="Ada"):
    """Drive the real /c/<club>/<event>/download to get a signed token."""
    c = A.app.test_client()
    resp = post(c, f"/c/{club_slug}/{slug}/download", {"cert_name": name})
    loc = resp.headers.get("Location", "")
    m = re.search(r"/preview/([^/?]+)", loc)
    return m.group(1) if m else None, resp


try:
    # Two approved clubs, each with an event at the SAME slug 'gala'.
    make_club_event("gala", "acme", "Acme Gala", (10, 20, 30))
    make_club_event("gala", "beta", "Beta Gala", (200, 100, 50))

    # ── the public pages resolve to the right club ───────────────────────────
    client = A.app.test_client()
    r.check("club A's public event page renders", client.get("/c/acme/gala").status_code == 200)
    r.check("club B's public event page renders", client.get("/c/beta/gala").status_code == 200)
    r.check("an unapproved/unknown club 404s", client.get("/c/ghost/gala").status_code == 404)
    r.check("a wrong event under a real club 404s", client.get("/c/acme/no-such").status_code == 404)

    # ── resolve_public_event is cached: the hot render path must not re-query ─
    A._PUBLIC_EVENT_CACHE.clear()
    calls = {"n": 0}
    real_get_event = A._repository.get_event
    A._repository.get_event = lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), real_get_event(*a, **k))[1]
    try:
        A.resolve_public_event("acme", "gala", require_active=True)
        first = calls["n"]
        A.resolve_public_event("acme", "gala", require_active=False)  # token path
        r.check("a second resolve is served from cache (no extra DB query)", calls["n"] == first, calls["n"])
        A._invalidate_public_event("acme", "gala")
        A.resolve_public_event("acme", "gala", require_active=True)
        r.check("invalidation forces a fresh query", calls["n"] == first + 1, calls["n"])
    finally:
        A._repository.get_event = real_get_event
        A._PUBLIC_EVENT_CACHE.clear()

    # ── THE core check: a token minted for A does not render B's certificate ─
    token_a, resp_a = mint_via_route("acme", "gala")
    r.check("minting under club A succeeds", token_a is not None, resp_a.status_code)
    club_in_a, slug_in_a, _vals = A.read_cert_token(token_a)
    r.check("the token carries club A, signed", club_in_a == "acme" and slug_in_a == "gala",
            (club_in_a, slug_in_a))

    # Resolve the token and render: it must draw A's template, never B's.
    resolved = A._certificate_from_token(token_a)
    r.check("the token resolves to club A's storage prefix", resolved and resolved[3] == "acme", resolved[3])
    img_a = client.get(f"/preview-image/{token_a}")
    r.check("A's token renders A's certificate (200)", img_a.status_code == 200)

    # Now forge: take A's token but try to pass it off as B's by editing the payload.
    # It is signed, so any edit invalidates it - there is no unsigned club to swap.
    forged = token_a[:-3] + ("aaa" if token_a[-3:] != "aaa" else "bbb")
    r.check("a tampered token is rejected outright", A.read_cert_token(forged) is None)

    # And a token minted for B renders B's, proving they never cross.
    token_b, _ = mint_via_route("beta", "gala")
    resolved_b = A._certificate_from_token(token_b)
    r.check("B's token resolves to club B's storage prefix", resolved_b and resolved_b[3] == "beta",
            resolved_b[3])
    # The two tokens produce different renders (different templates) - not crossed.
    a_bytes = client.get(f"/download-file/{token_a}").data
    b_bytes = client.get(f"/download-file/{token_b}").data
    r.check("A's and B's certificates are different artifacts (not crossed)", a_bytes != b_bytes)

    # ── cross-club URL manipulation on the participant flow ──────────────────
    # A participant on B's page cannot mint against A's event by swapping the club
    # segment - the mint resolves (club_from_url, slug) and would just make a B token.
    # The real IDOR risk is the club-less URL; there isn't one anymore.
    r.check("posting to /c/<clubB>/<slugA> mints a clubB token, not a clubA one",
            A.read_cert_token(mint_via_route("beta", "gala")[0])[0] == "beta")

    # ── the guarded 301 ──────────────────────────────────────────────────────
    # A legacy csi-aseb event (local file, via the fixture) redirects; unknown 404s.
    from _fixture import TEST_SLUG
    red = client.get(f"/events/{TEST_SLUG}")
    r.check("a real csi-aseb slug 301s", red.status_code == 301, red.status_code)
    r.check("the 301 points at /c/csi-aseb/",
            "/c/csi-aseb/" in red.headers.get("Location", ""), red.headers.get("Location"))
    unknown = client.get("/events/no-such-event-anywhere")
    r.check("an unknown slug 404s directly, NOT a 301 into a 404",
            unknown.status_code == 404, unknown.status_code)

    # ── legacy club-less token: grace honours it and logs; cutoff rejects it ──
    legacy_tok = A._cert_serializer().dumps({"s": TEST_SLUG, "v": {"name": "Ada Lovelace"}})
    A.LEGACY_TOKEN_GRACE = True
    r.check("a club-less legacy token resolves under grace",
            A._certificate_from_token(legacy_tok) is not None)
    A.LEGACY_TOKEN_GRACE = False
    r.check("with the grace period off, a club-less token is rejected",
            A._certificate_from_token(legacy_tok) is None)
    A.LEGACY_TOKEN_GRACE = True

    # ── a suspended club's tokens and pages all go dark ──────────────────────
    acme = A._repository.get_club_by_slug("acme")
    A._repository.set_club_status(acme["id"], "suspended")
    A._PUBLIC_EVENT_CACHE.clear()  # a status change via the route invalidates this
    r.check("a suspended club's public page 404s", client.get("/c/acme/gala").status_code == 404)
    r.check("a suspended club's already-minted token stops resolving",
            A._certificate_from_token(token_a) is None)
    A._repository.set_club_status(acme["id"], "approved")
    A._PUBLIC_EVENT_CACHE.clear()

    # ── template cache is club-keyed: same slug, same version, different bytes ─
    A._TEMPLATE_IMAGE_CACHE.clear()
    ta = A.get_template_image("gala", {"template_ext": ".png", "template_version": "vX"}, club_slug="acme")
    # force both to share a template_version so only the club key separates them
    tb = A.get_template_image("gala", {"template_ext": ".png", "template_version": "vX"}, club_slug="beta")
    r.check("two clubs' same-slug same-version templates do not collide in cache",
            ta is not None and tb is not None and
            list(ta.getdata())[0] != list(tb.getdata())[0],
            (list(ta.getdata())[0], list(tb.getdata())[0]))
finally:
    teardown_scratch(scratch)

sys.exit(r.finish())
