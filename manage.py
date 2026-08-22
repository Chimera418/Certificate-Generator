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

def db_init():
    """Apply migrations/*.sql to the Postgres named by DATABASE_URL.

    Re-runnable: every statement is IF NOT EXISTS, so pointing this at staging and
    then production is safe. Refuses to run without DATABASE_URL rather than
    silently doing nothing.
    """
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        raise SystemExit("DATABASE_URL is not set. db-init needs a Postgres connection string.")
    try:
        import psycopg
    except ImportError:
        raise SystemExit("psycopg is not installed. Run: pip install -r requirements.txt")

    migrations_dir = Path(__file__).resolve().parent / "migrations"
    files = sorted(migrations_dir.glob("*.sql"))
    if not files:
        raise SystemExit(f"No migration files found in {migrations_dir}.")

    with psycopg.connect(dsn) as conn:
        for path in files:
            log_message(f"Applying {path.name} ...")
            with conn.cursor() as cur:
                cur.execute(path.read_text(encoding="utf-8"))
            conn.commit()
    log_message(f"db-init complete: applied {len(files)} migration file(s).")




def _migration_plan():
    """Read-only. What the csi-aseb migration would insert and move."""
    from app import load_all_events, load_event, _supabase_list
    import db

    events_plan = []
    for e in sorted(load_all_events(active_only=False), key=lambda e: e.get("slug", "")):
        slug = e.get("slug")
        if not slug:
            continue
        clean = db._clean_config(load_event(slug) or {})
        events_plan.append({
            "slug": slug,
            "name": clean.get("name") or slug,
            "active": bool(clean.get("active")),
            "template_ext": clean.get("template_ext"),
            "template_version": clean.get("template_version"),
            "config": clean,
        })

    def list_prefix(prefix):
        out, stack, seen = [], [prefix], set()
        while stack:
            pfx = stack.pop()
            for entry in _supabase_list(pfx):
                name = entry.get("name")
                if not name:
                    continue
                full = pfx + name
                meta = entry.get("metadata")
                if meta and isinstance(meta, dict) and meta.get("size") is not None:
                    out.append((full, int(meta["size"])))
                else:
                    child = full + "/"
                    if child not in seen:
                        seen.add(child)
                        stack.append(child)
        return sorted(out)

    objects_plan = []
    for ep in events_plan:
        for path, size in list_prefix(ep["slug"] + "/"):
            objects_plan.append({"src": path, "dest": "csi-aseb/" + path, "size": size})
    return events_plan, objects_plan


def _sha(b):
    import hashlib
    return hashlib.sha256(b).hexdigest()


def _content_type_for(path):
    from app import TEMPLATE_CONTENT_TYPES, PARTICIPANT_CONTENT_TYPES
    import os as _os
    ext = _os.path.splitext(path)[1].lower()
    return (TEMPLATE_CONTENT_TYPES.get(ext)
            or PARTICIPANT_CONTENT_TYPES.get(ext)
            or "application/octet-stream")


