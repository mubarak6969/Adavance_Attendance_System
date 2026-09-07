# Attendance — Facial Recognition Attendance System

A small institution-scale attendance system: register students with a webcam,
train a lightweight face-recognition model on the captures, then let people
mark their own attendance at a kiosk by looking at a camera.

> **Honest description of the recognition approach.** This project does
> **not** use a biometric-grade face-embedding model (FaceNet, ArcFace,
> etc.). It detects a face with MediaPipe, converts the crop to a 32×32
> grayscale, histogram-equalized, flattened pixel vector, and classifies it
> with a `RandomForestClassifier`. That's enough to reliably recognize a
> small, cooperative dataset (a classroom that captured clear, well-lit
> reference photos) but it is sensitive to lighting, pose, and image
> quality, and it should not be marketed or relied on as secure biometric
> authentication.

## Features

- Multi-user accounts with two roles (`super_admin`, `user`): self-service
  registration with admin approval, a dedicated `/admin` user-management
  panel, and a premium Three.js-accented login/registration experience —
  see **Accounts & roles** below.
- Dashboard: student counts, recognition readiness, today's attendance,
  attendance rate, model status, recent activity, a 30-day trend chart.
- Student management: search, per-student image/attendance counts, a
  readiness badge (`Ready` / `Needs training` / `No images`), capture,
  recapture, delete.
- Guided registration wizard: student details → live capture with visible
  progress and states (camera denied, no face, capturing, upload) → done.
- Live kiosk (`/mark_attendance`, no login required): shows face-detection
  and recognition state in real time (no face / multiple faces / unknown /
  recognized / already marked today), and marks attendance automatically.
- Attendance history with date-range filters, search, pagination, and CSV
  export.
- Analytics: attendance trend (7/30/90 days) and per-student totals.
- Model training with a real progress bar, safe to re-run any time, and a
  status machine that can't get stuck in "running" (see **ML training** below).
- CSRF protection on every state-changing request, light/dark theme with
  persistence, an automated test suite (66 tests, SQLite + PostgreSQL).

## Architecture

```
app.py         Flask app factory, routes, auth, CSRF, error handlers
config.py      Environment-aware configuration (dev / production / testing)
db.py          SQLite or PostgreSQL schema, connection handling, queries
storage.py     Where face images and the model actually live (local files or
               database blobs) -- app.py and model.py never know which
model.py       Face embedding extraction + RandomForest training/prediction
util.py        Small shared helpers (atomic file writes)
templates/     Jinja templates (base layout + pages + error pages + icons)
static/        CSS design system + vanilla JS (no frontend framework)
tests/         pytest tests (isolated temp DB/dataset per test; a separate
               Postgres-backed suite runs only when TEST_DATABASE_URL is set)
```

No frontend framework or build step — server-rendered Jinja templates plus
plain JS modules per page, and a hand-written CSS design-token system (light
+ dark themes, defined once as CSS variables). Chart.js is loaded from a CDN
for the two charts; everything else is self-contained.

## Tech stack

Python 3.11, Flask, SQLite (local dev) or PostgreSQL (production, via
`psycopg`), OpenCV, MediaPipe, scikit-learn, Chart.js. Gunicorn for
production.

## Local development

```bash
python -m venv venv
venv\Scripts\activate            # Windows
# source venv/bin/activate       # macOS/Linux
pip install -r requirements.txt

copy .env.example .env           # Windows: copy, macOS/Linux: cp
# edit .env: set SUPER_ADMIN_EMAIL / SUPER_ADMIN_PASSWORD at minimum

python app.py
```

Then open `http://127.0.0.1:5000`. The database, dataset folder, and model
files are created automatically on first run — no manual setup needed.

Default super admin login in development is whatever `SUPER_ADMIN_EMAIL`/
`SUPER_ADMIN_PASSWORD` you set in `.env` (falls back to the insecure default
`admin@example.com` / `admin123` if unset, with a startup warning — **never
leave that in production**). See **Accounts & roles** below for how
additional users get in.

The camera pages (`/add_student` capture, `/mark_attendance` kiosk) need
`getUserMedia`, which browsers only grant on `localhost` or HTTPS — see
**Camera & HTTPS** below.

### Running the tests

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

