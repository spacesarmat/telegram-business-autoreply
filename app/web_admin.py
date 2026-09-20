from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import logging
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable
from urllib.parse import quote
from zoneinfo import ZoneInfo

from aiohttp import web

from .db import Database

logger = logging.getLogger(__name__)

STATUS_NAMES = {
    "new": "🆕 Новая",
    "in_progress": "🟡 В работе",
    "confirmed": "✅ Подтверждена",
    "paid": "💰 Оплачена",
    "completed": "🏁 Завершена",
    "cancelled": "❌ Отказ",
}
QUESTION_TYPES = {
    "text": "⌨️ Текст",
    "date": "📅 Дата",
    "time": "🕐 Время",
    "contact": "📱 Контакт",
    "guest_count": "👥 Гости",
    "choice": "🎛 Варианты",
}

StatusChangeCallback = Callable[[int, str], Awaitable[tuple[bool, str]]]
AmountChangeCallback = Callable[[int, int], Awaitable[tuple[bool, str]]]
ConcurrencySnapshotCallback = Callable[[], dict[str, int]]


@dataclass
class WebAdminHandle:
    runner: web.AppRunner

    async def close(self) -> None:
        await self.runner.cleanup()

    async def wait_closed(self) -> None:
        return None


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _money(value: Any, currency: str = "₽") -> str:
    try:
        amount = int(value or 0)
    except (TypeError, ValueError):
        amount = 0
    return f"{amount:,}".replace(",", " ") + f" {currency}"


def _parse_int(value: str | None, *, minimum: int = 0, maximum: int = 10**12) -> int | None:
    raw = (value or "").strip().replace(" ", "").replace("_", "")
    if not raw or not re.fullmatch(r"\d+", raw):
        return None
    number = int(raw)
    if number < minimum or number > maximum:
        return None
    return number


def _local_dt(value: str | None, tz: ZoneInfo) -> str:
    if not value:
        return "—"
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return _e(value)
    if dt.tzinfo is None:
        from datetime import timezone

        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).strftime("%d.%m.%Y %H:%M")


_BASE_CSS = """
:root{color-scheme:dark;--bg:#0b1220;--panel:#111b2e;--panel2:#16243b;--line:#263853;--text:#eef4ff;--muted:#91a4c2;--accent:#4f8cff;--green:#2dd4a8;--yellow:#f3c969;--red:#ff6b7a}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--text)}a{color:#87b1ff;text-decoration:none}a:hover{text-decoration:underline}
.shell{display:grid;grid-template-columns:230px 1fr;min-height:100vh}.side{padding:22px 14px;background:#0e1829;border-right:1px solid var(--line);position:sticky;top:0;height:100vh}.brand{font-size:17px;font-weight:800;padding:0 10px 20px}.nav a{display:block;padding:10px 11px;margin:3px 0;border-radius:9px;color:#d9e5f7}.nav a:hover,.nav a.active{background:#1b2b45;text-decoration:none}.main{padding:28px;max-width:1450px;width:100%}.top{display:flex;align-items:center;justify-content:space-between;gap:15px;margin-bottom:22px}.top h1{font-size:24px;margin:0}.muted{color:var(--muted)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px}.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:17px}.metric{font-size:27px;font-weight:800;margin-top:5px}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:12px;background:var(--panel)}table{border-collapse:collapse;width:100%;min-width:700px}th,td{padding:11px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);background:#101c30}tr:last-child td{border-bottom:0}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;border:1px solid #34517c;background:#1d3557;color:#fff;padding:8px 12px;border-radius:9px;cursor:pointer;font:inherit}.btn:hover{background:#26466f;text-decoration:none}.btn.primary{background:var(--accent);border-color:var(--accent)}.btn.danger{background:#54212a;border-color:#7f3340}.btn.ghost{background:transparent}.actions{display:flex;flex-wrap:wrap;gap:8px}
input,textarea,select{width:100%;background:#0d1728;color:var(--text);border:1px solid #314666;border-radius:9px;padding:9px 10px;font:inherit}textarea{min-height:100px;resize:vertical}.field{margin:0 0 14px}.field label{display:block;font-weight:650;margin-bottom:6px}.row{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.row3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}.section{margin-top:18px}.section h2{font-size:18px;margin:0 0 12px}.flash{padding:11px 13px;border-radius:9px;margin-bottom:16px;background:#173a31;border:1px solid #286552}.flash.err{background:#48242b;border-color:#80404d}.pill{display:inline-block;border-radius:999px;padding:4px 8px;background:#1c304f;color:#dbe9ff;font-size:12px}.kv{display:grid;grid-template-columns:180px 1fr;gap:7px 14px}.answer{white-space:pre-wrap}.login{max-width:420px;margin:10vh auto;padding:22px}.login h1{margin-top:0}.warn{padding:12px;background:#42351b;border:1px solid #6f5928;border-radius:10px}.ok{color:var(--green)}.bad{color:var(--red)}
@media(max-width:850px){.shell{display:block}.side{position:static;height:auto;border-right:0;border-bottom:1px solid var(--line)}.nav{display:flex;gap:4px;overflow:auto}.nav a{white-space:nowrap}.main{padding:18px}.row,.row3{grid-template-columns:1fr}.kv{grid-template-columns:1fr}.top{align-items:flex-start;flex-direction:column}}
"""


