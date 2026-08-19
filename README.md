# Certificate Generator

A Flask application for event-based certificate distribution.

Participants open an event page, validate their details against a CSV list, enter the name to print, preview the generated certificate, and download it as PNG.

> **📖 Complete Documentation:** For detailed instructions on setting up events, using the Admin Dashboard, sending bulk emails, and understanding the directory structure, please read the [Comprehensive Guide](guide.md).

## Features

- Multiple events with independent settings and participant lists
- Public event listing page
- Event-specific certificate download flow
- Validation modes:
  - player + team
  - name only
  - email
  - roll/badge id
  - custom fields
  - no validation
- Server-side certificate rendering with Pillow
- Font support with a shared font asset route
- Supabase Storage for templates and KV-backed persistence, with local file fallback
- Stateless certificate links: nothing is written to disk per request

## Tech Stack

- Python 3.11
- Flask 3
- Pillow
- Gunicorn

## Project Structure

- app.py: Main Flask app, routing, validation, rendering
- manage.py: CLI for bulk generation, bulk email, and CSV splitting
- templates/: HTML templates for public pages
- static/style.css: Shared styling
- fonts/: TTF files used for text rendering
- tests/: Runnable checks for the certificate flow and template uploads
- events/: Local event folders (template, CSV, config) - the fallback store
- generated_certificates/: Bulk export output from manage.py

## Requirements

- Python 3.11+
- pip

Install dependencies:

```bash
pip install -r requirements.txt
```

## Run Locally

Development:

```bash
python app.py
```

Production-like local run:

```bash
gunicorn app:app --bind 0.0.0.0:8000 --workers 1 --threads 2 --timeout 120
```

Then open:

- Home page: / 
- Event page: /events/<slug>

## Environment Variables

Required in any deployed environment:

- ADMIN_PASSWORD: Password for the organizer interface. Without it, admin login is disabled.
- SECRET_KEY: Signs admin sessions **and certificate links**. It must be the same
  value across every worker and across restarts. If it is unset the app generates a
  random key per process, logs a warning, and outstanding certificate links break on
  every restart.

Storage (optional, but required for a real deployment):

- SUPABASE_URL: Supabase project URL
- SUPABASE_SERVICE_KEY: Supabase service_role key
- SUPABASE_BUCKET: Club bucket holding every event folder (default: csi-aseb)
- KV_REST_API_URL: KV REST base URL
- KV_REST_API_TOKEN: KV auth token
- KV_EVENT_STATE_KEY / KV_EVENT_INDEX_KEY / KV_EVENT_CONFIG_PREFIX / KV_EVENT_CSV_PREFIX:
  Optional key names, all with sensible defaults

Tuning:

- SESSION_COOKIE_SECURE: Set to "true" when serving over HTTPS
- TEMPLATE_CACHE_MAX / RENDER_CACHE_MAX: In-memory cache sizes per worker (default 6 each)
- PREVIEW_MAX_WIDTH: Width of the on-screen preview image in px (default 1200)
- PREVIEW_JPEG_QUALITY: JPEG quality for the preview (default 90)
- DOWNLOAD_PNG_COMPRESS_LEVEL: PNG compression for downloads, 1-9 (default 3)

If the storage variables are not set, the app falls back to local files under the
runtime writable directory. That is fine locally and wrong on any host with an
ephemeral filesystem.

## Storage Model

Nothing that matters lives on the web server's disk:

| Data | Primary store | Fallback |
| --- | --- | --- |
| Certificate templates | Supabase Storage | Local `events/<slug>/template.*` |
| Participant lists | Supabase Storage | KV (legacy), then local `events/<slug>/data.csv` |
| Event configs | KV | Local `events/<slug>/config.json` |
| Generated certificates | Not stored - rendered on demand | n/a |

### Bucket layout

One bucket for the club, one folder per event:

```text
csi-aseb/
├── hackathon-2026/
│   ├── participants/
│   │   ├── data.csv          # what the app reads
│   │   └── source.xlsx       # the original upload, when it was a workbook
│   └── template/
│       └── template.png
└── intro-to-git/
    ├── participants/
    │   └── data.csv
    └── template/
        └── template.jpg
```

Supabase renders key prefixes as folders, so this is browsable in the dashboard.
Participant lists used to live in KV; KV is still read for events uploaded before
the move, but new uploads go to the bucket. That also removes an old limit: KV
writes put the whole value in the URL, so a few thousand rows would fail with a
414. Bucket uploads send the file as a request body.

A certificate link is a signed token containing the event slug and the printed
name, not a pointer to a saved file. Any worker can serve any link, links survive
redeploys, and no cleanup job is needed. Tokens are signed with SECRET_KEY, so
they cannot be forged or enumerated.

