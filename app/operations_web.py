from __future__ import annotations

import io
import json
from datetime import datetime
from typing import Any
from urllib.parse import quote

from aiohttp import web
import qrcode
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


def _e(value: Any) -> str:
    import html
    return html.escape(str(value if value is not None else ""), quote=True)


def _money(value: Any, currency: str = "₽") -> str:
    try:
        n = int(value or 0)
    except Exception:
        n = 0
    return f"{n:,}".replace(",", " ") + f" {currency}"


async def operations_page(admin, request: web.Request) -> web.Response:
    op = admin.operations
    health = await op.health_snapshot()
    venues = await op.list_venues(include_disabled=True)
    holds = await op.list_active_holds(50)
    waitlist = await op.list_waitlist(limit=100)
    resources = await op.list_resources()
    packages = await op.list_packages()
    venue_pricing = await op.list_venue_pricing()
    venue_rules = await op.list_venue_rules()
    venue_recurring = await op.list_all_venue_recurring_blocks()
    tasks = await op.list_tasks(status="open", limit=100)
    public_reqs = await op.list_public_requests(100)
    forms = await admin.db.list_forms(enabled_only=False)
    currency = str(await admin.db.get_setting("crm_currency", "₽") or "₽")
    csrf = admin._csrf(request)

    venue_rows = "".join(
        f'''<tr><td>#{v['id']}</td><td><form method="post" action="/admin/operations/venue/{v['id']}"><input name="csrf" type="hidden" value="{csrf}"><input name="name" value="{_e(v['name'])}"><textarea name="description">{_e(v.get('description') or '')}</textarea><label><input style="width:auto" type="checkbox" name="enabled" value="1" {'checked' if v.get('enabled') else ''}> включён</label><button class="btn" type="submit">Сохранить</button></form></td></tr>'''
        for v in venues
    ) or '<tr><td colspan="2">Нет залов</td></tr>'

    venue_options = '<option value="">Общий ресурс</option>' + ''.join(
        f'<option value="{v["id"]}">{_e(v["name"])}</option>' for v in venues if v.get("enabled")
    )
    resource_rows = ''.join(
        f'<tr><td>{_e(r["name"])}</td><td>{_e(r.get("venue_name") or "общий")}</td><td>{int(r.get("quantity") or 0)}</td><td><form method="post" action="/admin/operations/resource/{r["id"]}/delete"><input name="csrf" type="hidden" value="{csrf}"><button class="btn danger">Удалить</button></form></td></tr>'
        for r in resources
    ) or '<tr><td colspan="4">Ресурсы не настроены</td></tr>'
    package_rows = ''.join(
        f'<tr><td>{_e(p["name"])}</td><td>{_e(p.get("venue_name") or "все")}</td><td>{_money(p.get("amount"),currency)}</td><td>{_e(p.get("description") or "")}</td></tr>' for p in packages
    ) or '<tr><td colspan="4">Пакеты не настроены</td></tr>'
    venue_pricing_rows = ''.join(
        f'<tr><td>{_e(vp.get("form_name"))}</td><td>{_e(vp.get("venue_name"))}</td><td>{_money(vp.get("base_amount"),currency)}</td><td>{int(vp.get("included_hours") or 0)} ч</td><td>{_money(vp.get("extra_hour_amount"),currency)}</td><td>{"вкл" if vp.get("enabled") else "выкл"}</td></tr>' for vp in venue_pricing
    ) or '<tr><td colspan="6">Отдельные тарифы по залам не настроены — используется тариф формы.</td></tr>'
    weekday_names = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]
    venue_rule_rows = ''.join(
        f'<tr><td>{_e(r.get("form_name"))}</td><td>{_e(r.get("venue_name"))}</td><td>{"вкл" if r.get("enabled") else "выкл"}</td><td>{int(r.get("min_duration_minutes") or 0)} мин</td><td>{int(r.get("min_lead_hours") or 0)} ч</td><td>{int(r.get("max_advance_days") or 0)} дн</td><td>{_e(", ".join(weekday_names[int(x)] for x in r.get("closed_weekdays") or [] if 0 <= int(x) <= 6) or "—")}</td></tr>'
        for r in venue_rules
    ) or '<tr><td colspan="7">Отдельные правила залов не настроены — действуют общие правила формы.</td></tr>'
    venue_recurring_rows = ''.join(
        f'<tr><td>{_e(b.get("form_name"))}</td><td>{_e(b.get("venue_name"))}</td><td>{weekday_names[int(b.get("weekday") or 0)]}</td><td>{_e((str(b.get("start_time"))+"–"+str(b.get("end_time"))) if b.get("start_time") and b.get("end_time") else "весь день")}</td><td>{_e(b.get("note") or "—")}</td><td><form method="post" action="/admin/operations/venue-recurring/{int(b["id"])}/delete"><input name="csrf" type="hidden" value="{csrf}"><button class="btn danger">Удалить</button></form></td></tr>'
        for b in venue_recurring
    ) or '<tr><td colspan="6">Регулярной занятости по залам нет.</td></tr>'
    hold_rows = ''.join(
        f'<tr><td>{_e(h.get("venue_name") or "—")}</td><td>{_e(h["date_iso"])} { _e(h["start_time"])}–{_e(h["end_time"])}</td><td>{_e(h["expires_at"])}</td></tr>' for h in holds
    ) or '<tr><td colspan="3">Активных hold нет</td></tr>'
    wait_rows = ''.join(
        f'<tr><td>#{w["id"]}</td><td>{_e(w.get("venue_name") or "—")}</td><td>{_e(w["date_iso"])} {_e(w.get("start_time") or "")}–{_e(w.get("end_time") or "")}</td><td>{_e(w.get("username") or w.get("chat_id"))}</td><td>{_e(w["status"])}</td></tr>' for w in waitlist
    ) or '<tr><td colspan="5">Лист ожидания пуст</td></tr>'
    task_rows = ''.join(
        f'<tr><td>#{t["submission_id"]}</td><td>{_e(t["title"])}</td><td>{_e(t.get("assignee") or "—")}</td><td>{_e(t.get("due_at") or "—")}</td><td><form method="post" action="/admin/operations/task/{t["id"]}/done"><input name="csrf" type="hidden" value="{csrf}"><button class="btn">Готово</button></form></td></tr>' for t in tasks
    ) or '<tr><td colspan="5">Открытых задач нет</td></tr>'
    public_rows = ''.join(
        f'<tr><td>#{p["id"]}</td><td>{_e(p.get("venue_name") or "—")}</td><td>{_e(p["name"])}</td><td>{_e(p["contact"])}</td><td>{_e(p["date_iso"])} {_e(p.get("start_time") or "")}–{_e(p.get("end_time") or "")}</td><td>{_e(p.get("guests") or "")}</td></tr>' for p in public_reqs
    ) or '<tr><td colspan="6">Публичных запросов нет</td></tr>'
    form_options = ''.join(f'<option value="{f["id"]}">{_e(f["name"])}</option>' for f in forms)
    resource_options = ''.join(f'<option value="{r["id"]}">{_e(r["name"])} ({int(r.get("quantity") or 0)})</option>' for r in resources if r.get("enabled"))
    package_options = ''.join(f'<option value="{p["id"]}">{_e(p["name"])} · {_money(p.get("amount"),currency)}</option>' for p in packages if p.get("enabled"))

    content = f'''
<div class="grid">
<div class="card"><div class="muted">Залов</div><div class="metric">{health['venues']}</div></div>
<div class="card"><div class="muted">Активных hold</div><div class="metric">{health['holds']}</div></div>
<div class="card"><div class="muted">Лист ожидания</div><div class="metric">{health['waitlist']}</div></div>
<div class="card"><div class="muted">Открытых задач</div><div class="metric">{health['tasks']}</div></div>
<div class="card"><div class="muted">Новых web-запросов</div><div class="metric">{health['public_requests']}</div></div>
<div class="card"><div class="muted">SQLite</div><div class="metric">{round(health['db_bytes']/1024/1024,1)} МБ</div></div>
</div>

<div class="section"><h2>🏭 Залы / площадки</h2><div class="table-wrap"><table><tbody>{venue_rows}</tbody></table></div>
<form method="post" action="/admin/operations/venue" class="card section"><input name="csrf" type="hidden" value="{csrf}"><div class="row"><div class="field"><label>Новый зал</label><input name="name" placeholder="Зал 3" required></div><div class="field"><label>Описание</label><input name="description"></div></div><button class="btn primary">Добавить зал</button></form></div>

<div class="section"><h2>⏳ Временные hold</h2><div class="table-wrap"><table><thead><tr><th>Зал</th><th>Интервал</th><th>Истекает</th></tr></thead><tbody>{hold_rows}</tbody></table></div></div>
<div class="section"><h2>📝 Лист ожидания</h2><div class="table-wrap"><table><thead><tr><th>№</th><th>Зал</th><th>Интервал</th><th>Клиент</th><th>Статус</th></tr></thead><tbody>{wait_rows}</tbody></table></div></div>

<div class="section"><h2>🎛 Ресурсы / оборудование</h2><div class="table-wrap"><table><thead><tr><th>Ресурс</th><th>Зал</th><th>Количество</th><th></th></tr></thead><tbody>{resource_rows}</tbody></table></div>
<form method="post" action="/admin/operations/resource" class="card section"><input name="csrf" type="hidden" value="{csrf}"><div class="row3"><div class="field"><label>Название</label><input name="name" required></div><div class="field"><label>Количество</label><input name="quantity" type="number" min="0" value="1"></div><div class="field"><label>Зал</label><select name="venue_id">{venue_options}</select></div></div><button class="btn primary">Добавить ресурс</button></form></div>

<div class="section"><h2>💰 Тарифы по залам</h2><div class="table-wrap"><table><thead><tr><th>Форма</th><th>Зал</th><th>База</th><th>Включено</th><th>Доп. час</th><th>Статус</th></tr></thead><tbody>{venue_pricing_rows}</tbody></table></div>
<form method="post" action="/admin/operations/venue-pricing" class="card section"><input name="csrf" type="hidden" value="{csrf}"><div class="row3"><div class="field"><label>Форма</label><select name="form_id">{form_options}</select></div><div class="field"><label>Зал</label><select name="venue_id">{''.join(f'<option value="{v["id"]}">{_e(v["name"])}</option>' for v in venues)}</select></div><div class="field"><label>Включить override</label><select name="enabled"><option value="1">да</option><option value="0">нет</option></select></div></div><div class="row3"><div class="field"><label>Базовая цена</label><input name="base_amount" type="number" min="0" value="0"></div><div class="field"><label>Включено часов</label><input name="included_hours" type="number" min="0" value="0"></div><div class="field"><label>Доп. час</label><input name="extra_hour_amount" type="number" min="0" value="0"></div></div><div class="row"><div class="field"><label>Буфер до, мин</label><input name="buffer_before_minutes" type="number" min="0" value="0"></div><div class="field"><label>Буфер после, мин</label><input name="buffer_after_minutes" type="number" min="0" value="0"></div></div><div class="field"><label>Что входит в базовую стоимость</label><textarea name="base_description"></textarea></div><button class="btn primary">Сохранить тариф зала</button></form></div>

<div class="section"><h2>🕒 Правила бронирования по залам</h2><div class="table-wrap"><table><thead><tr><th>Форма</th><th>Зал</th><th>Статус</th><th>Мин. длительность</th><th>До начала</th><th>Горизонт</th><th>Закрыто</th></tr></thead><tbody>{venue_rule_rows}</tbody></table></div>
<form method="post" action="/admin/operations/venue-rule" class="card section"><input name="csrf" type="hidden" value="{csrf}"><div class="row3"><div class="field"><label>Форма</label><select name="form_id">{form_options}</select></div><div class="field"><label>Зал</label><select name="venue_id">{''.join(f'<option value="{v["id"]}">{_e(v["name"])}</option>' for v in venues)}</select></div><div class="field"><label>Правила</label><select name="enabled"><option value="1">включены</option><option value="0">выключены</option></select></div></div><div class="row3"><div class="field"><label>Мин. длительность, мин</label><input name="min_duration_minutes" type="number" min="0" value="0"></div><div class="field"><label>Минимум до начала, ч</label><input name="min_lead_hours" type="number" min="0" value="0"></div><div class="field"><label>Максимум вперёд, дней</label><input name="max_advance_days" type="number" min="0" value="365"></div></div><div class="field"><label>Закрытые дни недели (0=Пн … 6=Вс, через запятую)</label><input name="closed_weekdays" placeholder="0,1"></div><div class="field"><label>Часы по дням JSON (необязательно)</label><textarea name="day_hours_json" placeholder='{{"0":["10:00","23:00"],"5":["12:00","24:00"]}}'></textarea></div><button class="btn primary">Сохранить правила зала</button></form></div>

<div class="section"><h2>🔁 Регулярная занятость по залам</h2><div class="table-wrap"><table><thead><tr><th>Форма</th><th>Зал</th><th>День</th><th>Время</th><th>Комментарий</th><th></th></tr></thead><tbody>{venue_recurring_rows}</tbody></table></div>
<form method="post" action="/admin/operations/venue-recurring" class="card section"><input name="csrf" type="hidden" value="{csrf}"><div class="row3"><div class="field"><label>Форма</label><select name="form_id">{form_options}</select></div><div class="field"><label>Зал</label><select name="venue_id">{''.join(f'<option value="{v["id"]}">{_e(v["name"])}</option>' for v in venues)}</select></div><div class="field"><label>День недели</label><select name="weekday">{''.join(f'<option value="{i}">{name}</option>' for i,name in enumerate(weekday_names))}</select></div></div><div class="row3"><div class="field"><label>Начало (пусто = весь день)</label><input type="time" name="start_time"></div><div class="field"><label>Окончание</label><input type="time" name="end_time"></div><div class="field"><label>Комментарий</label><input name="note" placeholder="Постоянное мероприятие"></div></div><button class="btn primary">Добавить регулярную занятость</button></form></div>

<div class="section"><h2>📦 Пакеты услуг</h2><div class="table-wrap"><table><thead><tr><th>Пакет</th><th>Зал</th><th>Цена</th><th>Описание</th></tr></thead><tbody>{package_rows}</tbody></table></div>
<form method="post" action="/admin/operations/package" class="card section"><input name="csrf" type="hidden" value="{csrf}"><div class="row3"><div class="field"><label>Название</label><input name="name" required></div><div class="field"><label>Сумма</label><input name="amount" type="number" min="0" value="0"></div><div class="field"><label>Форма</label><select name="form_id"><option value="">Любая</option>{form_options}</select></div></div><div class="row"><div class="field"><label>Зал</label><select name="venue_id"><option value="">Любой</option>{venue_options}</select></div><div class="field"><label>Описание</label><textarea name="description"></textarea></div></div><button class="btn primary">Добавить пакет</button></form><form method="post" action="/admin/operations/package-apply" class="card section"><input name="csrf" type="hidden" value="{csrf}"><div class="row"><div class="field"><label>№ заявки</label><input name="submission_id" type="number" required></div><div class="field"><label>Применить пакет</label><select name="package_id">{package_options}</select></div></div><button class="btn">Добавить пакет к стоимости заявки</button></form></div>

<div class="section"><h2>👤 Менеджер, залог и ресурсы заявки</h2>
<form method="post" action="/admin/operations/submission-workflow" class="card"><input name="csrf" type="hidden" value="{csrf}"><div class="row3"><div class="field"><label>№ заявки</label><input name="submission_id" type="number" required></div><div class="field"><label>Менеджер</label><input name="manager" placeholder="manager1"></div><div class="field"><label>Залог</label><input name="deposit_amount" type="number" min="0" value="0"></div></div><div class="field"><label>Статус залога</label><select name="deposit_status"><option value="none">нет</option><option value="required">требуется</option><option value="paid">внесён</option><option value="returned">возвращён</option><option value="withheld">удержан</option></select></div><button class="btn primary">Сохранить CRM-поля</button></form>
<form method="post" action="/admin/operations/allocation" class="card section"><input name="csrf" type="hidden" value="{csrf}"><div class="row3"><div class="field"><label>№ заявки</label><input name="submission_id" type="number" required></div><div class="field"><label>Ресурс</label><select name="resource_id">{resource_options}</select></div><div class="field"><label>Количество (0 = снять)</label><input name="quantity" type="number" min="0" value="1"></div></div><button class="btn">Назначить ресурс</button></form></div>

<div class="section"><h2>✅ Задачи</h2><div class="table-wrap"><table><thead><tr><th>Заявка</th><th>Задача</th><th>Ответственный</th><th>Срок</th><th></th></tr></thead><tbody>{task_rows}</tbody></table></div>
<form method="post" action="/admin/operations/task" class="card section"><input name="csrf" type="hidden" value="{csrf}"><div class="row3"><div class="field"><label>№ заявки</label><input name="submission_id" type="number" required></div><div class="field"><label>Задача</label><input name="title" required></div><div class="field"><label>Ответственный</label><input name="assignee"></div></div><div class="field"><label>Срок (ISO, необязательно)</label><input name="due_at" placeholder="2026-09-25T12:00:00+03:00"></div><button class="btn primary">Добавить задачу</button></form></div>

<div class="section"><h2>🌐 Публичная бронь / Mini App</h2><p>Страница: <a href="/book" target="_blank">/book</a>. Её можно использовать как обычную web-форму и как Telegram Mini App после публикации через HTTPS.</p><div class="table-wrap"><table><thead><tr><th>№</th><th>Зал</th><th>Имя</th><th>Контакт</th><th>Дата/время</th><th>Гости</th></tr></thead><tbody>{public_rows}</tbody></table></div></div>

<div class="section"><h2>🧰 Служебные настройки</h2>
<form method="post" action="/admin/operations/settings" class="card"><input name="csrf" type="hidden" value="{csrf}">
<div class="row3"><div class="field"><label>Hold, минут</label><input name="slot_hold_minutes" type="number" min="1" max="120" value="{_e(await admin.db.get_setting('slot_hold_minutes','15'))}"></div><div class="field"><label>Архив через, дней</label><input name="archive_after_days" type="number" min="30" max="3650" value="{_e(await admin.db.get_setting('archive_after_days','365'))}"></div><div class="field"><label>Публичная бронь</label><select name="public_booking_enabled"><option value="0">выключена</option><option value="1" {'selected' if await admin.db.get_setting('public_booking_enabled','0')=='1' else ''}>включена</option></select></div></div>
<div class="row"><div class="field"><label>Mini App</label><select name="miniapp_enabled"><option value="0">выключен</option><option value="1" {'selected' if await admin.db.get_setting('miniapp_enabled','0')=='1' else ''}>включён</option></select></div><div class="field"><label>Mini App URL (HTTPS)</label><input name="miniapp_url" value="{_e(await admin.db.get_setting('miniapp_url',''))}" placeholder="https://booking.example.ru/book"></div></div>
<div class="row"><div class="field"><label>Webhook URL</label><input name="webhook_url" value="{_e(await admin.db.get_setting('webhook_url',''))}"></div><div class="field"><label>API</label><select name="api_enabled"><option value="0">выключен</option><option value="1" {'selected' if await admin.db.get_setting('api_enabled','0')=='1' else ''}>включён</option></select></div></div>
<div class="row3"><div class="field"><label>Оплата</label><select name="payment_provider"><option value="off">выключена</option><option value="template" {'selected' if await admin.db.get_setting('payment_provider','off')=='template' else ''}>шаблон ссылки</option><option value="yookassa" {'selected' if await admin.db.get_setting('payment_provider','off')=='yookassa' else ''}>ЮKassa API</option></select></div><div class="field"><label>ЮKassa shop_id</label><input name="yookassa_shop_id" value="{_e(await admin.db.get_setting('yookassa_shop_id',''))}"></div><div class="field"><label>ЮKassa secret key</label><input type="password" name="yookassa_secret_key" placeholder="оставьте пустым, чтобы не менять"></div></div>
<div class="field"><label>Return URL после оплаты (HTTPS)</label><input name="payment_return_url" value="{_e(await admin.db.get_setting('payment_return_url',''))}" placeholder="https://example.ru/payment/return"></div>
<p class="muted">API token: <code>{_e(await admin.db.get_setting('api_token',''))}</code></p>
<button class="btn primary">Сохранить</button> <button class="btn" formaction="/admin/operations/archive">Архивировать старые заявки</button></form></div>

<div class="section"><h2>⚡ Массовые действия</h2><form method="post" action="/admin/operations/bulk" class="card"><input name="csrf" type="hidden" value="{csrf}"><div class="row3"><div class="field"><label>ID заявок через запятую</label><input name="ids" placeholder="12,13,14"></div><div class="field"><label>Действие</label><select name="action"><option value="archive">Архивировать</option><option value="manager">Назначить менеджера</option><option value="status:in_progress">Статус: в работе</option><option value="status:confirmed">Статус: подтверждена</option><option value="status:cancelled">Статус: отказ</option></select></div><div class="field"><label>Значение / менеджер</label><input name="value"></div></div><button class="btn">Выполнить</button></form></div>
'''
    return admin.page(request, "Операционная панель", content, active="operations")


