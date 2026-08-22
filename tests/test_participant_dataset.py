"""
Phase 0.5a foundation: the parse-once participant dataset and the new
validate_participant_submission contract.

Two things are load-bearing here. First, values must come back VERBATIM ("Nimbus",
"07") for display while matching stays case-insensitive — resolving a csv field off
the normalized copy is the display bug the plan forbids. Second, the validator now
returns the matched rows, and the combination check it has always enforced must not
regress in the process (a participant still cannot pass an invalid player/team pair).

Run with: python tests/test_participant_dataset.py
"""
import os
import sys

from _fixture import (
    A,
    Results,
    setup_scratch_event,
    teardown_scratch,
)

r = Results()
scratch = setup_scratch_event()


def write_csv(slug, text):
    d = os.path.join(A.EVENTS_DIR, slug)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "data.csv"), "w", encoding="utf-8", newline="") as f:
        f.write(text)
    A._EVENT_CSV_CACHE.pop(slug, None)
    A._PARTICIPANT_DATASET_CACHE.pop(slug, None)


class Form(dict):
    """Stands in for request.form: a plain dict with .get, which is all it uses."""


try:
    slug = "ds"
    # Deliberately mixed case and a leading-zero number; position varies within a
    # team so ambiguity is real; whitespace padding on one cell.
    write_csv(slug,
        "player,team,position,points\n"
        "Ada Lovelace,Nimbus,1st,07\n"
        "Grace Hopper, Nimbus ,2nd,05\n"
        "Alan Turing,Cirrus,1st,09\n")

    ds = A.load_participant_dataset(slug)

    # ── verbatim vs normalized ───────────────────────────────────────────────
    r.check("columns are the normalized headers",
            ds.columns == ["player", "team", "position", "points"], ds.columns)
    r.check("raw value keeps its case", ds.raw_rows[0]["team"] == "Nimbus", ds.raw_rows[0]["team"])
    r.check("raw value keeps a leading zero", ds.raw_rows[0]["points"] == "07", ds.raw_rows[0]["points"])
    r.check("normalized value is lowercased", ds.norm_rows[0]["team"] == "nimbus", ds.norm_rows[0]["team"])
    r.check("surrounding whitespace is trimmed from raw", ds.raw_rows[1]["team"] == "Nimbus",
            repr(ds.raw_rows[1]["team"]))
    r.check("padded cell still matches normalized", ds.norm_rows[1]["team"] == "nimbus")

    # ── column_values: distinct, verbatim, first-seen order ──────────────────
    r.check("distinct teams verbatim", ds.column_values("team") == ["Nimbus", "Cirrus"],
            ds.column_values("team"))
    r.check("column_values dedupes case-insensitively (one Nimbus)",
            ds.column_values("team").count("Nimbus") == 1)
    r.check("column lookup is case-insensitive on the header",
            ds.column_values("TEAM") == ["Nimbus", "Cirrus"])

    # ── rows_matching: normalized compare, verbatim rows out ─────────────────
    one = ds.rows_matching({"player": "ada lovelace", "team": "nimbus"})
    r.check("exact pair matches one row", len(one) == 1, len(one))
    r.check("matched row is verbatim", one and one[0]["points"] == "07")
    team_only = ds.rows_matching({"team": "nimbus"})
    r.check("team-only key matches every member", len(team_only) == 2, len(team_only))
    r.check("their position disagrees (ambiguity is real)",
            {row["position"] for row in team_only} == {"1st", "2nd"})
    r.check("no match returns empty", ds.rows_matching({"team": "stratus"}) == [])

    # ── the loaders now share one parse but keep their shapes ────────────────
    r.check("csv_headers unchanged", A.csv_headers(slug) == ["player", "team", "position", "points"])
    r.check("load_team_names is sorted verbatim", A.load_team_names(slug) == ["Cirrus", "Nimbus"],
            A.load_team_names(slug))
    r.check("load_valid_participants normalized pairs",
            ("ada lovelace", "nimbus") in A.load_valid_participants(slug))
    r.check("load_unique_column_values verbatim", A.load_unique_column_values(slug, "points") == ["07", "05", "09"],
            A.load_unique_column_values(slug, "points"))

    # ── cache identity + self-invalidation on content change ─────────────────
    r.check("same content -> same cached object", A.load_participant_dataset(slug) is ds)
    write_csv(slug, "player,team,position,points\nZoe Zana,Zephyr,1st,10\n")
    ds2 = A.load_participant_dataset(slug)
    r.check("changed content -> fresh parse", ds2 is not ds)
    r.check("fresh parse reflects new data", ds2.column_values("team") == ["Zephyr"], ds2.column_values("team"))

    # ── the new validation contract ──────────────────────────────────────────
    write_csv(slug,
        "player,team,position,points\n"
        "Ada Lovelace,Nimbus,1st,07\n"
        "Grace Hopper,Nimbus,2nd,05\n")

    def cfg(vt, **extra):
        return dict({"validation_type": vt, "custom_fields": []}, **extra)

    rows, err = A.validate_participant_submission(slug, cfg("player_team"),
                                                  Form(registration_name="Ada Lovelace", team_name="Nimbus"))
    r.check("valid pair -> (rows, None)", err is None and rows and rows[0]["team"] == "Nimbus", (rows, err))

    rows, err = A.validate_participant_submission(slug, cfg("player_team"),
                                                  Form(registration_name="ADA LOVELACE", team_name="nimbus"))
    r.check("matching is case-insensitive", err is None and len(rows) == 1, (rows, err))
    r.check("but the returned row is verbatim", rows and rows[0]["player"] == "Ada Lovelace")

    # THE security check must not regress: a wrong pair still fails.
    rows, err = A.validate_participant_submission(slug, cfg("player_team"),
                                                  Form(registration_name="Ada Lovelace", team_name="Cirrus"))
    r.check("wrong pair is rejected (combination check intact)",
            rows is None and err == "Invalid player or team name.", (rows, err))
    rows, err = A.validate_participant_submission(slug, cfg("player_team"),
                                                  Form(registration_name="Mallory", team_name="Nimbus"))
    r.check("unknown player is rejected", rows is None and err == "Invalid player or team name.")
    rows, err = A.validate_participant_submission(slug, cfg("player_team"),
                                                  Form(registration_name="", team_name="Nimbus"))
    r.check("missing field -> its own error", rows is None and err == "Please fill all fields.")

    # None-validation: success with no row behind it (list, not None).
    rows, err = A.validate_participant_submission(slug, cfg("none"), Form())
    r.check("'none' returns ([], None), not an error", rows == [] and err is None, (rows, err))

    # A coarse custom key returns every matching row, for the ambiguity guard later.
    rows, err = A.validate_participant_submission(
        slug, cfg("custom", custom_fields=["team"]), Form(custom_team="Nimbus"))
    r.check("team-only custom match returns both members", err is None and len(rows) == 2, (rows, err))

    # Empty CSV must not crash any of this.
    write_csv("empty-ev", "")
    r.check("empty CSV -> empty dataset", A.load_participant_dataset("empty-ev").raw_rows == [])
    r.check("empty CSV -> no team names", A.load_team_names("empty-ev") == [])
    rows, err = A.validate_participant_submission("empty-ev", cfg("player_team"),
                                                  Form(registration_name="x", team_name="y"))
    r.check("empty CSV validation fails cleanly", rows is None and err == "Invalid player or team name.")

    # ── cross-club CSV isolation: same slug, two clubs, distinct rosters ──────
    # This is the security fix from Phase 3.5a: the read path and the caches are
    # club-scoped, so club B never sees club A's participants even when both own an
    # event at the same slug.
    import os as _os
    for club, roster in (("acme", "player,team\r\nAda,Nimbus\r\n".replace("\r\n", chr(10))),
                         ("beta", "player,team\r\nZoe,Zephyr\r\n".replace("\r\n", chr(10)))):
        d = _os.path.join(A.EVENTS_DIR, club, "gala")
        _os.makedirs(d, exist_ok=True)
        with open(_os.path.join(d, "data.csv"), "w", encoding="utf-8", newline="") as f:
            f.write(roster)
    A._EVENT_CSV_CACHE.clear(); A._PARTICIPANT_DATASET_CACHE.clear()

    acme_ds = A.load_participant_dataset("gala", club_slug="acme")
    beta_ds = A.load_participant_dataset("gala", club_slug="beta")
    r.check("club acme reads its own roster", acme_ds.column_values("player") == ["Ada"], acme_ds.column_values("player"))
    r.check("club beta reads its own roster", beta_ds.column_values("player") == ["Zoe"], beta_ds.column_values("player"))
    r.check("the two datasets did not collide in cache", acme_ds is not beta_ds)

    # Loading acme again after beta must still be acme (no cross-club cache hit).
    r.check("re-loading acme is still acme, not beta",
            A.load_participant_dataset("gala", club_slug="acme").column_values("player") == ["Ada"])

    # And validation is club-scoped: acme's participant is valid under acme, not beta.
    ok_acme, e1 = A.validate_participant_submission(
        "gala", {"validation_type": "player_team"}, Form(registration_name="Ada", team_name="Nimbus"),
        club_slug="acme")
    r.check("acme's participant validates under acme", e1 is None and ok_acme, e1)
    none_beta, e2 = A.validate_participant_submission(
        "gala", {"validation_type": "player_team"}, Form(registration_name="Ada", team_name="Nimbus"),
        club_slug="beta")
    r.check("acme's participant is INVALID under club beta (no cross-club leak)",
            none_beta is None and e2 is not None, (none_beta, e2))

    # A legacy (club-less) read is unaffected and still uses the bare-slug key.
    r.check("a club-less read of 'gala' finds no roster (not acme's or beta's)",
            A.load_participant_dataset("gala").raw_rows == [])
finally:
    teardown_scratch(scratch)

sys.exit(r.finish())