Uploading a template records a `template_version` on the event config. Every image
cache key includes that version, which is what makes a replaced template take
effect immediately instead of being served stale from memory.

### Supabase setup

1. Create a Supabase project.
2. Copy the project URL and the `service_role` key from Project Settings -> API.
3. Set `SUPABASE_URL` and `SUPABASE_SERVICE_KEY`.

The private bucket is created automatically on the first template upload. The
service key is server-side only and must never reach the browser.

Free Supabase projects pause after roughly a week of inactivity. Point the
keep-alive cron at `/healthz` rather than `/`: that endpoint deliberately makes a
Supabase request, so it keeps both the web service and the storage project awake.

## Tests

No test framework is required; each file runs on its own against a temporary
events directory with no network access.

```bash
python tests/test_certificate_flow.py
python tests/test_template_upload.py
python tests/test_supabase_storage.py
python tests/test_csrf.py
python tests/test_email_templates.py
python tests/test_participant_upload.py
```

## CSRF Protection

Every request using an unsafe method (POST, PUT, PATCH, DELETE) must carry a token
tied to the caller's session, submitted either as a `csrf_token` form field or an
`X-CSRF-Token` header. Enforcement lives in a `before_request` hook and is
**fail-closed by default**: a route added later is protected without anyone having
to remember to protect it. Use the `@csrf_exempt` decorator to opt a route out;
nothing does today.

Templates render the field with `{{ csrf_token() }}`. The admin autosave posts
`new FormData(form)`, so it picks the field up with no extra JavaScript.

Logging in clears the session and mints a fresh token, so a token observed before
login cannot be replayed against admin routes. Rejected requests get the
`csrf_error.html` page, or a JSON error for `X-Requested-With: XMLHttpRequest`
callers. Session cookies are also `SameSite=Lax`, which blocks the cross-site POST
independently in modern browsers.

## Public Flow

1. User opens home page /
2. User selects an active event
3. User submits required validation input(s)
4. User provides name to print
5. App validates and redirects to a signed preview link
6. The image is rendered on demand and the user downloads the PNG

## Organizer Interface

Alongside the public participant flow, the application also includes an authenticated organizer interface.

Future club organizers can use this interface to manage events and certificate settings without modifying the codebase.

Access location and credentials are intentionally not documented in this README and should be shared only through your club's internal handover process.

## Event Creation by Filesystem

You can define events directly in the events directory.

Create:

- events/<slug>/config.json
- events/<slug>/template.png (or template.jpg, template.jpeg, template.gif, template.webp)
- events/<slug>/data.csv

### config.json example

```json
{
  "name": "Think, Run, Debug Hackathon", # name of the event
  "slug": "think-run-debug", # this is the url for the ppl to access later on
  "active": true,
  "validation_type": "player_team",
  "custom_fields": [],
  "custom_dropdown_fields": [],
  "text_x": 1789,
  "text_y": 1440,
  "font_size": 100,
  "font_color": [50, 34, 24],
  "font_key": "montserrat_bold"
}
```

Notes:

- slug must be lowercase letters, numbers, and hyphens only
- text_x and text_y are center coordinates for certificate text
- font_color is RGB list [r, g, b]

## Participant Lists

Upload a `.csv` or an Excel `.xlsx`. A workbook is read from its **first sheet**
and converted to CSV once at upload time, so validation, dropdown suggestions and
bulk generation all stay CSV-only. The header row sets the column count; blank
rows are dropped, and ragged rows are padded or trimmed to match. The original
workbook is kept next to the derived CSV in the bucket.

CSV exported from Excel is read as `utf-8-sig`, so the byte-order mark Excel
writes does not end up glued to the first column name.

## Email Templates

The Send Emails form accepts `{participant_name}` and `{event_name}` in the subject
and both bodies. Substitution is a plain placeholder replacement, not `str.format()`:
unknown placeholders and stray braces (a CSS rule in an HTML body, say) are left
alone rather than raising, and `{event_name.__class__...}` style attribute traversal
is not evaluated. Values are HTML-escaped in the HTML part only.

## Validation Types and CSV Rules

All matching is case-insensitive and trimmed.

- player_team:
  - CSV must contain: player, team
- name_only:
  - CSV must contain: name
- email:
  - CSV must contain: email
- roll_no:
  - CSV must contain at least one: roll_no, id, badge_id, badge_number
- custom:
  - User can choose the validation fields according to the CSV uploaded
- none:
  - No participant lookup required

### CSV examples

player_team:

```csv
player,team
aneesh,alpha
sara,beta
```

name_only:

```csv
name
Aneesh Sagar Reddy
Sara Arjun
```

email:

```csv
email
aneesh@example.com
sara@example.com
```

badge_id:

