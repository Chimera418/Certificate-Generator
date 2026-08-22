"""
Phase 0 capacity work: download format, byte-capped template cache, render slots.

The load-bearing guarantee here is that an event with no `download_format` renders
byte-identically to how it did before the option existed. Everything else is new
behaviour that has to be opted into, per event, by an admin.

Run with: python tests/test_render_capacity.py
"""
import io
import sys
import threading
import time

from PIL import Image

from _fixture import (
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

r = Results()
scratch = setup_scratch_event()

# Restored in `finally`, so a failure part-way through does not leave the module
# globals resized for whatever suite runs next in the same process.
ORIGINAL_SLOTS = (A.RENDER_MAX_CONCURRENCY, A.RENDER_MAX_CONCURRENCY_PER_TENANT)
ORIGINAL_TIMEOUT = A.RENDER_QUEUE_TIMEOUT_SEC
ORIGINAL_CACHE = A._TEMPLATE_IMAGE_CACHE


def set_format(value):
    """Write download_format straight onto the stored config, bypassing the form."""
    config = A.load_event(TEST_SLUG)
    if value is None:
        config.pop("download_format", None)
    else:
        config["download_format"] = value
    A.save_event_config(TEST_SLUG, config)
    return A.load_event(TEST_SLUG)


try:
    # ── absent download_format is byte-identical to the old PNG path ─────────
    config = set_format(None)
    r.check("a legacy config has no download_format", "download_format" not in config)

    png_bytes, png_etag, png_mime = A.render_certificate(TEST_SLUG, TEST_NAME, config)
    r.check("absent format encodes PNG", png_mime == "image/png", png_mime)
    r.check("absent format emits PNG magic bytes", png_bytes[:8] == b"\x89PNG\r\n\x1a\n")

    # The pre-change code path, reproduced exactly: draw, then save as PNG at the
    # configured compress level. If these differ, "byte-identical" has been broken.
    reference = A.get_template_image(TEST_SLUG, config)
    metadata = dict(A.certificate_render_settings(config))
    metadata["cert_name"] = TEST_NAME
    A.draw_name_on_image(reference, metadata)
    buf = io.BytesIO()
    reference.save(buf, format="PNG", compress_level=A.DOWNLOAD_PNG_COMPRESS_LEVEL)
    r.check("absent format is byte-identical to the pre-change encode",
            png_bytes == buf.getvalue(),
            f"{len(png_bytes)} vs {len(buf.getvalue())} bytes")

    # ── opting in to JPEG ───────────────────────────────────────────────────
    A._RENDERED_CERT_CACHE.clear()
    config = set_format("jpeg")
    jpeg_bytes, jpeg_etag, jpeg_mime = A.render_certificate(TEST_SLUG, TEST_NAME, config)
    r.check("jpeg format encodes JPEG", jpeg_mime == "image/jpeg", jpeg_mime)
    r.check("jpeg format emits JPEG magic bytes", jpeg_bytes[:3] == b"\xff\xd8\xff")
    # Deliberately NOT asserted against the fixture template: it is a flat fill,
    # which PNG compresses to almost nothing and JPEG cannot beat. Real certificate
    # artwork is detailed, and that is where the plan's 3.5x saving lives - so the
    # comparison is made against a detailed template further down.
    r.check("jpeg still decodes at full template size",
            Image.open(io.BytesIO(jpeg_bytes)).size == reference.size,
            Image.open(io.BytesIO(jpeg_bytes)).size)
    r.check("switching format changes the etag", jpeg_etag != png_etag)

    # ── the size claim, on a template that resembles real artwork ───────────
    import random

    rng = random.Random(20260821)
    detailed = Image.frombytes(
        "RGB", (900, 600), bytes(rng.randrange(256) for _ in range(900 * 600 * 3)))
    buf = io.BytesIO()
    detailed.save(buf, format="PNG", compress_level=A.DOWNLOAD_PNG_COMPRESS_LEVEL)
    detailed_png = len(buf.getvalue())
    detailed_jpeg = len(A.encode_certificate(detailed.copy(), "download", "jpeg")[0])
    r.check("on a detailed template jpeg is the smaller encode",
            detailed_jpeg < detailed_png,
            f"jpeg {detailed_jpeg} vs png {detailed_png}")

    # ── preview is unaffected by the download format ────────────────────────
    A._RENDERED_CERT_CACHE.clear()
    preview_jpeg = A.render_certificate(TEST_SLUG, TEST_NAME, config, variant="preview")
    A._RENDERED_CERT_CACHE.clear()
    preview_png = A.render_certificate(TEST_SLUG, TEST_NAME, set_format(None), variant="preview")
    r.check("preview etag ignores the download format", preview_jpeg[1] == preview_png[1])
    r.check("preview bytes ignore the download format", preview_jpeg[0] == preview_png[0])

    # ── alpha overrides the event's choice ──────────────────────────────────
    alpha_slug = "alpha-event"
    import os

    alpha_dir = os.path.join(A.EVENTS_DIR, alpha_slug)
    os.makedirs(alpha_dir, exist_ok=True)
    with open(os.path.join(alpha_dir, "template.png"), "wb") as f:
        f.write(make_template_bytes(mode="RGBA"))
    A.save_event_config(alpha_slug, {
        "name": "Alpha Event", "slug": alpha_slug, "active": True,
        "validation_type": "none", "custom_fields": [], "custom_dropdown_fields": [],
        "text_x": 800, "text_y": 550, "font_size": 64,
        "font_color": [50, 34, 24], "font_key": "montserrat_bold",
        "download_format": "jpeg",
    })
    alpha_config = A.load_event(alpha_slug)
    alpha_image = A.get_template_image(alpha_slug, alpha_config)
    r.check("the alpha template really has alpha", alpha_image.mode == "RGBA", alpha_image.mode)
    alpha_bytes, _, alpha_mime = A.render_certificate(alpha_slug, TEST_NAME, alpha_config)
    r.check("an alpha template stays PNG despite asking for jpeg",
            alpha_mime == "image/png", alpha_mime)
    r.check("the alpha template's transparency survives",
            Image.open(io.BytesIO(alpha_bytes)).mode == "RGBA",
            Image.open(io.BytesIO(alpha_bytes)).mode)

    # ── download filename follows what was encoded ──────────────────────────
    r.check("png download is named .png",
            A.safe_download_name(TEST_NAME, TEST_SLUG, "image/png").endswith(".png"))
    r.check("jpeg download is named .jpg",
            A.safe_download_name(TEST_NAME, TEST_SLUG, "image/jpeg").endswith(".jpg"))
    r.check("an unknown mimetype falls back to .png",
            A.safe_download_name(TEST_NAME, TEST_SLUG, "image/tiff").endswith(".png"))
    r.check("the default argument keeps the old .png behaviour",
            A.safe_download_name(TEST_NAME, TEST_SLUG) == "Ada-Lovelace.png",
            A.safe_download_name(TEST_NAME, TEST_SLUG))

    # ── format normalization ────────────────────────────────────────────────
    r.check("'jpg' normalizes to jpeg", A.normalize_download_format("jpg") == "jpeg")
    r.check("'JPEG' normalizes case-insensitively", A.normalize_download_format(" JPEG ") == "jpeg")
    r.check("garbage falls back to the default",
            A.normalize_download_format("webp") == A.DEFAULT_DOWNLOAD_FORMAT)
    r.check("None falls back to the default",
            A.normalize_download_format(None) == A.DEFAULT_DOWNLOAD_FORMAT)
    r.check("garbage honours an explicit fallback",
            A.normalize_download_format("webp", "jpeg") == "jpeg")
    r.check("a garbage fallback is itself rejected",
            A.normalize_download_format("webp", "gif") == A.DEFAULT_DOWNLOAD_FORMAT)

    # ── the served routes ───────────────────────────────────────────────────
    set_format("jpeg")
    A._RENDERED_CERT_CACHE.clear()
    client = A.app.test_client()
    served = client.get(f"/download-file/{A.make_cert_token(TEST_SLUG, TEST_NAME)}")
    r.check("download route returns 200", served.status_code == 200, served.status_code)
    r.check("download route serves image/jpeg", served.mimetype == "image/jpeg", served.mimetype)
    r.check("download route names the file .jpg",
            ".jpg" in served.headers.get("Content-Disposition", ""),
            served.headers.get("Content-Disposition"))

    set_format(None)
    A._RENDERED_CERT_CACHE.clear()
    served = client.get(f"/download-file/{A.make_cert_token(TEST_SLUG, TEST_NAME)}")
    r.check("a legacy event still downloads image/png", served.mimetype == "image/png", served.mimetype)
    r.check("a legacy event still names the file .png",
            ".png" in served.headers.get("Content-Disposition", ""),
            served.headers.get("Content-Disposition"))

    # ── admin form round-trip ───────────────────────────────────────────────
    admin = admin_client()
    response = post(admin, f"/admin/events/{TEST_SLUG}/config", {
        "name": "Test Event", "validation_type": "email",
        "text_x": "800", "text_y": "550", "font_size": "64",
        "font_color": "#321812", "font_key": "montserrat_bold",
        "download_format": "jpeg",
    })
    r.check("saving settings returns 200", response.status_code == 200, response.status_code)
    r.check("the admin form persists the format",
            A.load_event(TEST_SLUG).get("download_format") == "jpeg",
            A.load_event(TEST_SLUG).get("download_format"))

    response = post(admin, f"/admin/events/{TEST_SLUG}/config", {
        "name": "Test Event", "validation_type": "email",
        "text_x": "800", "text_y": "550", "font_size": "64",
        "font_color": "#321812", "font_key": "montserrat_bold",
        "download_format": "not-a-format",
    })
    r.check("an invalid submitted format falls back to the stored one",
            A.load_event(TEST_SLUG).get("download_format") == "jpeg",
            A.load_event(TEST_SLUG).get("download_format"))

    # A form that omits the field entirely must not silently flip a JPEG event
    # back to PNG - the coordinate editor posts a subset of this form.
    response = post(admin, f"/admin/events/{TEST_SLUG}/config", {
        "name": "Test Event", "validation_type": "email",
        "text_x": "800", "text_y": "550", "font_size": "64",
        "font_color": "#321812", "font_key": "montserrat_bold",
    })
    r.check("omitting the field keeps the stored format",
            A.load_event(TEST_SLUG).get("download_format") == "jpeg",
            A.load_event(TEST_SLUG).get("download_format"))

    new_event = post(admin, "/admin/events/new", {
        "name": "Fresh Event", "slug": "fresh-event", "validation_type": "none",
        "text_x": "100", "text_y": "100", "font_size": "40",
        "font_color": "#321812", "font_key": "montserrat_bold",
    })
    r.check("creating an event redirects", new_event.status_code in (302, 303), new_event.status_code)
    r.check("a new event is created as jpeg",
            A.load_event("fresh-event").get("download_format") == "jpeg",
            A.load_event("fresh-event").get("download_format"))

    # ── byte-capped template cache ──────────────────────────────────────────
    cache = A._ByteCappedCache(max_bytes=1000, max_entries=100)
    cache.put("a", "va", 400)
    cache.put("b", "vb", 400)
    r.check("both entries fit under the byte cap", len(cache) == 2, len(cache))
    r.check("total bytes are tracked", cache.total_bytes == 800, cache.total_bytes)
    cache.get("a")  # 'a' is now the most recently used, so 'b' should go first
    cache.put("c", "vc", 400)
    r.check("the byte cap evicts", len(cache) == 2, len(cache))
    r.check("eviction is LRU, not insertion order",
            "a" in cache and "c" in cache and "b" not in cache,
            list(cache._entries))
    r.check("total bytes stay within the cap", cache.total_bytes <= 1000, cache.total_bytes)

    cache.put("a", "va2", 700)
    r.check("replacing a key does not double-count its bytes",
            cache.total_bytes <= 1000, cache.total_bytes)
    r.check("the replaced value is what comes back", cache.get("a") == "va2", cache.get("a"))

    oversized = A._ByteCappedCache(max_bytes=100, max_entries=10)
    oversized.put("big", "v", 500)
    r.check("an entry bigger than the whole budget is not retained", len(oversized) == 0, len(oversized))
    r.check("byte accounting stays at zero after that", oversized.total_bytes == 0, oversized.total_bytes)

    counted = A._ByteCappedCache(max_bytes=10 ** 9, max_entries=2)
    for key in ("a", "b", "c"):
        counted.put(key, key, 1)
    r.check("the entry cap still applies independently", len(counted) == 2, len(counted))
    r.check("the entry cap evicts the oldest", "a" not in counted and "c" in counted)

    counted.clear()
    r.check("clear() empties the cache", len(counted) == 0)
    r.check("clear() resets the byte counter", counted.total_bytes == 0, counted.total_bytes)

    # The real template cache holds the size it claims to.
    A._TEMPLATE_IMAGE_CACHE.clear()
    live_config = A.load_event(TEST_SLUG)
    A.get_template_image(TEST_SLUG, live_config)
    expected = A.decoded_image_bytes(A.get_template_image(TEST_SLUG, live_config))
    r.check("the template cache accounts for the decoded image",
            A._TEMPLATE_IMAGE_CACHE.total_bytes == expected,
            (A._TEMPLATE_IMAGE_CACHE.total_bytes, expected))

    # A budget too small for one template must not wedge rendering - it just
    # re-decodes every time.
    A._TEMPLATE_IMAGE_CACHE = A._ByteCappedCache(max_bytes=1024, max_entries=6)
    starved = A.render_certificate(TEST_SLUG, "Starved Cache", live_config)
    r.check("rendering still works with a cache too small to hold a template",
            starved is not None and len(starved[0]) > 0)
    r.check("the too-small cache holds nothing", len(A._TEMPLATE_IMAGE_CACHE) == 0,
            len(A._TEMPLATE_IMAGE_CACHE))
    A._TEMPLATE_IMAGE_CACHE = ORIGINAL_CACHE

    # ── render slots ────────────────────────────────────────────────────────
    A.configure_render_slots(max_concurrency=1, per_tenant=1)
    A.RENDER_QUEUE_TIMEOUT_SEC = 0

    # Warmed while slots are still free, so the cache-hit check below is testing
    # the cache rather than getting lucky with a leftover entry from an earlier
    # section - every config edit above changed the etag.
    slot_config = A.load_event(TEST_SLUG)
    warmed = A.render_certificate(TEST_SLUG, TEST_NAME, slot_config)
    r.check("the render cache is warm before the pool is exhausted", warmed is not None)

    held = threading.Event()
    release = threading.Event()

    def hold_a_slot():
        with A.render_slot(TEST_SLUG):
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold_a_slot, daemon=True)
    holder.start()
    r.check("a slot was taken", held.wait(5))

    try:
        with A.render_slot(TEST_SLUG):
            r.check("a second slot is refused when capacity is exhausted", False, "acquired")
    except A.RenderCapacityError:
        r.check("a second slot is refused when capacity is exhausted", True)

    # A cache hit must be served while every slot is busy, or a spike would 503
    # people whose certificate is already sitting in memory.
    cached_result = A.render_certificate(TEST_SLUG, TEST_NAME, slot_config)
    r.check("a cache hit is served without a slot",
            cached_result is not None and cached_result[1] == warmed[1])

    # And the route turns exhaustion into a 503, not a 404.
    busy = client.get(f"/download-file/{A.make_cert_token(TEST_SLUG, 'Uncached Person')}")
    r.check("an exhausted pool returns 503", busy.status_code == 503, busy.status_code)
    r.check("the 503 carries Retry-After", busy.headers.get("Retry-After") == "5",
            busy.headers.get("Retry-After"))

    missing = client.get(f"/download-file/{A.make_cert_token('no-such-event', TEST_NAME)}")
    r.check("a missing event is still a 404, not a 503", missing.status_code == 404, missing.status_code)

    release.set()
    holder.join(5)
    r.check("the slot was released", not holder.is_alive())

    # ── per-tenant fairness ─────────────────────────────────────────────────
    # Two global slots, one per tenant: a tenant saturating its own limit must
    # leave the other tenant's slot free.
    A.configure_render_slots(max_concurrency=2, per_tenant=1)
    A.RENDER_QUEUE_TIMEOUT_SEC = 0

    held = threading.Event()
    release = threading.Event()
    holder = threading.Thread(target=hold_a_slot, daemon=True)
    holder.start()
    r.check("tenant A holds its only slot", held.wait(5))

    try:
        with A.render_slot(TEST_SLUG):
            r.check("tenant A cannot exceed its per-tenant limit", False, "acquired")
    except A.RenderCapacityError:
        r.check("tenant A cannot exceed its per-tenant limit", True)

    try:
        with A.render_slot("some-other-tenant"):
            r.check("tenant B is not starved by tenant A", True)
    except A.RenderCapacityError:
        r.check("tenant B is not starved by tenant A", False, "refused")

    release.set()
    holder.join(5)

    # A queued request should wait and then succeed, rather than fail outright.
    A.configure_render_slots(max_concurrency=1, per_tenant=1)
    A.RENDER_QUEUE_TIMEOUT_SEC = 5
    held = threading.Event()
    release = threading.Event()
    holder = threading.Thread(target=hold_a_slot, daemon=True)
    holder.start()
    held.wait(5)
    threading.Timer(0.2, release.set).start()
    started = time.monotonic()
    try:
        with A.render_slot(TEST_SLUG):
            waited = time.monotonic() - started
        r.check("a request queues for a slot instead of failing", waited >= 0.15, f"{waited:.3f}s")
    except A.RenderCapacityError:
        r.check("a request queues for a slot instead of failing", False, "refused")
    holder.join(5)

    # ── the slot is released even when the render fails ─────────────────────
    A.configure_render_slots(max_concurrency=1, per_tenant=1)
    A.RENDER_QUEUE_TIMEOUT_SEC = 0
    broken = A.load_event(TEST_SLUG)
    broken["template_ext"] = ".png"
    broken["template_version"] = "does-not-exist"
    import os as _os

    _os.remove(_os.path.join(A.EVENTS_DIR, TEST_SLUG, "template.png"))
    A._TEMPLATE_IMAGE_CACHE.clear()
    failed = A.render_certificate(TEST_SLUG, "No Template Here", broken)
    r.check("a missing template returns None, not an exception", failed is None, failed)
    try:
        with A.render_slot(TEST_SLUG):
            r.check("the slot is released after a failed render", True)
    except A.RenderCapacityError:
        r.check("the slot is released after a failed render", False, "leaked")
finally:
    A._TEMPLATE_IMAGE_CACHE = ORIGINAL_CACHE
    A.RENDER_QUEUE_TIMEOUT_SEC = ORIGINAL_TIMEOUT
    A.configure_render_slots(max_concurrency=ORIGINAL_SLOTS[0], per_tenant=ORIGINAL_SLOTS[1])
    teardown_scratch(scratch)

sys.exit(r.finish())
