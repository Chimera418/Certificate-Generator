"""
Phase 0.5a render loop: fields schema, value resolution, multi-field rendering,
the multi-value token, and the security boundary that gives the feature its point.

The load-bearing checks:
  * a participant can set an `input` field and NOTHING else — position/points must
    come from the CSV or a static value, never from the form;
  * a legacy config (no `fields`) renders byte-identically to the old single-name
    path, until it grows a `fields` list;
  * a coarse match that leaves a csv field ambiguous is refused, not guessed.

Run with: python tests/test_multi_field.py
"""
import io
import os
import sys

from PIL import Image

from _fixture import (
    A,
    Results,
    post,
    setup_scratch_event,
    teardown_scratch,
)

r = Results()
scratch = setup_scratch_event()


def make_event(slug, fields=None, csv_text=None, validation_type="none", **extra):
    d = os.path.join(A.EVENTS_DIR, slug)
    os.makedirs(d, exist_ok=True)
    buf = io.BytesIO(); Image.new("RGB", (2000, 1400), (240, 235, 220)).save(buf, format="PNG")
    with open(os.path.join(d, "template.png"), "wb") as f:
        f.write(buf.getvalue())
    if csv_text is not None:
        with open(os.path.join(d, "data.csv"), "w", encoding="utf-8", newline="") as f:
            f.write(csv_text)
    cfg = dict({
        "name": slug, "slug": slug, "active": True, "validation_type": validation_type,
        "custom_fields": [], "custom_dropdown_fields": [],
        "text_x": 1000, "text_y": 700, "font_size": 90,
        "font_color": [50, 34, 24], "font_key": "montserrat_bold",
    }, **extra)
    if fields is not None:
        cfg["fields"] = fields
    A._EVENT_CSV_CACHE.pop(slug, None)
    A._PARTICIPANT_DATASET_CACHE.pop(slug, None)
    A.save_event_config(slug, cfg)
    return A.load_event(slug)


class Form(dict):
    pass


