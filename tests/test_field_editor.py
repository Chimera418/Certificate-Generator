"""
Phase 0.5b backend: the field-save validator (the third cap-enforcement place) and
the /fields route the editor autosaves to.

The validator REJECTS rather than coerces, so a broken or over-long list never
reaches storage; a csv field pointing at a column that isn't in the uploaded file
is refused, not silently rendered blank. A saved list must round-trip through the
renderer with the values it describes.

Run with: python tests/test_field_editor.py
"""
import io
import json
import os
import sys

from PIL import Image

from _fixture import A, Results, admin_client, csrf, post, setup_scratch_event, teardown_scratch

r = Results()
scratch = setup_scratch_event()


def make_event(slug, csv_text=None):
    d = os.path.join(A.EVENTS_DIR, slug)
    os.makedirs(d, exist_ok=True)
    buf = io.BytesIO(); Image.new("RGB", (2000, 1400), (240, 235, 220)).save(buf, format="PNG")
    with open(os.path.join(d, "template.png"), "wb") as f:
        f.write(buf.getvalue())
    if csv_text is not None:
        with open(os.path.join(d, "data.csv"), "w", encoding="utf-8", newline="") as f:
            f.write(csv_text)
    A._EVENT_CSV_CACHE.pop(slug, None); A._PARTICIPANT_DATASET_CACHE.pop(slug, None)
    A.save_event_config(slug, {
        "name": slug, "slug": slug, "active": True, "validation_type": "player_team",
        "custom_fields": [], "custom_dropdown_fields": [],
        "text_x": 1000, "text_y": 700, "font_size": 90,
        "font_color": [50, 34, 24], "font_key": "montserrat_bold",
    })


CSV = "player,team,position\nAda Lovelace,Nimbus,1st\nGrace Hopper,Nimbus,2nd\n"