Tests run against an isolated temp database/dataset per test (via
`tmp_path` + env var overrides in `tests/conftest.py`) — they never touch
your real `attendance.db` or `dataset/`. `tests/test_storage.py` covers the
local filesystem storage backend directly.

`tests/test_database_backend.py` covers the PostgreSQL storage backend
(students/attendance/face images/model, all as blobs) but is skipped unless
you set `TEST_DATABASE_URL` to a throwaway Postgres database — it creates
and drops tables freely, so never point it at anything with real data:

```bash
TEST_DATABASE_URL=postgresql://user:pass@host/throwaway_test_db pytest tests/ -v
```

## Model training

Click **Start Training** on the dashboard once students have captured
photos. Training runs in a background thread and reports live progress.
It requires at least `MIN_TRAINING_STUDENTS` (default 2) students with at
least `MIN_TRAINING_IMAGES_PER_STUDENT` (default 5) usable images each —
usable meaning MediaPipe found exactly one face in the photo. Students with
zero images, or whose photos didn't yield enough usable faces, are skipped
with a clear message rather than silently pretending they're recognizable.

Training can be safely re-run any time; only one job runs at a time (a
lock plus a persisted status file prevent two overlapping runs), and if the
server restarts or the job fails partway through, the status always
resolves back to a clean, retryable state — it can never get stuck
reporting "running" forever.

## Accounts & roles

Two roles, stored in the database (`users` table) rather than as a single
password in an environment variable:

- **`super_admin`** — provisioned automatically on first startup from
  `SUPER_ADMIN_NAME`/`SUPER_ADMIN_EMAIL`/`SUPER_ADMIN_PASSWORD` (idempotent:
  re-running never overwrites an existing password). Can do everything a
  `user` can, plus: delete students, train/retrain the model, and manage
  every account from `/admin` (approve, deactivate, reactivate, change
  role, delete). The system always keeps at least one active super admin —
  deactivating, demoting, or deleting the last one is blocked server-side.
- **`user`** — self-registers at `/register` (name, email, password).
  New accounts start `pending` and can't log in until a super admin
  approves them from `/admin`. Once active, a `user` can log in, register
  students, capture photos, view attendance/analytics, and export CSV —
  everything day-to-day except the destructive/system-level actions above.

**Data model note:** students and attendance records are **shared, not
per-user** — every active account (either role) sees and works with the
same institution-wide roster and attendance history, by design, the way
multiple staff at one school would expect to share one system. The `users`
table itself (names, emails, account status) is only ever visible to
`super_admin` via `/admin`; a `user` account has no route that can list or
read another account's details.

There's no email-sending step in this pass (an email verification stage
would need a paid service, which the free architecture below deliberately
avoids), so the flow is `registered → pending admin approval → active`,
shown honestly in the UI rather than faking a "verify your email" step
that doesn't actually send anything.

## Configuration

All tunable values are environment variables (see `.env.example` for the
full list with defaults): `SECRET_KEY`, `SUPER_ADMIN_NAME`/
`SUPER_ADMIN_EMAIL`/`SUPER_ADMIN_PASSWORD`,
`RECOGNITION_CONFIDENCE_THRESHOLD`, `FACE_DETECTION_CONFIDENCE`,
`CAPTURE_IMAGE_COUNT`, `RECOGNITION_POLL_INTERVAL_MS`,
`MIN_TRAINING_IMAGES_PER_STUDENT`, `MIN_TRAINING_STUDENTS`,
`SESSION_LIFETIME_HOURS`, `LOGIN_MAX_ATTEMPTS`/`LOGIN_LOCKOUT_SECONDS`,
`DATABASE_URL` (switches storage to PostgreSQL -- see **Database** and
**Storage**), and path overrides for the database/dataset/model/log files
(used only when `DATABASE_URL` is unset).

`FLASK_ENV` selects the config profile: `development` (default),
`production`, or `testing`. Production refuses to start unless `SECRET_KEY`,
`SUPER_ADMIN_EMAIL`, and `SUPER_ADMIN_PASSWORD` are all set explicitly.

## Security notes

