# Multi-Tenant Plan

Turning the single-club certificate generator into a platform where multiple clubs
each get their own login, dashboard, and event links — and where a single event can
serve 1000 participants arriving at once.

This document is the agreed design. **None of it is implemented yet.** Where a
number appears, it was measured against the real templates in `events/`, not
estimated.

---

## 1. Where the app is single-tenant today

| Concern | Current code | Why it breaks with multiple clubs |
| --- | --- | --- |
| Admin auth | `ADMIN_PASSWORD` app.py:160, `require_admin` app.py:1612 | One password for the whole deployment |
| Storage | `SUPABASE_BUCKET` app.py:101 | Bucket name *is* the club identity |
| Event keys | `_event_config_key` app.py:471, `_event_csv_key` app.py:489 | Slugs are globally unique |
| Certificate tokens | `_cert_serializer` app.py:1406 | Payload is `{slug, name}` with no club |
| Public routes | `/` app.py:1623, `/events/<slug>` app.py:1628 | Home page lists every event on the server |
| Email | `utils/emailer.py:52` | One set of SMTP credentials |

## 2. Build order

Capacity and multi-field come **first**. Both touch the render path, and multi-field
changes the event config schema that Phase 1 migrates into Postgres — settling the
schema first means migrating once instead of twice.

| Phase | Scope | Depends on |
| --- | --- | --- |
| **0** | Capacity: JPEG default, byte-capped cache, render semaphore, load test | — |
| **0.5a** | Multi-field *engine*: schema, validation contract, render, token, backward-compat | 0 |
| **0.5b** | Multi-field *UI*: field-list coordinate editor, multi-input event form, `depends_on` | 0.5a |
| **1** | Postgres schema, club registration, login, superadmin approvals | 0.5b |
| **2** | Storage re-pathing under club prefix, per-club quota | 1 |
| **3** | URL restructure, legacy redirect, **and** the token payload change | 2 |
| **3.5** | Authoring UX deferred from Phase 2: config edit, toggle, delete, field editor | 3 |
| **5** | Migrate `csi-aseb` into the first club row | 1–4 |

One phase per session. Each ships with tests.

**0.5b must run before Phase 1, not after.** The editor rebuild is the step most
likely to discover a missing field property — ordering, a per-field label, layering.
Discovering that after Phase 1 has migrated event configs into a `jsonb` column means
a second migration, which is the exact cost the phase ordering exists to avoid. Do not
interleave Phase 1 between the two halves of 0.5.

---

## 3. Phase 0 — Capacity for 1000 participants

Certificate rendering is CPU-bound Pillow work in the request path. This is the
constraint that decides the hosting plan.

### Measured cost per certificate

Against the real 3508x2480 templates, template already decoded and cached — the best
case the app ever hits:

| Step | Median | Output |
| --- | --- | --- |
| Template decode (once per worker) | 130 ms | 25 MB in RAM |
| `.copy()` per request | 17 ms | — |
| Preview encode (JPEG, downscaled to 1200 px) | 45–57 ms | 0.15 MB |
| **Download encode (PNG, compress_level=3)** | **310–500 ms** | **2.5–3.4 MB** |

One participant = one preview + one download ≈ **0.4–0.55 s of CPU, ~2.7 MB egress**.

### The download encoder is the whole problem

| Encoding | Median | Size |
| --- | --- | --- |
| PNG compress_level=0 | 321 ms | 24.90 MB |
| PNG compress_level=1 | 278 ms | 3.61 MB |
| PNG compress_level=3 *(current default)* | 396 ms | 2.54 MB |
| PNG compress_level=6 | 528 ms | 2.42 MB |
| **JPEG quality=92** | **45 ms** | **0.73 MB** |
| JPEG quality=85 | 40 ms | 0.55 MB |

Tuning `DOWNLOAD_PNG_COMPRESS_LEVEL` is not a lever — every PNG level costs
250–530 ms. JPEG is **~9x less CPU and ~3.5x fewer bytes**.

The tradeoff is real: JPEG rings around sharp text on flat backgrounds, which is what
a certificate often is. Invisible at q92 on a photographic or gradient template;
potentially visible on pure white with thin text.

**Decision:** default to JPEG q92 for opaque templates, PNG kept as an explicit
per-event "high quality" option. Templates with real alpha stay PNG regardless.

