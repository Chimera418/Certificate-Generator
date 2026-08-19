"""
Shared test setup.

Everything runs against a throwaway events directory with a generated template, so
the tests need no network, no Supabase, no KV, and no real event data on disk.
Import this before importing `app`.
"""
import io
import json
import os
import shutil
import sys
import tempfile

# Must be set before app imports: load_dotenv() will not override values that are
# already present, which is how we keep the real .env out of the tests.
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["ADMIN_PASSWORD"] = "test-admin-password"
os.environ["KV_REST_API_URL"] = ""
os.environ["KV_REST_API_TOKEN"] = ""
os.environ["SUPABASE_URL"] = ""
os.environ["SUPABASE_SERVICE_KEY"] = ""

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as A  # noqa: E402
from PIL import Image  # noqa: E402

TEST_SLUG = "test-event"
TEST_EMAIL = "ada@example.com"
TEST_NAME = "Ada Lovelace"
TEMPLATE_SIZE = (1600, 1100)


class Results:
    """Minimal assertion recorder so the suites run under plain `python`."""

    def __init__(self):
        self.failures = []

    def check(self, label, condition, detail=""):
        if not condition:
            self.failures.append(label)
        suffix = f" -> {detail}" if detail and not condition else ""
        print(f"[{'PASS' if condition else 'FAIL'}] {label}{suffix}")

    def finish(self):
        print()
        if self.failures:
            print(f"{len(self.failures)} FAILED: {self.failures}")
            return 1
        print("all checks passed")
        return 0


def make_xlsx_bytes(rows=None) -> bytes:
    """A minimal .xlsx workbook, for exercising the spreadsheet upload path."""
    from openpyxl import Workbook

    if rows is None:
        rows = [["name", "email"], [TEST_NAME, TEST_EMAIL], ["Grace Hopper", "grace@example.com"]]
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    buf = io.BytesIO()
    workbook.save(buf)
    return buf.getvalue()


def make_template_bytes(size=TEMPLATE_SIZE, color=(240, 235, 220), mode="RGB") -> bytes:
    """A synthetic certificate template. mode="RGBA" produces one with real alpha."""
    buf = io.BytesIO()
    fill = color + (255,) if mode == "RGBA" and len(color) == 3 else color
    Image.new(mode, size, fill).save(buf, format="PNG")
    return buf.getvalue()


def setup_scratch_event(validation_type="email") -> str:
    """
    Point every writable path at a temp dir containing one ready-to-use event.

    Event state (active/deleted flags) is redirected too: toggling an event in a
    test would otherwise write into the project's real event_states.json and leak
    into the next run.
    """
    scratch = tempfile.mkdtemp(prefix="cert-tests-")
    A.EVENTS_DIR = os.path.join(scratch, "events")
    A.GENERATED_DIR = os.path.join(scratch, "generated")
    A.EVENT_STATE_FILE = os.path.join(A.GENERATED_DIR, "event_states.json")
    A.LOG_DIR = os.path.join(scratch, "logs")
    A.LOG_FILE = os.path.join(A.LOG_DIR, "system.log")
    os.makedirs(A.EVENTS_DIR, exist_ok=True)
    os.makedirs(A.GENERATED_DIR, exist_ok=True)

    A._EVENT_CONFIG_CACHE.clear()
    A._TEMPLATE_IMAGE_CACHE.clear()
    A._RENDERED_CERT_CACHE.clear()
    A._EVENT_STATE_CACHE = None
    A._EVENT_STATE_CACHE_AT = 0.0

    event_dir = os.path.join(A.EVENTS_DIR, TEST_SLUG)
    os.makedirs(event_dir, exist_ok=True)

    with open(os.path.join(event_dir, "template.png"), "wb") as f:
        f.write(make_template_bytes())

    with open(os.path.join(event_dir, "data.csv"), "w", encoding="utf-8", newline="") as f:
        f.write(f"name,email\n{TEST_NAME},{TEST_EMAIL}\nGrace Hopper,grace@example.com\n")

    config = {
        "name": "Test Event",
        "slug": TEST_SLUG,
        "active": True,
        "validation_type": validation_type,
        "custom_fields": [],
        "custom_dropdown_fields": [],
        "text_x": 800,
        "text_y": 550,
        "font_size": 64,
        "font_color": [50, 34, 24],
        "font_key": "montserrat_bold",
    }
    with open(os.path.join(event_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    return scratch


def teardown_scratch(scratch: str) -> None:
    shutil.rmtree(scratch, ignore_errors=True)


def csrf(client) -> str:
    """This client's CSRF token, seeding one into the session if it has none."""
    with client.session_transaction() as sess:
        token = sess.get(A.CSRF_FIELD_NAME)
        if not token:
            token = "fixture-csrf-token"
            sess[A.CSRF_FIELD_NAME] = token
    return token


def post(client, url, data=None, **kwargs):
    """POST with the session's CSRF token attached, the way a real form does."""
    payload = dict(data or {})
    payload[A.CSRF_FIELD_NAME] = csrf(client)
    return client.post(url, data=payload, **kwargs)


def admin_client():
    client = A.app.test_client()
    # Logging in clears the session and mints a fresh token, so read it afterwards.
    post(client, "/admin/login", {"password": os.environ["ADMIN_PASSWORD"]})
    return client
