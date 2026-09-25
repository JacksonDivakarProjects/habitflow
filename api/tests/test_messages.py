"""/internal/message routes a chat message; draft cards carry what the bot renders."""

from sqlalchemy import select

from app.models import AuditLog, QueryLog
from app.timeutil import today

CHAT = 11


def send(client, text):
    r = client.post("/internal/message", json={"chat_id": CHAT, "text": text, "message_id": 5})
    assert r.status_code == 200, r.text
    return r.json()


def test_a_log_becomes_a_draft_card(client, db, fake_llm, make_intent):
    fake_llm.queue(make_intent(amount=5, metric="km"))
    body = send(client, "ran 5 km")
    assert (body["kind"], body["classified_by"]) == ("log", "rules")
    card = body["draft"]
    assert card["status"] == "pending"
    assert card["items"] == [{
        "habit": "Running", "amount": 5.0, "unit": "km", "quantity": "5 km",
        "log_date": today().isoformat(), "when": "today",
    }]
    assert db.get(AuditLog, card["audit_id"]).message_id == 5


def test_multi_log_card_has_one_item_per_log(client, fake_llm, make_intent):
    fake_llm.queue(make_intent(extra_logs=[
        {"habit_name": "reading", "amount": 20, "metric": "pages", "log_date": today().isoformat()}
    ]))
    items = send(client, "ran 4 miles and read 20 pages")["draft"]["items"]
    assert [i["quantity"] for i in items] == ["4 miles", "20 pages"]
    assert [i["habit"] for i in items] == ["Running", "Reading"]


def test_a_question_gets_an_answer_not_a_draft(client, db, fake_llm):
    body = send(client, "how much did I read this month?")
    assert body["kind"] == "answer" and body["draft"] is None
    assert body["answer"]["source"] == "template"
    assert body["answer"]["answer"] == "No Reading logged this month."
    assert fake_llm.calls == []                                       # no draft attempted
    assert db.execute(select(AuditLog)).first() is None
    assert db.get(QueryLog, body["answer"]["query_id"]).question.startswith("how much")


def test_small_talk_gets_help(client, db):
    body = send(client, "hi")
    assert body["kind"] == "chat" and "ran 5 km" in body["text"]
    assert "how much did I read" in body["text"]
    assert db.execute(select(QueryLog)).first() is None


def test_unparseable_log_gets_a_friendly_error(client, fake_llm):
    fake_llm.queue(*[RuntimeError("down")] * 3)
    body = send(client, "7 zzz qqq")
    assert body["kind"] == "error" and "ran 5 km" in body["text"]


def test_unclear_message_asks_the_classifier(client, llm_http, fake_llm, make_intent):
    llm_http.queue("/classify", {"kind": "log"})
    fake_llm.queue(make_intent(amount=4))
    body = send(client, "running this morning, four miles")
    assert (body["kind"], body["classified_by"]) == ("log", "llm")
    assert llm_http.payloads("/classify")[0]["habits"]           # classifier sees the habits


def test_unit_question_offers_the_habits_units_first(client, fake_llm, make_intent):
    fake_llm.queue(make_intent(metric=None))
    card = send(client, "ran 4")["draft"]
    assert card["needs_input"] == "unit"
    assert card["unit_options"][:3] == ["miles", "km", "meters"]
    assert len(card["unit_options"]) == 5


def test_new_habit_without_unit_gets_generic_options(client, fake_llm, make_intent):
    fake_llm.queue(make_intent(habit_name=None, proposed_habit="pushups", metric=None))
    card = send(client, "did 20 pushups")["draft"]
    assert card["needs_input"] == "habit"
    r = client.post("/internal/approve_habit", json={"audit_id": card["audit_id"], "accept": True})
    assert r.json()["needs_input"] == "unit"
    assert r.json()["unit_options"] == ["minutes", "hours", "reps", "pages", "km"]


def test_draft_lookup_for_the_sql_button(client, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    card = send(client, "ran 4 miles")["draft"]
    r = client.get(f"/internal/drafts/{card['audit_id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "pending" and "INSERT INTO daily_logs" in body["draft_sql"]
    assert body["final_sql"] is None

    client.post("/internal/execute", json={"audit_id": card["audit_id"]})
    after = client.get(f"/internal/drafts/{card['audit_id']}").json()
    assert after["status"] == "executed" and after["final_sql"]
    assert client.get("/internal/drafts/424242").status_code == 404


def test_the_old_draft_endpoint_still_works(client, fake_llm, make_intent):
    fake_llm.queue(make_intent())
    r = client.post("/internal/draft", json={"chat_id": CHAT, "text": "ran 4 miles"})
    assert r.status_code == 200 and r.json()["items"][0]["quantity"] == "4 miles"


def test_changing_a_saved_log_is_explained_not_logged(client, db, fake_llm):
    body = send(client, "change yesterday's run to 6 km")
    assert body["kind"] == "error" and "Undo" in body["text"]
    assert fake_llm.calls == []                               # no 6 km draft
    assert db.execute(select(AuditLog)).first() is None