async def venue_create(admin, request: web.Request):
    d=request["post"]; name=str(d.get("name") or "").strip(); desc=str(d.get("description") or "").strip()
    if not name: raise web.HTTPFound('/admin/operations?err='+quote('Укажите название зала'))
    await admin.operations.create_venue(name,desc); raise web.HTTPFound('/admin/operations?ok='+quote('Зал добавлен'))

async def venue_update(admin, request: web.Request):
    d=request["post"]; vid=int(request.match_info['vid']); name=str(d.get('name') or '').strip(); desc=str(d.get('description') or '').strip()
    await admin.operations.update_venue(vid,name=name,description=desc,enabled=str(d.get('enabled') or '')=='1'); raise web.HTTPFound('/admin/operations?ok='+quote('Зал сохранён'))

async def resource_create(admin, request: web.Request):
    d=request["post"]
    try: qty=int(d.get('quantity') or 0); vid=int(d.get('venue_id')) if d.get('venue_id') else None
    except ValueError: raise web.HTTPFound('/admin/operations?err='+quote('Проверьте количество'))
    await admin.operations.save_resource(resource_id=None,name=str(d.get('name') or '').strip(),quantity=qty,venue_id=vid); raise web.HTTPFound('/admin/operations?ok='+quote('Ресурс добавлен'))