- The whole app (dashboard, students, training, attendance history, CSV
  export, `/admin`) requires login, and destructive/system actions
  (delete student, train model, everything under `/admin`) additionally
  require the `super_admin` role, checked server-side on every request via
  `g.current_user` -- never trusted from request data, so a forged
  `role=super_admin` field in a form/JSON body has no effect. The kiosk
  (`/mark_attendance`, `/recognize_face`) is intentionally public — that's
  the point, it's what people use to mark their own attendance — and
  reveals nothing but a recognized name and confidence score.
- Passwords are never stored: every account's password is hashed with
  Werkzeug's `generate_password_hash` (`pbkdf2:sha256`); only the hash is
  ever persisted, for the super admin and every registered user alike.
- A deactivated or un-approved user's existing session stops working on
  their *next request*, not just their next login attempt -- session
  validity is rechecked against the live `users` row on every request.
- The system always keeps at least one active `super_admin`: deactivating,
  demoting, or deleting the last one is rejected server-side, not just
  hidden in the UI.
- CSRF protection is applied to all state-changing (POST/PUT/DELETE)
  requests except `/recognize_face`, which has no session to forge and
  requires an actual matching face to have any effect.
- Login is rate-limited (in-memory, per-process — see **Limitations**).
  Login and registration error messages are deliberately generic before a
  password is verified, so they don't reveal whether a given email is
  registered.
- The login page's `next` redirect parameter only accepts same-site
  relative paths, preventing open-redirect abuse.
- `/upload_face` validates that `student_id` is a digit-only, existing
  student ID before using it in a filesystem path, preventing path
  traversal through that field.
- Session cookies are `HttpOnly` + `SameSite=Lax`, and `Secure` in
  production. Flask's debug mode (and its interactive debugger) is only
  ever enabled in the `development` config.
- Errors are logged server-side (stdout, plus a local file when the
  filesystem allows it — see **Deployment**) and never expose stack traces
  to the browser — custom 400/403/404/405/500 pages replace Flask's
  defaults.

## Database

Two backends behind the same `db.py` functions — nothing else in the app
knows or cares which one is active:

- **SQLite** (default, local development): one file, no server, zero setup.
- **PostgreSQL** (production, when `DATABASE_URL` is set): used on hosts
  whose filesystem doesn't survive a restart or redeploy — see **Deploying
  for free** below for exactly why and how.

Schema: `students`, `attendance`, plus `face_images` and `model_artifacts`
(explained in **Storage** below), with indexes on the columns actually
queried and a **unique index on `(student_id, <date part of timestamp>)`**
— this is the actual fix for duplicate same-day attendance, not just an
application-level check, so it holds even under concurrent requests (tested
directly: 12 simultaneous recognition requests for the same student produce
exactly 1 attendance row, on both SQLite and Postgres). `db.init_db()` runs
on every startup and is safe against a database created before this
constraint existed: if it finds existing duplicates, it removes them
(keeping the earliest) before applying the index. `cleanup_duplicates.py`
remains as a standalone version of that same cleanup for anyone migrating
an old SQLite database file directly.

## Storage

The same local-vs-database split covers face images and the trained model,
via `storage.py`:

- **Local** (no `DATABASE_URL`): images under `dataset/<student_id>/`,
  the model in `model.pkl` + `model_meta.json` — exactly the original
  behavior, unchanged for local development.
- **Database** (`DATABASE_URL` set): images and the model are stored as
  blobs in the same Postgres database as `students`/`attendance` (the
  `face_images` and `model_artifacts` tables), instead of on a filesystem
  the host can wipe.

Storing face photos as database blobs rather than in a dedicated object
storage service (S3, Cloudflare R2, etc.) was a deliberate choice for this
project's scale: a classroom's worth of students at ~50 photos each is a
few hundred MB at most, comfortably inside a free Postgres tier's storage
limit, and it means production needs exactly one external credential
(`DATABASE_URL`) instead of two. If you outgrow that — many more students,
much larger images — move `storage.py`'s database backend to real object
storage; the interface (`save_face_image`/`list_face_images`/`save_model`/
etc.) doesn't change, only what's behind it.

`app.py`'s training route runs in a background thread; since that thread
has no Flask request context of its own, and `storage.py`'s database
backend needs one (it goes through `db.get_db()`), the training worker
explicitly pushes `with app.app_context():` for its duration — see
`_train_worker` in `app.py` if you're extending it.