def migrate_csi_aseb(dry_run):
    """Migrate the legacy csi-aseb data into the first club row. Re-runnable.

    Ordering note: objects are COPIED and verified to the new csi-aseb/ prefix
    BEFORE the club/events rows are inserted, and the legacy sources are deleted
    only AFTER. Inserting the rows first would flip resolve_public_event onto the
    club-scoped storage path before the objects exist there, breaking renders of
    the live 'think-run-debug' event mid-migration. Copy-first keeps both paths
    serving throughout the cutover, and 'copy, verify, then delete - never move'
    is what makes a re-run safe.
    """
    # Refuse without a real Postgres. Without this, repo() is the in-memory store:
    # the object COPY/DELETE below would still hit the real bucket while the club
    # and event rows land in a throwaway DB that vanishes on exit - leaving the
    # bucket migrated but no rows, so every csi-aseb event becomes unreachable.
    if not os.environ.get("DATABASE_URL", "").strip():
        raise SystemExit("DATABASE_URL is not set. migrate-csi-aseb needs the real "
                         "Postgres, or it would move bucket objects while writing rows "
                         "to a throwaway in-memory store. Aborting.")

    from app import (LEGACY_TOKEN_CLUB, repo, generate_password_hash,
                     _supabase_download, _supabase_upload, _supabase_delete)
    import db
    if repo().backend_name != "postgres":
        raise SystemExit(f"Repository backend is {repo().backend_name!r}, not Postgres. Aborting.")

    def _dl(path):
        # Missing objects can come back as 400 or 404 depending on Supabase;
        # for migration purposes any read failure means 'not readable at that
        # path'. The verify-after-copy step below is what actually guards data.
        from urllib import error as _e
        try:
            return _supabase_download(path)
        except (_e.HTTPError, _e.URLError, TimeoutError, OSError, ValueError):
            return None

    club_slug = LEGACY_TOKEN_CLUB  # "csi-aseb"
    events_plan, objects_plan = _migration_plan()

    log_message("=== csi-aseb migration " + ("(DRY RUN - changes nothing)" if dry_run else "(EXECUTE)") + " ===")
    log_message(f"club row: slug={club_slug!r}, name='CSI ASEB', status='approved'")
    log_message(f"events to insert: {len(events_plan)}")
    for ep in events_plan:
        log_message(f"  - {ep['slug']}: active={ep['active']}, ext={ep['template_ext']}, "
                    f"ver={str(ep['template_version'])[:12]}, config_keys={sorted(ep['config'].keys())}")
    log_message(f"objects to copy -> verify -> delete: {len(objects_plan)}")
    for ob in objects_plan:
        dest_present = _dl(ob["dest"]) is not None
        note = "  [dest already present - would verify & skip]" if dest_present else ""
        log_message(f"  - {ob['src']}  ->  {ob['dest']}  ({ob['size']} b){note}")

    if dry_run:
        log_message("DRY RUN complete. Nothing was changed. Re-run with --execute to apply.")
        return

    # ── EXECUTE ──────────────────────────────────────────────────────────────
    # 1. Export/backup FIRST (this file IS the backup - Supabase PITR is paid).
    import datetime, json as _json, os as _os
    from app import load_event, _supabase_list
    backups = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "backups")
    _os.makedirs(backups, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    export_path = _os.path.join(backups, f"csi-aseb-migration-{stamp}.json")
    export = {
        "created_at": datetime.datetime.now().isoformat(),
        "kv_configs": {ep["slug"]: load_event(ep["slug"]) for ep in events_plan},
        "bucket_objects": [{"path": ob["src"], "size": ob["size"]} for ob in objects_plan],
        "plan": {"club": club_slug, "events": [ep["slug"] for ep in events_plan]},
    }
    with open(export_path, "w", encoding="utf-8") as f:
        _json.dump(export, f, indent=2)
    log_message(f"backup written: {export_path}")

    # 2. Copy + verify every object (do NOT delete the source yet).
    for ob in objects_plan:
        src, dest = ob["src"], ob["dest"]
        dest_bytes = _dl(dest)
        src_bytes = _dl(src)
        if dest_bytes is not None and (src_bytes is None or _sha(dest_bytes) == _sha(src_bytes)):
            log_message(f"copy: {dest} already present and verified - skip")
            continue
        if src_bytes is None:
            raise SystemExit(f"ABORT: source {src} missing and dest not present - cannot migrate.")
        _supabase_upload(dest, src_bytes, _content_type_for(dest))
        check = _dl(dest)
        if check is None or _sha(check) != _sha(src_bytes):
            raise SystemExit(f"ABORT: verify failed for {dest} - source left intact.")
        log_message(f"copy+verify: {src} -> {dest} ({len(src_bytes)} b)")

    # 3. Insert the club row, then the events rows (cutover - objects already there).
    club = repo().get_club_by_slug(club_slug)
    if club is None:
        club = repo().create_club(club_slug, "CSI ASEB",
                                  generate_password_hash(_os.urandom(24).hex()))
        log_message(f"inserted club row {club_slug} (login password unusable - set via superadmin if needed)")
    else:
        log_message(f"club row {club_slug} already exists - reusing")
    repo().set_club_status(club["id"], "approved")

    for ep in events_plan:
        existing = repo().get_event(club["id"], ep["slug"])
        if existing is None:
            repo().create_event(club["id"], ep["slug"], ep["name"], ep["config"])
            log_message(f"inserted event row {club_slug}/{ep['slug']}")
        else:
            log_message(f"event row {club_slug}/{ep['slug']} already exists - updating")
        repo().update_event(club["id"], ep["slug"], name=ep["name"], config=ep["config"],
                            active=ep["active"], template_ext=ep["template_ext"],
                            template_version=ep["template_version"])

    # 4. Delete the legacy sources - only those whose dest is verified present.
    for ob in objects_plan:
        src, dest = ob["src"], ob["dest"]
        dest_bytes = _dl(dest)
        src_bytes = _dl(src)
        if src_bytes is None:
            log_message(f"delete: source {src} already gone")
            continue
        if dest_bytes is not None and _sha(dest_bytes) == _sha(src_bytes):
            _supabase_delete(src)
            log_message(f"delete: removed legacy source {src} (verified at {dest})")
        else:
            log_message(f"KEEP: {src} not verified at dest - source left intact")

    log_message("=== migration complete. Bridges left in place (per plan S9). ===")

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

    # db-init
    subparsers.add_parser("db-init", help="Create the clubs/events schema in the DATABASE_URL Postgres")

    # migrate-csi-aseb
    p_mig = subparsers.add_parser("migrate-csi-aseb", help="Migrate the legacy csi-aseb data into the first club row")
    mig_mode = p_mig.add_mutually_exclusive_group(required=True)
    mig_mode.add_argument("--dry-run", action="store_true", help="Print the plan, change nothing")
    mig_mode.add_argument("--execute", action="store_true", help="Apply the migration")

    args = parser.parse_args()

    if args.command == "split-csv":
        split_csv(args.input_file, args.output_dir, args.chunk_size)
    elif args.command == "bulk-generate":
        bulk_generate(args.slug, send_emails=False, max_workers=args.workers)
    elif args.command == "send-emails":
        bulk_generate(args.slug, send_emails=True, max_workers=args.workers)
    elif args.command == "db-init":
        db_init()
    elif args.command == "migrate-csi-aseb":
        migrate_csi_aseb(dry_run=args.dry_run)