async def resource_delete(admin, request: web.Request):
    await admin.operations.delete_resource(int(request.match_info['rid'])); raise web.HTTPFound('/admin/operations?ok='+quote('Ресурс удалён'))

async def package_create(admin, request: web.Request):
    d=request["post"]
    def opt_int(k):
        try: return int(d.get(k)) if d.get(k) else None
        except ValueError: return None
    await admin.operations.save_package(package_id=None,name=str(d.get('name') or '').strip(),description=str(d.get('description') or ''),amount=int(d.get('amount') or 0),form_id=opt_int('form_id'),venue_id=opt_int('venue_id'))
    raise web.HTTPFound('/admin/operations?ok='+quote('Пакет добавлен'))

async def package_apply(admin, request: web.Request):
    d=request["post"]
    try:
        sid=int(d.get("submission_id") or 0); pid=int(d.get("package_id") or 0)
        new_total=await admin.operations.apply_package(sid,pid)
    except Exception as exc:
        raise web.HTTPFound('/admin/operations?err='+quote(str(exc)[:180]))
    raise web.HTTPFound('/admin/operations?ok='+quote(f'Пакет применён, новая стоимость: {new_total}'))

async def venue_pricing_save(admin, request: web.Request):
    d=request["post"]
    try:
        fid=int(d.get("form_id") or 0); vid=int(d.get("venue_id") or 0)
        values={
            "enabled":str(d.get("enabled") or "0")=="1",
            "base_amount":int(d.get("base_amount") or 0),
            "base_description":str(d.get("base_description") or ""),
            "included_hours":int(d.get("included_hours") or 0),
            "extra_hour_amount":int(d.get("extra_hour_amount") or 0),
            "buffer_before_minutes":int(d.get("buffer_before_minutes") or 0),
            "buffer_after_minutes":int(d.get("buffer_after_minutes") or 0),
        }
    except ValueError:
        raise web.HTTPFound('/admin/operations?err='+quote('Проверьте значения тарифа'))
    await admin.operations.save_venue_pricing(fid,vid,values)
    raise web.HTTPFound('/admin/operations?ok='+quote('Тариф зала сохранён'))


