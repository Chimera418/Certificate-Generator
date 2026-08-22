"""
Template upload and cache invalidation.

Regression guard for the bug where the decoded template was cached forever under
the event slug, so replacing a template kept rendering the old image until the
process restarted. Render keys now embed a template version recorded on the config.

Run with: python tests/test_template_upload.py
"""
import io
import sys

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

NEW_SIZE = (1200, 800)
NEW_COLOR = (12, 200, 90)

r = Results()
scratch = setup_scratch_event()

try:
    admin = admin_client()

    config = A.load_event(TEST_SLUG)
    version_before = config.get("template_version")
    etag_before = A.render_certificate(TEST_SLUG, TEST_NAME, config)[1]

    # Warm every cache the way real traffic would before the replacement.
    admin.get(f"/admin/events/{TEST_SLUG}/template-preview")
    admin.get(f"/preview-image/{A.make_cert_token(TEST_SLUG, TEST_NAME)}")

    upload = io.BytesIO(make_template_bytes(NEW_SIZE, NEW_COLOR))
    response = post(admin, f"/admin/events/{TEST_SLUG}/upload-template",
        {"template_file": (upload, "replacement.png")},
        content_type="multipart/form-data",
    )
    r.check("upload returns 200", response.status_code == 200, response.status_code)
    r.check("upload reports success", b"Template uploaded successfully" in response.data)

    config_after = A.load_event(TEST_SLUG)
    r.check("config records the extension", config_after.get("template_ext") == ".png",
            config_after.get("template_ext"))
    r.check("config records a version", bool(config_after.get("template_version")))
    r.check("version changed", config_after.get("template_version") != version_before,
            (version_before, config_after.get("template_version")))

    # The upload pre-decodes the template into the image cache (from the bytes it
    # already holds), so the first certificate render skips the fetch + decode. This
    # asserts it is warm before any render forces a decode.
    warmed_key = A._template_cache_key(TEST_SLUG, config_after.get("template_version"))
    r.check("upload pre-warms the decoded-template cache",
            A._TEMPLATE_IMAGE_CACHE.get(warmed_key) is not None)
    r.check("config cache hands out copies, not shared references",
            A.load_event(TEST_SLUG) is not config)

    current = A.get_template_image(TEST_SLUG, config_after)
    r.check("decoded template reflects the new upload", current.size == NEW_SIZE, current.size)
    r.check("decoded template has the new colour",
            current.convert("RGB").getpixel((10, 10)) == NEW_COLOR,
            current.convert("RGB").getpixel((10, 10)))

    r.check("render etag changed after upload",
            A.render_certificate(TEST_SLUG, TEST_NAME, config_after)[1] != etag_before)

    served = admin.get(f"/preview-image/{A.make_cert_token(TEST_SLUG, TEST_NAME)}")
    r.check("served certificate uses the new template", served.status_code == 200, served.status_code)
    r.check("served certificate has the new dimensions",
            Image.open(io.BytesIO(served.data)).size == NEW_SIZE,
            Image.open(io.BytesIO(served.data)).size)

    # ── rejections ───────────────────────────────────────────────────────────
    bogus = post(admin, f"/admin/events/{TEST_SLUG}/upload-template",
                       {"template_file": (io.BytesIO(b"definitely not a png"), "fake.png")},
                       content_type="multipart/form-data")
    r.check("content that is not an image is rejected", bogus.status_code == 400, bogus.status_code)

    wrong_ext = post(admin, f"/admin/events/{TEST_SLUG}/upload-template",
                           {"template_file": (io.BytesIO(b"whatever"), "notes.txt")},
                           content_type="multipart/form-data")
    r.check("a non-image extension is rejected", wrong_ext.status_code == 400, wrong_ext.status_code)

    empty = post(admin, f"/admin/events/{TEST_SLUG}/upload-template",
                       {"template_file": (io.BytesIO(b""), "")},
                       content_type="multipart/form-data")
    r.check("an empty upload is rejected", empty.status_code == 400, empty.status_code)

    r.check("the good template survived the rejected uploads",
            A.get_template_image(TEST_SLUG, A.load_event(TEST_SLUG)).size == NEW_SIZE)
finally:
    teardown_scratch(scratch)

sys.exit(r.finish())
