# HabitFlow: usage guide and SOP

How to set HabitFlow up, use it day to day, run it, change it, and fix it.
For how it works internally, see the [README](../README.md).

- [1. One-time setup](#1-one-time-setup)
- [2. Daily use](#2-daily-use)
- [3. Asking about your routine](#3-asking-about-your-routine)
- [4. Operations](#4-operations)
- [5. Making changes](#5-making-changes)
- [6. Troubleshooting](#6-troubleshooting)

---

## 1. One-time setup

### Prerequisites

- **Docker Desktop**, installed and running.
- **A Telegram bot token.** In Telegram, message **@BotFather**, send
  `/newbot`, and copy the token.
- **Your numeric Telegram user id.** Message **@userinfobot** and it replies
  with it. Only this user can use the bot; everyone else is silently ignored.
- **A Groq API key** from <https://console.groq.com>.

### Configure `.env`

Copy `.env.example` to `.env` and fill it in:

```env
POSTGRES_USER=habitflow
POSTGRES_PASSWORD=<a strong password>
POSTGRES_DB=habitflow
DATABASE_URL=postgresql+psycopg://habitflow:<same password>@db:5432/habitflow

TELEGRAM_BOT_TOKEN=<from BotFather>
TELEGRAM_ALLOWED_USER_ID=<your numeric id>

GROQ_API_KEY=<from Groq>
GROQ_MODEL_NAME=llama-3.3-70b-versatile

# APP_TIMEZONE=Asia/Kolkata   # what "today" and "this week" mean
```

Rules:

- `DATABASE_URL` **must** start with `postgresql+psycopg://` and use the
  host `db`, which is the Postgres service's name inside Docker.
- Leave optional settings **commented out, not blank**. A blank value
  (`APP_TIMEZONE=`) overrides the built-in default with an empty string.
- Never commit `.env`. It is already in `.gitignore`.

### Start and verify

```bash
docker compose up -d --build
docker compose ps                      # db, llm, api "healthy"; bot "running"
curl http://localhost:8000/health/db   # {"db":"ok"}
docker compose logs -f bot             # look for "Bot polling..."
```

Then send `/start` to your bot in Telegram. If it doesn't answer, see
[Troubleshooting](#6-troubleshooting).

---

## 2. Daily use

Send the bot a plain message. It works out whether you are **logging**
something, **asking** about your routine, or just saying hi, and answers
accordingly. You never need a command to log or ask.

### Logging

| You send | What you get | What you do |
|---|---|---|
| `ran 4 miles` | 📝 **Log this?** • Running · 4 miles · today | ✅ Save, ✏️ Change or ✖ Cancel |
| `read 20` | Uses the habit's default unit, with "Unit guessed from your habit" | Save, or Change it |
| `ran 3 miles and read 20 pages` | One card, one line per log | One Save stores them all |
| `did 20 pushups` (a new habit) | ✨ "“Pushups” is a new habit. Create it?" | ✨ Create habit, or ✖ No |
| `ran 4` (no unit, and none can be guessed) | ❓ "What unit?" with buttons: miles · km · meters … | Tap a unit or type one; `cancel` drops it |
| `read 30 pages yesterday`, `ran 5k 2 days ago` | A card dated back | Save |

Nothing is saved until you tap **✅ Save**. **🔍 SQL** on a card shows the
exact statement that will run.

### After saving

The card becomes:

```
✅ Saved
• Running · 4 miles · today
   🔥 3-day streak · 12 miles this week
```

with a **↩️ Undo** button. Undo removes every log from that card. Undone logs
are kept in the audit trail but excluded from everything else: stats,
streaks, `/today` and answers to questions.

### Changing a draft (✏️ Change)

Tap **✏️ Change**. The bot asks "What should change?" and offers one-tap
fixes: **📅 It was yesterday**, **📅 It was today**, **↩️ Keep as is**. Or type
the fix in plain words:

- `6 miles, not 4`
- `km not miles`
- `wrong habit, it was reading`
- `also read 10 pages`

The old card is marked "✏️ Changed" and the updated one arrives below
(🔄 **Updated. Log this?**), so the latest card is always at the bottom of the
chat. If the fix can't be applied, the change stays open: try again, tap
Keep as is, or reply `cancel`.

Asking a question while a change or a unit question is open is fine: the
bot answers it and reminds you what it's still waiting for.

### Commands

| Command | What it does |
|---|---|
| `/today` | What you've logged today |
| `/stats` | 30-day totals and streaks. Compatible units are combined (km + miles, minutes + hours) |
| `/undo` | Undo your most recent log |
| `/habits` | Your habits and their default units |
| `/cancel`, or reply `cancel` / `never mind` | Drop an open question or change |
| `/help` | The in-bot version of this section |

### Signals on a card

- **⚡ The AI is offline**: Groq couldn't be reached, so the simple parser read
  your message. Check the card before saving.
- **⚠️ Not included: …**: part of the message couldn't be logged together
  (usually a brand-new habit). Send that part on its own.
- **Popup "This draft was replaced by a newer one."**: you tapped an older
  card. Only your latest draft can be saved.

---

## 3. Asking about your routine

### What you can ask

| You ask | You get |
|---|---|
| `how much did I read this month?` | 💬 60 pages of Reading this month, on 12 days. |
| `how many hours did I work last week` | 💬 38.5 hours of Work last week, on 5 days. |
| `did I meditate today` | 💬 Yes, 15 minutes of Meditation today. |
| `how many km did I run this week` | Converted for you, even if you logged miles |
| `what's my reading pattern` | A sentence about it plus a small table (by weekday) |
| `how many hours did I work each week this month` | A table by week |
| `which habit did I do most often last week` | A ranked table |
| `what's my longest running streak` | Start, end and length |
| `which days this week did I skip meditation` | The days with no log |

Periods it understands: today, yesterday, this/last week, this/last month,
this/last year, "in August", "last 7 days", "past 3 months". Without a
period, the answer covers all time. Weeks start on Monday, and every date is
in `APP_TIMEZONE`.

Every answer has a **🔍 SQL** button that shows the exact query used. The
number you see always comes from that query run on your data. Nothing is
estimated.

### How a question is answered

```
"how much did I read this month?"
  │
  ├─ classify: rules first (question words, "?", "pattern", "average"...);
  │            only unclear messages ask the LLM. LLM down: a number means a log.
  │
  ├─ simple total for one habit + one period?
  │     yes → built-in SQL template: instant, exact, works with the AI offline
  │     no  → the LLM writes one SELECT, given literal dates for "today",
  │           "this month"... (never CURRENT_DATE)
  │
  ├─ the guard checks it: one SELECT, only habit_logs and habits, only
  │  allowed functions, no system tables, capped at 200 rows.
  │  Rejected or failing SQL goes back to the LLM with the reason (3 tries).
  │
  ├─ run in a READ ONLY transaction with a 3-second timeout
  │
  └─ the LLM phrases the rows as a sentence (a plain summary if it's down);
     question, SQL, row count and answer are saved in query_log
```

**What the SQL can see.** Questions read the `habit_logs` view, never the raw
tables. It already leaves out undone logs, turns an old bare `m` into meters
or minutes, and gives `amount_in_habit_unit`: every log converted to its
habit's unit, so 5 km and 1 mile add up correctly for a miles habit. Amounts
that can't be converted (minutes of reading for a pages habit) are kept
separate instead of being added to the wrong total.

**When the AI is offline** simple totals still work (they don't need it). Other
questions get "I can't work that one out right now" with an example of what
does work.

**Checking accuracy.** Every question is in the database:

```bash
docker compose exec db psql -U habitflow -d habitflow -c \
  "SELECT created_at, question, source, attempts, row_count, error FROM query_log ORDER BY query_id DESC LIMIT 20;"
```

`source` is `template`, `llm` or `none` (not answered), and `attempts` counts
the SQL tries. To measure the model itself, run the eval set:
`cd llm && GROQ_API_KEY=... python -m evals.questions`.

---

## 4. Operations

| Task | Command |
|---|---|
| Stop / start | `docker compose stop` / `docker compose start` |
| Restart one service | `docker compose restart bot` (or `api`, `llm`) |
| Follow logs | `docker compose logs -f api bot llm` |
| Health | `curl localhost:8000/health/db` |
| Upgrade | `git pull && docker compose up -d --build` |
| Back up | `docker compose exec db pg_dump -U habitflow habitflow > backup-YYYY-MM-DD.sql` |
| Restore | `docker compose exec -T db psql -U habitflow habitflow < backup.sql` |
| API docs | <http://localhost:8000/docs> (reachable only from this machine) |

- **Back up weekly, and always before an upgrade.**
- **Migrations are automatic.** On startup the API applies any new
  `api/migrations/*.sql` once and records it in `schema_migrations`.
- ⚠️ **`docker compose down -v` deletes the database volume**, which is all
  your data. `docker compose down` (without `-v`) is safe.

### Investigating a message

Every draft is recorded in `audit_log`:

```bash
docker compose exec db psql -U habitflow -d habitflow -c \
 "SELECT audit_id, status, user_input, error_message, duration_ms
  FROM audit_log ORDER BY audit_id DESC LIMIT 10;"
```

| `status` | Meaning |
|---|---|
| `pending` | Waiting for ✅ Save |
| `awaiting_input` | Waiting for a unit, or for approval of a new habit |
| `executed` | Saved |
| `cancelled` | Cancelled, or the new habit was declined |
| `superseded` | Replaced by a newer draft in the same chat |
| `failed` | Couldn't be understood; the reason is in `error_message` |

Each draft's `intent` column (JSON) also holds `feedback_history`, every
Edit correction made to it.

---

## 5. Making changes

1. **Branch:** `git switch -c my-change`.
2. **Change the code.**
   - **Database schema:** add `api/migrations/NNNN_name.sql` (next number,
     idempotent `IF NOT EXISTS`). The API applies it at startup everywhere.
   - **LLM behaviour:** edit `llm/semantics.yaml` (logging: `rules`, `units`,
     `sql_examples`; questions: `classifier`, `query`, `answer`). The API tests
     fail if a SQL example there wouldn't pass the SQL guards, or a question
     example wouldn't run on the `habit_logs` view.
   - **What questions can read:** the `habit_logs` view (a migration) and the
     function whitelist in `api/app/sqlguard.py` (`check_select_sql`).
3. **Test everything.** This needs no Docker: the tests start a throwaway
   Postgres themselves.
   ```bash
   pip install -r api/requirements-dev.txt -r llm/requirements-dev.txt -r bot/requirements-dev.txt
   cd api && pytest; cd ../llm && pytest; cd ../bot && pytest; cd ../e2e && pytest; cd ..
   ruff check .
   ```
4. **If you touched the prompt or the model**, score the real model:
   ```bash
   cd llm && GROQ_API_KEY=... python -m evals.run --min-pass 0.9
   ```
   This makes 33 Groq requests. It shows each failing case and which fields
   were missed. For questions (routing and text-to-SQL):
   ```bash
   cd llm && GROQ_API_KEY=... python -m evals.questions --min-pass 0.9
   ```
5. **Push and open a PR.** CI runs lint plus the api, llm, bot and e2e
   suites. Merge only when it's green.
6. **Deploy:** on the machine running the bot, `git pull && docker compose up -d --build`,
   or publish images and pull them (next section).

### Publishing to Docker Hub

The compose file tags the images `jackdiva/habitflow:api-<tag>`,
`:llm-<tag>` and `:bot-<tag>` (defaults: `HABITFLOW_IMAGE=jackdiva/habitflow`,
`HABITFLOW_TAG=latest`).

**On your build machine (from the repo):**

```bash
docker login                                   # as jackdiva
export HABITFLOW_TAG=1.0.0                     # a version; also push "latest" if you like
docker compose build
docker compose push api llm bot                # db is the official postgres image
```

**On a server (no source code needed):**

```bash
mkdir habitflow && cd habitflow
# copy docker-compose.yml here (scp, or download it from the GitHub repo)
nano .env && chmod 600 .env                     # same variables as .env.example
echo HABITFLOW_TAG=1.0.0 >> .env                # pin the release you pushed
docker compose pull
docker compose up -d
```

Only those two files are needed: the `build:` lines are ignored when the
images can be pulled, and the database schema is created by the API on
first start. (`docker compose publish` works too and never includes your
secrets, but a compose file run straight from `oci://` can't read a local
`.env`, so copying the file is the supported route.) Upgrading is `HABITFLOW_TAG=<new>` in `.env`, then `docker compose
pull && docker compose up -d`. Your data stays in the `habitflow_pgdata`
volume. The images are built for the architecture of the machine that built
them (usually `amd64`); for an ARM server build with
`docker buildx build --platform linux/arm64` or use a matching machine.

### The single image (one container)

`jackdiva/habitflow:all-<tag>` runs **everything in one container**: Postgres 16,
the LLM service, the API and the bot, under a small supervisor
(`allinone/run_all.py`) that starts them in order, restarts any that crash
and shuts them down cleanly. The API and database listen inside the
container only; nothing is published.

**Run it** (`.env` needs only `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_ID`,
`GROQ_API_KEY`, and optionally `APP_TIMEZONE` / `GROQ_MODEL_NAME`; **no
`DATABASE_URL`**):

```bash
docker run -d --name habitflow --restart unless-stopped \
  -v habitflow_data:/data --env-file .env jackdiva/habitflow:all-1.1.0
docker logs -f habitflow          # "[supervisor] bot is healthy", then message the bot
```

or `docker compose --env-file .env.single -f docker-compose.single.yml up -d`
(settings in `.env.single`, copied from `.env.single.example`).

- **Keep the `/data` volume.** It *is* your database. Without `-v …:/data`
  the data lives in an anonymous volume and is lost with the container.
- **Backups:** `docker exec habitflow habitflow-backup > backup-$(date +%F).sql`.
  Restore into a fresh container with
  `docker exec -i habitflow psql -h /run/postgresql -U habitflow habitflow < backup.sql`.
- **Upgrades:** `docker pull` the new tag, `docker rm -f habitflow`, and run
  it again with the same volume. Migrations apply on start.
- **Your own Postgres instead:** set `DATABASE_URL` (any of `postgres://`,
  `postgresql://` or `postgresql+psycopg://`). The built-in database is then
  not started.
- **Debugging:** `docker exec habitflow ps -ef` shows the four processes; the
  log lines tagged `[supervisor]` show starts, crashes and restarts.

**Moving your existing compose data into it.** The single image can adopt
the `habitflow_pgdata` volume from the four-container setup:

```bash
# in the old setup: back up first, then stop it (the volume is kept)
docker compose exec db pg_dump -U <POSTGRES_USER> <POSTGRES_DB> > backup-before-move.sql
docker compose down
docker run -d --name habitflow --restart unless-stopped \
  -v habitflow_pgdata:/data/pgdata \
  -e POSTGRES_USER=<same as your .env> -e POSTGRES_DB=<same as your .env> \
  --env-file .env.single jackdiva/habitflow:all-1.1.0
```

Here `.env.single` is your `.env` without `DATABASE_URL`. The volume is used
in place: Postgres 16 data is required, and the supervisor rebuilds indexes
if the text collation ever differs.

**On Render:** create a free Web Service from the image with its settings
from `.env.single.example`; see the README section "Deploy on Render for free".

### Using Neon (managed Postgres) instead

With the database on [Neon](https://neon.com), the app container stores
nothing itself, so it runs anywhere (Render with no disk, a VM, your PC) and
redeploys can't lose data. Neon's free plan has 0.5 GB storage, 100
CU-hours of compute a month, and a 6-hour restore window.

1. Create a Neon project (Postgres 16, 17 or 18 all work), in the region
   closest to where the app runs (for Render Singapore: AWS `ap-southeast-1`).
2. Copy the **direct** connection string (Connect → not the `-pooler` one):
   `postgresql://neondb_owner:…@ep-….neon.tech/neondb?sslmode=require&channel_binding=require`
3. Put it in `.env` as `DATABASE_URL=…` and start the single image **without
   a `/data` volume**, or set it as `DATABASE_URL` in the Render service's Environment.
   The API creates the schema on first start.

**Staying inside the free compute.** Neon sleeps after 5 minutes with no open
connection. For `*.neon.tech` hosts the API reuses connections while you're
active and closes them all after a quiet minute, and the health checks don't
touch the database, so Neon wakes only when you use the bot. That's typically a few
CU-hours a month, not the ~180 that an always-open connection would cost.
`DATABASE_POOL=on|idle|off` overrides the automatic choice. The first message
after a quiet spell takes a few hundred milliseconds longer while Neon wakes.

**Backups:** Neon keeps 6 hours of history on the free plan; for anything
older, run `docker exec habitflow habitflow-backup > backup-$(date +%F).sql`
now and then (it dumps the Neon database when `DATABASE_URL` is set).

---

## 6. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Bot doesn't answer at all | Wrong `TELEGRAM_ALLOWED_USER_ID` (others are ignored silently), or a bad token | Check the id with @userinfobot; `docker compose logs bot` |
| "I can't reach the server right now" | API down or unhealthy | `docker compose ps`, then `docker compose logs api` |
| API crashes mentioning `psycopg2` | `DATABASE_URL` is missing `+psycopg` | Fix `.env`, then `docker compose up -d` |
| Every card says ⚡ AI is offline | Bad Groq key, wrong model name, or rate limit | `docker compose logs llm` |
| A question gets "I can't work that one out right now" | The LLM is offline; only simple totals work without it | `docker compose logs llm`; ask "how much did I … this month" meanwhile |
| An answer looks wrong | Tap 🔍 SQL to see the query; check the habit and dates it used | `query_log` has every question and its SQL (section 3) |
| "Sorry, I couldn't turn that into a log" | The model failed 3 times | Rephrase ("ran 3 miles"); the reason is in `audit_log.error_message` |
| Logs land on the wrong day | `APP_TIMEZONE` isn't your zone | Set it in `.env`, then `docker compose up -d` |
| Bot keeps asking the same question | An old question is still open | `/cancel` |
| Undo says "Nothing to undo" | Everything recent is already undone | `/today` shows what's still logged |
