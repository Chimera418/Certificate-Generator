"""
Roster disclosure guard.

Marking a person-level CSV column as a "Show as a dropdown" field publishes every
value of that column on the unauthenticated event page. For a team column that is a
short intended list; for a name, email, or roll-number column it is the whole
roster. The admin is still allowed to do it (a small public list is a legitimate
choice), but the consequence must be stated at the point of the choice.

This suite pins two things: the heuristic that decides when to warn, and that the
warning is actually rendered into the admin form next to the offending column.

Run with: python tests/test_roster_disclosure.py
"""
import os
import sys

from _fixture import (
    A,
    Results,
    admin_client,
    setup_scratch_event,
    teardown_scratch,
)

r = Results()
scratch = setup_scratch_event()

try:
    # ── the heuristic ────────────────────────────────────────────────────────
    person = ["name", "Full Name", "email", "E-Mail", "player", "roll_no",
              "Roll No", "reg number", "student id", "phone", "mobile", "USN", "team_id"]
    for col in person:
        r.check(f"person-level: {col!r}", A.looks_person_level(col), col)

    non_person = ["team", "position", "points", "date", "rank", "score",
                  "category", "house", "department", "event"]
    for col in non_person:
        r.check(f"not person-level: {col!r}", not A.looks_person_level(col), col)

    # Whole-word matching, so a hint substring inside another word does not fire.
    r.check("'video' does not trip on 'id'", not A.looks_person_level("video"))
    r.check("'teamwork' does not trip on 'team' hint list", not A.looks_person_level("teamwork"))

    # ── the admin form renders the warning ───────────────────────────────────
    slug = "disclosure-event"
    event_dir = os.path.join(A.EVENTS_DIR, slug)
    os.makedirs(event_dir, exist_ok=True)
    from PIL import Image
    import io as _io
    buf = _io.BytesIO(); Image.new("RGB", (1600, 1100), (240, 235, 220)).save(buf, format="PNG")
    with open(os.path.join(event_dir, "template.png"), "wb") as f:
        f.write(buf.getvalue())
    with open(os.path.join(event_dir, "data.csv"), "w", encoding="utf-8", newline="") as f:
        f.write("player,team,points\nAda Lovelace,Nimbus,07\nGrace Hopper,Nimbus,05\n")

    # A person-level column marked as a dropdown -> live (is-active) warning.
    A.save_event_config(slug, {
        "name": "Disclosure", "slug": slug, "active": True, "validation_type": "custom",
        "custom_fields": ["player", "team"], "custom_dropdown_fields": ["player"],
        "text_x": 800, "text_y": 550, "font_size": 64,
        "font_color": [50, 34, 24], "font_key": "montserrat_bold",
    })
    admin = admin_client()
    body = admin.get(f"/admin/events/{slug}").data.decode("utf-8", "replace")

    r.check("the general consequence line is shown", "publishes your whole roster" in body)
    r.check("the person column carries a standing warning",
            'class="roster-warning is-active"' in body)
    r.check("the warning names the leaking column",
            "Publishes every <strong>player</strong>" in body)
    r.check("the toggle no longer reads the innocuous 'Suggest values'",
            "Suggest values" not in body)
    r.check("the toggle now says what it does", "Show as a dropdown" in body)
    r.check("person-level columns are flagged for the client script",
            'data-person-level="true"' in body)

    # A non-person column marked as a dropdown -> no roster warning at all.
    A.save_event_config(slug, {
        "name": "Disclosure", "slug": slug, "active": True, "validation_type": "custom",
        "custom_fields": ["player", "team"], "custom_dropdown_fields": ["team"],
        "text_x": 800, "text_y": 550, "font_size": 64,
        "font_color": [50, 34, 24], "font_key": "montserrat_bold",
    })
    body = admin.get(f"/admin/events/{slug}").data.decode("utf-8", "replace")
    r.check("a team dropdown raises no roster warning as active",
            'class="roster-warning is-active"' not in body)
    # 'player' is still a listed column, so its (dimmed) warning is present but idle.
    r.check("the idle person-column warning is present but not active",
            'class="roster-warning"' in body and 'roster-warning is-active' not in body)

    # ── the leak itself still requires a deliberate opt-in ───────────────────
    # No dropdowns at all -> the public page names no participant.
    A.save_event_config(slug, {
        "name": "Disclosure", "slug": slug, "active": True, "validation_type": "custom",
        "custom_fields": ["player", "team"], "custom_dropdown_fields": [],
        "text_x": 800, "text_y": 550, "font_size": 64,
        "font_color": [50, 34, 24], "font_key": "montserrat_bold",
    })
    public = A.app.test_client().get(f"/events/{slug}").data.decode("utf-8", "replace")
    r.check("with no dropdown, the roster is not on the public page",
            "Ada Lovelace" not in public and "Grace Hopper" not in public)
finally:
    teardown_scratch(scratch)

sys.exit(r.finish())
