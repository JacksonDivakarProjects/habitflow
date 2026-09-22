"""
Whole conversations: Telegram handler -> API -> Postgres, with a scripted
LLM. Each test reads like a chat transcript.
"""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select, text

from app.models import AuditLog, DailyLog, Habit
from app.timeutil import today

TODAY = today().isoformat()
YESTERDAY = (today() - timedelta(days=1)).isoformat()


def _logs(db):
    db.expire_all()
    return db.execute(
        select(DailyLog).where(DailyLog.voided_at.is_(None)).order_by(DailyLog.log_id)
    ).scalars().all()


# ------------------------------------------------------------------
# The everyday path
# ------------------------------------------------------------------
def test_log_approve_and_undo(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent())

    [card] = chat.say("ran 4 miles")
    assert card.startswith("📝 4 miles of Running today")
    assert chat.last.buttons == ["✅ Approve", "✏️ Edit", "🗑️ Discard"]

    chat.tap("✅ Approve")
    assert chat.last.text == (
        "✅ Logged 4 miles of Running today\n🌱 Day 1 of a new streak · 4 miles this week"
    )
    assert [float(x.amount) for x in _logs(db)] == [4.0]

    chat.tap("↩️ Undo")
    assert chat.last.text == "↩️ Undone: 4 miles of Running today."
    assert chat.last.buttons == []
    assert _logs(db) == []


def test_streak_grows_across_days(chat, db, fake_llm, make_intent):
    for days_ago in (2, 1):
        db.add(DailyLog(habit_id=1, amount=3, metric="miles", source="manual",
                        log_date=today() - timedelta(days=days_ago)))
    db.commit()
    fake_llm.queue(make_intent())

    chat.say("ran 4 miles")
    chat.tap("✅ Approve")

    assert "🔥 3-day streak" in chat.last.text


def test_unknown_unit_is_asked_for(chat, db, fake_llm, make_intent):
    db.add(Habit(name="pushups", display_name="Pushups", metric=None))
    db.commit()
    fake_llm.queue(make_intent(habit_name="pushups", metric=None))

    assert chat.say("did 20 pushups") == [
        "Got it: 4 of Pushups today. What unit? (e.g. minutes, pages, km)"
    ]
    [card] = chat.say("reps")  # known-unit shortcut: no LLM call
    assert card.startswith("📝 4 reps of Pushups today")
    chat.tap("✅ Approve")
    assert _logs(db)[0].metric == "reps"


def test_cancel_a_unit_question(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent(metric=None))
    chat.say("ran 4")

    assert chat.say("never mind") == ["OK, dropped it."]
    assert db.execute(select(AuditLog.status)).scalars().all() == ["cancelled"]
    fake_llm.queue(make_intent())
    assert chat.say("ran 4 miles")[0].startswith("📝")  # back to normal


# ------------------------------------------------------------------
# New habits
# ------------------------------------------------------------------
def test_new_habit_created_then_unit_asked(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent(habit_name=None, proposed_habit="pushups", metric=None, amount=20))

    [offer] = chat.say("did 20 pushups")
    assert offer == (
        "“Pushups” is a new habit. Create it and log 20 today? I'll ask for the unit next."
    )
    chat.tap("✅ Create & log")
    assert chat.last.text.startswith("Created Pushups. What unit")

    chat.say("reps")
    chat.tap("✅ Approve")
    assert "✅ Logged 20 reps of Pushups today" in chat.last.text
    habit = db.execute(select(Habit).where(Habit.name == "pushups")).scalar_one()
    assert habit.metric is None  # no default: the habit keeps asking next time


def test_new_habit_declined(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent(habit_name=None, proposed_habit="pushups", metric="reps"))
    chat.say("did 20 pushups")

    chat.tap("❌ No thanks")

    assert chat.last.text == "OK, I didn't create it."
    assert db.execute(select(Habit).where(Habit.name == "pushups")).first() is None


