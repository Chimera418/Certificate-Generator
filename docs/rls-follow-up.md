# Row-Level Security: a follow-up, not a flag

Phase 1 isolates clubs at the **application layer**: every club-scoped call in
`db.py` takes `club_id` as an argument, there is no "get event by slug" that spans
clubs, and `tests/test_club_auth.py` proves a club cannot read another's event
(and that a miss is 404, never 403). That is the enforcement today.

Postgres Row-Level Security is the usual next layer — a database-level net so that
even a forgotten `WHERE club_id = …` cannot leak across tenants. **It is worth
doing, but it is a project, not a switch, and turning it on naively would be worse
than leaving it off** because it would *look* like enforcement while enforcing
nothing.

## Why "just enable RLS" is a trap here

The app connects to Supabase with **`SUPABASE_SERVICE_KEY`**. The service role
**bypasses RLS entirely** (`BYPASSRLS`). So if we `ALTER TABLE events ENABLE ROW
LEVEL SECURITY` and write policies while every query still runs as the service
role, the policies are inert. An auditor reading the schema would see RLS enabled
and reasonably assume the database enforces isolation — when only the application
does. That false assurance is the real hazard.

## What making RLS real actually requires

1. **Stop querying as the service role.** Connect as a role that is *subject* to
   RLS, and carry the club identity per request — e.g. `SET LOCAL
   app.current_club_id = …` inside the request's transaction, with the connection
   reset on release so a pooled connection never keeps a previous request's tenant
   context (a classic pooling footgun).
2. **Three policies, because the app has three access modes**, and only one is a
   simple session-tenant match:
   - *club admin* — `club_id = current_setting('app.current_club_id')`;
   - *superadmin* — must bypass (a separate role, or a policy keyed on a superadmin
     flag), since it spans clubs;
   - *participant* — **unauthenticated**, reading any *approved* club's *active*
     events by slug. There is no session tenant here, so this needs a distinct
     read policy (`... where status = 'approved' and active`), not the session-key
     net. This is the mode that makes RLS more than a one-liner.
3. **Migrations run as a `BYPASSRLS`/superuser role**, or `ALTER TABLE` and policy
   changes will misbehave under an active policy.

Until step 1 is done, RLS is theatre. Do it as its own change, with its own tests
that connect as the RLS-subject role and confirm a cross-club `SELECT` returns zero
rows at the database level — not just through the app.
