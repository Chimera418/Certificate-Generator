"""
CSRF protection.

Enforcement is fail-closed for every unsafe method, so the important test is not
"the routes I remembered are protected" but "every registered POST route is
protected" - that is what catches a route added later without a second thought.

Run with: python tests/test_csrf.py
"""
import io
import re
import sys

from _fixture import (
    TEST_EMAIL,
    TEST_NAME,
    TEST_SLUG,
    A,
    Results,
    admin_client,
    csrf,
    post,
    setup_scratch_event,
    teardown_scratch,
)

DOWNLOAD_URL = f"/events/{TEST_SLUG}/download"
VALID_FORM = {"registration_name": TEST_EMAIL, "cert_name": TEST_NAME}

r = Results()
scratch = setup_scratch_event()


def sample_url(rule) -> str:
    """A concrete URL for a rule, filling any converters with a plausible value."""
    values = {}
    for argument in rule.arguments:
        values[argument] = TEST_SLUG if argument == "slug" else "x" * 32
    return rule.build(values, append_unknown=False)[1]


try:
    client = A.app.test_client()

    # ── every unsafe route is covered, not just the ones we thought of ───────
    unsafe_rules = [
        rule for rule in A.app.url_map.iter_rules()
        if rule.methods & set(A.CSRF_UNSAFE_METHODS) and rule.endpoint not in A._CSRF_EXEMPT_ENDPOINTS
    ]
    r.check("there are unsafe routes to protect", len(unsafe_rules) > 5, len(unsafe_rules))

    unprotected = []
    for rule in unsafe_rules:
        method = "POST" if "POST" in rule.methods else sorted(rule.methods & set(A.CSRF_UNSAFE_METHODS))[0]
        fresh = A.app.test_client()
        response = fresh.open(sample_url(rule), method=method, data={})
        if response.status_code != 400:
            unprotected.append((rule.endpoint, response.status_code))
    r.check("every unsafe route rejects a request with no token", not unprotected, unprotected)

    # ── the check itself ────────────────────────────────────────────────────
    no_token = client.post(DOWNLOAD_URL, data=VALID_FORM)
    r.check("missing token is rejected", no_token.status_code == 400, no_token.status_code)
    r.check("rejection explains itself", b"Request Expired" in no_token.data)

    csrf(client)  # seed a token into this client's session
    wrong = client.post(DOWNLOAD_URL, data=dict(VALID_FORM, csrf_token="not-the-token"))
    r.check("wrong token is rejected", wrong.status_code == 400, wrong.status_code)

    empty = client.post(DOWNLOAD_URL, data=dict(VALID_FORM, csrf_token=""))
    r.check("empty token is rejected", empty.status_code == 400, empty.status_code)

    good = post(client, DOWNLOAD_URL, VALID_FORM)
    r.check("valid token is accepted", good.status_code == 302, good.status_code)

    # ── a token is bound to one session ─────────────────────────────────────
    other = A.app.test_client()
    stolen = other.post(DOWNLOAD_URL, data=dict(VALID_FORM, csrf_token=csrf(client)))
    r.check("a token from another session is rejected", stolen.status_code == 400, stolen.status_code)

    # ── header form, for fetch() callers that do not use FormData ────────────
    header_client = A.app.test_client()
    token = csrf(header_client)
    via_header = header_client.post(DOWNLOAD_URL, data=VALID_FORM,
                                    headers={A.CSRF_HEADER_NAME: token})
    r.check("token in the X-CSRF-Token header is accepted", via_header.status_code == 302,
            via_header.status_code)

    # ── XHR callers get JSON, not an HTML page ──────────────────────────────
    xhr = A.app.test_client()
    csrf(xhr)
    xhr_response = xhr.post(f"/admin/events/{TEST_SLUG}/config", data={"name": "x"},
                            headers={"X-Requested-With": "XMLHttpRequest"})
    r.check("XHR rejection is JSON", xhr_response.status_code == 400
            and xhr_response.is_json and xhr_response.get_json().get("ok") is False,
            (xhr_response.status_code, xhr_response.headers.get("Content-Type")))

    # The admin autosave posts new FormData(form), which picks up the hidden
    # field automatically - so it must keep working with no JS changes.
    autosave_admin = admin_client()
    autosave = post(autosave_admin, f"/admin/events/{TEST_SLUG}/config",
                    {"name": "Test Event", "text_x": "820"},
                    headers={"X-Requested-With": "XMLHttpRequest"})
    r.check("admin autosave still works with the hidden field",
            autosave.status_code == 200 and autosave.get_json().get("ok") is True,
            (autosave.status_code, autosave.data[:120]))
    r.check("admin autosave actually saved", A.load_event(TEST_SLUG).get("text_x") == 820,
            A.load_event(TEST_SLUG).get("text_x"))

    # ── safe methods are untouched ──────────────────────────────────────────
    r.check("GET is not blocked", client.get(f"/events/{TEST_SLUG}", follow_redirects=True).status_code == 200)
    r.check("GET /healthz is not blocked", client.get("/healthz").status_code == 200)

    # ── session handling around login ───────────────────────────────────────
    fresh = A.app.test_client()
    before_login = csrf(fresh)
    login = post(fresh, "/admin/login", {"password": "test-admin-password"})
    r.check("login succeeds with a valid token", login.status_code == 302, login.status_code)
    with fresh.session_transaction() as sess:
        after_login = sess.get(A.CSRF_FIELD_NAME)
        logged_in = sess.get("admin_logged_in")
    r.check("login logs the admin in", logged_in is True)
    r.check("login rotates the CSRF token", after_login and after_login != before_login)

    bad_login = A.app.test_client()
    csrf(bad_login)
    r.check("login without a token is rejected",
            bad_login.post("/admin/login", data={"password": "test-admin-password"}).status_code == 400)

    admin = admin_client()
    r.check("admin session works after login", admin.get("/admin").status_code == 200)
    post(admin, "/admin/logout", {})
    with admin.session_transaction() as sess:
        r.check("logout clears the session", not sess.get("admin_logged_in") and not sess.get(A.CSRF_FIELD_NAME),
                dict(sess))

    # ── an admin action still works end to end with a token ─────────────────
    admin = admin_client()
    before_toggle = A.load_event(TEST_SLUG).get("active")
    toggled = post(admin, f"/admin/events/{TEST_SLUG}/toggle", {})
    r.check("admin toggle works with a token", toggled.status_code == 302, toggled.status_code)
    r.check("admin toggle actually changed state",
            A.load_event(TEST_SLUG).get("active") != before_toggle)

    forged = A.app.test_client()
    csrf(forged)
    r.check("admin toggle is refused without a token",
            forged.post(f"/admin/events/{TEST_SLUG}/toggle", data={}).status_code == 400)

    post(admin, f"/admin/events/{TEST_SLUG}/toggle", {})  # restore the original state
    r.check("toggling back restores the original state",
            A.load_event(TEST_SLUG).get("active") == before_toggle)

    # ── rendered forms actually carry the field ─────────────────────────────
    admin = admin_client()
    pages = {
        "event page": client.get(f"/events/{TEST_SLUG}", follow_redirects=True).data,
        "admin login": A.app.test_client().get("/admin/login").data,
        "admin dashboard": admin.get("/admin").data,
        "admin event editor": admin.get(f"/admin/events/{TEST_SLUG}").data,
        "admin logs": admin.get("/admin/logs").data,
        "email form": admin.get(f"/admin/events/{TEST_SLUG}/send_emails").data,
    }
    for label, html in pages.items():
        text = html.decode("utf-8", errors="replace")
        forms = len(re.findall(r'<form\b[^>]*method\s*=\s*["\']post["\']', text, re.IGNORECASE))
        fields = text.count('name="csrf_token"')
        r.check(f"{label}: every POST form carries a token",
                forms > 0 and fields >= forms, (forms, fields))

    # The placement editor saves over fetch, not a <form>, so it carries its token
    # for the X-CSRF-Token header instead of a hidden field. The token must be
    # present in the page, and the /fields endpoint must reject a save without it.
    editor = admin.get(f"/admin/events/{TEST_SLUG}/coordinates").data.decode("utf-8", "replace")
    token = csrf(admin)
    r.check("coordinate editor embeds the CSRF token for its fetch save", token in editor, token[:8])
    unprotected = admin.post(
        f"/admin/events/{TEST_SLUG}/fields", data='{"fields": []}',
        content_type="application/json", headers={"X-Requested-With": "XMLHttpRequest"})
    r.check("the field-save endpoint refuses a POST with no token", unprotected.status_code == 400,
            unprotected.status_code)
finally:
    teardown_scratch(scratch)

sys.exit(r.finish())
