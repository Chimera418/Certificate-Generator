"""
Participant list uploads: CSV and Excel.

A workbook is converted to CSV once at upload time so validation, dropdowns and
bulk generation stay CSV-only. What matters here is that the conversion produces
the same result as the equivalent CSV, and that malformed uploads are rejected
rather than half-saved.

Run with: python tests/test_participant_upload.py
"""
import io
import sys

from _fixture import (
    TEST_EMAIL,
    TEST_NAME,
    TEST_SLUG,
    A,
    Results,
    admin_client,
    make_xlsx_bytes,
    post,
    setup_scratch_event,
    teardown_scratch,
)

r = Results()
scratch = setup_scratch_event()


def upload(client, data, filename):
    return post(client, f"/admin/events/{TEST_SLUG}/upload-csv",
                {"csv_file": (io.BytesIO(data), filename)},
                content_type="multipart/form-data")


try:
    admin = admin_client()

    # ── conversion ──────────────────────────────────────────────────────────
    text = A.xlsx_to_csv_text(make_xlsx_bytes())
    r.check("workbook converts to csv text", "name,email" in text, text[:60])
    r.check("conversion keeps every data row",
            TEST_NAME in text and "Grace Hopper" in text, text)
    r.check("conversion produces one line per row",
            len([l for l in text.splitlines() if l.strip()]) == 3, text.splitlines())

    ragged = A.xlsx_to_csv_text(make_xlsx_bytes([
        ["name", "email"],
        ["Ada", "ada@example.com", "extra ignored"],
        ["Grace"],
        [None, None],
        ["Alan", "alan@example.com"],
    ]))
    lines = [l for l in ragged.splitlines() if l.strip()]
    r.check("rows longer than the header are trimmed", "extra ignored" not in ragged, ragged)
    r.check("short rows are padded to the header width",
            any(l.startswith("Grace,") for l in lines), lines)
    r.check("blank rows are dropped", len(lines) == 4, lines)

    numbers = A.xlsx_to_csv_text(make_xlsx_bytes([
        ["roll_no", "name"], [12345, "Ada"], ["BL.SC.U4AIE001", "Grace"],
    ]))
    r.check("numeric cells become plain text", "12345,Ada" in numbers, numbers)

    r.check("an empty workbook converts to empty text",
            A.xlsx_to_csv_text(make_xlsx_bytes([])) == "")

    # ── the upload endpoint ─────────────────────────────────────────────────
    csv_bytes = f"name,email\n{TEST_NAME},{TEST_EMAIL}\nGrace Hopper,grace@example.com\n".encode()
    response = upload(admin, csv_bytes, "roster.csv")
    r.check("csv upload succeeds", response.status_code == 200, response.status_code)
    r.check("csv upload reports success", b"Participant list uploaded." in response.data)
    r.check("csv upload does not claim conversion", b"Converted from" not in response.data)
    r.check("csv rows are readable afterwards",
            {row["email"] for row in A.load_csv_rows(TEST_SLUG)} == {TEST_EMAIL, "grace@example.com"},
            A.load_csv_rows(TEST_SLUG))

    A._EVENT_CSV_CACHE.clear()
    response = upload(admin, make_xlsx_bytes(), "roster.xlsx")
    r.check("xlsx upload succeeds", response.status_code == 200, response.status_code)
    r.check("xlsx upload says it converted the workbook",
            b"Converted from the first sheet" in response.data)
    A._EVENT_CSV_CACHE.clear()
    r.check("xlsx rows are readable afterwards",
            {row["email"] for row in A.load_csv_rows(TEST_SLUG)} == {TEST_EMAIL, "grace@example.com"},
            A.load_csv_rows(TEST_SLUG))
    r.check("headers are discovered from the workbook",
            set(A.csv_headers(TEST_SLUG)) == {"name", "email"}, A.csv_headers(TEST_SLUG))

    # Excel writes a BOM when exporting CSV; it must not end up in the first header.
    A._EVENT_CSV_CACHE.clear()
    upload(admin, b"\xef\xbb\xbfname,email\nAda,ada@example.com\n", "excel-export.csv")
    A._EVENT_CSV_CACHE.clear()
    r.check("a BOM from Excel does not corrupt the first column",
            A.csv_headers(TEST_SLUG)[0] == "name", A.csv_headers(TEST_SLUG))

    # ── rejections ──────────────────────────────────────────────────────────
    A._EVENT_CSV_CACHE.clear()
    good_headers = A.csv_headers(TEST_SLUG)

    bad_ext = upload(admin, b"name,email\n", "roster.txt")
    r.check("a .txt upload is rejected", bad_ext.status_code == 400, bad_ext.status_code)
    r.check("the rejection names the accepted formats", b".csv or .xlsx" in bad_ext.data)

    fake_xlsx = upload(admin, b"this is not a workbook", "roster.xlsx")
    r.check("a fake .xlsx is rejected", fake_xlsx.status_code == 400, fake_xlsx.status_code)
    r.check("the rejection explains why", b"valid .xlsx workbook" in fake_xlsx.data)

    missing_column = upload(admin, b"nickname,phone\nada,123\n", "wrong.csv")
    r.check("a file missing required columns is rejected",
            missing_column.status_code == 400, missing_column.status_code)
    r.check("the rejection names the missing column", b"email" in missing_column.data)

    empty_sheet = upload(admin, make_xlsx_bytes([]), "empty.xlsx")
    r.check("an empty workbook is rejected", empty_sheet.status_code == 400, empty_sheet.status_code)

    A._EVENT_CSV_CACHE.clear()
    r.check("the good list survived every rejected upload",
            A.csv_headers(TEST_SLUG) == good_headers, A.csv_headers(TEST_SLUG))
finally:
    teardown_scratch(scratch)

sys.exit(r.finish())