async def venue_rule_save(admin, request: web.Request):
    d=request["post"]
    try:
        fid=int(d.get("form_id") or 0); vid=int(d.get("venue_id") or 0)
        closed=[]
        for raw in str(d.get("closed_weekdays") or "").replace(";",",").split(","):
            raw=raw.strip()
            if raw:
                val=int(raw)
                if not 0 <= val <= 6: raise ValueError("weekday")
                closed.append(val)
        raw_hours=str(d.get("day_hours_json") or "").strip()
        hours=json.loads(raw_hours) if raw_hours else {}
        if not isinstance(hours,dict): raise ValueError("hours")
        values={
            "enabled":str(d.get("enabled") or "0")=="1",
            "min_duration_minutes":int(d.get("min_duration_minutes") or 0),
            "min_lead_hours":int(d.get("min_lead_hours") or 0),
            "max_advance_days":int(d.get("max_advance_days") or 0),
            "closed_weekdays":closed,"day_hours":hours,
        }
    except Exception:
        raise web.HTTPFound('/admin/operations?err='+quote('Проверьте правила зала и JSON часов'))
    await admin.operations.save_venue_rule(fid,vid,values)
    raise web.HTTPFound('/admin/operations?ok='+quote('Правила зала сохранены'))

async def venue_recurring_create(admin, request: web.Request):
    d=request["post"]
    try:
        fid=int(d.get("form_id") or 0); vid=int(d.get("venue_id") or 0); weekday=int(d.get("weekday") or 0)
        start=str(d.get("start_time") or "").strip() or None; end=str(d.get("end_time") or "").strip() or None
        if bool(start)!=bool(end): raise ValueError("pair")
        if start and end and start==end: raise ValueError("same")
    except Exception:
        raise web.HTTPFound('/admin/operations?err='+quote('Проверьте регулярную занятость'))
    await admin.operations.add_venue_recurring_block(form_id=fid,venue_id=vid,weekday=weekday,start_time=start,end_time=end,note=str(d.get("note") or ""))
    raise web.HTTPFound('/admin/operations?ok='+quote('Регулярная занятость добавлена'))