try:
    slug = "editor"
    make_event(slug, CSV)

    def V(fields):
        return A.validate_fields_payload(slug, fields)

    # ── happy path ───────────────────────────────────────────────────────────
    ok_fields = [
        {"id": "name", "source": "input", "x": 1000, "y": 400, "font_size": 90},
        {"id": "position", "source": "csv", "column": "position", "x": 1000, "y": 700},
        {"id": "date", "source": "static", "value": "20 Aug 2026", "x": 1000, "y": 900},
    ]
    fields, err = V(ok_fields)
    r.check("a valid list is accepted", err is None and len(fields) == 3, err)
    r.check("csv column survives normalization", fields[1]["column"] == "position")

    # ── the cap: reject a longer list (third enforcement place) ──────────────
    six = [{"id": f"f{i}", "source": "static", "value": str(i), "x": 10, "y": 10 * i} for i in range(6)]
    _, err = V(six)
    r.check("more than five fields is rejected on save", err is not None and "most" in err.lower(), err)
    r.check("exactly five is allowed", V(six[:5])[1] is None)

    # ── empty / malformed ────────────────────────────────────────────────────
    r.check("an empty list is rejected", V([])[1] is not None)
    r.check("a non-list is rejected", V("not a list")[1] is not None)
    r.check("a non-dict entry is rejected", V([{"id": "a", "source": "static", "value": "x", "x": 1, "y": 1}, 5])[1] is not None)
    r.check("an unknown source is rejected", V([{"id": "a", "source": "moon", "x": 1, "y": 1}])[1] is not None)

    # ── csv column integrity ─────────────────────────────────────────────────
    _, err = V([{"id": "p", "source": "csv", "column": "salary", "x": 1, "y": 1}])
    r.check("a csv field on a missing column is rejected", err is not None and "salary" in err, err)
    _, err = V([{"id": "p", "source": "csv", "column": "", "x": 1, "y": 1}])
    r.check("a csv field with no column is rejected", err is not None)

    make_event("nocsv")  # no CSV uploaded
    _, err = A.validate_fields_payload("nocsv", [{"id": "p", "source": "csv", "column": "team", "x": 1, "y": 1}])
    r.check("a csv field with no uploaded file is rejected", err is not None and "uploaded" in err.lower(), err)

    # ── depends_on only survives if it names a real field in the same list ───
    fields, _ = V([
        {"id": "team", "source": "csv", "column": "team", "x": 1, "y": 1},
        {"id": "pos", "source": "csv", "column": "position", "x": 1, "y": 2, "depends_on": "team"},
        {"id": "date", "source": "static", "value": "x", "x": 1, "y": 3, "depends_on": "ghost"},
    ])
    r.check("depends_on to a sibling is kept (reserved)", fields[1]["depends_on"] == "team")
    r.check("depends_on to a missing field is dropped", fields[2]["depends_on"] is None)

    # ── the route: editor page renders with injected data ────────────────────
    admin = admin_client()
    page = admin.get(f"/admin/events/{slug}/coordinates")
    body = page.data.decode("utf-8", "replace")
    r.check("editor page renders", page.status_code == 200, page.status_code)
    r.check("editor injects the fields JSON", '"fields":' in body and "editor-data" in body)
    r.check("editor injects the columns", "position" in body and "team" in body)
    # A CSV cell carrying a </script> breakout must be unicode-escaped inside the
    # <script id="editor-data"> JSON block, never emitted as raw markup. The event
    # carries the payload in a real cell value so it flows through sampleRows.
    breakout = "</script><script>alert(1)</script>"
    make_event("xss", "player,team,position\n" + breakout + ",Nimbus,1st\n")
    xss_body = admin.get("/admin/events/xss/coordinates").data.decode("utf-8", "replace")
    r.check("the raw </script> breakout payload never appears verbatim",
            breakout not in xss_body)
    r.check("the breakout payload is present but unicode-escaped",
            "\\u003c/script\\u003e\\u003cscript\\u003e" in xss_body)

    # ── the route: save round-trips ──────────────────────────────────────────
    resp = admin.post(f"/admin/events/{slug}/fields",
                      data=json.dumps({"fields": ok_fields}),
                      content_type="application/json",
                      headers={"X-CSRF-Token": csrf(admin), "X-Requested-With": "XMLHttpRequest"})
    r.check("saving fields returns ok", resp.status_code == 200 and resp.get_json().get("ok"), resp.status_code)
    stored = A.load_event(slug).get("fields")
    r.check("fields are persisted to the config", stored and len(stored) == 3, stored)
    r.check("persisted csv field keeps its column", stored[1]["column"] == "position")

    # A saved multi-field config renders with the described values.
    A._RENDERED_CERT_CACHE.clear()
    matched = A.load_participant_dataset(slug).rows_matching({"player": "ada lovelace", "team": "nimbus"})
    nf = A.normalize_fields(A.load_event(slug))
    values, verr = A.resolve_field_values(nf, matched, {"name": "Ada Lovelace"})
    r.check("saved config resolves the csv field from the row", verr is None and values["position"] == "1st", (values, verr))
    r.check("saved static field resolves", values["date"] == "20 Aug 2026")

    # ── the route rejects a bad save with a message, changing nothing ────────
    before = A.load_event(slug).get("fields")
    bad = admin.post(f"/admin/events/{slug}/fields",
                     data=json.dumps({"fields": [{"id": "p", "source": "csv", "column": "nope", "x": 1, "y": 1}]}),
                     content_type="application/json",
                     headers={"X-CSRF-Token": csrf(admin), "X-Requested-With": "XMLHttpRequest"})
    r.check("a bad save is a 400 with an error", bad.status_code == 400 and bad.get_json().get("error"), bad.status_code)
    r.check("a rejected save does not change stored fields", A.load_event(slug).get("fields") == before)

    # ── a save without CSRF is refused (the boundary still holds) ────────────
    nocsrf = admin.post(f"/admin/events/{slug}/fields",
                        data=json.dumps({"fields": ok_fields}), content_type="application/json",
                        headers={"X-Requested-With": "XMLHttpRequest"})
    r.check("a save without a CSRF token is refused", nocsrf.status_code == 400, nocsrf.status_code)
finally:
    teardown_scratch(scratch)

sys.exit(r.finish())