class WebAdmin:
    def __init__(
        self,
        *,
        db: Database,
        timezone: ZoneInfo,
        username: str,
        password: str,
        on_status_change: StatusChangeCallback,
        on_amount_change: AmountChangeCallback,
        concurrency_snapshot: ConcurrencySnapshotCallback,
    ) -> None:
        self.db = db
        self.timezone = timezone
        self.username = username or "admin"
        self.password = password
        self.on_status_change = on_status_change
        self.on_amount_change = on_amount_change
        self.concurrency_snapshot = concurrency_snapshot
        self.enabled = bool(password and not password.startswith("PASTE_") and len(password) >= 10)
        self._key = hashlib.sha256((password or secrets.token_hex(16)).encode("utf-8")).digest()

    def _session_cookie(self) -> str:
        exp = int(time.time()) + 12 * 3600
        payload = f"{exp}:{self.username}"
        sig = hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}:{sig}"

    def _valid_session(self, request: web.Request) -> bool:
        raw = request.cookies.get("tgadmin", "")
        parts = raw.split(":")
        if len(parts) != 3:
            return False
        exp_s, username, sig = parts
        try:
            exp = int(exp_s)
        except ValueError:
            return False
        if exp < int(time.time()) or username != self.username:
            return False
        payload = f"{exp}:{username}"
        expected = hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)

    def _csrf(self, request: web.Request) -> str:
        cookie = request.cookies.get("tgadmin", "")
        return hmac.new(self._key, ("csrf:" + cookie).encode(), hashlib.sha256).hexdigest()

    async def auth_middleware(self, app: web.Application, handler):
        async def middleware_handler(request: web.Request):
            if request.path == "/health" or request.path.startswith("/admin/login"):
                return await handler(request)
            if not self.enabled:
                return await self.disabled_page(request)
            if not self._valid_session(request):
                raise web.HTTPFound("/admin/login?next=" + quote(request.path_qs, safe=""))
            if request.method == "POST":
                data = await request.post()
                token = str(data.get("csrf") or "")
                if not hmac.compare_digest(token, self._csrf(request)):
                    raise web.HTTPForbidden(text="CSRF token invalid")
                request["post"] = data
            return await handler(request)

        return middleware_handler

    def page(self, request: web.Request, title: str, content: str, *, active: str = "") -> web.Response:
        nav = [
            ("dashboard", "/admin", "🏠 Обзор"),
            ("requests", "/admin/requests", "📋 Заявки"),
            ("availability", "/admin/availability", "📅 Занятость"),
            ("pricing", "/admin/pricing", "💰 Тарифы"),
            ("forms", "/admin/forms", "📝 Формы"),
            ("buttons", "/admin/buttons", "🔘 Кнопки"),
            ("settings", "/admin/settings", "⚙️ Настройки"),
        ]
        nav_html = "".join(
            f'<a class="{"active" if key == active else ""}" href="{url}">{label}</a>'
            for key, url, label in nav
        )
        flash = request.query.get("ok")
        err = request.query.get("err")
        flash_html = ""
        if flash:
            flash_html += f'<div class="flash">{_e(flash)}</div>'
        if err:
            flash_html += f'<div class="flash err">{_e(err)}</div>'
        csrf = self._csrf(request) if self._valid_session(request) else ""
        body = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>{_e(title)} — TG AutoReply</title><style>{_BASE_CSS}</style></head>