### JPEG chroma subsampling: use `subsampling=0`

JPEG defaults to 4:2:0, which halves chroma resolution and bleeds colour around
sharp text edges. Measured as RMS error against the un-encoded render, over the
text bounding box only (encode-only timings, excluding copy and draw):

| Setting | Encode | Size | RMS err, dark brown text | RMS err, saturated red text |
| --- | --- | --- | --- | --- |
| q92, default 4:2:0 | 14.6 ms | 752 KB | 1.49 | 3.51 |
| **q92, `subsampling=0`** | **23.7 ms** | **952 KB** | **1.42** | **1.68** |
| q90, `subsampling=0` | 23.3 ms | 870 KB | 1.68 | 2.00 |
| q95, `subsampling=0` | 24.8 ms | 1165 KB | 0.98 | 1.15 |

It barely matters for near-neutral text (1.49 → 1.42) and **halves the error for
saturated text** (3.51 → 1.68) — which is what a club using its brand colour has.
The cost is ~+10 ms per request and ~+200 KB.

Take it: a request goes from ~55 ms to ~65 ms and stays **6x cheaper than PNG's
396 ms** while producing 2.6x fewer bytes. It also removes the main visual objection
to defaulting to JPEG at all.

### What "absent" means

`download_format` absent means **PNG**, so every event that exists today renders
byte-identically with no migration.

New events created after Phase 0 must write `download_format: "jpeg"` explicitly at
creation time. That way "absent" only ever describes a legacy config, the
byte-identical guarantee holds, and every new event gets the capacity win without
anyone opting in.

**Consequence to watch:** an event created *before* Phase 0 is still PNG. Before
running a 1000-participant event on an existing config, flip that event to JPEG or
it will render at 396 ms per download.

### What "1000 at once" costs

| Scenario | Arrival rate | Cores (PNG) | Cores (JPEG) |
| --- | --- | --- | --- |
| 1000 over ~1 hour | 0.3/s | ~0.2 | ~0.03 |
| **1000 in 10 min** *(realistic)* | 1.7/s | ~1 | ~0.12 |
| 1000 in 1 min | 17/s | ~9 | ~1 |

The middle row is what happens: a club posts the link at event close and most people
click within minutes. **Render's free plan (`plan: free` in render.yaml) cannot serve
any of these** — a fraction of a CPU means multi-second waits, `--workers 2` buys no
real parallelism, and idle spin-down makes the first participant pay a cold start
plus a Supabase template fetch.

### Memory is the other ceiling

A decoded template is **25 MB**. `TEMPLATE_CACHE_MAX` defaults to 6, so **150 MB per
worker**; caches are per-worker not shared, two workers = 300 MB before serving a
request, plus 25 MB per in-flight render. On a 512 MB instance this OOMs.
Multi-tenancy makes it worse: many clubs means many distinct templates cycling
through a cache sized by *entry count* rather than bytes.

**Change:** cap the template cache by total bytes, budgeted from instance RAM. Add a
semaphore bounding concurrent renders so a spike queues instead of OOM-killing the
worker, with a per-club slot limit so one club cannot starve another.

### Why the render cache does not help

`_RENDERED_CERT_CACHE` keys on an etag including the participant's name, so 1000
people produce 1000 distinct entries and zero hits. It only helps one user refreshing
their own link. Raising `RENDER_CACHE_MAX` does nothing for this workload.

### Pre-generation, and why it fights the quota

Clubs upload the full CSV up front, so certificates *could* be rendered at publish
time and served as signed Supabase URLs — zero app CPU at peak.

| Format | 1000 certificates | vs 100 MB club quota | vs 1 GB free tier |
| --- | --- | --- | --- |
| PNG | ~2.5 GB | 25x over | 2.5x over |
| JPEG q92 | ~730 MB | 7x over | fits, but alone |

Viable only with JPEG, a much larger quota, and retention that deletes an event's
renders weeks after it ends. Revisit if on-demand proves too slow in practice.

### Phase 0 deliverables

1. Per-event `download_format`, defaulting to JPEG q92 for opaque templates.
   `encode_certificate` app.py:1330 is the function to change.
