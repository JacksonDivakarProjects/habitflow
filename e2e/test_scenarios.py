"""
Whole conversations: Telegram handler -> API -> Postgres, with a scripted
LLM. Each test reads like a chat transcript.
"""

from datetime import timedelta

from sqlalchemy import select, text

from app.models import AuditLog, DailyLog, Habit, QueryLog
from app.timeutil import today

TODAY = today().isoformat()
YESTERDAY = (today() - timedelta(days=1)).isoformat()
CARD_BUTTONS = ["✅ Save", "✏️ Change", "✖ Cancel", "🔍 SQL"]


def _logs(db):
    db.expire_all()
    return db.execute(
        select(DailyLog).where(DailyLog.voided_at.is_(None)).order_by(DailyLog.log_id)
    ).scalars().all()


# ------------------------------------------------------------------
# The everyday path
# ------------------------------------------------------------------
def test_log_save_and_undo(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent())

    assert chat.say("ran 4 miles") == ["📝 Log this?\n• Running · 4 miles · today"]
    assert chat.last.buttons == CARD_BUTTONS

    chat.tap("✅ Save")
    assert chat.last.text == (
        "✅ Saved\n• Running · 4 miles · today\n   🌱 day 1 of a new streak · 4 miles this week"
    )
    assert [float(x.amount) for x in _logs(db)] == [4.0]

    chat.tap("↩️ Undo")
    assert chat.last.text == "↩️ Undone: 4 miles of Running today"
    assert chat.last.buttons == []
    assert _logs(db) == []


def test_streak_grows_across_days(chat, db, fake_llm, make_intent):
    for days_ago in (2, 1):
        db.add(DailyLog(habit_id=1, amount=3, metric="miles", source="manual",
                        log_date=today() - timedelta(days=days_ago)))
    db.commit()
    fake_llm.queue(make_intent())

    chat.say("ran 4 miles")
    chat.tap("✅ Save")

    assert "🔥 3-day streak" in chat.last.text


