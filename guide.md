# Certificate Generator — Comprehensive Guide

Welcome to your unified Certificate Generator! This platform provides a self-serve web portal for participants to claim their certificates, alongside a powerful backend CLI for administrators to generate and email certificates in bulk.

---

## 1. Directory Structure & Setup

A typical project structure looks like this:

```text
certificate_generator/
├── app.py                  # The Flask Web Server
├── manage.py               # The Backend CLI Tool
├── profiles.yaml           # Reusable Layout Profiles
├── .env                    # Environment & SMTP Variables
├── events/                 # Your events live here!
│   └── demo/
│       ├── config.json     # Event configuration
│       ├── data.csv        # Participant records
│       └── template.png    # The certificate design
└── generated_certificates/ # Where bulk outputs are saved
```

### Environment Variables (`.env`)
Before using the tool in production, configure your `.env` file. You need this to protect your admin dashboard and send emails:

```env
ADMIN_PASSWORD=your_secure_password
SECRET_KEY=your_random_secret_string

# SMTP Email Configuration (Example for Outlook)
SMTP_HOST=smtp-mail.outlook.com
SMTP_PORT=587
SMTP_USER=your_email@bl.students.amrita.edu
SMTP_PASS=your_app_password
SMTP_FROM=your_email@bl.students.amrita.edu
SMTP_FROM_NAME=Certificate Generator
SMTP_STARTTLS=true
```

---

## 2. Managing Events

Every event gets its own folder inside `events/`. The name of the folder is the **slug** (e.g., `events/demo/` means the slug is `demo`).

Inside each event folder, you need 3 things:

1. **`template.png`**: The blank certificate design.
2. **`data.csv`**: A CSV file containing at least `name` and `email` columns.
3. **`config.json`**: The rules for rendering text onto the image.

### Using Layout Profiles (`profiles.yaml`)
Instead of guessing x/y coordinates for every event, you can define shared profiles in `profiles.yaml` at the root directory:

```yaml
profiles:
  workshop:
    text_x: 1500
    text_y: 1000
    font_size: 90
    font_color: [0, 0, 0]
    font_key: montserrat_bold
```

Then, in your event's `config.json`, just inherit it:

```json
{
  "name": "My Awesome Workshop",
  "slug": "demo",
  "active": true,
  "profile": "workshop",
  "validation_type": "email"
}
```

---

## 3. The Participant Web Portal

To let users download their own certificates on demand, you run the web server:

```bash
python app.py
```

### Accessing the Site
- **Public Portal:** `http://localhost:5000/` — Participants select an event, enter their email/name, and download their personalized PNG.
- **Admin Dashboard:** `http://localhost:5000/admin` — Login with your `ADMIN_PASSWORD` from `.env`.
    - **Coordinate Editor:** Visually adjust the `text_x`, `text_y`, `font_size`, and font styles using a live, draggable preview.
    - **Event Management:** Toggle events on/off or delete them entirely.
    - **Bulk Email Delivery:** Click **Send Emails** to open a comprehensive email configuration panel. You can customize the Subject, Plain Text Body, and HTML Body with live variable injection (`{participant_name}`, `{event_name}`). Clicking send dispatches the emails in the background!
    - **System Logs:** View real-time output of your background email tasks by clicking **View Logs** in the top navigation.

---

## 4. The Admin CLI (`manage.py`)

For large-scale operations or environments where you prefer the terminal, you can use the CLI tool. Open a terminal and run `python manage.py <command>`.

### Bulk Generate (Local Export)
If you need to generate 500 certificates for physical printing or a backup:
```bash
python manage.py bulk-generate <event_slug>
```
*Output: All PNGs will be saved to `generated_certificates/<event_slug>/exports/` with clean file names like `Certificate_John_Doe.png`.*

### Bulk Email Delivery
To automatically generate AND email certificates to everyone in the `data.csv`:
```bash
python manage.py send-emails <event_slug>
```
*Note: This relies on the SMTP variables in your `.env` file. It runs in parallel, processing multiple emails at once for speed.*

### Splitting Huge CSVs
If your `data.csv` is thousands of rows long and you want to process it in batches:
```bash
python manage.py split-csv path/to/large-data.csv --chunk-size 100
```
*Output: This will chop the CSV into multiple files (`_part1.csv`, `_part2.csv`) inside a `splits/` directory.*