2. `_TEMPLATE_IMAGE_CACHE` evicts on total decoded bytes, not entry count.
3. Semaphore bounding concurrent renders, with per-club fairness.
4. Load-test script: 1000 distinct names against the download path, reporting
   p50/p95 latency and peak RSS. Plain Python with a thread pool — k6 is JavaScript
   and fits nothing else in this repo.
5. `--workers` = available cores, `--worker-class gthread`, 2–4 threads each. Pillow
   releases the GIL during encode, so threads genuinely help.
6. Move off Render free. Verify with the load test, not a spec sheet.

Existing events with no `download_format` must behave exactly as they do today.

---

## 4. Phase 0.5 — Multiple text fields, max 5

Today a certificate carries one piece of text: the typed name, drawn by
`draw_name_on_image` app.py:1457 using the five scalars in
`certificate_render_settings` app.py:1321. Clubs want name **plus** team, position,
points, and similar.

### Rendering cost is negligible

| Fields drawn | JPEG q92 | PNG compress_level=3 |
| --- | --- | --- |
| 1 | 55 ms | 348 ms |
| 3 | 52 ms | 401 ms |
| 6 | 60 ms | 446 ms |

Six fields add ~6 ms under JPEG, ~100 ms under PNG (more ink, more entropy to
compress) — another argument for the Phase 0 JPEG default. Multi-field does not
change the capacity conclusions.

### Config schema

The five scalars become a `fields` list, each field owning its own placement and
typography:

```json
"fields": [
  {
    "id": "name",
    "label": "Your name",
    "source": "input",
    "x": 1789, "y": 1440,
    "font_size": 100,
    "font_color": [50, 34, 24],
    "font_key": "montserrat_bold",
    "align": "center",
    "max_width": 2200,
    "overflow": "shrink"
  },
  { "id": "team",     "source": "csv",    "column": "team",             "x": 1789, "y": 1700, "font_size": 60 },
  { "id": "position", "source": "csv",    "column": "position",         "x": 1789, "y": 1900, "font_size": 48 },
  { "id": "date",     "source": "static", "value": "20 August 2026",    "x": 1789, "y": 2100, "font_size": 40 }
]
```

### Three value sources

| `source` | Value from | Controlled by |
| --- | --- | --- |
| `input` | The participant types it, as today | The participant |
| `csv` | Their matched row, by column name | The club's uploaded list |
| `static` | Fixed string on the event config | The club |

**This distinction is the point of the feature, not a detail.** A participant already
types their own name and can type anything — that is accepted today. But `position`
or `points` must be `csv` or `static`, or participants award themselves whatever they
like. Only `input` is participant-controlled; everything else resolves server-side
from the row they validated against.

### Hard cap: 5 fields

Enforced in three places: config validation rejects a longer list server-side, the
editor disables "Add field" at 5, and token minting refuses to resolve more than 5
values. The cap keeps the measured numbers valid and keeps five draggable elements on
one canvas usable.

Numeric fields like `points` are drawn as text. **Store CSV values verbatim as
strings** — coerce to a number and `07` prints as `7`, and a blank cell prints as `0`
on someone's certificate.

### Overflow

Several fields make collisions likely, and a long name currently overflows the
template silently. Each field gets `max_width` with `overflow`: `shrink` (reduce font
size until it fits, the default) or `truncate`.

### Derived fields: dropdown drives auto-fill

The intended flow: the participant picks their team from a dropdown, and position and
points fill in from that team's CSV row. They never type them and cannot change them.

**Blocker — the matched row is currently discarded.**
`validate_participant_submission` app.py:1226 returns only an error string or `None`.
`load_valid_participants` app.py:1162 reduces the CSV to a set of `(player, team)`
tuples. After a successful validation there is no row left to read `position` from.

Change it to return `(matched_row, None)` on success and `(None, error)` on failure.
Every `csv` field resolves against `matched_row`. **This lands first — nothing else in
the phase works without it.** The tuple/set loaders stay useful as fast membership
checks, but the row must come back with them.

**Dependent dropdowns.** `load_team_names` app.py:1189 already populates a team
dropdown, and `build_custom_form_fields` app.py:1118 already builds dropdowns from
custom columns. What is missing is dependency — choosing a team should filter the
player dropdown to that team's members:

```json
{ "column": "player", "is_dropdown": true, "depends_on": "team" }
```

