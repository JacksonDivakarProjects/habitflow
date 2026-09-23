# HabitFlow

A personal habit tracker you talk to on Telegram. Send plain text like
"ran 4 miles yesterday"; an LLM turns it into a structured log entry, you
review the draft (including the SQL it wrote), and nothing is saved until
you tap **Approve**.

```
Telegram ──> bot ──HTTP──> api ──HTTP──> llm ──> Groq (Llama 3.3 70B)
                            │
                            └──> Postgres (habits, daily_logs, audit_log)
```

| Service | What it does |
|---|---|
| `bot/` | Telegram front end (python-telegram-bot). Single allowed user. |
| `api/` | FastAPI. Drafting loop, validation, approval state machine, writes. |
| `llm/` | FastAPI wrapper around Groq. Builds the prompt from `semantics.yaml`. |
| `api/migrations/` | The whole schema and starter habits, applied by the API at startup. |

**Setting it up, using it, running it or fixing it?** See the
[usage guide and SOP](docs/SOP.md), including how
[reminders and time zones](docs/SOP.md#3-reminders-and-time) work.

## How a message becomes a log

1. **Draft (Loop 1).** `api` asks `llm` for an intent: habit, amount, unit,
   date, and a draft `INSERT`. The intent is validated (known habit, positive
   amount, real date that is not in the future, and SQL that passes the
   structural check and an `EXPLAIN` dry run). If validation fails, the error
   is sent back to the LLM and it tries again, up to 3 attempts.
2. **Fill gaps.** If the unit is missing, the bot asks for one. If the habit is
   unknown, the bot offers to create it.
3. **Review.** You get a card showing the preview and the SQL. **✏️ Edit**
   lets you reply with a correction and regenerates the draft in place
   (Loop 2). **🗑️ Discard** drops it. **✅ Approve** writes it.
4. **Execute.** `api` builds its own parameterized `INSERT` from the validated
   fields. The LLM's SQL is only shown and dry-run checked, never executed.
   The confirmation shows your streak and weekly total, plus a **↩️ Undo**
   button.

**Several habits at once.** "ran 3 miles and read 20 pages" becomes one card
with one line per log. Approve saves them all, and one Undo reverts them all.
A part that can't be logged directly, such as a brand-new habit, is listed
under *Not included* with the reason, so you can send it on its own.

**Edits never lose your draft.** If a correction can't be applied, the
original card stays approvable and you can simply try again. Every
correction is recorded in the draft's `feedback_history`.

**Units.** The LLM decides the unit before anything is stored: it
normalizes names ("mins" becomes minutes), reads ambiguous ones from the
habit ("500 m" of running is meters, "30m" of meditation is minutes), and
suggests one when you didn't say it, including a sensible default for a new
habit (pushups become reps). The rules and examples are in the `units`
section of `llm/semantics.yaml`. The API then re-checks every answer with
the same rules (`api/app/units.py`) and shows the result on the card for
you to approve. Totals convert within a family (km and miles, minutes and
hours, ml and liters), so "5 km" and "1 mile" add up in `/stats`.

**AI offline.** If the LLM is unreachable, simple messages about known
habits ("ran 3 miles yesterday") and simple corrections ("6 km, not 4",
"it was yesterday") still work through a regex parser (`api/app/parser.py`).
The card is marked *AI is offline* so you know to check it.

Every step is recorded in `audit_log`. Its `status` column moves through
`pending` / `awaiting_input` → `executed` | `failed` | `cancelled` |
`superseded`. Each chat has at most one `pending` draft; starting a newer
one supersedes the older draft.

### SQL safety

The LLM's `draft_sql` is parsed with `sqlglot` (`api/app/sqlguard.py`)
before it gets near the database. Only a single `INSERT INTO daily_logs`
that reads from nothing but `habits` is accepted. That rules out
multi-statement input, CTEs, `ON CONFLICT`, unknown functions (`pg_sleep`,
`pg_read_file`, ...) and any write to another table. SQL that passes is
`EXPLAIN`ed inside a savepoint. It is never run.

## Setup

Requires Docker with Compose.

```bash
cp .env.example .env      # fill in Postgres, Telegram and Groq credentials
docker compose up -d --build
docker compose logs -f bot
```

- **`TELEGRAM_BOT_TOKEN`:** from [@BotFather](https://t.me/BotFather).
- **`TELEGRAM_ALLOWED_USER_ID`:** your numeric Telegram id (ask
  [@userinfobot](https://t.me/userinfobot)).
- **`GROQ_API_KEY`:** from <https://console.groq.com>.
- **`APP_TIMEZONE`:** defines what "today" means. Defaults to `Asia/Kolkata`.

The API is published on `127.0.0.1:8000` only; `/internal/*` has no auth.
The interactive docs are at <http://localhost:8000/docs>.

### Schema changes

The whole schema lives in `api/migrations/`, starting with
`0000_baseline.sql` and `0000_seed_habits.sql`. The API applies each file
once, in order, at startup, and records it in `schema_migrations`. A fresh
Postgres therefore needs nothing mounted, and an existing database
(including one created by the old `db/init` scripts) is upgraded in place.
New changes go in `api/migrations/NNNN_name.sql` and must be idempotent
(`IF NOT EXISTS`); `api/tests/test_deploy.py` checks all three cases.

### Docker images

The compose file names the images `jackdiva/habitflow:api-<tag>`,
`:llm-<tag>` and `:bot-<tag>` (override with `HABITFLOW_IMAGE` /
`HABITFLOW_TAG`). See [docs/SOP.md](docs/SOP.md#publishing-to-docker-hub)
for publishing them and deploying on a server with just the compose file
and `.env`.

Or run **everything in one container**, Postgres included, from
`jackdiva/habitflow:all-<tag>` (`allinone/`, `docker-compose.single.yml`,
`.env.single.example`): see [the single image](docs/SOP.md#the-single-image-one-container), and
[deploy it on Render for free](#deploy-on-render-for-free-single-container--neon).

## Deploy on Render for free (single container + Neon)

This runs the whole app as **one free Render Web Service** straight from the
`jackdiva/habitflow:all-<tag>` image on Docker Hub, with the data in **Neon**
Postgres (also free). No Blueprint and no repo connection are needed: you
create the service in the dashboard and paste in the settings from
[`.env.single.example`](.env.single.example). The service stores nothing
itself, so redeploys can't lose data. It's independent of the docker compose
setup, which keeps using `.env` and its own database.

### How the image fits Render's free web services

| Render free web service rule | What the image does |
|---|---|
| Must listen on `$PORT` (Render sets it) | Serves a status page there: `/` ("HabitFlow is running.") and `/healthz` (JSON). Nothing else is public; the API and `/internal/*` stay on 127.0.0.1 inside the container |
| Sleeps after 15 min without inbound HTTP requests | The bot polls Telegram, which is outbound. So every 10 minutes the container requests its own public URL (`RENDER_EXTERNAL_URL`, set by Render) and stays awake, and the bot and 9 pm reminders keep working. `/healthz` shows `keep_awake.pings_ok` |
| Temporary filesystem, wiped on deploys | `DATABASE_URL` points at Neon, so the built-in database isn't used. Without it, the logs warn loudly |
| 750 free hours a month per workspace | One service awake all month uses about 730 hours. Don't run a second free service in the same workspace |

### Before you deploy (once)

1. **Neon database.** Create a project at <https://neon.com> in **AWS
   Singapore (ap-southeast-1)**. Copy the connection string from *Connect*
   and remove `-pooler` from the host to get the direct address:
   `postgresql://neondb_owner:<password>@ep-….c-4.ap-southeast-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require`
2. **A Telegram bot just for Render.** In @BotFather send `/newbot` and keep
   the token. Telegram lets only one program read a bot's messages, so don't
   reuse the token of a bot that runs somewhere else (like the compose setup).
3. **Your Telegram user id** (from @userinfobot) and **a Groq API key**.
4. **Your settings file.** Copy `.env.single.example` to `.env.single` on your
   PC and fill in the four required values. It's gitignored; never commit it.

### Deploy

1. Open <https://dashboard.render.com> → **+ New** → **Web Service**.
2. Under **Source Code**, choose **Existing Image**. For **Image URL** enter
   `docker.io/jackdiva/habitflow:all-1.2.0` (public, so no credentials),
   then **Connect**.
3. Fill in:

   | Field | Value |
   |---|---|
   | **Name** | `habitflow` (your URL becomes `https://habitflow-xxxx.onrender.com`) |
   | **Region** | **Singapore**, next to the Neon database |
   | **Instance Type** | **Free** |

4. **Environment Variables:** click **Add from .env** and paste your
   `.env.single`, or add the four one by one:

   | Key | Value |
   |---|---|
   | `DATABASE_URL` | the Neon **direct** connection string |
   | `TELEGRAM_BOT_TOKEN` | the Render bot's token |
   | `TELEGRAM_ALLOWED_USER_ID` | your numeric Telegram id |
   | `GROQ_API_KEY` | your Groq key |

   Don't add `PORT`: Render sets it (10000).
5. **Advanced** → **Health Check Path**: `/healthz`.
6. Click **Deploy Web Service**.

If the **Free** instance type isn't offered for an image, create the Web
Service from the **Git repository** instead, with **Language: Docker**,
**Dockerfile Path** `./allinone/Dockerfile` and **Docker Build Context
Directory** `.`. Everything else is the same; Render builds the image itself.

### Check that it works

- Open `https://<your-service>.onrender.com/` → `HabitFlow is running.`
- Open `…/healthz` → `"status": "ok"`, all processes `true`.
- **Logs** show:
  ```
  [supervisor] web service mode: status page on port 10000 (only / and /healthz are public)
  [supervisor] DATABASE_URL is set: using the external database
  [supervisor] keep-awake: requesting https://<your-service>.onrender.com/ every 10 min ...
  [supervisor] api is healthy
  ... Bot polling...
  ```
- Message your Render bot `/start`, then log something (`ran 3 miles`). In
  Neon's *Tables* view, `habits`, `daily_logs`, `audit_log`,
  `reminder_settings` and `schema_migrations` now exist.

Startup takes a minute or two on the free instance (0.1 CPU). **Optional
backup for keep-awake:** a free monitor such as [UptimeRobot](https://uptimerobot.com)
or [cron-job.org](https://cron-job.org) requesting your `/` URL every 5–10
minutes wakes the service if Render ever puts it to sleep.

### Day to day

| Task | How |
|---|---|
| **Deploy a new version** | Publish a new tag (e.g. `all-1.3.0`, see below). Service → **Settings** → **Image URL** → new tag, then **Manual Deploy** → *Deploy latest reference* |
| **Redeploy the same tag** | **Manual Deploy** → *Deploy latest reference* (Render pulls the image again) |
| **Roll back** | Put the previous tag back in **Image URL** and deploy |
| **Change a setting or secret** (e.g. a new Neon password) | Service → **Environment** → edit → *Save and deploy* |
| **Logs** | Service → **Logs**; `[supervisor]` lines show starts, crashes, restarts and keep-awake problems |
| **Backup** | Neon keeps 6 h of history on the free plan. For older copies, run `pg_dump` from your PC with the same URL |
| **Stop it** | Service → **Settings** → *Suspend* |

**Publishing a new image** (from the repo, logged in to Docker Hub as jackdiva):

```bash
docker build -f allinone/Dockerfile -t jackdiva/habitflow:all-<tag> .
docker push jackdiva/habitflow:all-<tag>
```

Render keeps no copy of pulled images, so keep every tag you deploy on
Docker Hub.

**Cost:** $0. Render's free web service plus Neon's free plan. The app
closes its Neon connections after a quiet minute, so Neon sleeps between
uses and stays within its free compute.

### If something's wrong

| Symptom | Cause and fix |
|---|---|
| `Conflict: terminated by other getUpdates request` | The same bot token is running somewhere else. Stop the other copy or use a separate bot for Render |
| `api exited with 3 while starting` repeating | The database can't be reached. Check `DATABASE_URL` (direct address, `sslmode=require`) and that the Neon project isn't suspended |
| `password authentication failed` | The Neon password changed. Update `DATABASE_URL` under **Environment** |
| Deploy stuck on the health check | Startup is slow on 0.1 CPU. Wait a couple of minutes, then check the logs for which process isn't healthy |
| Bot stops answering after a while | The service fell asleep: check `/healthz` → `keep_awake.last_error`, and add an UptimeRobot monitor as a backup |
| Service suspended near month end | The workspace ran out of its 750 free hours (another free service is using them) |
| Image pull failed | The tag doesn't exist on Docker Hub, or the repository was made private |
| Bot doesn't answer, no errors | `TELEGRAM_ALLOWED_USER_ID` isn't your id (other users are silently ignored) |
| Every card says "AI is offline" | Bad `GROQ_API_KEY`, or Groq is rate limiting |

## Using the bot

| Send | Result |
|---|---|
| `ran 4 miles` | Card: *4 miles of Running today* → ✅ → *🔥 3-day streak · 12 miles this week* |
| `read 20` | Uses the default unit (pages), marked *(suggested unit)* |
| `learned rust for 2 hours` | Offers to create a *Learning Rust* habit (and asks for a unit if it can't tell) |
| `/today` | What you've logged today |
| `/stats` | Totals and streaks for the last 30 days |
| `/undo` | Void your most recent log (it stays in the audit trail) |
| `ran 3 miles and read 20 pages` | One card with two lines; Approve logs both |
| `/habits` | Tracked habits and their default units |
| `/remind 21:00` (or `9pm`) | A daily evening check-in, sent only if a streak is at risk or something is unlogged. `/remind off` stops it; `/remind` shows the setting |
| `/cancel`, or reply “cancel” | Drop an open unit question or edit |

Errors are shown in plain language, for example "This draft was replaced by
a newer one." Errors from a button press appear as a popup, so the card
stays as it was.

## Tests

Each service has its own suite. The LLM and the Telegram API are faked, so
no network calls or API keys are needed.

```bash
cd api && pip install -r requirements-dev.txt && pytest   # needs Postgres, see below
cd llm && pip install -r requirements-dev.txt && pytest
cd bot && pip install -r requirements-dev.txt && pytest
cd e2e && pytest                                           # needs api + bot requirements
ruff check .                                               # from repo root
```

| Suite | What it covers |
|---|---|
| `api/tests` | Every draft, clarify, approve, edit, undo, discard and reminder path; the SQL guard; units and the regex parser; migrations. `test_feedback.py` covers the ✏️ Edit flow on its own. |
| `llm/tests` | Prompt construction, the `/extract` endpoint, and the eval scorer. |
| `bot/tests` | Every handler and button with a faked API: wording, error popups, "cancel" words, reminders and scheduling. |
| `e2e` | Whole conversations. The real bot handlers call the real API (in process) over a real database, with only Telegram and the LLM scripted. A small `Chat` simulator records every message, button and edit. |

### LLM evals

`llm/evals/cases.yaml` holds 33 real phrases covering plain logs, units,
relative dates, new habits, several habits in one message, and
corrections, each with the expected intent. To score the real model
(this makes one Groq request per case):

```bash
cd llm && GROQ_API_KEY=... python -m evals.run --min-pass 0.9
```

Run it after changing `semantics.yaml` or `GROQ_MODEL_NAME`. It is not run
in CI because it needs a key and costs tokens.

The API tests need a real Postgres, because the schema relies on JSONB,
partial unique indexes and `EXPLAIN`. They create a throwaway
`habitflow_test` database and never touch your data. The server comes from:

- **`TEST_DATABASE_URL`** if set. It can point at the compose database, for
  example `postgresql+psycopg://user:pass@localhost:5432/habitflow`.
- **An embedded Postgres** (`pgserver`, from `requirements-dev.txt`)
  otherwise. It runs anywhere, with no Docker needed.

`api/tests/test_regressions.py`, `test_sqlguard.py` and parts of
`bot/tests/test_bot.py` pin bugs that have already been fixed. Each of
those tests failed before its fix.

CI (`.github/workflows/ci.yml`) runs ruff and all three suites on every
push to `main` and on every pull request.
