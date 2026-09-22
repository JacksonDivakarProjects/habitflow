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
3. **Review.** You get a card showing the preview and the SQL. **✏️ Feedback**
   regenerates the draft in place (Loop 2). **✅ Approve** writes it.
4. **Execute.** `api` builds its own parameterized `INSERT` from the validated
   fields. The LLM's SQL is only shown and dry-run checked, never executed.

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

> **Schema changes:** `db/init/*.sql` runs only when the `pgdata` volume is
> empty. To re-initialize (this **deletes all data**):
> `docker compose down -v && docker compose up -d`.

## Using the bot

| Send | Result |
|---|---|
| `ran 4 miles today` | Card: *4 miles of Running on …* → Approve |
| `read 20` | Uses the default unit (pages), marked *(suggested)* |
| `learned rust for 2 hours` | Offers to create a *Learning Rust* habit |
| `/habits` | Tracked habits and their default units |
| `/stats` | Totals for the last 30 days |
| `/cancel` | Drop an open unit question or feedback prompt |

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