Filtering is client-side. The server still re-validates the full combination on
submit — a filtered dropdown is convenience, never a check.

**Split across 0.5a / 0.5b.** The client-side filtering is UI work and lands in 0.5b.
Two things stay in 0.5a:

- **Reserve the `depends_on` key now.** Config validation must accept and ignore it in
  0.5a, so 0.5b adds behaviour rather than schema. Otherwise "the schema is frozen for
  Phase 1" is not true.
- **Server-side combination validation is engine work, and already exists** —
  `validate_participant_submission` checks the `(player, team)` pair today. It must not
  regress while the function is refactored to return the matched row. 0.5b adds no new
  security boundary; it must not be where the check first appears.

**When the key matches several rows.** Selecting only a team matches every member.
Fine for a team-level attribute like position, wrong for a per-person one. At render
time, if the submitted values match multiple rows and a `csv` field differs across
them, the field is ambiguous — do not silently take the first row.

Surface it at **setup**, not at participant time: when a club maps a field to a
column, check the uploaded CSV and warn — *"position is not consistent within every
team; participants will need to select their name too."* The club fixes it during
setup and participants never hit a failure.

**Do not leak the roster.** A dropdown of team names is fine. A dropdown of every
participant's name publishes the full roster to anyone opening the event page — the
same mistake the club-name dropdown would have been. Person-level fields default to a
text input with server-side validation; the dropdown is an explicit per-field opt-in
with the consequence stated in the admin UI.

### Token payload

Resolved values are inlined at mint time. Measured:

| Payload | Token |
| --- | --- |
| Today `{s, n}` | 92 chars |
| Club + slug + 5 resolved values | 216 chars |
| Club + slug + name + CSV row reference | 123 chars |

All far inside URL limits, so size does not decide it. Inlining wins because it
matches the stateless design: token + template + config reproduces the image, with no
lookup into a list that may have been re-uploaded since.

Consequence to accept and state in the admin UI: if a club fixes a CSV typo and
re-uploads, already-issued links keep printing the old value. Re-issuing means the
participant submits the form again.

`certificate_render_settings` and the render fingerprint must cover **every field's
value and settings**, or moving one field serves stale cached renders of the others.

### CSV parsing cost under load

`load_team_names`, `load_unique_column_values`, `load_valid_participants` and
`load_csv_rows` each re-read and re-parse the CSV independently, and rendering the
event form calls several of them. Dependent dropdowns add more. At 1000 participants
arriving in minutes this is repeated work on every *form render*, not just every
certificate.

Parse once into a per-(event, csv version) structure holding rows, membership sets,
and per-column unique values — cached like the template image, invalidated on upload.

### Backward compatibility

Events with no `fields` synthesise a single `input` field named `name` from `text_x`,
`text_y`, `font_size`, `font_color`, `font_key`. Write the new shape on next save.
Old `{s, n}` tokens map to that field. **Existing events must render byte-identically
until a club edits them.**

### Coordinate editor

The largest piece of work. `templates/admin/coordinate_editor.html` (386 lines)
assumes one draggable element bound to five inputs. It becomes a field list with add
/ remove / reorder, click-to-select, drag the selected field, and per-field controls
that rebind on selection. Live preview renders all fields, not just the selected one.

---

## 5. Phase 1 — Postgres schema and club auth

KV/Redis stops being a source of truth. It may stay as a pure cache or be removed
entirely; nothing should break if it is empty.

```sql
create table clubs (
  id            uuid primary key default gen_random_uuid(),
  slug          text unique not null,              -- url segment, [a-z0-9-]
  name          text not null,
  password_hash text not null,
  status        text not null default 'pending',   -- pending | approved | suspended
  quota_bytes   bigint not null default 104857600, -- 100 MB
  created_at    timestamptz not null default now()
);

create table events (
  id               uuid primary key default gen_random_uuid(),
  club_id          uuid not null references clubs(id) on delete cascade,
  slug             text not null,
  name             text not null,
  config           jsonb not null default '{}'::jsonb,
  active           boolean not null default false,
  template_ext     text,
  template_version text,
  created_at       timestamptz not null default now(),
  unique (club_id, slug)
);
```

`config` holds the Phase 0.5 shape (`fields`, `validation_type`, `custom_fields`,
`custom_dropdown_fields`, `download_format`), so the renderer does not change again.