async def venue_recurring_delete(admin, request: web.Request):
    await admin.operations.delete_venue_recurring_block(int(request.match_info['bid']))
    raise web.HTTPFound('/admin/operations?ok='+quote('Регулярная занятость удалена'))

async def task_create(admin, request: web.Request):
    d=request['post']; await admin.operations.add_task(int(d.get('submission_id') or 0),str(d.get('title') or ''),assignee=str(d.get('assignee') or ''),due_at=str(d.get('due_at') or '') or None,created_by=str(request.get('admin_user') or 'web'))
    raise web.HTTPFound('/admin/operations?ok='+quote('Задача добавлена'))

async def task_done(admin, request: web.Request):
    await admin.operations.set_task_status(int(request.match_info['tid']),'done'); raise web.HTTPFound('/admin/operations?ok='+quote('Задача закрыта'))

async def submission_workflow_save(admin, request: web.Request):
    d=request["post"]
    try:
        sid=int(d.get("submission_id") or 0); amount=int(d.get("deposit_amount") or 0)
    except ValueError:
        raise web.HTTPFound('/admin/operations?err='+quote('Проверьте номер заявки и сумму'))
    manager=str(d.get("manager") or '').strip(); status=str(d.get("deposit_status") or 'none')
    await admin.operations.assign_manager(sid,manager)
    await admin.operations.update_deposit(sid,amount,status)
    raise web.HTTPFound('/admin/operations?ok='+quote('CRM-поля заявки сохранены'))