try:
    # ── normalize_fields: legacy synthesis ───────────────────────────────────
    legacy = make_event("legacy")
    fields = A.normalize_fields(legacy)
    r.check("legacy config yields exactly one field", len(fields) == 1, len(fields))
    f0 = fields[0]
    r.check("synthesized field is the input name", f0["id"] == "name" and f0["source"] == "input")
    r.check("synthesized field takes legacy coords", (f0["x"], f0["y"]) == (1000, 700), (f0["x"], f0["y"]))
    r.check("synthesized field has no max_width (byte-identical path)", f0["max_width"] is None)
    r.check("synthesized field centers", f0["align"] == "center")

    # ── normalize_fields: cap, sources, reserved depends_on ──────────────────
    many = [{"id": f"f{i}", "source": "static", "value": str(i), "x": 10 * i, "y": 10} for i in range(8)]
    capped = A.normalize_fields({"fields": many})
    r.check("more than five fields is capped to five", len(capped) == A.MAX_FIELDS, len(capped))

    weird = A.normalize_fields({"fields": [
        {"id": "a", "source": "banana", "x": 1, "y": 2},                 # bad source -> input
        {"id": "b", "source": "csv", "column": "Team", "align": "diagonal", "overflow": "explode"},
        {"id": "c", "source": "static", "value": "x", "depends_on": "a"},
        {"id": "d", "source": "csv", "column": "team", "depends_on": "ghost"},
    ]})
    r.check("unknown source falls back to input", weird[0]["source"] == "input")
    r.check("csv column is normalized", weird[1]["column"] == "team", weird[1]["column"])
    r.check("bad align falls back to center", weird[1]["align"] == "center")
    r.check("bad overflow falls back to shrink", weird[1]["overflow"] == "shrink")
    r.check("depends_on to a real field is kept (reserved, inert)", weird[2]["depends_on"] == "a")
    r.check("depends_on to a missing field is dropped", weird[3]["depends_on"] is None)

    # ── resolve_field_values: the security boundary ──────────────────────────
    csv = ("player,team,position,points\n"
           "Ada Lovelace,Nimbus,1st,07\n"
           "Grace Hopper,Nimbus,2nd,05\n")
    fields = [
        {"id": "name", "source": "input", "x": 1000, "y": 400},
        {"id": "position", "source": "csv", "column": "position", "x": 1000, "y": 700},
        {"id": "points", "source": "csv", "column": "points", "x": 1000, "y": 900},
        {"id": "date", "source": "static", "value": "20 August 2026", "x": 1000, "y": 1100},
    ]
    ev = make_event("secure", fields=fields, csv_text=csv, validation_type="player_team")
    nf = A.normalize_fields(ev)
    # Ada validated as (player=Ada, team=Nimbus): one matching row.
    matched = A.load_participant_dataset("secure").rows_matching({"player": "ada lovelace", "team": "nimbus"})

    # A participant TRIES to set position and points via the form. They must not win:
    # resolve only ever reads `input` fields from the caller-supplied inputs, and
    # position/points are csv fields, so the form values below are simply ignored.
    values, err = A.resolve_field_values(nf, matched, {"name": "Ada Lovelace",
                                                       "position": "1st", "points": "99"})
    r.check("resolve succeeds", err is None, err)
    r.check("input name is the participant's", values["name"] == "Ada Lovelace")
    r.check("csv position comes from the ROW, not the form", values["position"] == "1st", values["position"])
    r.check("csv points come from the ROW verbatim (07, not 99)", values["points"] == "07", values["points"])
    r.check("static date is the club's fixed string", values["date"] == "20 August 2026")

    # Grace's row must give her own values, proving csv is row-bound.
    grace_row = A.load_participant_dataset("secure").rows_matching({"player": "grace hopper", "team": "nimbus"})
    gvalues, _ = A.resolve_field_values(nf, grace_row, {"name": "Grace Hopper"})
    r.check("a different participant gets their own csv value", gvalues["points"] == "05", gvalues["points"])

    # ── ambiguity guard ──────────────────────────────────────────────────────
    # Validate by team only -> both members match; position differs -> refuse.
    team_only = A.load_participant_dataset("secure").rows_matching({"team": "nimbus"})
    _, amb_err = A.resolve_field_values(nf, team_only, {"name": "Somebody"})
    r.check("ambiguous csv field is refused, not guessed", amb_err is not None, amb_err)
    r.check("the refusal names the offending field", "position" in amb_err.lower() or "Position" in amb_err)

    # points is also ambiguous; position is checked first — either way it must refuse.
    consistent_fields = A.normalize_fields({"fields": [
        {"id": "team", "source": "csv", "column": "team", "x": 1, "y": 1},
    ]})
    _, team_err = A.resolve_field_values(consistent_fields, team_only, {})
    r.check("a csv field consistent across the match is NOT ambiguous", team_err is None, team_err)

    # ── byte-identical: legacy render == synthesized single-field render ──────
    live = A.load_event("legacy")
    A._RENDERED_CERT_CACHE.clear()
    new_bytes = A.render_certificate("legacy", "Ada Lovelace", live)[0]

    reference = A.get_template_image("legacy", live)
    A.draw_name_on_image(reference, {
        "cert_name": "Ada Lovelace", "text_x": 1000, "text_y": 700,
        "font_size": 90, "font_color": [50, 34, 24], "font_key": "montserrat_bold",
    })
    buf = io.BytesIO(); reference.save(buf, format="PNG", compress_level=A.DOWNLOAD_PNG_COMPRESS_LEVEL)
    r.check("legacy multi-field render is byte-identical to the old draw",
            new_bytes == buf.getvalue(), f"{len(new_bytes)} vs {len(buf.getvalue())}")

    # ── the render fingerprint covers every field ────────────────────────────
    A._RENDERED_CERT_CACHE.clear()
    vals = {"name": "Ada", "position": "1st", "points": "07", "date": "20 August 2026"}
    etag0 = A.render_certificate("secure", vals, ev)[1]
    etag_same = A.render_certificate("secure", dict(vals), ev)[1]
    r.check("identical values -> identical etag (cache hits)", etag0 == etag_same)
    etag_val = A.render_certificate("secure", dict(vals, points="09"), ev)[1]
    r.check("changing one field's value changes the etag", etag_val != etag0)

    moved = A.load_event("secure")
    moved["fields"] = [dict(f) for f in fields]
    moved["fields"][1]["x"] = 1234  # move the position field only
    etag_moved = A.render_certificate("secure", vals, moved)[1]
    r.check("moving one field changes the etag (no stale sibling renders)", etag_moved != etag0)

    # ── overflow ─────────────────────────────────────────────────────────────
    long_text = "An Extraordinarily Long Participant Name That Overflows The Box"
    shrink_f = A.normalize_fields({"fields": [
        {"id": "name", "source": "input", "x": 1000, "y": 700, "font_size": 120,
         "max_width": 900, "overflow": "shrink"}]})
    trunc_f = A.normalize_fields({"fields": [
        {"id": "name", "source": "input", "x": 1000, "y": 700, "font_size": 120,
         "max_width": 900, "overflow": "truncate"}]})
    img = A.get_template_image("legacy", live)
    draw = A.ImageDraw.Draw(img)
    font = A.get_font(120, "montserrat_bold")
    _, shrunk_font = A._fit_text(draw, long_text, shrink_f[0], font)
    r.check("shrink reduces the font below its nominal size", shrunk_font.size < 120, shrunk_font.size)
    trunc_text, _ = A._fit_text(draw, long_text, trunc_f[0], font)
    r.check("truncate clips with an ellipsis", trunc_text.endswith("…") and len(trunc_text) < len(long_text),
            trunc_text)
    short = "Ada"
    same_text, same_font = A._fit_text(draw, short, shrink_f[0], font)
    r.check("text that already fits is untouched", same_text == short and same_font.size == 120)

    # ── token round trip with multiple values, and legacy {s,n} ──────────────
    tok = A.make_cert_token("secure", vals, club_slug="acme")
    club_back, slug_back, vals_back = A.read_cert_token(tok)
    r.check("multi-value token round trips with the club",
            club_back == "acme" and slug_back == "secure" and vals_back == vals, (club_back, vals_back))
    legacy_token = A._cert_serializer().dumps({"s": "legacy", "n": "Ada Lovelace"})
    r.check("a pre-0.5 {s,n} token still resolves club-lessly to the name field",
            A.read_cert_token(legacy_token) == (None, "legacy", {"name": "Ada Lovelace"}))
    r.check("display_name picks the name field", A.display_name(vals) == "Ada")
    r.check("display_name falls back to the first value",
            A.display_name({"team": "Nimbus", "x": ""}) == "Nimbus")

    # ── end to end through the routes ────────────────────────────────────────
    client = A.app.test_client()
    tok_route = A.make_cert_token("secure", vals)
    dl = client.get(f"/download-file/{tok_route}")
    r.check("multi-field certificate downloads", dl.status_code == 200, dl.status_code)
    r.check("download names the file after the participant", "Ada" in dl.headers.get("Content-Disposition", ""))

    # ── the participant form: single name unchanged, extra inputs collected ──
    import re as _re
    single = make_event("single-in", validation_type="player_team",
                        csv_text="player,team\nAda,Nimbus\n")  # legacy, no fields
    page = client.get("/events/single-in", follow_redirects=True).data.decode("utf-8", "replace")
    r.check("a single-name event shows no extra input boxes", 'name="field_' not in page)
    resp = post(client, "/events/single-in/download",
                {"cert_name": "Ada", "registration_name": "Ada", "team_name": "Nimbus"})
    _, _, sv = A.read_cert_token(_re.search(r"/preview/([^/?]+)", resp.headers.get("Location", "")).group(1))
    r.check("single-name flow is unchanged", sv == {"name": "Ada"}, sv)

    two_inputs = [
        {"id": "name", "label": "Your name", "source": "input", "x": 100, "y": 100},
        {"id": "note", "label": "Dedication", "source": "input", "x": 100, "y": 300},
    ]
    make_event("two-in", fields=two_inputs, validation_type="player_team",
               csv_text="player,team\nAda,Nimbus\n")
    page = client.get("/events/two-in", follow_redirects=True).data.decode("utf-8", "replace")
    r.check("a second input field renders its own box", 'name="field_note"' in page)
    r.check("the extra input is labelled", "Dedication" in page)
    r.check("the primary box is relabelled from the field", "Your name" in page)
    resp = post(client, "/events/two-in/download",
                {"cert_name": "Ada Lovelace", "field_note": "With honours",
                 "registration_name": "Ada", "team_name": "Nimbus"})
    _, _, mv = A.read_cert_token(_re.search(r"/preview/([^/?]+)", resp.headers.get("Location", "")).group(1))
    r.check("both input fields are captured, csv/static untouched",
            mv == {"name": "Ada Lovelace", "note": "With honours"}, mv)
finally:
    teardown_scratch(scratch)

sys.exit(r.finish())