`on delete cascade` removes a club's event rows; the club's bucket prefix must be
deleted separately in the same handler.

### Auth

- Self-registration creates `status='pending'`.
- A pending club **can** log in and configure; its public pages under `/c/<club>`
  stay dark (404) until approved.
- Approval and password resets live in a superadmin view gated by the existing
  `ADMIN_PASSWORD`. Clubs never see it.
- `werkzeug.security.generate_password_hash` / `check_password_hash`. Never store or
  compare plaintext.
- Session carries `club_id`. `require_admin` splits into `require_club` and
  `require_superadmin`.
- Login is a **text field with autocomplete after 3 characters**. The autocomplete
  endpoint returns nothing for shorter prefixes, so the full club list is not handed
  out on page load.

The existing participant flow must still work unchanged after this phase.

### Postgres access, and the fallback rule

An in-memory repository implementation is **required**, not merely allowed: tests in
`tests/` run with no network, so the repository needs a substitutable backend.

But it must never activate implicitly. The app already has a silent-fallback pattern
— local files when Supabase is unset — and the README correctly calls that "fine
locally and wrong on any host with an ephemeral filesystem". **Do not repeat it for
auth.** A credential store that silently falls back when Postgres is unreachable
either loses every club account or authenticates against an empty table.

The rule:

- The in-memory backend is selected **explicitly** — injected by tests, or behind an
  env var that is unset in production.
- If Postgres is configured and unreachable, requests **fail closed** with a 503.
  Never degrade to a fallback store.
- Startup logs which backend is active, loudly, the same way the missing-`SECRET_KEY`
  path warns today.

### RLS: app-level scoping now, and be honest about why

App-level scoping (`WHERE club_id = %s` on every query) is the mechanism for Phase 1.

RLS is a documented follow-up, but the follow-up note must state the real
precondition: **the app connects with `SUPABASE_SERVICE_KEY`, and the service role
bypasses RLS entirely.** Enabling RLS while every query runs as service_role buys
nothing — it is security theatre that reads like defence in depth in a later audit.

Making RLS meaningful requires moving off the service key to per-request credentials
carrying the club identity. That is a real project, not a flag. Write it down that
way, or someone will later assume the database is enforcing isolation when only the
application is.

### Phase 1 boundary

In scope:

- `clubs` and `events` tables, as a migration file.
- A repository layer over both, with the in-memory backend above.
- Registration creating `status='pending'`; login; logout; club autocomplete at
  3+ characters.
- `require_admin` splitting into `require_club` and `require_superadmin`.
- Superadmin views: approve a pending club, reset a club password.
- Event configs read from Postgres, with KV demoted to cache or legacy read.

Out of scope — each has its own phase:

- Storage re-pathing and quota (Phase 2)
- URL restructure and the legacy redirect (Phase 3)
- Token payload gaining the club (Phase 4)
- Migrating `csi-aseb` into the first club row (Phase 5)

**The one thing that makes this boundary safe:** through Phases 1 and 2 there is
exactly one club, so resolving an event by slug alone still returns the right row and
`/events/<slug>` keeps working untouched. Phase 3 is where global slug lookup stops
being safe, and Phase 4 is where tokens stop being unambiguous. Do not pull either
forward, and do not leave them behind when a second club is created.

---

## 6. Phase 2 — Storage and quota

```text
<bucket>/
└── <club-slug>/
    └── <event-slug>/
        ├── participants/
        │   ├── data.csv
        │   └── source.xlsx
        └── template/
            └── template.png
```

`SUPABASE_BUCKET` stops meaning "the club" and becomes just the container name. Every
storage path helper gains a club-slug segment.

**Quota.** Supabase free tier is 1 GB total, so each club gets a hard cap (default
100 MB, per-club overridable by a superadmin). Before any upload, sum object sizes
under `<club-slug>/` and reject if it would cross the cap, with a clear message.
Over-quota blocks new uploads; it never deletes anything.

**List on upload; do not cache.** Uploads are rare admin actions, not a hot path, so
there is no listing storm worth caching away, and a per-worker cache would let two
workers each admit an upload that together cross the cap. Revisit Redis caching only
if upload volume ever makes listing a measurable cost.