# ------------------------------------------------------------------
# ✏️ Edit
# ------------------------------------------------------------------
def test_edit_corrects_the_card_in_place(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    chat.say("ran 4 miles")
    card = chat.last

    chat.tap("✏️ Edit")
    assert "What should change?" in card.text
    assert card.buttons == ["✅ Approve", "✏️ Edit", "🗑️ Discard"]  # still usable

    fake_llm.queue(make_intent(amount=6, log_date=YESTERDAY))
    assert chat.say("6 miles, and it was yesterday") == []  # no new message...
    assert card.text.startswith("🔄 Updated\n\n📝 6 miles of Running yesterday")  # ...edited

    chat.tap("✅ Approve", card)
    [log] = _logs(db)
    assert (float(log.amount), log.log_date.isoformat()) == (6.0, YESTERDAY)


def test_edit_twice(chat, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    chat.say("ran 4 miles")
    for amount in (5, 7):
        chat.tap("✏️ Edit")
        fake_llm.queue(make_intent(amount=amount))
        chat.say(f"{amount} miles")

    assert "7 miles of Running today" in chat.last.text


def test_failed_edit_leaves_card_usable(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    chat.say("ran 4 miles")
    chat.tap("✏️ Edit")
    fake_llm.queue(*[make_intent(amount=0)] * 3)

    [reply] = chat.say("make it zero")
    assert reply.startswith("I couldn't apply that change.")

    fake_llm.queue(make_intent(amount=5))
    chat.say("ok then 5")  # the edit stayed open
    chat.tap("✅ Approve")
    assert [float(x.amount) for x in _logs(db)] == [5.0]


def test_edit_into_a_new_habit(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    chat.say("ran 4 miles")
    card = chat.last
    chat.tap("✏️ Edit")
    fake_llm.queue(make_intent(habit_name=None, proposed_habit="swimming", metric="laps"))

    [offer] = chat.say("no, it was swimming, 4 laps")

    assert card.text == "✏️ Changed. See my next message." and card.buttons == []
    assert offer.startswith("“Swimming” is a new habit.")
    chat.tap("✅ Create & log")
    chat.tap("✅ Approve")
    assert "4 laps of Swimming" in chat.last.text


def test_edit_cancelled_with_a_word(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    chat.say("ran 4 miles")
    chat.tap("✏️ Edit")

    assert chat.say("cancel") == ["OK, I left the draft as it was."]
    chat.tap("✅ Approve")
    assert [float(x.amount) for x in _logs(db)] == [4.0]


def test_edit_while_ai_is_down(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    chat.say("ran 4 miles")
    chat.tap("✏️ Edit")
    fake_llm.queue(RuntimeError("groq down"))

    chat.say("6 km, not 4")

    assert "📝 6 km of Running today" in chat.last.text
    assert "AI is offline" in chat.last.text
    chat.tap("✅ Approve")
    assert (float(_logs(db)[0].amount), _logs(db)[0].metric) == (6.0, "km")


# ------------------------------------------------------------------
# Discard, superseded cards, errors
# ------------------------------------------------------------------
def test_discard(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    chat.say("ran 4 miles")

    chat.tap("🗑️ Discard")

    assert chat.last.text == "🗑️ Discarded."
    assert _logs(db) == []


def test_approving_an_older_card_explains_why_not(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent(), make_intent(amount=5))
    chat.say("ran 4 miles")
    old_card = chat.last
    chat.say("ran 5 miles")

    chat.tap("✅ Approve", old_card)

    assert chat.popups == ["This draft was replaced by a newer one."]
    assert old_card.buttons  # card left as it was
    chat.tap("✅ Approve")  # the newer one works
    assert [float(x.amount) for x in _logs(db)] == [5.0]


def test_gibberish_with_ai_down(chat, fake_llm):
    fake_llm.queue(*[RuntimeError("groq down")] * 3)

    [reply] = chat.say("asdfgh")

    assert reply.startswith("Sorry, I couldn't turn that into a log.")


def test_simple_log_with_ai_down(chat, db, fake_llm):
    fake_llm.queue(RuntimeError("groq down"))

    [card] = chat.say("read 20 pages yesterday")

    assert card.startswith("📝 20 pages of Reading yesterday")
    assert "AI is offline" in card


def test_malicious_sql_from_the_model_never_runs(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent(draft_sql="DELETE FROM habits"), make_intent())

    [card] = chat.say("ran 4 miles")

    assert card.startswith("📝 4 miles of Running today")
    assert db.execute(text("SELECT count(*) FROM habits")).scalar() == 6


def test_strangers_are_ignored(bot_module, db, fake_llm):
    from conftest import Chat

    stranger = Chat(bot_module, user_id=12345)

    assert stranger.say("ran 4 miles") == []
    assert stranger.say("/today") == []
    assert fake_llm.calls == []
    assert db.execute(select(AuditLog)).first() is None


# ------------------------------------------------------------------
# Several habits at once
# ------------------------------------------------------------------
def test_two_habits_in_one_message(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent(amount=3, extra_logs=[
        {"habit_name": "reading", "amount": 20, "metric": "pages", "log_date": TODAY},
        {"habit_name": "pushups", "amount": 10, "metric": "reps", "log_date": TODAY},
    ]))

    [card] = chat.say("ran 3 miles, read 20 pages and did 10 pushups")
    assert card.startswith("📝 3 miles of Running today\n📝 20 pages of Reading today")
    assert "⚠️ Not included: Pushups (new habit, send it on its own)" in card

    chat.tap("✅ Approve")
    assert chat.last.text.count("✅ Logged") == 2
    assert len(_logs(db)) == 2

    chat.tap("↩️ Undo")
    assert _logs(db) == []


# ------------------------------------------------------------------
# Commands
# ------------------------------------------------------------------
def test_today_stats_and_undo_commands(chat, db, fake_llm, make_intent):
    assert chat.say("/today")[0].startswith("Nothing logged yet today.")
    fake_llm.queue(make_intent(amount=5, metric="km"), make_intent(amount=1))
    chat.say("ran 5 km")
    chat.tap("✅ Approve")
    chat.say("ran 1 mile")
    chat.tap("✅ Approve")

    assert chat.say("/today") == ["📅 Today so far:\n• Running: 5 km\n• Running: 1 mile"]
    assert chat.say("/stats") == ["📊 Last 30 days:\n• Running: 4.11 miles on 1 day"]
    assert chat.say("/undo") == ["↩️ Undid your last log: 1 mile of Running today."]
    assert chat.say("/stats") == ["📊 Last 30 days:\n• Running: 3.11 miles on 1 day"]
    assert chat.say("/cancel") == ["There's nothing to cancel."]


def test_help_and_start(chat):
    assert "/remind" in chat.say("/help")[0]
    assert chat.say("/start")[0].startswith("👋 HabitFlow is ready.")


# ------------------------------------------------------------------
# Reminders
# ------------------------------------------------------------------
class _JobQueue:
    def __init__(self):
        self.jobs = []

    def run_daily(self, callback, time, chat_id, name):
        self.jobs.append(SimpleNamespace(callback=callback, time=time, chat_id=chat_id,
                                         name=name, removed=False,
                                         schedule_removal=lambda: None))

    def get_jobs_by_name(self, name):
        return []


def test_reminder_end_to_end(chat, db):
    chat.context.job_queue = _JobQueue()
    assert chat.say("/remind 9pm")[0].startswith("⏰ Done. I'll check in every day at 21:00")
    assert chat.say("/remind")[0].startswith("⏰ Reminders are on at 21:00")

    for days_ago in (1, 2):
        db.add(DailyLog(habit_id=1, amount=3, metric="miles", source="manual",
                        log_date=today() - timedelta(days=days_ago)))
    db.commit()

    job = chat.context.job_queue.jobs[-1]
    job_context = SimpleNamespace(job=job, bot=SimpleNamespace(send_message=chat._send_message))
    asyncio.run(job.callback(job_context))

    assert chat.last.text.splitlines()[:2] == [
        "⏰ Evening check-in",
        "🔥 Running: log it today to keep your 2-day streak going.",
    ]
    assert chat.say("/remind off") == ["🔕 Reminders are off."]


def test_reminder_is_silent_when_all_done(chat, db):
    db.add(DailyLog(habit_id=1, amount=3, metric="miles", source="manual", log_date=today()))
    db.commit()
    send = AsyncMock()
    job_context = SimpleNamespace(job=SimpleNamespace(chat_id=chat.user_id),
                                  bot=SimpleNamespace(send_message=send))

    asyncio.run(chat.bot.send_reminder(job_context))

    send.assert_not_awaited()


# ------------------------------------------------------------------
# Final review findings, end to end
# ------------------------------------------------------------------
def test_new_log_while_a_unit_question_is_open(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent(metric=None, amount=5))
    chat.say("ran 5")                                     # "What unit?"
    fake_llm.queue(make_intent(habit_name="reading", amount=20, metric="pages"))

    dropped, card = chat.say("read 20 pages")

    assert dropped == "OK, I dropped “ran 5” (no unit) and read this as a new log."
    assert card.startswith("📝 20 pages of Reading today")
    chat.tap("✅ Approve")
    [log] = _logs(db)
    assert (log.habit_id, log.metric) == (2, "pages")  # not "5 pages of Running"


def test_offline_edit_switches_habit(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    chat.say("ran 4 miles")
    card = chat.last
    chat.tap("✏️ Edit")
    fake_llm.queue(RuntimeError("groq down"))

    chat.say("reading, not running")

    assert "4 pages of Reading today" in card.text
