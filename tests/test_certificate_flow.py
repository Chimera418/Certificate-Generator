"""
The public certificate flow.

Certificate links are signed tokens rendered on demand, not files on disk, so the
things worth pinning down are: tokens round-trip and reject tampering, renders are
cached but invalidate when anything visible changes, and a request never writes to
the filesystem.

Run with: python tests/test_certificate_flow.py
"""
import io
import os
import sys

from PIL import Image

from _fixture import (
    TEMPLATE_SIZE,
    TEST_EMAIL,
    TEST_NAME,
    TEST_SLUG,
    A,
    Results,
    admin_client,
    make_template_bytes,
    post,
    setup_scratch_event,
    teardown_scratch,
)

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"

r = Results()
scratch = setup_scratch_event()

try:
    assert not A._kv_enabled(), "KV must be disabled for these tests"
    assert not A._supabase_enabled(), "Supabase must be disabled for these tests"

    client = A.app.test_client()
    config = A.load_event(TEST_SLUG)

    # ── pages ────────────────────────────────────────────────────────────────
    r.check("GET / returns 200", client.get("/").status_code == 200)
    # /events/<slug> is now a 301 to the club-scoped URL; follow it to the form.
    r.check("event page renders (via the club-scoped redirect)",
            client.get(f"/events/{TEST_SLUG}", follow_redirects=True).status_code == 200)
    r.check("the flat /events URL 301s to /c/csi-aseb/",
            client.get(f"/events/{TEST_SLUG}").status_code == 301)

    health = client.get("/healthz")
    r.check("GET /healthz returns 200", health.status_code == 200)
    r.check("healthz reports storage state", health.get_json().get("storage") == "disabled",
            health.get_json())

    # ── template layer ───────────────────────────────────────────────────────
    r.check("has_template", A.has_template(TEST_SLUG, config))
    r.check("template bytes load", bool(A.load_template_bytes(TEST_SLUG, config)))
    image = A.get_template_image(TEST_SLUG, config)
    r.check("template decodes without inventing alpha", image is not None and image.mode == "RGB")

    # ── tokens ───────────────────────────────────────────────────────────────
    token = A.make_cert_token(TEST_SLUG, TEST_NAME)
    # Tokens now carry a value map; a bare name is stored as the "name" field.
    r.check("token round trips", A.read_cert_token(token) == (None, TEST_SLUG, {"name": TEST_NAME}))
    r.check("tampered token rejected", A.read_cert_token(token[:-2] + "xy") is None)
    r.check("garbage token rejected", A.read_cert_token("not-a-token") is None)

    # ── rendering and cache invalidation ─────────────────────────────────────
    rendered = A.render_certificate(TEST_SLUG, TEST_NAME, config)
    r.check("download render is a PNG",
            rendered is not None and rendered[0][:8] == b"\x89PNG\r\n\x1a\n")
    r.check("download render reports image/png", rendered[2] == "image/png", rendered[2])
    etag = rendered[1]
    r.check("repeat render hits the cache", A.render_certificate(TEST_SLUG, TEST_NAME, config)[1] == etag)

    moved = dict(config, text_y=config["text_y"] + 25)
    r.check("moving the text changes the etag",
            A.render_certificate(TEST_SLUG, TEST_NAME, moved)[1] != etag)
    restyled = dict(config, font_size=config["font_size"] + 10)
    r.check("resizing the font changes the etag",
            A.render_certificate(TEST_SLUG, TEST_NAME, restyled)[1] != etag)
    r.check("a different name changes the etag",
            A.render_certificate(TEST_SLUG, "Grace Hopper", config)[1] != etag)

    # ── preview is cheap, download is the real artifact ──────────────────────
    preview = A.render_certificate(TEST_SLUG, TEST_NAME, config, variant="preview")
    download = A.render_certificate(TEST_SLUG, TEST_NAME, config, variant="download")
    r.check("preview and download have separate cache entries", preview[1] != download[1])
    r.check("preview is JPEG", preview[2] == "image/jpeg", preview[2])
    preview_image = Image.open(io.BytesIO(preview[0]))
    download_image = Image.open(io.BytesIO(download[0]))
    r.check("preview is downscaled to the preview width",
            preview_image.width == A.PREVIEW_MAX_WIDTH, preview_image.size)
    r.check("preview carries fewer pixels than the download",
            preview_image.width < download_image.width,
            (preview_image.size, download_image.size))
    r.check("download keeps full resolution",
            download_image.size == TEMPLATE_SIZE, download_image.size)
    r.check("an opaque template decodes as RGB, not RGBA",
            A.get_template_image(TEST_SLUG, config).mode == "RGB",
            A.get_template_image(TEST_SLUG, config).mode)
    r.check("a template with real alpha keeps it",
            A.decode_template(make_template_bytes(mode="RGBA")).mode == "RGBA")

    # ── full participant journey ─────────────────────────────────────────────
    response = post(client, f"/events/{TEST_SLUG}/download",
                    {"registration_name": TEST_EMAIL, "cert_name": TEST_NAME})
    r.check("valid submission redirects", response.status_code == 302, response.status_code)
    location = response.headers.get("Location", "")
    r.check("redirect points at /preview/", "/preview/" in location, location)

    link_token = location.rsplit("/", 1)[-1]
    page = client.get(f"/preview/{link_token}")
    r.check("preview page renders", page.status_code == 200, page.status_code)
    r.check("preview page shows the name", TEST_NAME.encode() in page.data)

    img_response = client.get(f"/preview-image/{link_token}")
    r.check("preview image renders",
            img_response.status_code == 200 and img_response.data[:3] == JPEG_MAGIC,
            img_response.status_code)
    r.check("preview image is served as JPEG",
            img_response.headers.get("Content-Type") == "image/jpeg",
            img_response.headers.get("Content-Type"))
    cache_control = img_response.headers.get("Cache-Control", "")
    r.check("preview image is private", "private" in cache_control, cache_control)
    r.check("preview image is revalidated, not immutable", "immutable" not in cache_control, cache_control)

    download = client.get(f"/download-file/{link_token}")
    r.check("download returns a PNG",
            download.status_code == 200 and download.data[:8] == b"\x89PNG\r\n\x1a\n",
            download.status_code)
    r.check("download is named after the participant",
            "Ada-Lovelace.png" in download.headers.get("Content-Disposition", ""),
            download.headers.get("Content-Disposition"))

    # ── the flow is stateless ────────────────────────────────────────────────
    before = set(os.listdir(A.GENERATED_DIR))
    post(client, f"/events/{TEST_SLUG}/download",
         {"registration_name": TEST_EMAIL, "cert_name": "Someone Else"})
    after = set(os.listdir(A.GENERATED_DIR))
    r.check("a certificate request writes nothing to disk", before == after, after - before)

    # ── rejections ───────────────────────────────────────────────────────────
    bad = post(client, f"/events/{TEST_SLUG}/download",
               {"registration_name": "nobody@example.com", "cert_name": "X"})
    r.check("unknown participant is rejected", bad.status_code == 400, bad.status_code)

    missing = post(client, f"/events/{TEST_SLUG}/download",
                   {"registration_name": TEST_EMAIL, "cert_name": ""})
    r.check("missing printed name is rejected", missing.status_code == 400, missing.status_code)

    r.check("unknown legacy cert id 404s",
            client.get("/preview-image/deadbeefdeadbeefdeadbeefdeadbeef").status_code == 404)
    r.check("bad token redirects home", client.get("/preview/nonsense").status_code == 302)

    # ── upload validation helpers ────────────────────────────────────────────
    r.check("non-image rejected", A.validate_template_upload(b"hello world", ".png") is not None)
    r.check("real png accepted", A.validate_template_upload(make_template_bytes(), ".png") is None)
    r.check("extension mismatch rejected", A.validate_template_upload(make_template_bytes(), ".webp") is not None)

    # ── admin surface still works ────────────────────────────────────────────
    r.check("admin requires login", A.app.test_client().get("/admin").status_code == 302)
    admin = admin_client()
    # /admin is the clubs dashboard now; the legacy event manager moved to /admin/legacy-events.
    r.check("clubs dashboard renders", admin.get("/admin").status_code == 200)
    r.check("legacy events dashboard renders", admin.get("/admin/legacy-events").status_code == 200)
    r.check("admin event editor renders", admin.get(f"/admin/events/{TEST_SLUG}").status_code == 200)
    r.check("admin template preview serves",
            admin.get(f"/admin/events/{TEST_SLUG}/template-preview").status_code == 200)
    render_preview = admin.get(f"/admin/events/{TEST_SLUG}/render-preview?cert_name=Test")
    r.check("admin render preview serves",
            render_preview.status_code == 200 and render_preview.data[:3] == JPEG_MAGIC,
            render_preview.status_code)
    r.check("admin logs render", admin.get("/admin/logs").status_code == 200)
    r.check("dashboard names the real validation type",
            b"Email" in admin.get("/admin/legacy-events").data
            and b"Name Only" not in admin.get("/admin/legacy-events").data,
            "email event should not be labelled 'Name Only'")
    r.check("every validation type has a label",
            set(A.VALIDATION_TYPE_LABELS) == A.VALIDATION_TYPES,
            set(A.VALIDATION_TYPES) - set(A.VALIDATION_TYPE_LABELS))

    # ── the CSV is fetched once per page, not once per column ────────────────
    A._EVENT_CSV_CACHE.clear()
    reads = []
    original_read = A._read_event_csv_from_file
    A._read_event_csv_from_file = lambda slug, club_slug=None: (reads.append(slug), original_read(slug, club_slug))[1]
    try:
        client.get(f"/events/{TEST_SLUG}")
        post(client, f"/events/{TEST_SLUG}/download",
             {"registration_name": TEST_EMAIL, "cert_name": TEST_NAME})
        r.check("a page view plus a submission reads the CSV once", len(reads) == 1, reads)
    finally:
        A._read_event_csv_from_file = original_read

    A._EVENT_CSV_CACHE.clear()
    A.save_event_csv(TEST_SLUG, "name,email\nNew Person,new@example.com\n")
    r.check("saving a CSV invalidates the cache",
            "New Person" in (A.load_event_csv_text(TEST_SLUG) or ""))
finally:
    teardown_scratch(scratch)

sys.exit(r.finish())