**The quota is soft, and that is fine — but say so.** Removing the cache narrows the
race, it does not close it: two concurrent uploads can both list, both see 90 MB, and
both admit 8 MB. The overshoot is bounded by `MAX_CONTENT_LENGTH` (10 MB, app.py:76)
times the number of simultaneous admin uploads, which is small.

Accept that bound rather than chasing exactness. A `clubs.used_bytes` counter updated
transactionally would be atomic but drifts against reality on failed uploads and
out-of-band deletes, so it needs reconciliation against the listing anyway — and the
listing is the ground truth. Document the bound; do not describe the quota as a hard
guarantee.

### Deterministic paths, not a fallback chain

`csi-aseb` is not migrated until Phase 5, so its objects stay at the legacy
`<event>/…` paths while new clubs write to `<club-slug>/<event>/…`.

Resolve this from the **config, deterministically**: a config loaded from Postgres
carries its club, so it reads the club-prefixed path; a legacy config (file or KV,
no club) reads the legacy path. **Do not try one path and fall back to the other** —
a fallback chain costs a wasted 404 round trip on every new-scheme read and makes
behaviour depend on what happens to exist in the bucket.

The club is **derived context, never persisted**. `events.config` is a `jsonb`
column; attaching a `_club_slug` to the config dict risks writing it back and drifting
if a club slug ever changes. Return it alongside the config or strip it before save,
and test that a round-tripped config carries no underscore-prefixed keys.

### Phase 2 boundary

In scope: club-prefixed path helpers with deterministic legacy resolution;
`_supabase_list` + `club_storage_bytes` + quota enforcement with superadmin per-club
override; and the **real, permanent** club-scoped write routes needed to exercise it —
create event, upload template, upload CSV, under `/dashboard`.

Out of scope: config editing, activate/deactivate, delete, and re-homing the field
editor (authoring UX, not storage); `/c/<club>/<event>` participant URLs and the
legacy redirect (Phase 3); token change (Phase 4); `csi-aseb` bucket migration
(Phase 5).

**`/dashboard` does not move in Phase 3** — §7 restructures the *participant* URLs.
So club write routes built here are permanent, not scaffolding, and nothing is built
twice.

---

## 7. Phase 3 — URLs become club-scoped

| Route | Purpose |
| --- | --- |
| `/` | Landing + login/register |
| `/c/<club>` | That club's public event list |
| `/c/<club>/<event>` | Participant certificate flow |
| `/dashboard` | Logged-in club's admin, scoped by session |
| `/admin` | Superadmin: approvals, resets, quotas |
| `/events/<slug>` | **301** to `/c/csi-aseb/<slug>` so live links survive |

---

## 8. Phase 4 — Tokens carry the club

Not optional. Today the payload is `{"s": slug, "n": name}` app.py:1411. Once slugs
are unique only within a club, two clubs with `hackathon-2026` would share valid
tokens. The payload becomes `{"c": club, "s": slug, "v": {…field values}}` and the
verifier resolves the event by the club/slug pair.

Tokens without `c` resolve to `csi-aseb` for a grace period, then are rejected.

### Phase 4 merges into Phase 3. Do not ship them apart.

Phase 3 makes the club part of the URL. Phase 4 makes it part of the signed token.
Shipping 3 without 4 opens the exact hole the token change exists to close, so they
are one phase.

**Scoping the token routes by URL is not a substitute — it is worse.** A route like
`/c/<club>/preview/<token>` takes the club from an **unsigned URL segment** and pairs
it with a **signed slug**. There is nothing to bind them: an attacker constructs
`/c/club-b/preview/<token-minted-for-club-a>` and, if club B has an event with the
same slug, renders club A's participant name on club B's template. Putting the club
in the URL makes it attacker-supplied.

The token is the only signed artifact in the flow. The club belongs **in the token**,
and `/preview/<token>` and `/download-file/<token>` should keep resolving the club
from the payload — not from the path.

### The csi-aseb bridge

Two separate bridges, both needing an explicit end:

**Redirect.** `/events/<slug>` → 301 → `/c/csi-aseb/<slug>`, but **only when csi-aseb
actually has that event**. Otherwise 404 directly. A 301 into a 404 gets cached by
the browser and is then hard to explain.

