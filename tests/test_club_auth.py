"""
Phase 1: club registration, login, the pending/approved gate, superadmin
approvals, and cross-club isolation.

The checks that matter most:
  * a pending club can log in and use its dashboard, but its public page 404s
    (never 403 - a probe must not tell "pending" from "no such club") until a
    superadmin approves it;
  * when Postgres is configured but unreachable, the app fails closed with a 503
    and NEVER falls back to an empty in-memory store;
  * three clubs, and none can read another's event - isolation is structural, and
    a miss is 404, not 403.

Run with: python tests/test_club_auth.py
"""
import sys

from _fixture import A, Results, admin_client, csrf, post, setup_scratch_event, teardown_scratch

import db  # the repository module, for the Postgres-down stub

r = Results()
scratch = setup_scratch_event()


def client():
    return A.app.test_client()


def register(c, slug, name="Club", password="password1"):
    return post(c, "/register", {"name": name, "slug": slug, "password": password})


def approve(club_slug):
    """Approve a club straight through the repository (superadmin action tested separately)."""
    club = A._repository.get_club_by_slug(club_slug)
    A._repository.set_club_status(club["id"], "approved")


try:
    # ── registration creates a pending club; password is hashed ──────────────
    c = client()
    resp = register(c, "acme-club", name="Acme Robotics", password="hunter2!x")
    r.check("registration redirects into the dashboard", resp.status_code in (302, 303), resp.status_code)
    club = A._repository.get_club_by_slug("acme-club")
    r.check("the club exists and is pending", club and club["status"] == "pending", club and club["status"])
    r.check("the password is stored hashed, never in plaintext",
            "hunter2!x" not in (club["password_hash"] if club else ""))
    r.check("the hash verifies", A.check_password_hash(club["password_hash"], "hunter2!x"))

    # ── the pending gate: dashboard works, public page 404s ──────────────────
    r.check("a pending club reaches its dashboard", c.get("/dashboard").status_code == 200)
    r.check("a pending club's public page is 404", c.get("/c/acme-club").status_code == 404)
    r.check("an unknown club's public page is also 404 (indistinguishable)",
            c.get("/c/no-such-club").status_code == 404)

    approve("acme-club")
    r.check("once approved, the public page is 200", c.get("/c/acme-club").status_code == 200)

    # ── login ────────────────────────────────────────────────────────────────
    fresh = client()
    bad = post(fresh, "/login", {"slug": "acme-club", "password": "wrong"})
    r.check("a wrong password is rejected", bad.status_code == 400, bad.status_code)
    r.check("a rejected login sets no session", fresh.get("/dashboard").status_code == 302)
    good = post(fresh, "/login", {"slug": "acme-club", "password": "hunter2!x"})
    r.check("a correct login redirects to the dashboard", good.status_code in (302, 303))
    r.check("a logged-in club reaches its dashboard", fresh.get("/dashboard").status_code == 200)
    unknown = post(client(), "/login", {"slug": "ghost-club", "password": "whatever"})
    r.check("an unknown club login is refused the same way", unknown.status_code == 400)

    # ── logout ───────────────────────────────────────────────────────────────
    post(fresh, "/logout", {})
    r.check("after logout the dashboard is gated again", fresh.get("/dashboard").status_code == 302)

    # ── autocomplete: nothing under 3 chars ──────────────────────────────────
    register(client(), "acme-two")
    register(client(), "beta-club")
    approve("acme-two")
    empty = client().get("/clubs/autocomplete?q=ac").get_json()
    r.check("autocomplete returns nothing under three characters", empty["slugs"] == [], empty)
    hits = client().get("/clubs/autocomplete?q=acm").get_json()
    r.check("autocomplete returns matches at three characters",
            set(hits["slugs"]) == {"acme-club", "acme-two"}, hits)
    r.check("autocomplete does not leak non-matching clubs", "beta-club" not in hits["slugs"])

    # ── cross-club isolation, with three clubs ───────────────────────────────
    a = A._repository.get_club_by_slug("acme-club")
    b = A._repository.get_club_by_slug("beta-club")
    third = A._repository.create_club("gamma-club", "Gamma", A.generate_password_hash("password1"))
    A._repository.create_event(a["id"], "invitational", "Acme Invitational", {"fields": []})
    A._repository.create_event(b["id"], "beta-open", "Beta Open", {"fields": []})

    r.check("a club sees its own event", A._repository.get_event(a["id"], "invitational") is not None)
    r.check("club B cannot read club A's event by slug",
            A._repository.get_event(b["id"], "invitational") is None)
    r.check("club B's event list excludes club A's events",
            [e["slug"] for e in A._repository.list_events(b["id"])] == ["beta-open"])
    r.check("a third club sees neither", A._repository.list_events(third["id"]) == [])

    # Same slug can exist under two clubs without collision.
    A._repository.create_event(b["id"], "invitational", "Beta Invitational", {"fields": []})
    r.check("the same slug is a different event under a different club",
            A._repository.get_event(a["id"], "invitational")["name"] == "Acme Invitational" and
            A._repository.get_event(b["id"], "invitational")["name"] == "Beta Invitational")

    # find_event_globally is safe only while one approved club claims a slug.
    # Approve beta so "beta-open" is unique-among-approved and "invitational" is
    # now claimed by two approved clubs (acme + beta).
    A._repository.set_club_status(b["id"], "approved")
    r.check("a slug unique among approved clubs resolves globally",
            A._repository.find_event_globally("beta-open") is not None)
    r.check("a slug claimed by two approved clubs does NOT resolve globally (Phase 3 territory)",
            A._repository.find_event_globally("invitational") is None)

    # ── event configs read from Postgres (repository), KV demoted ────────────
    # An active event under an approved club, stored only in the repository (no
    # KV, no file), must resolve through load_event(slug) - proving the read path
    # prefers the store. csi-aseb's file-backed events are covered byte-identically
    # by the other suites; here we prove the new source is consulted.
    A._repository.update_event(a["id"], "invitational",
                               config={"validation_type": "none", "fields": [
                                   {"id": "name", "source": "input", "x": 100, "y": 100}]},
                               active=True)
    A._EVENT_CONFIG_CACHE.pop("invitational", None)
    # 'invitational' is claimed by two approved clubs now, so it is intentionally
    # ambiguous globally. Use a slug unique to one approved club instead.
    A._repository.update_event(b["id"], "beta-open",
                               config={"validation_type": "none", "fields": [
                                   {"id": "name", "source": "input", "x": 50, "y": 60}]},
                               active=True)
    A._EVENT_CONFIG_CACHE.pop("beta-open", None)
    loaded = A.load_event("beta-open")
    r.check("load_event resolves a repository-stored event", loaded is not None)
    r.check("the loaded config comes from the store", loaded and loaded.get("fields") and
            loaded["fields"][0]["y"] == 60, loaded and loaded.get("fields"))
    r.check("the store's active flag carries through", loaded and loaded.get("active") is True)

    # An inactive repo event still loads, but reports active=False.
    A._repository.update_event(b["id"], "beta-open", active=False)
    A._EVENT_CONFIG_CACHE.pop("beta-open", None)
    r.check("an inactive repo event reports active=False", A.load_event("beta-open").get("active") is False)
    A._repository.update_event(b["id"], "beta-open", active=True)
    A._EVENT_CONFIG_CACHE.pop("beta-open", None)

    # ── superadmin routes, and the two sessions never cross ──────────────────
    club_session = client()
    post(club_session, "/login", {"slug": "acme-club", "password": "hunter2!x"})
    r.check("a club session cannot reach the superadmin club list",
            club_session.get("/admin/clubs").status_code == 302)  # bounced to admin login

    su = admin_client()
    r.check("the superadmin can list clubs", su.get("/admin/clubs").status_code == 200)
    r.check("a superadmin session cannot reach a club dashboard",
            su.get("/dashboard").status_code == 302)  # bounced to club login

    pending = A._repository.create_club("delta-club", "Delta", A.generate_password_hash("password1"))
    post(su, f"/admin/clubs/{pending['id']}/status", {"status": "approved"})
    r.check("the superadmin can approve a club",
            A._repository.get_club_by_id(pending["id"])["status"] == "approved")
    post(su, f"/admin/clubs/{pending['id']}/status", {"status": "suspended"})
    r.check("the superadmin can suspend a club",
            A._repository.get_club_by_id(pending["id"])["status"] == "suspended")
    post(su, f"/admin/clubs/{pending['id']}/reset-password", {"password": "brandnewpw"})
    r.check("the superadmin can reset a club password",
            A.check_password_hash(A._repository.get_club_by_id(pending["id"])["password_hash"], "brandnewpw"))

    # A suspended club cannot log in.
    suspended_login = post(client(), "/login", {"slug": "delta-club", "password": "brandnewpw"})
    r.check("a suspended club cannot log in", suspended_login.status_code == 403, suspended_login.status_code)

    # ── Postgres configured but unreachable -> 503, never a silent fallback ───
    class DownRepository(db.Repository):
        """Every call behaves as if Postgres is unreachable."""
        backend_name = "down"
        def __getattribute__(self, name):
            if name in ("backend_name",):
                return object.__getattribute__(self, name)
            def boom(*a, **k):
                raise db.DatabaseUnavailable("connection refused")
            return boom

    saved = A._repository
    A._repository = DownRepository()
    try:
        down = client()
        # A route that touches the store must 503, not 500 and not an empty result.
        resp = down.get("/clubs/autocomplete?q=acme")
        r.check("an unreachable Postgres yields 503 on a store-backed route",
                resp.status_code == 503, resp.status_code)
        r.check("the 503 carries Retry-After", resp.headers.get("Retry-After") == "10")
        # Login must not authenticate against nothing - it must 503, not 'incorrect'.
        login_resp = post(down, "/login", {"slug": "acme-club", "password": "hunter2!x"})
        r.check("login fails closed with 503 when the store is down, not a false negative",
                login_resp.status_code == 503, login_resp.status_code)
        with down.session_transaction() as sess:
            r.check("the store-down login never establishes a session", "club_id" not in sess)
    finally:
        A._repository = saved

    r.check("after recovery the store works again", client().get("/clubs/autocomplete?q=acm").status_code == 200)
finally:
    teardown_scratch(scratch)

sys.exit(r.finish())