<body><div class="shell"><aside class="side"><div class="brand">TG Business AutoReply</div><nav class="nav">{nav_html}<a href="/admin/logout">🚪 Выйти</a></nav></aside><main class="main"><div class="top"><h1>{_e(title)}</h1><span class="muted">TZ: {_e(self.timezone.key)}</span></div>{flash_html}{content}</main></div>
<script>document.querySelectorAll('form[method="post"]').forEach(f=>{{if(!f.querySelector('input[name="csrf"]')){{let i=document.createElement('input');i.type='hidden';i.name='csrf';i.value={csrf!r};f.appendChild(i)}}}});</script></body></html>"""
        return web.Response(text=body, content_type="text/html", charset="utf-8", headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin"})

    async def health(self, request: web.Request) -> web.Response:
        return web.Response(text="ok\n", content_type="text/plain")

    async def disabled_page(self, request: web.Request) -> web.Response:
        body = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Web Admin</title><style>{_BASE_CSS}</style></head><body><div class="login card"><h1>Telegram Business AutoReply</h1><div class="warn"><b>Web-админка отключена.</b><br><br>Задайте <code>WEB_ADMIN_PASSWORD</code> длиной не менее 10 символов и пересоздайте контейнер.</div><p class="muted">Healthcheck: <code>/health</code></p></div></body></html>"""
        return web.Response(text=body, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def login_get(self, request: web.Request) -> web.Response:
        if not self.enabled:
            return await self.disabled_page(request)
        error = '<div class="flash err">Неверный логин или пароль.</div>' if request.query.get("error") else ""
        next_url = request.query.get("next", "/admin")
        body = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>Вход — TG AutoReply</title><style>{_BASE_CSS}</style></head><body><div class="login card"><h1>Web Admin</h1><p class="muted">Telegram Business AutoReply</p>{error}<form method="post" action="/admin/login"><input type="hidden" name="next" value="{_e(next_url)}"><div class="field"><label>Логин</label><input name="username" autocomplete="username" required></div><div class="field"><label>Пароль</label><input name="password" type="password" autocomplete="current-password" required></div><button class="btn primary" type="submit">Войти</button></form></div></body></html>"""
        return web.Response(text=body, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def login_post(self, request: web.Request) -> web.StreamResponse:
        data = await request.post()
        username = str(data.get("username") or "")
        password = str(data.get("password") or "")
        if not (hmac.compare_digest(username, self.username) and hmac.compare_digest(password, self.password)):
            await asyncio.sleep(0.8)
            raise web.HTTPFound("/admin/login?error=1")
        target = str(data.get("next") or "/admin")
        if not target.startswith("/") or target.startswith("//"):
            target = "/admin"
        response = web.HTTPFound(target)
        response.set_cookie("tgadmin", self._session_cookie(), httponly=True, samesite="Lax", max_age=12 * 3600, path="/")
        raise response

    async def logout(self, request: web.Request) -> web.StreamResponse:
        response = web.HTTPFound("/admin/login")
        response.del_cookie("tgadmin", path="/")
        raise response

    async def dashboard(self, request: web.Request) -> web.Response:
        stat = await self.db.stats()
        counts = await self.db.submission_status_counts()
        connection = await self.db.latest_business_connection()
        recent = await self.db.list_submissions(limit=8)
        queue = self.concurrency_snapshot()
        cards = f"""<div class="grid">
<div class="card"><div class="muted">Заявок</div><div class="metric">{stat['submissions']}</div></div>
<div class="card"><div class="muted">Новых</div><div class="metric">{counts['new']}</div></div>
<div class="card"><div class="muted">В работе</div><div class="metric">{counts['in_progress']}</div></div>
<div class="card"><div class="muted">Подтверждено / оплачено</div><div class="metric">{counts['confirmed'] + counts['paid']}</div></div>
<div class="card"><div class="muted">Контактов</div><div class="metric">{stat['contacts']}</div></div>
<div class="card"><div class="muted">Business</div><div class="metric">{'🟢' if connection and connection.get('enabled') else '⚪'}</div><div class="muted">{'подключён' if connection and connection.get('enabled') else 'не подключён'}</div></div>
<div class="card"><div class="muted">Telegram updates</div><div class="metric">{queue['active']} / {queue['max_concurrent']}</div><div class="muted">ожидает: {queue['waiting']} · chat-lock: {queue['chat_locks']}</div></div>
</div>"""
        currency = str(await self.db.get_setting("crm_currency", "₽") or "₽")
        rows = "".join(self._request_row(item, currency) for item in recent) or '<tr><td colspan="6" class="muted">Заявок пока нет</td></tr>'
        table = f"""<div class="section"><h2>Последние заявки</h2><div class="table-wrap"><table><thead><tr><th>№</th><th>Клиент</th><th>Форма</th><th>Статус</th><th>Сумма</th><th>Создана</th></tr></thead><tbody>{rows}</tbody></table></div></div>"""
        return self.page(request, "Обзор", cards + table, active="dashboard")

    def _request_row(self, item: dict[str, Any], currency: str = "₽") -> str:
        full_name = " ".join(x for x in [item.get("first_name"), item.get("last_name")] if x) or item.get("username") or "—"
        return f"<tr><td><a href=\"/admin/requests/{int(item['id'])}\">#{int(item['id'])}</a></td><td>{_e(full_name)}</td><td>{_e(item.get('form_name'))}</td><td>{_e(STATUS_NAMES.get(str(item.get('status') or 'new'), item.get('status')))}</td><td>{_e(_money(item.get('total_amount'), currency))}</td><td>{_e(_local_dt(item.get('created_at'), self.timezone))}</td></tr>"

    async def requests(self, request: web.Request) -> web.Response:
        status = request.query.get("status", "all")
        query = (request.query.get("q") or "").strip()
        items = await self.db.search_submissions(query, limit=100) if query else await self.db.list_submissions(None if status == "all" else status, limit=100)
        status_options = '<option value="all">Все статусы</option>' + "".join(
            f'<option value="{key}" {"selected" if status == key else ""}>{_e(label)}</option>' for key, label in STATUS_NAMES.items()
        )
        currency = str(await self.db.get_setting("crm_currency", "₽") or "₽")
        rows = "".join(self._request_row(x, currency) for x in items) or '<tr><td colspan="6" class="muted">Ничего не найдено</td></tr>'
        content = f"""<form method="get" class="card"><div class="row3"><div class="field"><label>Поиск</label><input name="q" value="{_e(query)}" placeholder="№, имя, @username, телефон"></div><div class="field"><label>Статус</label><select name="status">{status_options}</select></div><div class="field"><label>&nbsp;</label><button class="btn primary" type="submit">Найти</button></div></div></form><div class="section table-wrap"><table><thead><tr><th>№</th><th>Клиент</th><th>Форма</th><th>Статус</th><th>Сумма</th><th>Создана</th></tr></thead><tbody>{rows}</tbody></table></div>"""
        return self.page(request, "Заявки", content, active="requests")

    async def request_detail(self, request: web.Request) -> web.Response:
        sid = int(request.match_info["sid"])
        item = await self.db.get_submission(sid)
        if not item:
            raise web.HTTPNotFound(text="Заявка не найдена")
        currency = str(await self.db.get_setting("crm_currency", "₽") or "₽")
        prepay = int(item.get("prepayment_amount") or 0)
        total = int(item.get("total_amount") or 0)
        balance = max(0, total - prepay)
        status_opts = "".join(f'<option value="{k}" {"selected" if item.get("status") == k else ""}>{_e(v)}</option>' for k, v in STATUS_NAMES.items())
        answers_data = item.get("answers") or {}
        questions = await self.db.list_form_questions(int(item["form_id"])) if item.get("form_id") else []
        if questions:
            answers = "".join(
                f'<div><b>{_e(q.get("label"))}:</b> <span class="answer">{_e(answers_data.get(str(q.get("id"))) or "—")}</span></div>'
                for q in questions
            )
        else:
            answers = "".join(f'<div><b>{_e(k)}:</b> <span class="answer">{_e(v)}</span></div>' for k, v in answers_data.items())
        answers = answers or '<span class="muted">Нет ответов</span>'
        events = await self.db.list_submission_events(sid, limit=30)
        event_rows = "".join(f'<tr><td>{_e(_local_dt(ev.get("created_at"), self.timezone))}</td><td>{_e(ev.get("event_type"))}</td><td>{_e(ev.get("old_value") or "—")}</td><td>{_e(ev.get("new_value") or "—")}</td></tr>' for ev in events) or '<tr><td colspan="4" class="muted">Нет событий</td></tr>'
        client = " ".join(x for x in [item.get("first_name"), item.get("last_name")] if x) or "—"
        content = f"""<div class="grid"><div class="card"><div class="muted">Клиент</div><h3>{_e(client)}</h3><div>@{_e(item.get('username') or '—')}</div><div>User ID: {_e(item.get('user_id') or '—')}</div></div><div class="card"><div class="muted">Форма</div><h3>{_e(item.get('form_name'))}</h3><div>{_e(_local_dt(item.get('created_at'), self.timezone))}</div></div><div class="card"><div class="muted">Стоимость</div><div class="metric">{_e(_money(total,currency))}</div><div>Предоплата: {_e(_money(prepay,currency))}<br>Остаток: {_e(_money(balance,currency))}</div></div></div>
<div class="section row"><div class="card"><h2>Статус</h2><form method="post" action="/admin/requests/{sid}/status"><div class="field"><select name="status">{status_opts}</select></div><button class="btn primary">Сохранить и уведомить клиента</button></form></div><div class="card"><h2>CRM</h2><form method="post" action="/admin/requests/{sid}/crm"><div class="row"><div class="field"><label>Стоимость</label><input name="total_amount" value="{total}"></div><div class="field"><label>Предоплата</label><input name="prepayment_amount" value="{prepay}"></div></div><div class="field"><label>Внутренняя заметка</label><textarea name="internal_note">{_e(item.get('internal_note') or '')}</textarea></div><button class="btn primary">Сохранить</button></form></div></div>
<div class="section card"><h2>Ответы формы</h2><div class="kv">{answers}</div></div><div class="section"><h2>История</h2><div class="table-wrap"><table><thead><tr><th>Время</th><th>Событие</th><th>Было</th><th>Стало</th></tr></thead><tbody>{event_rows}</tbody></table></div></div>"""
        return self.page(request, f"Заявка #{sid}", content, active="requests")

    async def request_status_post(self, request: web.Request) -> web.StreamResponse:
        sid = int(request.match_info["sid"])
        data = request["post"]
        status = str(data.get("status") or "")
        ok, message = await self.on_status_change(sid, status)
        key = "ok" if ok else "err"
        raise web.HTTPFound(f"/admin/requests/{sid}?{key}={quote(message)}")

    async def request_crm_post(self, request: web.Request) -> web.StreamResponse:
        sid = int(request.match_info["sid"])
        data = request["post"]
        current = await self.db.get_submission(sid)
        if not current:
            raise web.HTTPNotFound()
        total = _parse_int(str(data.get("total_amount") or ""))
        prepay = _parse_int(str(data.get("prepayment_amount") or ""))
        note = str(data.get("internal_note") or "").strip()
        if total is None or prepay is None:
            raise web.HTTPFound(f"/admin/requests/{sid}?err={quote('Суммы должны быть целыми числами')}")
        if prepay > total and total > 0:
            raise web.HTTPFound(f"/admin/requests/{sid}?err={quote('Предоплата не может быть больше стоимости')}")
        old_total = int(current.get("total_amount") or 0)
        if total != old_total:
            ok, msg = await self.on_amount_change(sid, total)
            if not ok:
                # Amount is intentionally saved even when Telegram notification fails.
                notice = msg
            else:
                notice = msg
        else:
            notice = "Карточка обновлена"
        await self.db.update_submission_crm_field(sid, "prepayment_amount", prepay, None)
        await self.db.update_submission_crm_field(sid, "internal_note", note[:1500], None)
        raise web.HTTPFound(f"/admin/requests/{sid}?ok={quote(notice)}")

    async def settings_get(self, request: web.Request) -> web.Response:
        enabled = (await self.db.get_setting("autoresponder_enabled", "1")) == "1"
        greeting = await self.db.get_setting("greeting", "") or ""
        cooldown = await self.db.get_setting("cooldown_hours", "168") or "168"
        columns = await self.db.get_setting("menu_columns", "1") or "1"
        triggers = await self.db.get_setting("menu_triggers", "/menu\nменю\nзаявка") or ""
        currency = await self.db.get_setting("crm_currency", "₽") or "₽"
        day_start = await self.db.get_setting("booking_day_start", "10:00") or "10:00"
        day_end = await self.db.get_setting("booking_day_end", "23:00") or "23:00"
        slot = await self.db.get_setting("booking_slot_minutes", "60") or "60"
        content = f"""<form method="post" action="/admin/settings" class="card"><div class="field"><label><input style="width:auto" type="checkbox" name="autoresponder_enabled" value="1" {'checked' if enabled else ''}> Автоответчик включён</label></div><div class="field"><label>Приветствие</label><textarea name="greeting">{_e(greeting)}</textarea></div><div class="row3"><div class="field"><label>Cooldown, часов</label><input name="cooldown_hours" value="{_e(cooldown)}"></div><div class="field"><label>Кнопок в строке</label><select name="menu_columns">{''.join(f'<option {"selected" if columns == str(i) else ""}>{i}</option>' for i in (1,2,3))}</select></div><div class="field"><label>Валюта</label><input name="crm_currency" value="{_e(currency)}" maxlength="8"></div></div><div class="field"><label>Фразы вызова меню — по одной на строке</label><textarea name="menu_triggers">{_e(triggers)}</textarea></div><div class="row3"><div class="field"><label>Начало дня</label><input name="booking_day_start" value="{_e(day_start)}"></div><div class="field"><label>Конец дня</label><input name="booking_day_end" value="{_e(day_end)}"></div><div class="field"><label>Шаг времени, мин</label><input name="booking_slot_minutes" value="{_e(slot)}"></div></div><button class="btn primary">Сохранить настройки</button></form>"""
        return self.page(request, "Настройки", content, active="settings")

    async def settings_post(self, request: web.Request) -> web.StreamResponse:
        data = request["post"]
        cooldown = _parse_int(str(data.get("cooldown_hours") or ""), minimum=1, maximum=24 * 365)
        slot = _parse_int(str(data.get("booking_slot_minutes") or ""), minimum=15, maximum=240)
        columns = str(data.get("menu_columns") or "1")
        if cooldown is None or slot is None or columns not in {"1", "2", "3"}:
            raise web.HTTPFound("/admin/settings?err=" + quote("Проверьте числовые настройки"))
        for key, value in {
            "autoresponder_enabled": "1" if data.get("autoresponder_enabled") else "0",
            "greeting": str(data.get("greeting") or "")[:4000],
            "cooldown_hours": str(cooldown),
            "menu_columns": columns,
            "menu_triggers": str(data.get("menu_triggers") or "")[:1000],
            "crm_currency": str(data.get("crm_currency") or "₽")[:8],
            "booking_day_start": str(data.get("booking_day_start") or "10:00")[:5],
            "booking_day_end": str(data.get("booking_day_end") or "23:00")[:5],
            "booking_slot_minutes": str(slot),
        }.items():
            await self.db.set_setting(key, value)
        raise web.HTTPFound("/admin/settings?ok=" + quote("Настройки сохранены"))

    async def pricing(self, request: web.Request) -> web.Response:
        forms = await self.db.list_forms_with_pricing()
        rows = "".join(f'<tr><td><a href="/admin/pricing/{int(f["id"])}">{_e(f["name"])}</a></td><td>{"✅" if f.get("pricing_enabled") else "⚪"}</td><td>{_e(_money(f.get("base_amount")))}</td><td>{_e(f.get("included_hours"))} ч</td><td>{_e(_money(f.get("extra_hour_amount")))}</td><td>{_e(f.get("addon_count"))}</td></tr>' for f in forms)
        content = f'<div class="table-wrap"><table><thead><tr><th>Форма</th><th>Расчёт</th><th>База</th><th>Включено</th><th>Доп. час</th><th>Услуг</th></tr></thead><tbody>{rows}</tbody></table></div>'
        return self.page(request, "Тарифы", content, active="pricing")

    async def pricing_detail(self, request: web.Request) -> web.Response:
        fid = int(request.match_info["fid"])
        form = await self.db.get_form(fid)
        if not form:
            raise web.HTTPNotFound()
        p = await self.db.get_form_pricing(fid)
        addons = await self.db.list_form_addons(fid)
        addon_rows = "".join(f'''<tr><td><form method="post" action="/admin/pricing/{fid}/addon/{int(a['id'])}"><input name="name" value="{_e(a['name'])}"></td><td><input name="amount" value="{int(a['amount'])}"></td><td><label><input style="width:auto" type="checkbox" name="enabled" value="1" {'checked' if a['enabled'] else ''}> Вкл</label></td><td><button class="btn">Сохранить</button></form></td><td><form method="post" action="/admin/pricing/{fid}/addon/{int(a['id'])}/delete"><button class="btn danger">Удалить</button></form></td></tr>''' for a in addons) or '<tr><td colspan="5" class="muted">Дополнительных услуг нет</td></tr>'
        content = f"""<div class="card"><form method="post" action="/admin/pricing/{fid}"><div class="field"><label><input style="width:auto" type="checkbox" name="enabled" value="1" {'checked' if p.get('enabled') else ''}> Автоматический расчёт включён</label></div><div class="row3"><div class="field"><label>Базовая стоимость</label><input name="base_amount" value="{int(p.get('base_amount') or 0)}"></div><div class="field"><label>Включено часов</label><input name="included_hours" value="{int(p.get('included_hours') or 0)}"></div><div class="field"><label>Доп. начатый час</label><input name="extra_hour_amount" value="{int(p.get('extra_hour_amount') or 0)}"></div></div><div class="row"><div class="field"><label>Буфер до, мин</label><input name="buffer_before_minutes" value="{int(p.get('buffer_before_minutes') or 0)}"></div><div class="field"><label>Буфер после, мин</label><input name="buffer_after_minutes" value="{int(p.get('buffer_after_minutes') or 0)}"></div></div><button class="btn primary">Сохранить тариф</button></form></div><div class="section"><h2>Дополнительные услуги</h2><div class="table-wrap"><table><thead><tr><th>Название</th><th>Цена</th><th>Статус</th><th></th><th></th></tr></thead><tbody>{addon_rows}</tbody></table></div><div class="card section"><form method="post" action="/admin/pricing/{fid}/addon"><div class="row"><div class="field"><label>Новая услуга</label><input name="name" required placeholder="Например: Свет"></div><div class="field"><label>Цена</label><input name="amount" required value="0"></div></div><button class="btn">Добавить услугу</button></form></div></div>"""
        return self.page(request, f"Тариф: {form['name']}", content, active="pricing")

    async def pricing_post(self, request: web.Request) -> web.StreamResponse:
        fid = int(request.match_info["fid"])
        data = request["post"]
        values = {
            "enabled": 1 if data.get("enabled") else 0,
            "base_amount": _parse_int(str(data.get("base_amount") or "")),
            "included_hours": _parse_int(str(data.get("included_hours") or ""), maximum=72),
            "extra_hour_amount": _parse_int(str(data.get("extra_hour_amount") or "")),
            "buffer_before_minutes": _parse_int(str(data.get("buffer_before_minutes") or ""), maximum=24*60),
            "buffer_after_minutes": _parse_int(str(data.get("buffer_after_minutes") or ""), maximum=24*60),
        }
        if any(v is None for k, v in values.items() if k != "enabled"):
            raise web.HTTPFound(f"/admin/pricing/{fid}?err=" + quote("Проверьте числовые значения"))
        for field, value in values.items():
            await self.db.update_form_pricing(fid, field, value)
        raise web.HTTPFound(f"/admin/pricing/{fid}?ok=" + quote("Тариф сохранён"))

    async def addon_create(self, request: web.Request) -> web.StreamResponse:
        fid = int(request.match_info["fid"])
        data = request["post"]
        name = str(data.get("name") or "").strip()[:80]
        amount = _parse_int(str(data.get("amount") or ""))
        if not name or amount is None:
            raise web.HTTPFound(f"/admin/pricing/{fid}?err=" + quote("Укажите название и цену"))
        await self.db.add_form_addon(fid, name, amount)
        raise web.HTTPFound(f"/admin/pricing/{fid}?ok=" + quote("Услуга добавлена"))

    async def addon_update(self, request: web.Request) -> web.StreamResponse:
        fid, aid = int(request.match_info["fid"]), int(request.match_info["aid"])
        data = request["post"]
        name = str(data.get("name") or "").strip()[:80]
        amount = _parse_int(str(data.get("amount") or ""))
        if not name or amount is None:
            raise web.HTTPFound(f"/admin/pricing/{fid}?err=" + quote("Некорректные данные услуги"))
        await self.db.update_form_addon_field(aid, "name", name)
        await self.db.update_form_addon_field(aid, "amount", amount)
        await self.db.update_form_addon_field(aid, "enabled", 1 if data.get("enabled") else 0)
        raise web.HTTPFound(f"/admin/pricing/{fid}?ok=" + quote("Услуга сохранена"))

    async def addon_delete(self, request: web.Request) -> web.StreamResponse:
        fid, aid = int(request.match_info["fid"]), int(request.match_info["aid"])
        await self.db.delete_form_addon(aid)
        raise web.HTTPFound(f"/admin/pricing/{fid}?ok=" + quote("Услуга удалена"))

    async def availability(self, request: web.Request) -> web.Response:
        today = datetime.now(self.timezone).date()
        blocks = await self.db.list_availability_blocks(start_date=(today - timedelta(days=7)).isoformat(), limit=300)
        rows = ""
        for b in blocks:
            period = "весь день" if not b.get("start_time") else f"{b.get('start_time')}–{b.get('end_time')}"
            source = f'<a href="/admin/requests/{int(b["source_submission_id"])}">заявка #{int(b["source_submission_id"])}</a>' if b.get("source_submission_id") else "вручную"
            delete = "" if b.get("source_submission_id") else f'<form method="post" action="/admin/availability/{int(b["id"])}/delete"><button class="btn danger">Удалить</button></form>'
            rows += f'<tr><td>{_e(b.get("date_iso"))}</td><td>{_e(period)}</td><td>{_e(b.get("note") or "—")}</td><td>{source}</td><td>{delete}</td></tr>'
        content = f"""<div class="card"><form method="post" action="/admin/availability"><div class="row3"><div class="field"><label>Дата</label><input type="date" name="date_iso" value="{today.isoformat()}" required></div><div class="field"><label>Начало (пусто = весь день)</label><input type="time" name="start_time"></div><div class="field"><label>Окончание</label><input type="time" name="end_time"></div></div><div class="field"><label>Комментарий</label><input name="note" placeholder="Монтаж, технические работы..."></div><button class="btn primary">Добавить блокировку</button></form></div><div class="section table-wrap"><table><thead><tr><th>Дата</th><th>Время</th><th>Комментарий</th><th>Источник</th><th></th></tr></thead><tbody>{rows or '<tr><td colspan="5" class="muted">Нет блокировок</td></tr>'}</tbody></table></div>"""
        return self.page(request, "Занятость", content, active="availability")

    async def availability_add(self, request: web.Request) -> web.StreamResponse:
        data = request["post"]
        date_iso = str(data.get("date_iso") or "")
        start = str(data.get("start_time") or "").strip() or None
        end = str(data.get("end_time") or "").strip() or None
        note = str(data.get("note") or "").strip()[:200] or None
        try:
            base_date = datetime.strptime(date_iso, "%Y-%m-%d").date()
        except ValueError:
            raise web.HTTPFound("/admin/availability?err=" + quote("Некорректная дата"))
        if bool(start) != bool(end):
            raise web.HTTPFound("/admin/availability?err=" + quote("Укажите и начало, и окончание"))
        if not start:
            await self.db.add_availability_block(date_iso, note=note)
        else:
            if not (re.fullmatch(r"\d{2}:\d{2}", start) and re.fullmatch(r"\d{2}:\d{2}", end)):
                raise web.HTTPFound("/admin/availability?err=" + quote("Некорректное время"))
            if end > start:
                await self.db.add_availability_block(date_iso, start, end, note=note)
            else:
                await self.db.add_availability_block(date_iso, start, "24:00", note=note)
                await self.db.add_availability_block((base_date + timedelta(days=1)).isoformat(), "00:00", end, note=note)
        raise web.HTTPFound("/admin/availability?ok=" + quote("Занятость добавлена"))

    async def availability_delete(self, request: web.Request) -> web.StreamResponse:
        bid = int(request.match_info["bid"])
        block = await self.db.get_availability_block(bid)
        if block and not block.get("source_submission_id"):
            await self.db.delete_availability_block(bid)
        raise web.HTTPFound("/admin/availability?ok=" + quote("Блокировка удалена"))

    async def forms(self, request: web.Request) -> web.Response:
        forms = await self.db.list_forms()
        rows = "".join(f'<tr><td><a href="/admin/forms/{int(f["id"])}">{_e(f["name"])}</a></td><td>{"✅" if f["enabled"] else "⚪"}</td><td>{int(f["question_count"])}</td><td>{int(f["button_count"])}</td></tr>' for f in forms)
        content = f'<div class="table-wrap"><table><thead><tr><th>Форма</th><th>Статус</th><th>Вопросов</th><th>Кнопок</th></tr></thead><tbody>{rows}</tbody></table></div>'
        return self.page(request, "Формы", content, active="forms")

    async def form_detail(self, request: web.Request) -> web.Response:
        fid = int(request.match_info["fid"])
        form = await self.db.get_form(fid)
        if not form:
            raise web.HTTPNotFound()
        qs = await self.db.list_form_questions(fid)
        rows = ""
        for q in qs:
            types = "".join(
                f'<option value="{k}" {"selected" if q.get("input_type") == k else ""}>{_e(v)}</option>'
                for k, v in QUESTION_TYPES.items()
            )
            choice_values = "\n".join(str(x) for x in (q.get("choice_options") or []))
            choice_editor = (
                f'<div class="field"><label>Варианты кнопок — по одному на строку</label>'
                f'<textarea name="choice_options" placeholder="Вариант 1\nВариант 2\n✨ Другое">{_e(choice_values)}</textarea>'
                f'<div class="muted">Для типа «🎛 Варианты». Если есть «Другое», бот попросит клиента описать свой вариант.</div></div>'
            )
            rows += (
                f'<tr><td colspan="5"><form method="post" action="/admin/forms/{fid}/question/{int(q["id"])}">'
                f'<div class="row3"><div class="field"><label>Поле</label><input name="label" value="{_e(q["label"])}"></div>'
                f'<div class="field"><label>Тип</label><select name="input_type">{types}</select></div>'
                f'<div class="field"><label>Позиция</label><input name="position" value="{int(q["position"])}"></div></div>'
                f'<div class="field"><label>Вопрос</label><input name="prompt" value="{_e(q["prompt"])}"></div>'
                f'{choice_editor}'
                f'<label><input style="width:auto" type="checkbox" name="required" value="1" {"checked" if q["required"] else ""}> обязательный</label> '
                f'<button class="btn">Сохранить</button></form></td></tr>'
            )
        content = f"""<div class="card"><form method="post" action="/admin/forms/{fid}"><div class="row"><div class="field"><label>Название</label><input name="name" value="{_e(form['name'])}"></div><div class="field"><label>Статус</label><select name="enabled"><option value="1" {'selected' if form['enabled'] else ''}>Включена</option><option value="0" {'selected' if not form['enabled'] else ''}>Выключена</option></select></div></div><button class="btn primary">Сохранить форму</button></form></div><div class="section"><h2>Вопросы</h2><div class="table-wrap"><table><tbody>{rows or '<tr><td class="muted">Вопросов нет</td></tr>'}</tbody></table></div><div class="card section"><form method="post" action="/admin/forms/{fid}/question"><div class="row"><div class="field"><label>Название поля</label><input name="label" required></div><div class="field"><label>Текст вопроса</label><input name="prompt" required></div></div><button class="btn">Добавить вопрос</button></form></div></div>"""
        return self.page(request, f"Форма: {form['name']}", content, active="forms")

    async def form_post(self, request: web.Request) -> web.StreamResponse:
        fid = int(request.match_info["fid"])
        data = request["post"]
        name = str(data.get("name") or "").strip()[:80]
        if not name:
            raise web.HTTPFound(f"/admin/forms/{fid}?err=" + quote("Название обязательно"))
        await self.db.update_form_field(fid, "name", name)
        await self.db.update_form_field(fid, "enabled", 1 if str(data.get("enabled")) == "1" else 0)
        raise web.HTTPFound(f"/admin/forms/{fid}?ok=" + quote("Форма сохранена"))

    async def question_create(self, request: web.Request) -> web.StreamResponse:
        fid = int(request.match_info["fid"])
        data = request["post"]
        label = str(data.get("label") or "").strip()[:80]
        prompt = str(data.get("prompt") or "").strip()[:500]
        if not label or not prompt:
            raise web.HTTPFound(f"/admin/forms/{fid}?err=" + quote("Заполните название и вопрос"))
        await self.db.add_form_question(fid, label, prompt, True)
        raise web.HTTPFound(f"/admin/forms/{fid}?ok=" + quote("Вопрос добавлен"))

    async def question_update(self, request: web.Request) -> web.StreamResponse:
        fid, qid = int(request.match_info["fid"]), int(request.match_info["qid"])
        data = request["post"]
        label = str(data.get("label") or "").strip()[:80]
        prompt = str(data.get("prompt") or "").strip()[:500]
        qtype = str(data.get("input_type") or "text")
        pos = _parse_int(str(data.get("position") or ""), minimum=1, maximum=9999)
        raw_options = str(data.get("choice_options") or "")
        options = [" ".join(line.strip().split()) for line in raw_options.splitlines() if line.strip()]
        if not label or not prompt or qtype not in QUESTION_TYPES or pos is None:
            raise web.HTTPFound(f"/admin/forms/{fid}?err=" + quote("Проверьте вопрос"))
        if qtype == "choice" and not (2 <= len(options) <= 20):
            raise web.HTTPFound(
                f"/admin/forms/{fid}?err=" + quote("Для типа «Варианты» укажите от 2 до 20 кнопок")
            )
        for field, value in {
            "label": label, "prompt": prompt, "input_type": qtype, "position": pos,
            "required": 1 if data.get("required") else 0,
        }.items():
            await self.db.update_form_question_field(qid, field, value)
        if qtype == "choice":
            await self.db.update_form_question_options(qid, options)
        raise web.HTTPFound(f"/admin/forms/{fid}?ok=" + quote("Вопрос сохранён"))

    async def buttons(self, request: web.Request) -> web.Response:
        buttons = await self.db.list_buttons()
        forms = await self.db.list_forms()
        rows = ""
        for b in buttons:
            bound = await self.db.get_bound_form(int(b["id"]))
            opts = '<option value="">Без формы</option>' + "".join(f'<option value="{int(f["id"])}" {"selected" if bound and int(bound["id"]) == int(f["id"]) else ""}>{_e(f["name"])}</option>' for f in forms)
            rows += f'''<tr><td colspan="6"><form method="post" action="/admin/buttons/{int(b['id'])}"><div class="row3"><div class="field"><label>Название</label><input name="title" value="{_e(b['title'])}"></div><div class="field"><label>Форма</label><select name="form_id">{opts}</select></div><div class="field"><label>Позиция</label><input name="position" value="{int(b['position'])}"></div></div><div class="field"><label>Обычный ответ</label><textarea name="response">{_e(b['response'])}</textarea></div><label><input style="width:auto" type="checkbox" name="enabled" value="1" {'checked' if b['enabled'] else ''}> включена</label> <button class="btn">Сохранить</button></form></td></tr>'''
        content = f'<div class="table-wrap"><table><tbody>{rows}</tbody></table></div><div class="card section"><form method="post" action="/admin/buttons"><div class="row"><div class="field"><label>Новая кнопка</label><input name="title" required></div><div class="field"><label>Ответ</label><input name="response" required></div></div><button class="btn">Добавить кнопку</button></form></div>'
        return self.page(request, "Кнопки меню", content, active="buttons")

    async def button_create(self, request: web.Request) -> web.StreamResponse:
        data = request["post"]
        title = str(data.get("title") or "").strip()[:80]
        response = str(data.get("response") or "").strip()[:4000]
        if not title or not response:
            raise web.HTTPFound("/admin/buttons?err=" + quote("Заполните название и ответ"))
        await self.db.add_button(title, response)
        raise web.HTTPFound("/admin/buttons?ok=" + quote("Кнопка добавлена"))

    async def button_update(self, request: web.Request) -> web.StreamResponse:
        bid = int(request.match_info["bid"])
        data = request["post"]
        title = str(data.get("title") or "").strip()[:80]
        response = str(data.get("response") or "")[:4000]
        position = _parse_int(str(data.get("position") or ""), minimum=1, maximum=9999)
        if not title or position is None:
            raise web.HTTPFound("/admin/buttons?err=" + quote("Проверьте данные кнопки"))
        for field, value in {"title": title, "response": response, "position": position, "enabled": 1 if data.get("enabled") else 0}.items():
            await self.db.update_button_field(bid, field, value)
        form_raw = str(data.get("form_id") or "").strip()
        await self.db.set_button_form(bid, int(form_raw) if form_raw.isdigit() else None)
        raise web.HTTPFound("/admin/buttons?ok=" + quote("Кнопка сохранена"))

    def application(self) -> web.Application:
        app = web.Application(middlewares=[self.auth_middleware])
        app.router.add_get("/health", self.health)
        app.router.add_get("/", lambda r: (_ for _ in ()).throw(web.HTTPFound("/admin")))
        app.router.add_get("/admin/login", self.login_get)
        app.router.add_post("/admin/login", self.login_post)
        app.router.add_get("/admin/logout", self.logout)
        app.router.add_get("/admin", self.dashboard)
        app.router.add_get("/admin/requests", self.requests)
        app.router.add_get("/admin/requests/{sid:\\d+}", self.request_detail)
        app.router.add_post("/admin/requests/{sid:\\d+}/status", self.request_status_post)
        app.router.add_post("/admin/requests/{sid:\\d+}/crm", self.request_crm_post)
        app.router.add_get("/admin/settings", self.settings_get)
        app.router.add_post("/admin/settings", self.settings_post)
        app.router.add_get("/admin/pricing", self.pricing)
        app.router.add_get("/admin/pricing/{fid:\\d+}", self.pricing_detail)
        app.router.add_post("/admin/pricing/{fid:\\d+}", self.pricing_post)
        app.router.add_post("/admin/pricing/{fid:\\d+}/addon", self.addon_create)
        app.router.add_post("/admin/pricing/{fid:\\d+}/addon/{aid:\\d+}", self.addon_update)
        app.router.add_post("/admin/pricing/{fid:\\d+}/addon/{aid:\\d+}/delete", self.addon_delete)
        app.router.add_get("/admin/availability", self.availability)
        app.router.add_post("/admin/availability", self.availability_add)
        app.router.add_post("/admin/availability/{bid:\\d+}/delete", self.availability_delete)
        app.router.add_get("/admin/forms", self.forms)
        app.router.add_get("/admin/forms/{fid:\\d+}", self.form_detail)
        app.router.add_post("/admin/forms/{fid:\\d+}", self.form_post)
        app.router.add_post("/admin/forms/{fid:\\d+}/question", self.question_create)
        app.router.add_post("/admin/forms/{fid:\\d+}/question/{qid:\\d+}", self.question_update)
        app.router.add_get("/admin/buttons", self.buttons)
        app.router.add_post("/admin/buttons", self.button_create)
        app.router.add_post("/admin/buttons/{bid:\\d+}", self.button_update)
        return app


async def start_web_admin(
    *,
    port: int,
    db: Database,
    timezone: ZoneInfo,
    username: str,
    password: str,
    on_status_change: StatusChangeCallback,
    on_amount_change: AmountChangeCallback,
    concurrency_snapshot: ConcurrencySnapshotCallback,
) -> WebAdminHandle:
    admin = WebAdmin(
        db=db,
        timezone=timezone,
        username=username,
        password=password,
        on_status_change=on_status_change,
        on_amount_change=on_amount_change,
        concurrency_snapshot=concurrency_snapshot,
    )
    runner = web.AppRunner(admin.application(), access_log=logger)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logger.info("Web admin listening on 0.0.0.0:%s (enabled=%s)", port, admin.enabled)
    return WebAdminHandle(runner)
