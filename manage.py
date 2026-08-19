import argparse
import os
import concurrent.futures
import csv
import threading
from pathlib import Path

# Add the current directory to sys.path so app and utils are importable
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import (
    GENERATED_DIR,
    LOG_DIR,
    LOG_FILE,
    load_event,
    load_event_csv_text,
    render_certificate,
    safe_download_name,
)
from utils.emailer import EmailSender

_log_lock = threading.Lock()

def log_message(msg: str):
    import datetime
    print(msg)
    # Worker threads all write here, so serialise to avoid interleaved lines.
    with _log_lock:
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
        except Exception:
            pass

def load_csv_rows_raw(slug: str) -> list[dict]:
    content = load_event_csv_text(slug)
    if not content:
        return []
    import csv
    reader = csv.DictReader(content.splitlines())
    rows = []
    for row in reader:
        new_row = {k.strip().lower() if k else '': v.strip() if v else '' for k, v in row.items()}
        rows.append(new_row)
    return rows

def split_csv(input_file: str, output_dir: str, chunk_size: int = 100):
    """Split a large CSV into smaller chunks."""
    input_path = Path(input_file)
    output_path = Path(output_dir)

    if not input_path.exists():
        print(f"Error: Input file {input_file} does not exist.")
        return

    output_path.mkdir(parents=True, exist_ok=True)

    with open(input_path, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        try:
            headers = next(reader)
        except StopIteration:
            print("Empty CSV file.")
            return

        chunk_idx = 1
        current_rows = []

        for row in reader:
            current_rows.append(row)
            if len(current_rows) >= chunk_size:
                _write_chunk(output_path / f"{input_path.stem}_part{chunk_idx}.csv", headers, current_rows)
                current_rows = []
                chunk_idx += 1

        if current_rows:
            _write_chunk(output_path / f"{input_path.stem}_part{chunk_idx}.csv", headers, current_rows)

    print(f"Successfully split {input_file} into chunks in {output_dir}")

def _write_chunk(path: Path, headers: list, rows: list):
    with open(path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)

_export_names_lock = threading.Lock()
_used_export_names: set = set()


def _unique_export_path(output_dir: Path, filename: str) -> Path:
    """
    Reserve an export filename.

    safe_download_name() strips accents and punctuation, so distinct participants
    can normalise to the same filename and silently overwrite each other. Suffix
    the duplicates instead.
    """
    stem, suffix = os.path.splitext(filename)
    with _export_names_lock:
        candidate = filename
        counter = 2
        while candidate in _used_export_names:
            candidate = f"{stem}-{counter}{suffix}"
            counter += 1
        _used_export_names.add(candidate)
    return output_dir / candidate


def process_participant(slug: str, row: dict, event_config: dict, output_dir: Path, emailer: EmailSender = None,
                        subject_template: str = None, plain_body_template: str = None, html_body_template: str = None) -> str:
    # Use name from 'name' column or 'player' column
    cert_name = row.get("name") or row.get("player") or "Participant"
    email = row.get("email")

    # Rendered straight to bytes: no blank template copy is written per participant.
    # Exports are the real artifact, so always the full-resolution download variant.
    rendered = render_certificate(slug, cert_name, event_config, variant="download")
    if rendered is None:
        raise RuntimeError(f"No usable certificate template for event '{slug}'")
    png_bytes, _, _ = rendered

    target_img = _unique_export_path(output_dir, safe_download_name(cert_name, slug))
    target_img.write_bytes(png_bytes)
    friendly_name = target_img.name

    status = f"Generated {friendly_name}"

    if emailer and email:
        try:
            emailer.send_certificate(
                participant_email=email,
                participant_name=cert_name,
                event_name=event_config.get("name", slug),
                certificate_path=str(target_img),
                subject_template=subject_template,
                plain_body_template=plain_body_template,
                html_body_template=html_body_template
            )
            status += f" and emailed to {email}"
        except Exception as e:
            status += f", but email failed: {e}"

    return status

def bulk_generate(slug: str, send_emails: bool = False, max_workers: int = 4,
                  subject_template: str = None, plain_body_template: str = None, html_body_template: str = None):
    event_config = load_event(slug)
    if not event_config:
        log_message(f"Error: Event '{slug}' not found or inactive.")
        return

    rows = load_csv_rows_raw(slug)
    if not rows:
        log_message(f"Error: No participants found in data.csv for event '{slug}'.")
        return

    output_dir = Path(GENERATED_DIR) / slug / "exports"
    output_dir.mkdir(parents=True, exist_ok=True)

    emailer = EmailSender() if send_emails else None
    if send_emails:
        log_message(f"Email delivery enabled. SMTP Host: {emailer.smtp_host}")

    log_message(f"Starting bulk generation for {len(rows)} participants in event '{slug}'...")

    success_count = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_participant, slug, row, event_config, output_dir, emailer,
                                   subject_template, plain_body_template, html_body_template): row for row in rows}

        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
                log_message(f"[SUCCESS] {result}")
                success_count += 1
            except Exception as exc:
                log_message(f"[ERROR] Participant processing generated an exception: {exc}")

    log_message(f"Completed! Successfully processed {success_count}/{len(rows)} participants.")
    log_message(f"Certificates exported to: {output_dir}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Certificate Generator Management CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # split-csv
    parser_split = subparsers.add_parser("split-csv", help="Split a large CSV into smaller chunks")
    parser_split.add_argument("input_file", help="Path to the input CSV file")
    parser_split.add_argument("--output-dir", default="splits", help="Directory to save the chunks")
    parser_split.add_argument("--chunk-size", type=int, default=100, help="Number of rows per chunk")

    # bulk-generate
    parser_bulk = subparsers.add_parser("bulk-generate", help="Generate all certificates for an event locally")
    parser_bulk.add_argument("slug", help="Event slug")
    parser_bulk.add_argument("--workers", type=int, default=4, help="Max concurrent workers")

    # send-emails
    parser_email = subparsers.add_parser("send-emails", help="Generate and email certificates for an event")
    parser_email.add_argument("slug", help="Event slug")
    parser_email.add_argument("--workers", type=int, default=4, help="Max concurrent workers")

    args = parser.parse_args()

    if args.command == "split-csv":
        split_csv(args.input_file, args.output_dir, args.chunk_size)
    elif args.command == "bulk-generate":
        bulk_generate(args.slug, send_emails=False, max_workers=args.workers)
    elif args.command == "send-emails":
        bulk_generate(args.slug, send_emails=True, max_workers=args.workers)
