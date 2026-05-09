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
- Optional KV-backed persistence with local file fallback

## Tech Stack

- Python 3.11
- Flask 3
- Pillow
- Gunicorn

## Project Structure

- app.py: Main Flask app, routing, validation, rendering
- templates/: HTML templates for public pages
- static/style.css: Shared styling
- fonts/: TTF files used for text rendering
- events/: Event folders (template, CSV, config)
- generated_certificates/: Runtime artifacts (generated PNG + metadata)

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

- SECRET_KEY: Flask session secret (recommended in non-local environments)
- KV_REST_API_URL: Optional KV REST base URL
- KV_REST_API_TOKEN: Optional KV auth token
- KV_EVENT_STATE_KEY: Optional key for event state map
- KV_EVENT_INDEX_KEY: Optional key for event index list
- KV_EVENT_CONFIG_PREFIX: Optional prefix for event config keys
- KV_EVENT_CSV_PREFIX: Optional prefix for event CSV keys

If KV variables are not set, the app uses local files under the runtime writable directory.

## Public Flow

1. User opens home page /
2. User selects an active event
3. User submits required validation input(s)
4. User provides name to print
5. App generates a certificate image and redirects to preview
6. User downloads the certificate PNG

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
- GET /preview/<cert_id> : Preview page
- GET /preview-image/<cert_id> : Rendered certificate image
- GET /download-file/<cert_id> : Download final PNG
- GET /assets/fonts/<font_key>.ttf : Font asset

## Performance Notes

The app includes in-memory caches for:

- Event configs
- Template images
- Loaded fonts
- Rendered certificate previews

This reduces repeated disk and KV calls for frequent preview/download traffic.

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
gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 120
```

## License

No license file is included in this repository yet.
If needed, add one (for example MIT) before public distribution.
