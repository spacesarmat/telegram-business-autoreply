from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("ADMIN_IDS", "1")
os.environ.setdefault("DATABASE_PATH", ":memory:")

from app.advanced import AdvancedService
from app.db import Database, utc_now_iso
from app.games import TicTacToeStore, bot_move, winner
from app.operations import OperationsService
from app.payments import PaymentService
from app.v22 import V22Service, guest_upper_bound


@pytest_asyncio.fixture
async def services(tmp_path):
    db = Database(str(tmp_path / "test.sqlite3"))
    advanced = AdvancedService(db, ZoneInfo("Europe/Moscow"))
    operations = OperationsService(db, ZoneInfo("Europe/Moscow"))
    payments = PaymentService(db)
    v22 = V22Service(db)
    await db.init()
    await advanced.init()
    await operations.init()
    await payments.init()
    await v22.init()
    return db, advanced, operations, payments, v22


def test_tic_tac_toe_rules_and_reset():
    store = TicTacToeStore()
    game, result = store.play(10, 0)
    assert game.board[0] == "X"
    assert game.board.count("O") == 1
    assert result is None
    reset = store.reset(10)
    assert reset.board == [""] * 9
    assert winner(["X", "X", "X", "", "", "", "", "", ""]) == "X"
    assert bot_move(["X", "", "X", "O", "O", "", "", "", ""]) == 5


def test_guest_parser_and_conditional_questions():
    assert guest_upper_bound("от 150 до 250") == 250
    assert guest_upper_bound("120 гостей") == 120
    assert guest_upper_bound("более 400") == 401
    assert V22Service.question_visible({"condition_question_id": 1, "condition_operator": "equals", "condition_value": "Свадьба"}, {"1": "свадьба"})
    assert not V22Service.question_visible({"condition_question_id": 1, "condition_operator": "gte", "condition_value": "150"}, {"1": "120"})


@pytest.mark.asyncio
async def test_additive_migration_keeps_old_submission(services):
    db, _, _, _, v22 = services
    forms = await db.list_forms()
    form = forms[0]
    session = {
        "form_id": form["id"], "chat_id": 100, "business_connection_id": "old",
        "submission_token": "old-token", "answers": {}, "user_id": 5,
        "username": "old", "first_name": "Old", "last_name": "Client",
    }
    sid = await db.create_form_submission(session, str(form["name"]), calculated_amount=10000)
    await v22.init()
    restored = await db.get_submission(sid)
    assert restored and restored["total_amount"] == 10000
    assert "discount_amount" in restored


@pytest.mark.asyncio
async def test_two_halls_capacity_filter(services):
    db, _, operations, _, v22 = services
    form_id = int((await db.list_forms())[0]["id"])
    venues = await operations.venues_for_form(form_id)
    assert len(venues) >= 2
    await operations.update_venue(int(venues[0]["id"]), name=venues[0]["name"], description="", enabled=True, capacity=300)
    await operations.update_venue(int(venues[1]["id"]), name=venues[1]["name"], description="", enabled=True, capacity=120)
    result = await v22.suitable_venues(form_id, "от 150 до 250")
    assert result[0]["suitable"] is True
    assert result[1]["suitable"] is False
    assert "120" in result[1]["capacity_reason"]


@pytest.mark.asyncio
async def test_hold_limit_and_separate_venues(services):
    db, _, operations, _, _ = services
    await db.set_setting("hold_rate_max", "2")
    venues = await operations.list_venues()
    form_id = int((await db.list_forms())[0]["id"])
    future = (datetime.now(ZoneInfo("Europe/Moscow")).date() + timedelta(days=30)).isoformat()
    segment = [(future, "20:00", "23:00")]
    ok, _ = await operations.acquire_hold(form_id=form_id, venue_id=int(venues[0]["id"]), chat_id=50, submission_token="a", segments=segment)
    assert ok
    # Another hall may hold the same time independently.
    ok, _ = await operations.acquire_hold(form_id=form_id, venue_id=int(venues[1]["id"]), chat_id=51, submission_token="b", segments=segment)
    assert ok
    ok, reason = await operations.acquire_hold(form_id=form_id, venue_id=int(venues[1]["id"]), chat_id=50, submission_token="c", segments=[(future, "10:00", "11:00")])
    assert ok
    ok, reason = await operations.acquire_hold(form_id=form_id, venue_id=int(venues[1]["id"]), chat_id=50, submission_token="d", segments=[(future, "12:00", "13:00")])
    assert not ok and "временных резервов" in reason