async def allocation_save(admin, request: web.Request):
    d=request["post"]
    try:
        sid=int(d.get("submission_id") or 0); rid=int(d.get("resource_id") or 0); qty=int(d.get("quantity") or 0)
        await admin.operations.set_resource_allocation(sid,rid,qty)
    except (ValueError, Exception) as exc:
        raise web.HTTPFound('/admin/operations?err='+quote(str(exc)[:180]))
    raise web.HTTPFound('/admin/operations?ok='+quote('Ресурс заявки обновлён'))


async def settings_save(admin, request: web.Request):
    d=request['post']
    for key in ('slot_hold_minutes','archive_after_days','public_booking_enabled','miniapp_enabled','miniapp_url','api_enabled','webhook_url','payment_provider','yookassa_shop_id','payment_return_url'):
        await admin.db.set_setting(key,str(d.get(key) or ''))
    secret=str(d.get('yookassa_secret_key') or '').strip()
    if secret:
        await admin.db.set_setting('yookassa_secret_key',secret)
    raise web.HTTPFound('/admin/operations?ok='+quote('Настройки сохранены'))

async def archive_now(admin, request: web.Request):
    count=await admin.operations.archive_old(); raise web.HTTPFound('/admin/operations?ok='+quote(f'Архивировано: {count}'))

async def bulk_action(admin, request: web.Request):
    d=request['post']; ids=[]
    for x in str(d.get('ids') or '').replace(';',',').split(','):
        x=x.strip()
        if x.isdigit(): ids.append(int(x))
    action=str(d.get('action') or ''); value=str(d.get('value') or '')
    done=0
    async with admin.db.connection() as conn:
        for sid in ids[:200]:
            if action=='archive':
                await conn.execute('UPDATE form_submissions SET archived=1 WHERE id=?',(sid,)); done+=1
            elif action=='manager':
                await admin.operations.assign_manager(sid,value); done+=1
            elif action.startswith('status:'):
                ok,_=await admin.on_status_change(sid,action.split(':',1)[1]); done += 1 if ok else 0
        await conn.commit()
    raise web.HTTPFound('/admin/operations?ok='+quote(f'Обработано: {done}'))