def test_sql_button_on_a_card(chat, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    chat.say("ran 4 miles")
    card = chat.last

    before = len(chat.messages)
    chat.tap("🔍 SQL")
    [sql] = chat.texts_since(before)
    assert sql.startswith("🔍 SQL for this log\nINSERT INTO daily_logs")
    assert chat.last.buttons == ["✖ Close"]
    assert card.buttons == CARD_BUTTONS                      # the card is untouched

    chat.tap("✖ Close")
    assert chat.last is card and len(chat.deleted) == 1      # the SQL message is gone
    chat.tap("✅ Save")                                      # and the card still works
    assert chat.last.text.startswith("✅ Saved")


# ------------------------------------------------------------------
# Units
# ------------------------------------------------------------------
def test_unit_question_answered_with_a_button(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent(metric=None))

    [question] = chat.say("ran 4")
    assert question.startswith("❓ Got it: 4 of Running today. What unit?")
    assert chat.last.buttons[:3] == ["miles", "km", "meters"]

    chat.tap("km")                                         # no LLM call needed
    assert chat.last.text == "📝 Log this?\n• Running · 4 km · today"
    chat.tap("✅ Save")
    assert _logs(db)[0].metric == "km"


def test_unit_question_answered_by_typing(chat, db, fake_llm, make_intent):
    db.add(Habit(name="pushups", display_name="Pushups", metric=None))
    db.commit()
    fake_llm.queue(make_intent(habit_name="pushups", metric=None))

    chat.say("did 20 pushups")
    question = chat.last
    [card] = chat.say("reps")
    assert card == "📝 Log this?\n• Pushups · 4 reps · today"
    assert question.buttons == []                          # the old question is tidied
    chat.tap("✅ Save")
    assert _logs(db)[0].metric == "reps"


def test_cancel_a_unit_question(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent(metric=None))
    chat.say("ran 4")

    assert chat.say("never mind") == ["OK, dropped it."]
    assert db.execute(select(AuditLog.status)).scalars().all() == ["cancelled"]
    fake_llm.queue(make_intent())
    assert chat.say("ran 4 miles")[0].startswith("📝")    # back to normal


def test_new_log_while_a_unit_question_is_open(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent(metric=None, amount=5))
    chat.say("ran 5")                                      # "What unit?"
    fake_llm.queue(make_intent(habit_name="reading", amount=20, metric="pages"))

    dropped, card = chat.say("read 20 pages")

    assert dropped == "OK, I dropped “ran 5” (no unit) and read this as a new log."
    assert card == "📝 Log this?\n• Reading · 20 pages · today"
    chat.tap("✅ Save")
    [log] = _logs(db)
    assert (log.habit_id, log.metric) == (2, "pages")    # not "5 pages of Running"


def test_question_while_a_unit_question_is_open(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent(metric=None))
    chat.say("ran 4")

    answer, reminder = chat.say("how much did I run this week?")
    assert answer == "💬 No Running logged this week."
    assert "still waiting for the unit" in reminder

    chat.tap("miles")                                      # the question is still open
    chat.tap("✅ Save")
    assert _logs(db)[0].metric == "miles"


# ------------------------------------------------------------------
# New habits
# ------------------------------------------------------------------
def test_new_habit_created_then_unit_asked(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent(habit_name=None, proposed_habit="pushups", metric=None, amount=20))

    [offer] = chat.say("did 20 pushups")
    assert offer == (
        "✨ “Pushups” is a new habit. Create it and log 20 today? I'll ask for the unit next."
    )
    chat.tap("✨ Create habit")
    assert chat.last.text.startswith("❓ Created Pushups. What unit")

    chat.tap("reps")
    chat.tap("✅ Save")
    assert "• Pushups · 20 reps · today" in chat.last.text
    habit = db.execute(select(Habit).where(Habit.name == "pushups")).scalar_one()
    assert habit.metric is None  # no default: the habit keeps asking next time


def test_new_habit_declined(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent(habit_name=None, proposed_habit="pushups", metric="reps"))
    chat.say("did 20 pushups")

    chat.tap("✖ No")

    assert chat.last.text == "OK, I didn't create it."
    assert db.execute(select(Habit).where(Habit.name == "pushups")).first() is None


# ------------------------------------------------------------------
# ✏️ Change
# ------------------------------------------------------------------
def test_change_by_typing(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    chat.say("ran 4 miles")
    card = chat.last

    chat.tap("✏️ Change")
    assert chat.last.text.startswith("✏️ What should change?\n• Running · 4 miles · today")
    assert chat.last.buttons == ["📅 It was yesterday", "📅 It was today", "↩️ Keep as is"]

    fake_llm.queue(make_intent(amount=6, log_date=YESTERDAY))
    [updated] = chat.say("6 miles, and it was yesterday")
    assert updated == "🔄 Updated. Log this?\n• Running · 6 miles · yesterday"
    assert card.text.startswith("✏️ Changed: “6 miles, and it was yesterday”")
    assert card.buttons == []                              # the old card can't be saved

    chat.tap("✅ Save")
    [log] = _logs(db)
    assert (float(log.amount), log.log_date.isoformat()) == (6.0, YESTERDAY)


def test_change_with_the_yesterday_button(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    chat.say("ran 4 miles")
    chat.tap("✏️ Change")

    fake_llm.queue(make_intent(log_date=YESTERDAY))
    chat.tap("📅 It was yesterday")
    assert chat.last.text == "🔄 Updated. Log this?\n• Running · 4 miles · yesterday"
    assert "it was yesterday" in str(fake_llm.calls[-1])
    chat.tap("✅ Save")
    assert _logs(db)[0].log_date.isoformat() == YESTERDAY


def test_change_twice(chat, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    chat.say("ran 4 miles")
    for amount in (5, 7):
        chat.tap("✏️ Change")
        fake_llm.queue(make_intent(amount=amount))
        chat.say(f"{amount} miles")

    assert chat.last.text == "🔄 Updated. Log this?\n• Running · 7 miles · today"


def test_failed_change_stays_open(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    chat.say("ran 4 miles")
    chat.tap("✏️ Change")
    fake_llm.queue(*[make_intent(amount=0)] * 3)

    [reply] = chat.say("make it zero")
    assert reply.startswith("I couldn't apply that change.")

    fake_llm.queue(make_intent(amount=5))
    chat.say("ok then 5")                                  # the change stayed open
    chat.tap("✅ Save")
    assert [float(x.amount) for x in _logs(db)] == [5.0]


def test_change_into_a_new_habit(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    chat.say("ran 4 miles")
    card = chat.last
    chat.tap("✏️ Change")
    fake_llm.queue(make_intent(habit_name=None, proposed_habit="swimming", metric="laps"))

    [offer] = chat.say("no, it was swimming, 4 laps")

    assert card.buttons == []
    assert offer.startswith("✨ “Swimming” is a new habit.")
    chat.tap("✨ Create habit")
    chat.tap("✅ Save")
    assert "• Swimming · 4 laps · today" in chat.last.text


def test_keep_as_is(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    chat.say("ran 4 miles")
    chat.tap("✏️ Change")

    chat.tap("↩️ Keep as is")
    assert chat.last.text == "OK, I left the draft as it was."
    chat.tap("✅ Save")
    assert [float(x.amount) for x in _logs(db)] == [4.0]


def test_change_cancelled_with_a_word(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    chat.say("ran 4 miles")
    chat.tap("✏️ Change")

    assert chat.say("cancel") == ["OK, I left the draft as it was."]
    chat.tap("✅ Save")
    assert [float(x.amount) for x in _logs(db)] == [4.0]


def test_change_while_ai_is_down(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    chat.say("ran 4 miles")
    chat.tap("✏️ Change")
    fake_llm.queue(RuntimeError("groq down"))

    [updated] = chat.say("6 km, not 4")

    assert updated.startswith("🔄 Updated. Log this?\n• Running · 6 km · today")
    assert "AI is offline" in updated
    chat.tap("✅ Save")
    assert (float(_logs(db)[0].amount), _logs(db)[0].metric) == (6.0, "km")


def test_offline_change_switches_habit(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    chat.say("ran 4 miles")
    chat.tap("✏️ Change")
    fake_llm.queue(RuntimeError("groq down"))

    [updated] = chat.say("reading, not running")

    assert "• Reading · 4 pages · today" in updated


# ------------------------------------------------------------------
# Cancel, superseded cards, errors
# ------------------------------------------------------------------
def test_cancel(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    chat.say("ran 4 miles")

    chat.tap("✖ Cancel")

    assert chat.last.text == "✖ Cancelled. Nothing was saved."
    assert _logs(db) == []


def test_saving_an_older_card_explains_why_not(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent(), make_intent(amount=5))
    chat.say("ran 4 miles")
    old_card = chat.last
    chat.say("ran 5 miles")

    chat.tap("✅ Save", old_card)

    assert chat.popups == ["This draft was replaced by a newer one."]
    assert old_card.buttons                                # card left as it was
    chat.tap("✅ Save")                                    # the newer one works
    assert [float(x.amount) for x in _logs(db)] == [5.0]


def test_gibberish_with_ai_down(chat, fake_llm):
    fake_llm.queue(*[RuntimeError("groq down")] * 3)

    [reply] = chat.say("asdfgh 12")

    assert reply.startswith("I couldn't turn that into a log.")


def test_simple_log_with_ai_down(chat, db, fake_llm):
    fake_llm.queue(RuntimeError("groq down"))

    [card] = chat.say("read 20 pages yesterday")

    assert card.startswith("📝 Log this?\n• Reading · 20 pages · yesterday")
    assert "AI is offline" in card


def test_malicious_sql_from_the_model_never_runs(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent(draft_sql="DELETE FROM habits"), make_intent())

    [card] = chat.say("ran 4 miles")

    assert card.startswith("📝 Log this?")
    assert db.execute(text("SELECT count(*) FROM habits")).scalar() == 6


def test_strangers_are_ignored(bot_module, db, fake_llm):
    from conftest import Chat

    stranger = Chat(bot_module, user_id=12345)

    assert stranger.say("ran 4 miles") == []
    assert stranger.say("how much did I run?") == []
    assert stranger.say("/today") == []
    assert fake_llm.calls == []
    assert db.execute(select(AuditLog)).first() is None
    assert db.execute(select(QueryLog)).first() is None


# ------------------------------------------------------------------
# Several habits at once
# ------------------------------------------------------------------
def test_two_habits_in_one_message(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent(amount=3, extra_logs=[
        {"habit_name": "reading", "amount": 20, "metric": "pages", "log_date": TODAY},
        {"habit_name": "pushups", "amount": 10, "metric": "reps", "log_date": TODAY},
    ]))

    [card] = chat.say("ran 3 miles, read 20 pages and did 10 pushups")
    assert card.startswith(
        "📝 Log this?\n• Running · 3 miles · today\n• Reading · 20 pages · today")
    assert "⚠️ Not included: Pushups (new habit, send it on its own)" in card

    chat.tap("✅ Save")
    assert chat.last.text.count("• ") == 2
    assert len(_logs(db)) == 2

    chat.tap("↩️ Undo")
    assert _logs(db) == []


# ------------------------------------------------------------------
# Questions about the routine
# ------------------------------------------------------------------
def test_log_then_ask_how_much(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent(habit_name="reading", amount=20, metric="pages"))
    chat.say("read 20 pages")
    chat.tap("✅ Save")
    fake_llm.queue(make_intent(habit_name="reading", amount=15, metric="pages"))
    chat.say("read 15 pages")
    chat.tap("✅ Save")

    [answer] = chat.say("how much did I read this month?")
    assert answer == "💬 35 pages of Reading this month, on 1 day."
    assert len(fake_llm.calls) == 2                      # the question needed no LLM

    before = len(chat.messages)
    chat.tap("🔍 SQL")
    [sql] = chat.texts_since(before)
    assert sql.startswith("🔍 SQL used (built-in template)\nSELECT")
    assert "FROM habit_logs" in sql and "habit = 'reading'" in sql


def test_undone_logs_are_not_counted(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    chat.say("ran 4 miles")
    chat.tap("✅ Save")
    chat.tap("↩️ Undo")

    assert chat.say("did I run today") == ["💬 No, there's no Running logged today."]


def test_hours_question_converts_units(chat, db, fake_llm, make_intent):
    db.add(Habit(name="work", display_name="Work", metric="hours"))
    db.commit()
    for amount, unit in ((6, "hours"), (90, "minutes")):
        fake_llm.queue(make_intent(habit_name="work", amount=amount, metric=unit))
        chat.say(f"worked {amount} {unit}")
        chat.tap("✅ Save")

    assert chat.say("how many hours did I work this week") == [
        "💬 7.5 hours of Work this week, on 1 day."]
    assert chat.say("how many minutes did I work this week") == [
        "💬 450 minutes of Work this week, on 1 day."]


def test_pattern_question_goes_through_the_llm(chat, db, llm_http):
    for days_ago, pages in ((0, 10), (1, 30), (7, 20)):
        db.add(DailyLog(habit_id=2, amount=pages, metric="pages", source="manual",
                        log_date=today() - timedelta(days=days_ago)))
    db.commit()
    llm_http.queue("/query_sql", {"sql": "SELECT log_date, sum(amount) AS pages FROM habit_logs "
                                         "WHERE habit = 'reading' GROUP BY 1 ORDER BY 1"})
    llm_http.queue("/answer", {"answer": "You read on 3 days; your best day was 30 pages."})

    [answer] = chat.say("what is my reading pattern")

    lines = answer.splitlines()
    assert lines[0] == "💬 You read on 3 days; your best day was 30 pages."
    assert lines[2] == "📊 pages by log date"                # sentence, gap, chart title
    bars = lines[3:]
    assert len(bars) == 3 and all("█" in b for b in bars)   # one bar per day
    assert bars[1].rstrip().endswith("30")                   # yesterday: the longest bar
    assert max(bars, key=lambda b: b.count("█")) == bars[1]
    assert chat.last.buttons == ["🔍 SQL"]
    sent = llm_http.payloads("/query_sql")[0]
    assert sent["dates"]["today"] == TODAY


def test_bad_llm_sql_is_retried_then_answered(chat, db, llm_http):
    llm_http.queue("/query_sql",
                   {"sql": "DELETE FROM daily_logs"},
                   {"sql": "SELECT count(*) AS logs FROM habit_logs"})
    llm_http.queue("/answer", {"answer": "You have 0 logs so far."})

    assert chat.say("what's my overall consistency") == ["💬 You have 0 logs so far."]
    assert db.execute(text("SELECT count(*) FROM habits")).scalar() == 6
    [entry] = db.execute(select(QueryLog)).scalars().all()
    assert entry.attempts == 2


def test_complex_question_with_ai_down(chat):
    [answer] = chat.say("what is my reading pattern")
    assert answer.startswith("🤔 I can't work that one out right now")
    assert "how much did I read this month" in answer


def test_small_talk(chat):
    [reply] = chat.say("hi")
    assert reply.startswith("Log what you did")
    assert "how much did I read this month?" in reply


# ------------------------------------------------------------------
# Commands
# ------------------------------------------------------------------
def test_today_stats_and_undo_commands(chat, db, fake_llm, make_intent):
    assert chat.say("/today")[0].startswith("Nothing logged yet today.")
    fake_llm.queue(make_intent(amount=5, metric="km"), make_intent(amount=1))
    chat.say("ran 5 km")
    chat.tap("✅ Save")
    chat.say("ran 1 mile")
    chat.tap("✅ Save")

    assert chat.say("/today") == ["📅 Today so far\n• Running · 5 km\n• Running · 1 mile"]
    assert chat.say("/stats") == ["📊 Last 30 days\n• Running · 4.11 miles on 1 day"]
    assert chat.say("/undo") == ["↩️ Undone: 1 mile of Running today"]
    assert chat.say("/stats") == ["📊 Last 30 days\n• Running · 3.11 miles on 1 day"]
    assert chat.say("/cancel") == ["There's nothing to cancel."]


def test_help_and_start(chat):
    help_text = chat.say("/help")[0]
    assert "/today" in help_text and "/remind" not in help_text
    assert chat.say("/start")[0].startswith("👋 HabitFlow is ready.")


def test_sql_of_an_answer_opens_and_closes(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    chat.say("ran 4 miles")
    chat.tap("✅ Save")
    chat.say("how much did I run this week?")
    answer = chat.last

    chat.tap("🔍 SQL")
    assert chat.last.text.startswith("🔍 SQL used (built-in template)")
    chat.tap("✖ Close")
    assert chat.last is answer
    assert answer.buttons == ["🔍 SQL"]                      # can be opened again
    chat.tap("🔍 SQL")
    assert chat.last.text.startswith("🔍 SQL used")


def test_asking_to_change_a_saved_log_explains_undo(chat, db, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    chat.say("ran 4 miles")
    chat.tap("✅ Save")

    [reply] = chat.say("change today's run to 6 km")
    assert "can't be changed or deleted" in reply and "Undo" in reply
    assert [float(x.amount) for x in _logs(db)] == [4.0]     # no second 6 km log
    assert len(fake_llm.calls) == 1