@pytest.mark.asyncio
async def test_addon_resource_remaining_quantity(services):
    db, _, operations, _, v22 = services
    form_id = int((await db.list_forms())[0]["id"])
    addon_id = await db.add_form_addon(form_id, "Радиомикрофон", 2000, quantity_enabled=True, min_quantity=1, max_quantity=6)
    resource_id = await operations.save_resource(resource_id=None, name="Радиомикрофон", quantity=4, venue_id=None)
    await v22.link_addon_resource(addon_id, resource_id)
    future = (datetime.now(timezone.utc).date() + timedelta(days=20)).isoformat()
    session = {"form_id": form_id, "chat_id": 12, "business_connection_id": "x", "submission_token": "res-1", "answers": {}}
    sid = await db.create_form_submission(session, "Тест")
    await db.add_availability_block(future, "18:00", "23:00", source_submission_id=sid)
    await operations.set_resource_allocation(sid, resource_id, 2)
    assert await v22.addon_available_quantity(addon_id, [(future, "19:00", "22:00")]) == 2


@pytest.mark.asyncio
async def test_promocode_client_profile_and_documents(services):
    db, _, _, _, v22 = services
    form_id = int((await db.list_forms())[0]["id"])
    sid = await db.create_form_submission({"form_id": form_id, "chat_id": 9, "business_connection_id": "x", "submission_token": "promo", "answers": {}}, "Тест", calculated_amount=10000)
    await v22.create_promotion(code="SALE10", name="Скидка", discount_type="percent", discount_value=10)
    assert await v22.apply_promotion(sid, "sale10", "c:9") == 1000
    assert int((await db.get_submission(sid))["total_amount"]) == 9000
    await v22.set_client_profile("c:9", blacklisted=True, blacklist_reason="no-show", tags=["VIP", "VIP"], loyalty_points=50, loyalty_level="silver")
    blocked, reason = await v22.client_blocked("c:9")
    assert blocked and reason == "no-show"
    async with db.connection() as conn:
        await conn.execute("INSERT INTO document_templates(kind,name,body,created_at,updated_at) VALUES('contract','Основной','Договор №{id}: {client}',?,?)", (utc_now_iso(), utc_now_iso()))
        await conn.commit()
    document_id = await v22.create_document(sid, "contract", {"id": sid, "client": "Иван"})
    assert document_id > 0
    checklist_id = await v22.add_checklist_item(sid, "Проверить зал", "before")
    await v22.complete_checklist_item(checklist_id)
    assert await v22.add_event_photo(sid, "after", "/data/uploads/after.jpg") > 0
    assert await v22.add_incident(sid, "Повреждён стул", deposit_withheld=1000) > 0
    assert await v22.queue_notification("manager1", "incident", "Проверить зал", submission_id=sid) > 0
    assert await v22.portal_token(sid) == await v22.portal_token(sid)


@pytest.mark.asyncio
async def test_multiple_payments_are_summed_idempotently(services, monkeypatch):
    db, _, _, payments, _ = services
    form_id = int((await db.list_forms())[0]["id"])
    sid = await db.create_form_submission({"form_id": form_id, "chat_id": 19, "business_connection_id": "x", "submission_token": "pay", "answers": {}}, "Тест", calculated_amount=10000)

    async def verify(payment_id):
        value = "2000.00" if payment_id == "p1" else "3000.00"
        return {"id": payment_id, "status": "succeeded", "amount": {"value": value, "currency": "RUB"}, "metadata": {"submission_id": str(sid)}}, "ok"

    monkeypatch.setattr(payments, "_fetch_yookassa_payment", verify)
    assert (await payments.handle_yookassa_notification({"object": {"id": "p1"}}))[0]
    assert (await payments.handle_yookassa_notification({"object": {"id": "p2"}}))[0]
    assert int((await db.get_submission(sid))["prepayment_amount"]) == 5000
    await payments.handle_yookassa_notification({"object": {"id": "p2"}})
    assert int((await db.get_submission(sid))["prepayment_amount"]) == 5000


@pytest.mark.asyncio
async def test_night_booking_and_monday_rule(services):
    _, advanced, operations, _, _ = services
    # Monday is weekday=0 and must not be mistaken for a missing value.
    form_id = 1
    await advanced.update_rule(form_id, {"enabled": 1, "closed_weekdays_json": "[0]"})
    today = datetime.now(ZoneInfo("Europe/Moscow")).date()
    monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    interval = {"date_iso": monday.isoformat(), "start_time": "21:00", "end_time": "02:00", "duration_minutes": 300, "segments": [(monday.isoformat(), "21:00", "24:00"), ((monday + timedelta(days=1)).isoformat(), "00:00", "02:00")]}
    assert "закрыта" in (await advanced.booking_rule_violation(form_id, interval) or "")
    # Overnight segment overlap remains detected by operational availability.
    venue = (await operations.list_venues())[0]
    await operations.db.add_availability_block(monday.isoformat(), "22:00", "24:00", venue_id=int(venue["id"]))
    assert await operations.availability_conflicts(venue_id=int(venue["id"]), date_iso=monday.isoformat(), start_time="21:00", end_time="24:00")