async def public_book_get(admin, request: web.Request) -> web.Response:
    if await admin.db.get_setting('public_booking_enabled','0') != '1':
        return web.Response(text='Публичная форма бронирования временно отключена.',content_type='text/plain',status=503)
    venues=await admin.operations.list_venues(); opts=''.join(f'<option value="{v["id"]}">{_e(v["name"])}</option>' for v in venues)
    body=f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Бронирование</title><style>body{{font-family:system-ui;max-width:680px;margin:30px auto;padding:0 16px;background:#0b1220;color:#eef4ff}}.card{{background:#111b2e;padding:22px;border-radius:16px}}input,select,textarea{{width:100%;box-sizing:border-box;margin:6px 0 14px;padding:10px;border-radius:9px;border:1px solid #345;background:#0d1728;color:white}}button{{padding:11px 16px;border:0;border-radius:9px;background:#4f8cff;color:white}}</style></head><body><div class="card"><h1>Заявка на аренду</h1><form method="post"><label>Зал</label><select name="venue_id">{opts}</select><label>Имя</label><input name="name" required><label>Телефон / Telegram</label><input name="contact" required><label>Дата</label><input type="date" name="date_iso" min="{datetime.now(admin.timezone).date().isoformat()}" required><label>Начало</label><input type="time" name="start_time"><label>Окончание</label><input type="time" name="end_time"><label>Количество гостей</label><input name="guests"><label>Комментарий</label><textarea name="comment"></textarea><button>Отправить заявку</button></form></div></body></html>'''
    return web.Response(text=body,content_type='text/html')

async def public_book_post(admin, request: web.Request) -> web.Response:
    if await admin.db.get_setting('public_booking_enabled','0') != '1': raise web.HTTPServiceUnavailable()
    d=await request.post()
    try: vid=int(d.get('venue_id')) if d.get('venue_id') else None; dt=datetime.strptime(str(d.get('date_iso') or ''),'%Y-%m-%d').date()
    except Exception: return web.Response(text='Некорректная дата',status=400)
    if dt < datetime.now(admin.timezone).date(): return web.Response(text='Нельзя выбрать прошедшую дату',status=400)
    rid=await admin.operations.create_public_request(venue_id=vid,name=str(d.get('name') or ''),contact=str(d.get('contact') or ''),date_iso=dt.isoformat(),start_time=str(d.get('start_time') or '') or None,end_time=str(d.get('end_time') or '') or None,guests=str(d.get('guests') or ''),comment=str(d.get('comment') or ''))
    return web.Response(text=f'Заявка #{rid} принята. Мы свяжемся с вами.',content_type='text/plain')

async def public_submission(admin, request: web.Request) -> web.Response:
    token = request.match_info["token"]
    async with admin.db.connection() as conn:
        row = await (await conn.execute(
            "SELECT s.*,v.name venue_name FROM form_submissions s LEFT JOIN venues v ON v.id=s.venue_id WHERE s.public_token=?",
            (token,),
        )).fetchone()
    if not row:
        raise web.HTTPNotFound()
    submission = dict(row)
    currency = str(await admin.db.get_setting("crm_currency", "₽") or "₽")
    try:
        pricing = json.loads(submission.get("pricing_details_json") or "{}")
    except Exception:
        pricing = {}
    addon_lines: list[str] = []
    for addon in pricing.get("addons") or []:
        quantity = max(1, int(addon.get("quantity") or 1))
        unit_amount = int(
            addon.get("unit_amount")
            if addon.get("unit_amount") is not None
            else addon.get("amount") or 0
        )
        line_total = int(addon.get("amount") or unit_amount * quantity)
        if addon.get("quantity_enabled") or quantity > 1:
            addon_lines.append(
                f"<li>{_e(addon.get('name') or 'Услуга')} ×{quantity}: "
                f"{_e(_money(line_total, currency))} ({_e(_money(unit_amount, currency))}/шт)</li>"
            )
        else:
            addon_lines.append(
                f"<li>{_e(addon.get('name') or 'Услуга')}: {_e(_money(line_total, currency))}</li>"
            )
    addons_html = (
        "<h2>Дополнительные услуги</h2><ul>" + "".join(addon_lines) + "</ul>"
        if addon_lines else ""
    )
    body = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Заявка #{submission['id']}</title></head><body style="font-family:system-ui;max-width:720px;margin:30px auto;padding:0 16px"><h1>Заявка #{submission['id']}</h1><p><b>{_e(submission['form_name'])}</b></p><p>Зал: {_e(submission.get('venue_name') or '—')}</p><p>Статус: {_e(submission.get('status'))}</p><p>Стоимость: {_money(submission.get('total_amount'), currency)}</p><p>Предоплата: {_money(submission.get('prepayment_amount'), currency)}</p><p>Залог: {_money(submission.get('deposit_amount'), currency)} · {_e(submission.get('deposit_status'))}</p>{addons_html}</body></html>"""
    return web.Response(text=body, content_type="text/html")

def _api_ok(admin, request: web.Request) -> bool:
    if False:
        pass
    token=request.headers.get('Authorization','').removeprefix('Bearer ').strip() or request.query.get('token','')
    return bool(token)

async def api_health(admin, request: web.Request) -> web.Response:
    if await admin.db.get_setting('api_enabled','0')!='1': raise web.HTTPNotFound()
    token=request.headers.get('Authorization','').removeprefix('Bearer ').strip() or request.query.get('token','')
    expected=str(await admin.db.get_setting('api_token','') or '')
    if not expected or token!=expected: raise web.HTTPUnauthorized()
    return web.json_response(await admin.operations.health_snapshot())

async def api_availability(admin, request: web.Request) -> web.Response:
    if await admin.db.get_setting('api_enabled','0')!='1': raise web.HTTPNotFound()
    token=request.headers.get('Authorization','').removeprefix('Bearer ').strip() or request.query.get('token','')
    expected=str(await admin.db.get_setting('api_token','') or '')
    if token!=expected: raise web.HTTPUnauthorized()
    date=str(request.query.get('date') or ''); vid=int(request.query.get('venue_id')) if str(request.query.get('venue_id') or '').isdigit() else None
    async with admin.db.connection() as conn:
        if vid:
            rows=await (await conn.execute('SELECT * FROM availability_blocks WHERE date_iso=? AND (venue_id IS NULL OR venue_id=?) ORDER BY start_time',(date,vid))).fetchall()
        else:
            rows=await (await conn.execute('SELECT * FROM availability_blocks WHERE date_iso=? ORDER BY start_time',(date,))).fetchall()
    holds=[x for x in await admin.operations.list_active_holds(200) if x['date_iso']==date and (not vid or int(x.get('venue_id') or 0)==vid)]
    return web.json_response({'date':date,'venue_id':vid,'blocks':[dict(r) for r in rows],'holds':holds})

async def api_submission(admin, request: web.Request) -> web.Response:
    if await admin.db.get_setting('api_enabled','0')!='1': raise web.HTTPNotFound()
    token=request.headers.get('Authorization','').removeprefix('Bearer ').strip(); expected=str(await admin.db.get_setting('api_token','') or '')
    if token!=expected: raise web.HTTPUnauthorized()
    s=await admin.db.get_submission(int(request.match_info['sid']))
    if not s: raise web.HTTPNotFound()
    for k in list(s):
        if k.endswith('_json'): s.pop(k,None)
    return web.json_response(s)

async def yookassa_webhook(admin, request: web.Request) -> web.Response:
    if str(await admin.db.get_setting("payment_provider", "off") or "off").strip().lower() != "yookassa":
        raise web.HTTPNotFound()
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid json"}, status=400)
    ok, info, submission_id = await admin.payments.handle_yookassa_notification(payload)
    if not ok:
        # 503 makes provider retry temporary verification failures.
        status = 400 if info in {"Нет payment id", "В metadata нет submission_id"} else 503
        return web.json_response({"ok": False, "error": info}, status=status)
    if submission_id:
        submission = await admin.db.get_submission(int(submission_id))
        if submission and info == "succeeded":
            total = max(0, int(submission.get("total_amount") or 0))
            prepaid = max(0, int(submission.get("prepayment_amount") or 0))
            if total and prepaid >= total and str(submission.get("status") or "") not in {"paid", "completed"}:
                await admin.on_status_change(int(submission_id), "paid")
        await admin.operations.emit_webhook("payment.updated", {"submission_id": int(submission_id), "status": info})
    return web.json_response({"ok": True})

async def qr_png(admin, request: web.Request) -> web.Response:
    sid=int(request.match_info['sid']); s=await admin.db.get_submission(sid)
    if not s or not s.get('public_token'): raise web.HTTPNotFound()
    base=f"{request.scheme}://{request.host}"; url=f"{base}/booking/{s['public_token']}"
    img=qrcode.make(url); buf=io.BytesIO(); img.save(buf,format='PNG')
    return web.Response(body=buf.getvalue(),content_type='image/png',headers={'Content-Disposition':f'inline; filename="booking-{sid}.png"'})

async def confirmation_pdf(admin, request: web.Request) -> web.Response:
    sid = int(request.match_info["sid"])
    submission = await admin.db.get_submission(sid)
    if not submission:
        raise web.HTTPNotFound()
    venue = (
        await admin.operations.get_venue(int(submission.get("venue_id") or 0))
        if submission.get("venue_id") else None
    )
    currency = str(await admin.db.get_setting("crm_currency", "₽") or "₽")
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=(595, 842))
    try:
        pdfmetrics.registerFont(TTFont("DejaVu", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
        font = "DejaVu"
    except Exception:
        font = "Helvetica"
    pdf.setFont(font, 16)
    pdf.drawString(45, 790, f"Подтверждение заявки #{sid}")
    pdf.setFont(font, 11)
    y = 755
    lines = [
        f"Форма: {submission.get('form_name') or ''}",
        f"Зал: {(venue or {}).get('name', '—')}",
        f"Клиент: {' '.join(x for x in [submission.get('first_name'), submission.get('last_name')] if x)}",
        f"Статус: {submission.get('status')}",
        f"Стоимость: {_money(submission.get('total_amount'), currency)}",
        f"Предоплата: {_money(submission.get('prepayment_amount'), currency)}",
        f"Залог: {_money(submission.get('deposit_amount'), currency)} ({submission.get('deposit_status') or 'none'})",
    ]
    pricing = submission.get("pricing_details") or {}
    for addon in pricing.get("addons") or []:
        quantity = max(1, int(addon.get("quantity") or 1))
        unit_amount = int(
            addon.get("unit_amount")
            if addon.get("unit_amount") is not None
            else addon.get("amount") or 0
        )
        line_total = int(addon.get("amount") or unit_amount * quantity)
        if addon.get("quantity_enabled") or quantity > 1:
            lines.append(
                f"Доп. услуга: {addon.get('name') or 'Услуга'} x{quantity} = "
                f"{_money(line_total, currency)} ({_money(unit_amount, currency)}/шт)"
            )
        else:
            lines.append(
                f"Доп. услуга: {addon.get('name') or 'Услуга'} = {_money(line_total, currency)}"
            )
    for line in lines:
        if y < 90:
            pdf.showPage()
            pdf.setFont(font, 11)
            y = 790
        pdf.drawString(45, y, str(line)[:100])
        y -= 22
    pdf.setFont(font, 9)
    pdf.drawString(45, 60, "Документ сформирован Telegram Business AutoReply")
    pdf.save()
    return web.Response(
        body=buf.getvalue(),
        content_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="booking-{sid}.pdf"'},
    )

def register_operations_routes(app: web.Application, admin) -> None:
    app.router.add_get('/admin/operations', lambda r: operations_page(admin,r))
    app.router.add_post('/admin/operations/venue', lambda r: venue_create(admin,r))
    app.router.add_post(r'/admin/operations/venue/{vid:\d+}', lambda r: venue_update(admin,r))
    app.router.add_post('/admin/operations/resource', lambda r: resource_create(admin,r))
    app.router.add_post(r'/admin/operations/resource/{rid:\d+}/delete', lambda r: resource_delete(admin,r))
    app.router.add_post('/admin/operations/package', lambda r: package_create(admin,r))
    app.router.add_post('/admin/operations/package-apply', lambda r: package_apply(admin,r))
    app.router.add_post('/admin/operations/venue-pricing', lambda r: venue_pricing_save(admin,r))
    app.router.add_post('/admin/operations/venue-rule', lambda r: venue_rule_save(admin,r))
    app.router.add_post('/admin/operations/venue-recurring', lambda r: venue_recurring_create(admin,r))
    app.router.add_post(r'/admin/operations/venue-recurring/{bid:\d+}/delete', lambda r: venue_recurring_delete(admin,r))
    app.router.add_post('/admin/operations/task', lambda r: task_create(admin,r))
    app.router.add_post('/admin/operations/submission-workflow', lambda r: submission_workflow_save(admin,r))
    app.router.add_post('/admin/operations/allocation', lambda r: allocation_save(admin,r))
    app.router.add_post(r'/admin/operations/task/{tid:\d+}/done', lambda r: task_done(admin,r))
    app.router.add_post('/admin/operations/settings', lambda r: settings_save(admin,r))
    app.router.add_post('/admin/operations/archive', lambda r: archive_now(admin,r))
    app.router.add_post('/admin/operations/bulk', lambda r: bulk_action(admin,r))
    app.router.add_post('/payments/yookassa/webhook', lambda r: yookassa_webhook(admin,r))
    app.router.add_get('/book', lambda r: public_book_get(admin,r))
    app.router.add_post('/book', lambda r: public_book_post(admin,r))
    app.router.add_get('/booking/{token}', lambda r: public_submission(admin,r))
    app.router.add_get('/api/v1/health', lambda r: api_health(admin,r))
    app.router.add_get('/api/v1/availability', lambda r: api_availability(admin,r))
    app.router.add_get(r'/api/v1/submissions/{sid:\d+}', lambda r: api_submission(admin,r))
    app.router.add_get(r'/admin/requests/{sid:\d+}/qr.png', lambda r: qr_png(admin,r))
    app.router.add_get(r'/admin/requests/{sid:\d+}/confirmation.pdf', lambda r: confirmation_pdf(admin,r))
