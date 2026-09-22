# HabitFlow: usage guide and SOP

How to set HabitFlow up, use it day to day, run it, change it, and fix it.
For how it works internally, see the [README](../README.md).

- [1. One-time setup](#1-one-time-setup)
- [2. Daily use](#2-daily-use)
- [3. Reminders and time](#3-reminders-and-time)
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

# APP_TIMEZONE=Asia/Kolkata   # see section 3
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

### Logging

| You send | What happens | What you do |
|---|---|---|
| `ran 4 miles` | Draft card: 📝 4 miles of Running today | ✅ Approve, ✏️ Edit or 🗑️ Discard |
| `read 20` | Uses the habit's default unit, marked *(suggested unit)* | Approve, or Edit it |
| `ran 3 miles and read 20 pages` | One card, one line per log | One Approve saves them all |
| `did 20 pushups` (a new habit) | "“Pushups” is a new habit. Create it?" | ✅ Create & log, or ❌ No thanks |
| A habit with no default unit | "What unit? (e.g. …)" | Reply `reps`, `km`, …, or `cancel` |
| `read 30 pages yesterday`, `ran 5k 2 days ago` | Draft dated back | Approve |

Nothing is saved until you tap **✅ Approve**.

### After approving

The card becomes `✅ Logged 4 miles of Running today` with a line such as
`🔥 3-day streak · 12 miles this week`, and a **↩️ Undo** button. Undo
removes every log from that card. Undone logs are kept in the audit trail
but excluded from stats, streaks and `/today`.

### Correcting a draft (✏️ Edit)

Tap **✏️ Edit**, then reply with the fix in plain words:

- `6 miles, not 4`
- `it was yesterday`
- `km not miles`
- `wrong habit, it was reading`
- `also read 10 pages`

The card updates in place (🔄 Updated). If the fix can't be applied, the
draft stays exactly as it was: you can try again, approve it anyway, or
reply `cancel`. If the fix turns the draft into a question (a new habit, a
missing unit), the old card says "✏️ Changed. See my next message."

### Commands

| Command | What it does |
|---|---|
| `/today` | What you've logged today |
| `/stats` | 30-day totals and streaks. Compatible units are combined (km + miles, minutes + hours) |
| `/undo` | Undo your most recent log |
| `/habits` | Your habits and their default units |
| `/remind 21:00`, `/remind 9pm`, `/remind off`, `/remind` | Daily check-in (see [section 3](#3-reminders-and-time)) |
| `/cancel`, or reply `cancel` / `never mind` | Drop an open question or edit |
| `/help` | The in-bot version of this section |

### Signals on a card

- **⚡ AI is offline**: Groq couldn't be reached, so the simple parser read
  your message. Check the card before approving.
- **⚠️ Not included: …**: part of the message couldn't be logged together
  (usually a brand-new habit). Send that part on its own.
- **Popup "This draft was replaced by a newer one."**: you tapped an older
  card. Only your latest draft can be approved.

---

## 3. Reminders and time

### What a reminder does

Once a day, at the time you choose, the bot checks your habits and sends
**one** check-in message, but only if there is something to act on:

```
⏰ Evening check-in
🔥 Running: log it today to keep your 5-day streak going.
🌱 Reels: you started yesterday. Log it today to make it 2 days.
Not logged yet today: Meditation.
✅ Done today: Reading.
Just reply here, like “ran 3 miles”.
```

If everything is already logged, **no message is sent**.

### Setting it

| You send | Result |
|---|---|
| `/remind 21:00` | Daily at 21:00 |
| `/remind 9pm`, `/remind 9:15 PM` | 12-hour clock works too (21:00, 21:15) |
| `/remind 7` | A bare number is the 24-hour clock: 07:00 |
| `/remind 12am` / `/remind 12pm` | Midnight / noon |
| `/remind` | Shows the current setting |
| `/remind off` | Stops reminders. The time is remembered for next time |
| `/remind 20:30` | Changes the time. The old schedule is replaced, never duplicated |

Rejected (the bot replies "I didn't understand that time"): `25:00`, `9:60`,
`13pm`, `noon`, `9.30`. There is one reminder per chat.

### How the timing works

```
/remind 9pm
  │
  ├─ bot → API  PUT /internal/reminders {"remind_at": "9pm"}
  │             API parses it to 21:00 and stores it in reminder_settings
  │             (survives restarts)
  │
  └─ bot schedules a daily job at 21:00 in APP_TIMEZONE
        (python-telegram-bot's job queue, job name "reminder:<chat_id>")

Every day at 21:00 (APP_TIMEZONE)
  bot → API  GET /internal/reminders/check
        API works out "today" in APP_TIMEZONE and sorts each active habit:
          logged today                          → "Done today"
          no log in the last 14 days            → skipped (dormant, no nagging)
          streak running through yesterday      → "🔥 keep your streak" / "🌱"
          otherwise                             → "Not logged yet today"
  bot sends the message, or nothing if there's nothing to act on
```

**Time zone.** The reminder time and the meaning of "today" both come from
`APP_TIMEZONE` (default `Asia/Kolkata`). The bot uses it to schedule the job
and the API uses it to decide what counts as today, so they always agree.
Both read the same `.env`. If you set it, use an IANA name such as
`Europe/London` or `America/New_York`, then run `docker compose up -d` to
restart. Daylight-saving changes are handled automatically: 21:00 stays
21:00 local time.

**What "today" means at reminder time.** The check runs when the reminder
fires, so pick an evening time. A reminder at `00:30` would evaluate the
*new* day, when you haven't logged anything yet, and every habit would
show as not logged.

**Streak rules used by the reminder.** A streak counts consecutive days with
at least one log, ending today, or ending yesterday if today isn't logged
yet. At reminder time, a habit logged yesterday but not today has a streak
"at risk": log it before midnight (in `APP_TIMEZONE`) to keep it.

**Restarts.** Settings live in the database. When the bot starts, it
reloads every enabled reminder from the API and schedules it again. Compose
starts the bot only after the API is healthy, so this normally just works.
If the API happened to be unreachable at that moment, the bot logs "API not
reachable on startup" and schedules nothing. Send `/remind` with your time
again, or run `docker compose restart bot`.

**Downtime.** If the bot isn't running at the exact reminder time, that
day's check-in may be skipped. It isn't guaranteed to be sent late. The
next day's runs as normal.

**Checking it's scheduled.** After a bot restart, `docker compose logs bot`
shows `reminder scheduled for <chat_id> at 21:00`. The stored setting is in
the database:

```bash
docker compose exec db psql -U habitflow -d habitflow -c "SELECT * FROM reminder_settings;"
```

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
| `pending` | Waiting for Approve |
| `awaiting_input` | Waiting for a unit, or for approval of a new habit |
| `executed` | Saved |
| `cancelled` | Discarded, or the new habit was declined |
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
   - **LLM behaviour:** edit `llm/semantics.yaml`. The API tests fail if a
     SQL example there wouldn't pass the SQL guard.
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
   were missed.
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

---

## 6. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Bot doesn't answer at all | Wrong `TELEGRAM_ALLOWED_USER_ID` (others are ignored silently), or a bad token | Check the id with @userinfobot; `docker compose logs bot` |
| "I can't reach the server right now" | API down or unhealthy | `docker compose ps`, then `docker compose logs api` |
| API crashes mentioning `psycopg2` | `DATABASE_URL` is missing `+psycopg` | Fix `.env`, then `docker compose up -d` |
| Every card says ⚡ AI is offline | Bad Groq key, wrong model name, or rate limit | `docker compose logs llm` |
| "Sorry, I couldn't turn that into a log" | The model failed 3 times | Rephrase ("ran 3 miles"); the reason is in `audit_log.error_message` |
| Logs land on the wrong day | `APP_TIMEZONE` isn't your zone | Set it in `.env`, then `docker compose up -d` |
| Reminder never arrives | Not set; nothing needed logging (it stays quiet on purpose); or the bot was down at that time | `/remind` to check; `docker compose logs bot` for "reminder scheduled" |
| Reminder at the wrong hour | `APP_TIMEZONE` differs from where you are | Set `APP_TIMEZONE`, restart, then `/remind <time>` again |
| Bot keeps asking the same question | An old question is still open | `/cancel` |
| Undo says "Nothing to undo" | Everything recent is already undone | `/today` shows what's still logged |