## Deployment

```
Procfile:
  web: gunicorn app:app --workers=1 --threads=8 --timeout=120
```

**Why one worker, many threads:** training state (`train_status.json`) and
the training lock are per-process. Multiple worker *processes* would each
have their own lock and could start overlapping training jobs, or read a
status file mid-write from another worker. A single worker with several
threads gives real concurrency for I/O-bound requests (recognition calls,
page loads) while keeping training state consistent. If you outgrow this,
move training status into the database (or Redis) and the constraint goes
away — not needed at this project's scale.

`gunicorn` only runs on Linux/macOS (no `fcntl` on Windows) — it installs
fine everywhere but won't launch on a Windows host. That's expected: it's
meant to run on your deployment platform's Linux environment, not your
Windows dev machine, where `python app.py` is what you actually run.

A `/health` endpoint is available for your host's health checks — it
returns `{"status": "ok"}` (or `503` if the database isn't reachable) and
nothing sensitive.

### Deploying for free

The combination below runs this app for **$0/month**, with no credit card
required anywhere, and no data loss on restart/redeploy: Render's free web
service (compute) + Neon's free Postgres (everything durable). Verified
live against each provider's current published limits as of writing —
recheck them yourself before you rely on this long-term, since free-tier
terms do shift.

**Why this specific pair, not the more obvious options:**
- Render's **free web service** has no persistent disk at all (confirmed
  from Render's own docs: "Free web services don't support... Persistent
  disks," and local files are explicitly lost on every redeploy). That's
  exactly why `storage.py`'s database backend exists — everything durable
  moves off that disk and into Postgres instead.
- Render's own **free Postgres** expires 30 days after creation and is
  deleted after a 14-day grace period — fine for a demo, not for something
  you actually want to keep running. Don't use it here.
- **Neon** (neon.tech) was chosen for the database over Supabase because
  Neon's free Postgres has no time limit and needs no credit card, and —
  important for a service that isn't hit every minute — its compute
  auto-scales to zero and **wakes itself automatically** on the next query.
  Supabase's free tier instead **pauses the whole project after a week of
  inactivity**, requiring you to go into their dashboard and manually
  resume it before the app works again. For an attendance kiosk that might
  go quiet over a weekend, that difference matters.
- Object storage for face photos (Cloudflare R2, etc.) was deliberately
  **not** used — R2 requires adding a payment method to your Cloudflare
  account to enable it at all (confirmed from Cloudflare's own R2 setup
  docs: "Complete the checkout flow to add an R2 subscription"), even
  though usage within the free tier costs nothing. Storing images as
  Postgres blobs instead (see **Storage** above) avoids that entirely and
  keeps this to one external credential.

**One-time setup:**

1. **Create a free Neon project** at [neon.tech](https://neon.tech) — no
   card needed. Copy the connection string it gives you (starts with
   `postgresql://`); that's your `DATABASE_URL`.
2. Push this repo to GitHub if it isn't already (`git remote -v` should show
   your fork/repo).
3. On [render.com](https://render.com), **New → Web Service**, connect the
   repo, and choose the **Free** compute plan. If you'd rather not click
   through every field by hand, this repo includes `render.yaml` — Render
   reads it automatically under **New → Blueprint**, pre-filling everything
   below except the secrets it deliberately leaves for you to type in.
4. **Build command:** `pip install -r requirements.txt`
5. **Start command:** `gunicorn app:app --workers=1 --threads=8 --timeout=120`
6. **Environment variables** (Render's dashboard, not `.env` — that file
   never leaves your machine): see the table below. No disk to configure —
   there isn't one on the free plan, and this app no longer needs one.
7. Deploy. Render builds, starts the Gunicorn command above, and gives you
   an `https://<your-service>.onrender.com` URL with HTTPS already active —
   required for the camera pages to work at all. On first request after
   this app runs `db.init_db()` against your Neon database, creating the
   schema automatically — no manual migration step.

**Required environment variables** (set these in Render's dashboard, never
in git):

| Variable | Value |
|---|---|
| `FLASK_ENV` | `production` |
| `SECRET_KEY` | Generate locally: `python -c "import secrets; print(secrets.token_hex(32))"` — paste the output in, don't reuse a dev value |
| `SUPER_ADMIN_NAME` | Your choice, e.g. `Admin` |
| `SUPER_ADMIN_EMAIL` | The super admin's login email |
| `SUPER_ADMIN_PASSWORD` | A real password — provisions the one account that can manage every other user |
| `DATABASE_URL` | The connection string Neon gave you in step 1 |

Nothing else — `DATABASE_PATH`/`DATASET_DIR`/`MODEL_PATH`/etc. are only
used when `DATABASE_URL` is absent (local development).

**Free-tier limits worth knowing before you rely on this:**
- Render's free web service **spins down after periods of inactivity** and
  takes tens of seconds to wake on the next request (a cold start, not data
  loss) — the first person to open the kiosk after a quiet stretch will see
  a slow load, not a broken one. It "might restart... at any time" per
  Render's own docs, which is exactly why nothing durable lives on it.
- Neon's free tier gives 0.5GB of storage per project and 100 compute-hours
  a month — generous for a classroom-scale dataset (a few hundred MB of
  face photos at most) but worth watching if you scale up.
- Render's free plan is explicitly documented as not intended for
  production traffic ("Do not use them for production applications"). This
  pairing is honestly a free/demo-tier deployment, appropriate for a
  portfolio project or a small pilot — not a guarantee-backed production
  SLA. If you outgrow it, both Render and Neon upgrade to paid plans
  without changing anything about how this app talks to them.

**Redeploying safely:** pushing to your connected branch (or clicking
"Manual Deploy") rebuilds and restarts the service. Because students,
attendance, face images, and the trained model all live in Neon rather than
on Render's container filesystem, a redeploy does **not** lose any of them
— only code changes take effect. If a redeploy happens to land mid-training,
this app's stale-status self-heal (see **Model training** above) means you
just click **Start Training** again afterward — no manual fix needed.

**Retraining after deploy:** exactly the same as local — log in, go to the
dashboard, click **Start Training**. It fetches images from Neon, trains,
and writes the model back to Neon; no redeploy involved.

**Backup considerations:** Neon's free tier doesn't include automated
backups. Use the CSV export for attendance records specifically, or connect
`psql`/a Postgres client to your `DATABASE_URL` periodically if you want a
full off-platform copy of everything (`pg_dump` works normally against it).

### Camera & HTTPS

Browsers only grant `getUserMedia` (camera access) on secure contexts:
`https://` or `localhost`. Any real deployment needs HTTPS on the kiosk
page and the registration/capture page, or cameras simply won't open. Most
PaaS hosts (Render, Railway, Fly.io, Heroku) provide HTTPS automatically on
their default domain — if you put a custom domain in front, make sure TLS
is configured there too.

## Known limitations

- **Recognition accuracy** — see the honest description at the top. Not
  biometric-grade; sensitive to lighting/pose. Good enough for a
  cooperative, well-lit dataset at classroom scale.
- **Local SQLite persistence** — if you deploy without setting
  `DATABASE_URL` on a host with an ephemeral filesystem, you're back to the
  original problem: everything gets wiped on restart. `DATABASE_URL` (see
  **Database**, **Storage**, **Deploying for free**) is what actually
  solves this in production, not optional polish.
- **Face images as database blobs don't scale indefinitely** — appropriate
  at classroom scale (a free Postgres tier's storage limit), not designed
  for a large multi-institution deployment. See **Storage** for what to
  change if you outgrow it.
- **Render's free web service isn't meant for production traffic** and
  sleeps on inactivity (cold start on the next request, not data loss) —
  see **Deploying for free**'s limits section. Upgrading to a paid Render
  plan removes this without any code change.
- **Login rate limiting is per-process, in-memory** — it resets on
  restart and isn't shared across multiple worker processes. Fine for the
  single-worker deployment this project ships with; would need a shared
  store (Redis) to scale further.
- **No email verification** — account approval is entirely manual (a
  super admin reviewing `/admin`); see **Accounts & roles** for why.
- **Student/attendance data is shared across all accounts, not siloed
  per user** — this is a deliberate design choice (one institution, one
  shared roster), not an oversight; see **Accounts & roles**.
- **No audit log of admin actions** beyond the application log — out of
  scope for this pass.

## License

No license file is included; add one if you plan to distribute this.
