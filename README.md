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
| `db/init/` | Schema and seed data, applied when the Postgres volume is first created. |

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

If the LLM is unreachable, simple messages about known habits ("ran 3 miles
yesterday") still work. They are read by a regex parser (`api/app/parser.py`),
and the card is marked *AI is offline* so you know to check it.

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

`db/init/*.sql` is the baseline and only runs when the `pgdata` volume is
empty. Later changes go in `api/migrations/NNNN_name.sql`. The API applies
each file once, in order, at startup, and records it in
`schema_migrations`, so an existing database is upgraded in place. Write
migrations to be idempotent (`IF NOT EXISTS`).

## Using the bot

| Send | Result |
|---|---|
| `ran 4 miles` | Card: *4 miles of Running today* → ✅ → *🔥 3-day streak · 12 miles this week* |
| `read 20` | Uses the default unit (pages), marked *(suggested unit)* |
| `learned rust for 2 hours` | Offers to create a *Learning Rust* habit (and asks for a unit if it can't tell) |
| `/today` | What you've logged today |
| `/stats` | Totals and streaks for the last 30 days |
| `/undo` | Void your most recent log (it stays in the audit trail) |
| `/habits` | Tracked habits and their default units |
| `/cancel` | Drop an open unit question or edit |

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
ruff check .                                               # from repo root
```

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