```csv
roll_no,name
BL.SC.U4AIE12345,Aneesh Sagar Reddy
BL.SC.U4CSE12345,Sara Arjun
```

custom:

```csv
department,employee_id,name
engineering,E102,Aneesh Sagar Reddy
design,D008,Sara Arjun
```

## Public Routes

- GET / : List active events
- GET /events/<slug> : Event certificate form
- POST /events/<slug>/download : Validate + generate certificate
- GET /preview/<token> : Preview page
- GET /preview-image/<token> : Rendered certificate image
- GET /download-file/<token> : Download final PNG
- GET /assets/fonts/<font_key>.ttf : Font asset
- GET /healthz : Liveness check; also pings storage to keep it awake

## Performance Notes

Certificate rendering happens on the GET that serves the image, not on the form
POST, so submitting the form stays fast even under event-day traffic spikes.

Encoding, not drawing, is what costs. On a typical A4 300 DPI template (3508x2480,
8.7 MP), drawing the participant's name takes about 4 ms while encoding the image
takes hundreds. Two consequences shape the render path:

- **The preview is downscaled and sent as JPEG.** Nobody views 3508 px in an `<img>`
  tag, and the full-size PNG was ~2.5 MB per view.
- **Downloads keep full resolution** but drop the pointless alpha channel and use a
  lighter PNG compression level. The file ends up slightly *smaller* than before.

Measured on the bundled A4 template:

| Path | Before | After |
| --- | --- | --- |
| Preview image | 274 ms, 2.5 MB | **41 ms, 154 KB** |
| Download | 389 ms, 2.76 MB | **213 ms, 2.65 MB** |

Participant CSVs are cached for 30 seconds as well. One page view used to re-fetch
the CSV once per dropdown column plus again during validation; a custom-validation
event with three dropdowns went from 4 fetches per submission to 1.

Templates are held as RGB unless the file genuinely has alpha, which is 25% less
memory per cached image. Bounded LRU caches cover event configs, decoded templates,
loaded fonts, and rendered output; raise TEMPLATE_CACHE_MAX and RENDER_CACHE_MAX if
the instance has memory to spare.

Rendering with OpenCV was evaluated and rejected: `cv2.putText` supports only
Hershey stroke fonts, so it cannot draw Montserrat or any other TrueType face, and
it would only have touched the ~1% of render time spent drawing text.

## Troubleshooting

### 404 when opening an event

Use /events/<slug>, not /<slug>.

Correct example:

- /events/introduction-to-git-and-github

### Event does not appear on home page

Common causes:

- active is false in config.json
- Event is marked deleted in runtime event state
- Missing or invalid config.json for that slug

### Validation always fails

Check:

- validation_type matches CSV headers
- Required headers exist exactly (case-insensitive is fine)
- Submitted values exist in CSV rows

### Certificate text position looks wrong

Adjust text_x, text_y, and font_size in config.json, then retry preview.

## Advanced Features

### Reusable Layout Profiles

Instead of defining `text_x`, `text_y`, etc., for every single event, you can use shared profiles.
Create a `profiles.yaml` file in the root directory:

```yaml
profiles:
  workshop:
    text_x: 1500
    text_y: 1000
    font_size: 90
    font_color: [0, 0, 0]
    font_key: montserrat_bold
```

Then in an event's `config.json`, simply add `"profile": "workshop"`. The event will inherit these values automatically, though local `config.json` values always take precedence.

### Command Line Interface (CLI)

For bulk operations and administrative tasks, use `manage.py`.

#### Split Large CSVs
If your participant CSV is too large, you can chunk it:
```bash
python manage.py split-csv data.csv --chunk-size 100 --output-dir splits/
```

#### Bulk Generation
If you want to generate all certificates for an event at once (for physical printing or backup), use:
```bash
python manage.py bulk-generate <event_slug>
```
Certificates will be saved to `generated_certificates/<event_slug>/exports/`.

#### Bulk Email Delivery
You can email certificates directly to all participants using SMTP. First, configure your `.env`:
```env
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your_email@gmail.com
SMTP_PASS=your_app_password
SMTP_FROM=your_email@gmail.com
SMTP_FROM_NAME=Certificate Generator
SMTP_STARTTLS=true
```

Then run the emailer command (it will generate and send in parallel):
```bash
python manage.py send-emails <event_slug>
```

## Deployment Notes

The repository includes process and platform config files for WSGI deployment.

Recommended production run command:

```bash
gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --worker-class gthread --timeout 120
```

Multiple workers are safe now that templates live in Supabase and certificates are
rendered on demand, but only if SECRET_KEY is set to a fixed value: every worker
must sign certificate links with the same key.

## License

No license file is included in this repository yet.
If needed, add one (for example MIT) before public distribution.