**Legacy tokens.** A payload with no `c` resolves to `csi-aseb`. This is the bridge
most likely to become permanent by accident, so give it a way to end: put the cutoff
behind an env var, and log every legacy-token resolution. When the log goes quiet,
the grace period is over and the branch can be deleted. Without the log there is no
evidence on which to ever remove it.

---

## 9. Phase 5 — Migration

The existing `csi-aseb` data becomes the first club:

1. Insert a `clubs` row for `csi-aseb`, `status='approved'`.
2. For each event in KV / `events/`, insert an `events` row pointing at that club.
3. Move bucket objects from `<event>/…` to `csi-aseb/<event>/…`.
4. Keep `/events/<slug>` redirecting while old links circulate.

Write it as a re-runnable `manage.py` command so it can be pointed at a staging
project first.

### The live-path smoke test is a precondition, not a follow-up

The network-free tests run against the in-memory repository and a `urlopen` stub, so
the psycopg and live-Supabase paths are **unexercised**. Phase 5 mutates both. Running
it as the first real exercise of that path means discovering any behavioural
difference while rewriting production data.

Order:

1. `manage.py db-init` against a real Postgres, then a smoke run of login, event
   create, template upload, and certificate render against live Supabase. Staging
   project if there is one.
2. Export first: dump the KV configs and list the bucket to a local file. Supabase
   point-in-time recovery is a paid feature, so this export **is** the backup.
3. `--dry-run` on the migration, printing every row it would insert and every object
   it would move, changing nothing. This is a required flag, not a nicety.
4. Run for real against staging, verify, then production.

### Copy, verify, then delete — never move

For each object: copy to the new `csi-aseb/<event>/…` path, read it back and compare
size or hash, and only then delete the legacy object. A move that half-completes on a
network error leaves the file at neither path. Re-runnability falls out of this for
free: an object already present and verified at the new path is skipped.

### The two bridges have different clocks

Removing them is **not** part of Phase 5.

- The **legacy-token grace** ends when the `[legacy-token]` log goes quiet. That is
  bounded — outstanding links stop being clicked within weeks of an event.
- The **`/events/<slug>` 301** has no such bound. Certificate links sit in WhatsApp
  groups and inboxes indefinitely, and the redirect costs one lookup. Keeping it
  permanently is a defensible outcome; deleting it on a schedule is not.

---

## 10. Email — unchanged, unlinked

`utils/emailer.py`, `/admin/events/<slug>/send_emails` app.py:1978, the
`email_form.html` template, and `manage.py`'s bulk send are left untouched and are not
linked from club dashboards.

The send route lands under `require_superadmin`, not `require_club`, since it sends
from the deployment's shared SMTP account. Hiding a link does not gate a URL, and a
club typing that path would be spending someone else's sender reputation.

---

## 11. Global constraints

- Every club-scoped query filters by `session['club_id']`, not just by slug.
- A club requesting another club's resource gets **404, never 403** — do not confirm
  that another club's event exists.
- Pick one Postgres access approach (stdlib `urllib` like the existing KV/Storage
  code, or `psycopg` added to `requirements.txt`), state which, and stay consistent.
- KV code paths must not crash when empty; they stop being authoritative.
- Nothing is written to disk per request. Do not reintroduce the old file-based flow.
- Tests in `tests/` are plain scripts, no framework, run as `python tests/test_x.py`
  against a temp dir with no network. Match that style; do not add pytest.
- The tests that matter are cross-club isolation: club A cannot read, edit, or mint
  tokens for club B's event — and a participant cannot set their own `csv`-sourced
  field values.

---

## 12. Open items

- Whether Redis is kept as a cache or dropped outright.
- Rate limiting on registration and login, once registration is public.
- What happens to a club's stored files when a club is suspended vs deleted.
- Whether "1000 at once" means the 10-minute or the 1-minute row in §3. The 1-minute
  case needs pre-generation, which collides with the 100 MB quota.

## Note on misc/PERFORMANCE_ANALYSIS.md

**Stale.** It describes the old flow that wrote each certificate to
`generated_certificates/` and redirected to a `cert_id` — replaced by the stateless
signed-token flow in commit 0c50488. Its cache recommendations (100 templates, 50
rendered images) predate the current defaults of 6 and 6, and at 25 MB per template a
cache of 100 would need 2.5 GB. The numbers in §3 are current; that file is history.
