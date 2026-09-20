from __future__ import annotations

import asyncio
import calendar as pycalendar
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

from .advanced import AdvancedService, WEEKDAY_NAMES
from .backup import BackupManager
from .db import Database
from .reminders import ReminderService

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
    "file": "📎 Файл / фото",
}

StatusChangeCallback = Callable[[int, str], Awaitable[tuple[bool, str]]]
AmountChangeCallback = Callable[[int, int], Awaitable[tuple[bool, str]]]
ConcurrencySnapshotCallback = Callable[[], dict[str, int]]
PaymentLinkCallback = Callable[[int, str], Awaitable[tuple[bool, str]]]


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


def _answer_html(value: Any) -> str:
    raw = str(value or "")
    if raw.startswith("attachment:"):
        payload = raw[len("attachment:"):]
        stored, _, original = payload.partition("|")
        if re.fullmatch(r"[A-Za-z0-9_.-]+", stored):
            label = original or stored
            return f'<a href="/admin/files/{quote(stored, safe="")}">📎 {_e(label)}</a>'
    return f'<span class="answer">{_e(raw or "—")}</span>'


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
.calendar-head{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:14px}.month-grid{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:1px;background:var(--line);border:1px solid var(--line);border-radius:12px;overflow:hidden}.weekday{padding:8px;text-align:center;background:#142139;color:var(--muted);font-weight:700}.day{min-height:125px;background:var(--panel);padding:7px;overflow:hidden}.day.other{background:#0d1728;color:#60708a}.day.today{outline:2px solid var(--accent);outline-offset:-2px}.day-num{font-weight:800;margin-bottom:5px}.cal-event{display:block;margin:3px 0;padding:4px 6px;border-radius:7px;font-size:11px;line-height:1.25;color:#fff;overflow:hidden;text-overflow:ellipsis}.cal-event.rental{background:#214e79}.cal-event.before,.cal-event.after{background:#55461e}.cal-event.manual{background:#542c4b}.legend{display:flex;gap:12px;flex-wrap:wrap;margin:10px 0 16px}.legend span{display:inline-flex;align-items:center;gap:6px}.dot{width:10px;height:10px;border-radius:3px;display:inline-block}.dot.rental{background:#214e79}.dot.tech{background:#55461e}.dot.manual{background:#542c4b}.small{font-size:12px}
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
        backup_manager: BackupManager,
        reminder_service: ReminderService,
        advanced: AdvancedService,
        users_config: str = "",
        on_payment_link: PaymentLinkCallback | None = None,
    ) -> None:
        self.db = db
        self.timezone = timezone
        self.username = username or "admin"
        self.password = password
        self.on_status_change = on_status_change
        self.on_amount_change = on_amount_change
        self.concurrency_snapshot = concurrency_snapshot
        self.backup_manager = backup_manager
        self.reminder_service = reminder_service
        self.advanced = advanced
        self.on_payment_link = on_payment_link
        self.users: dict[str, dict[str, str]] = {}
        if password and not password.startswith("PASTE_") and len(password) >= 10:
            self.users[self.username] = {"password": password, "role": "owner"}
        for raw in (users_config or "").replace("\n", ",").split(","):
            raw = raw.strip()
            if not raw:
                continue
            parts = raw.split(":", 2)
            if len(parts) != 3:
                continue
            login, secret, role = [x.strip() for x in parts]
            if login and len(secret) >= 10 and role in {"owner", "manager", "viewer"}:
                self.users[login] = {"password": secret, "role": role}
        self.enabled = bool(self.users)
        key_material = "|".join(f"{u}:{v['password']}:{v['role']}" for u,v in sorted(self.users.items())) or secrets.token_hex(16)
        self._key = hashlib.sha256(key_material.encode("utf-8")).digest()

    def _session_cookie(self, username: str, role: str) -> str:
        exp = int(time.time()) + 12 * 3600
        payload = f"{exp}:{username}:{role}"
        sig = hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}:{sig}"

    def _session_data(self, request: web.Request) -> tuple[str, str] | None:
        raw = request.cookies.get("tgadmin", "")
        parts = raw.split(":")
        if len(parts) != 4:
            return None
        exp_s, username, role, sig = parts
        try:
            exp = int(exp_s)
        except ValueError:
            return None
        user = self.users.get(username)
        if exp < int(time.time()) or not user or user.get("role") != role:
            return None
        payload = f"{exp}:{username}:{role}"
        expected = hmac.new(self._key, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        return username, role

    def _valid_session(self, request: web.Request) -> bool:
        return self._session_data(request) is not None

    def _csrf(self, request: web.Request) -> str:
        cookie = request.cookies.get("tgadmin", "")
        return hmac.new(self._key, ("csrf:" + cookie).encode(), hashlib.sha256).hexdigest()

    async def auth_middleware(self, app: web.Application, handler):
        async def middleware_handler(request: web.Request):
            if request.path == "/health" or request.path == "/calendar.ics" or request.path.startswith("/admin/login"):
                return await handler(request)
            if not self.enabled:
                return await self.disabled_page(request)
            session = self._session_data(request)
            if not session:
                raise web.HTTPFound("/admin/login?next=" + quote(request.path_qs, safe=""))
            username, role = session
            request["admin_user"] = username
            request["admin_role"] = role
            if request.method == "POST":
                if role == "viewer":
                    raise web.HTTPForbidden(text="Роль viewer доступна только для просмотра")
                if role != "owner" and (request.path.startswith("/admin/settings") or request.path.startswith("/admin/integrations") or "/restore" in request.path):
                    raise web.HTTPForbidden(text="Это действие доступно только владельцу")
                data = await request.post()
                token = str(data.get("csrf") or "")
                if not hmac.compare_digest(token, self._csrf(request)):
                    raise web.HTTPForbidden(text="CSRF token invalid")
                request["post"] = data
                safe_details = ", ".join(k for k in data.keys() if k not in {"csrf", "password"})
                try:
                    response = await handler(request)
                except web.HTTPException as exc:
                    if 300 <= exc.status < 400:
                        await self.advanced.add_audit(username, role, request.method + " " + request.path, request.path, safe_details)
                    raise
                await self.advanced.add_audit(username, role, request.method + " " + request.path, request.path, safe_details)
                return response
            return await handler(request)

        return middleware_handler

    def page(self, request: web.Request, title: str, content: str, *, active: str = "") -> web.Response:
        nav = [
            ("dashboard", "/admin", "🏠 Обзор"),
            ("requests", "/admin/requests", "📋 Заявки"),
            ("clients", "/admin/clients", "👥 Клиенты"),
            ("client_requests", "/admin/client-requests", "📨 Запросы клиентов"),
            ("analytics", "/admin/analytics", "📊 Аналитика"),
            ("calendar", "/admin/calendar", "🗓 Календарь"),
            ("availability", "/admin/availability", "📅 Занятость"),
            ("reminders", "/admin/reminders", "🔔 Напоминания"),
            ("backups", "/admin/backups", "💾 Бэкапы"),
            ("pricing", "/admin/pricing", "💰 Тарифы"),
            ("rules", "/admin/rules", "🧭 Правила брони"),
            ("audit", "/admin/audit", "🧾 Журнал"),
            ("integrations", "/admin/integrations", "🔌 Интеграции"),
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
<body><div class="shell"><aside class="side"><div class="brand">TG Business AutoReply</div><nav class="nav">{nav_html}<a href="/admin/logout">🚪 Выйти</a></nav></aside><main class="main"><div class="top"><h1>{_e(title)}</h1><span class="muted">{_e((request.get("admin_user") or ""))} · {_e((request.get("admin_role") or ""))} · TZ: {_e(self.timezone.key)}</span></div>{flash_html}{content}</main></div>
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
        user = self.users.get(username)
        if not user or not hmac.compare_digest(password, user.get("password", "")):
            await asyncio.sleep(0.8)
            raise web.HTTPFound("/admin/login?error=1")
        target = str(data.get("next") or "/admin")
        if not target.startswith("/") or target.startswith("//"):
            target = "/admin"
        response = web.HTTPFound(target)
        response.set_cookie("tgadmin", self._session_cookie(username, user["role"]), httponly=True, samesite="Lax", max_age=12 * 3600, path="/")
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
        backups = await self.backup_manager.list_backups()
        reminders_enabled = (await self.db.get_setting("reminders_enabled", "0")) == "1"
        cards = f"""<div class="grid">
<div class="card"><div class="muted">Заявок</div><div class="metric">{stat['submissions']}</div></div>
<div class="card"><div class="muted">Новых</div><div class="metric">{counts['new']}</div></div>
<div class="card"><div class="muted">В работе</div><div class="metric">{counts['in_progress']}</div></div>
<div class="card"><div class="muted">Подтверждено / оплачено</div><div class="metric">{counts['confirmed'] + counts['paid']}</div></div>
<div class="card"><div class="muted">Контактов</div><div class="metric">{stat['contacts']}</div></div>
<div class="card"><div class="muted">Business</div><div class="metric">{'🟢' if connection and connection.get('enabled') else '⚪'}</div><div class="muted">{'подключён' if connection and connection.get('enabled') else 'не подключён'}</div></div>
<div class="card"><div class="muted">Telegram updates</div><div class="metric">{queue['active']} / {queue['max_concurrent']}</div><div class="muted">ожидает: {queue['waiting']} · chat-lock: {queue['chat_locks']}</div></div>
<div class="card"><div class="muted">Напоминания</div><div class="metric">{'🔔' if reminders_enabled else '⚪'}</div><div class="muted">{'включены' if reminders_enabled else 'выключены'}</div></div>
<div class="card"><div class="muted">Резервных копий</div><div class="metric">{len(backups)}</div><div class="muted">последняя: {_e(datetime.fromtimestamp(backups[0].mtime, tz=self.timezone).strftime('%d.%m %H:%M') if backups else '—')}</div></div>
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
                f'<div><b>{_e(q.get("label"))}:</b> {_answer_html(answers_data.get(str(q.get("id"))) or "—")}</div>'
                for q in questions
            )
        else:
            answers = "".join(f'<div><b>{_e(k)}:</b> {_answer_html(v)}</div>' for k, v in answers_data.items())
        answers = answers or '<span class="muted">Нет ответов</span>'
        events = await self.db.list_submission_events(sid, limit=30)
        event_rows = "".join(f'<tr><td>{_e(_local_dt(ev.get("created_at"), self.timezone))}</td><td>{_e(ev.get("event_type"))}</td><td>{_e(ev.get("old_value") or "—")}</td><td>{_e(ev.get("new_value") or "—")}</td></tr>' for ev in events) or '<tr><td colspan="4" class="muted">Нет событий</td></tr>'
        client = " ".join(x for x in [item.get("first_name"), item.get("last_name")] if x) or "—"
        content = f"""<div class="grid"><div class="card"><div class="muted">Клиент</div><h3>{_e(client)}</h3><div>@{_e(item.get('username') or '—')}</div><div>User ID: {_e(item.get('user_id') or '—')}</div></div><div class="card"><div class="muted">Форма</div><h3>{_e(item.get('form_name'))}</h3><div>{_e(_local_dt(item.get('created_at'), self.timezone))}</div></div><div class="card"><div class="muted">Стоимость</div><div class="metric">{_e(_money(total,currency))}</div><div>Предоплата: {_e(_money(prepay,currency))}<br>Остаток: {_e(_money(balance,currency))}</div></div></div>
<div class="section row"><div class="card"><h2>Статус</h2><form method="post" action="/admin/requests/{sid}/status"><div class="field"><select name="status">{status_opts}</select></div><button class="btn primary">Сохранить и уведомить клиента</button></form></div><div class="card"><h2>CRM</h2><form method="post" action="/admin/requests/{sid}/crm"><div class="row"><div class="field"><label>Стоимость</label><input name="total_amount" value="{total}"></div><div class="field"><label>Предоплата</label><input name="prepayment_amount" value="{prepay}"></div></div><div class="field"><label>Внутренняя заметка</label><textarea name="internal_note">{_e(item.get('internal_note') or '')}</textarea></div><button class="btn primary">Сохранить</button></form></div></div>
<div class="section card"><h2>Ответы формы</h2><div class="kv">{answers}</div></div><div class="section"><h2>История</h2><div class="table-wrap"><table><thead><tr><th>Время</th><th>Событие</th><th>Было</th><th>Стало</th></tr></thead><tbody>{event_rows}</tbody></table></div></div>"""
        payment_template = str(await self.db.get_setting("payment_link_template", "") or "").strip()
        if payment_template:
            content += f'<div class="section card"><h2>Предоплата</h2><p class="muted">Отправить клиенту настроенную ссылку на предоплату.</p><form method="post" action="/admin/requests/{sid}/payment"><button class="btn primary">💳 Отправить ссылку на оплату</button></form></div>'
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
        content = f"""<div class="card"><form method="post" action="/admin/pricing/{fid}"><div class="field"><label><input style="width:auto" type="checkbox" name="enabled" value="1" {'checked' if p.get('enabled') else ''}> Автоматический расчёт включён</label></div><div class="row3"><div class="field"><label>Базовая стоимость</label><input name="base_amount" value="{int(p.get('base_amount') or 0)}"></div><div class="field"><label>Включено часов</label><input name="included_hours" value="{int(p.get('included_hours') or 0)}"></div><div class="field"><label>Доп. начатый час</label><input name="extra_hour_amount" value="{int(p.get('extra_hour_amount') or 0)}"></div></div><div class="field"><label>Описание базовой стоимости</label><textarea name="base_description" rows="5" maxlength="1500" placeholder="Например: до 6 часов аренды, мебель, базовая уборка, дежурный администратор">{_e(p.get('base_description') or '')}</textarea><div class="muted">Это пояснение увидит клиент в предварительном расчёте стоимости.</div></div><div class="row"><div class="field"><label>Буфер до, мин</label><input name="buffer_before_minutes" value="{int(p.get('buffer_before_minutes') or 0)}"></div><div class="field"><label>Буфер после, мин</label><input name="buffer_after_minutes" value="{int(p.get('buffer_after_minutes') or 0)}"></div></div><button class="btn primary">Сохранить тариф</button></form></div><div class="section"><h2>Дополнительные услуги</h2><div class="table-wrap"><table><thead><tr><th>Название</th><th>Цена</th><th>Статус</th><th></th><th></th></tr></thead><tbody>{addon_rows}</tbody></table></div><div class="card section"><form method="post" action="/admin/pricing/{fid}/addon"><div class="row"><div class="field"><label>Новая услуга</label><input name="name" required placeholder="Например: Свет"></div><div class="field"><label>Цена</label><input name="amount" required value="0"></div></div><button class="btn">Добавить услугу</button></form></div></div>"""
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
        base_description = str(data.get("base_description") or "").strip()[:1500]
        await self.db.update_form_pricing(fid, "base_description", base_description)
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

    @staticmethod
    def _parse_booking_date(value: str | None):
        raw = (value or "").strip()
        for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
        return None

    @staticmethod
    def _parse_clock(value: str | None) -> tuple[int, int] | None:
        raw = (value or "").strip().replace(".", ":")
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
        if not match:
            return None
        hour, minute = int(match.group(1)), int(match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return hour, minute

    @staticmethod
    def _is_end_question(question: dict[str, Any]) -> bool:
        label = str(question.get("label") or "").casefold()
        prompt = str(question.get("prompt") or "").casefold()
        return any(word in label or word in prompt for word in ("оконч", "до сколь", "конец", "заверш"))

    def _booking_window(self, submission: dict[str, Any], questions: list[dict[str, Any]]) -> tuple[datetime, datetime] | None:
        answers = submission.get("answers") or {}
        booked_date = None
        start_clock = None
        end_clock = None
        time_values: list[tuple[dict[str, Any], tuple[int, int]]] = []
        for q in questions:
            qid = str(q.get("id"))
            value = answers.get(qid)
            if q.get("input_type") == "date" and booked_date is None:
                booked_date = self._parse_booking_date(value)
            if q.get("input_type") == "time":
                parsed = self._parse_clock(value)
                if parsed:
                    time_values.append((q, parsed))
                    if self._is_end_question(q):
                        end_clock = parsed
                    elif start_clock is None:
                        start_clock = parsed
        if booked_date is None:
            return None
        if start_clock is None and time_values:
            start_clock = time_values[0][1]
        if end_clock is None and len(time_values) >= 2:
            end_clock = time_values[1][1]
        if start_clock is None:
            return None
        if end_clock is None:
            end_clock = ((start_clock[0] + 1) % 24, start_clock[1])
        start = datetime.combine(booked_date, datetime.min.time()).replace(
            hour=start_clock[0], minute=start_clock[1], tzinfo=self.timezone
        )
        end = datetime.combine(booked_date, datetime.min.time()).replace(
            hour=end_clock[0], minute=end_clock[1], tzinfo=self.timezone
        )
        if end <= start:
            end += timedelta(days=1)
        return start, end

    @staticmethod
    def _event_segments(start: datetime, end: datetime) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        day = start.date()
        while day <= end.date():
            day_start = datetime.combine(day, datetime.min.time(), tzinfo=start.tzinfo)
            next_day = day_start + timedelta(days=1)
            seg_start = max(start, day_start)
            seg_end = min(end, next_day)
            if seg_end > seg_start:
                left = seg_start.strftime("%H:%M")
                right = "24:00" if seg_end == next_day else seg_end.strftime("%H:%M")
                result.append((day.isoformat(), f"{left}–{right}"))
            day += timedelta(days=1)
        return result

    async def calendar_view(self, request: web.Request) -> web.Response:
        now = datetime.now(self.timezone)
        raw_month = (request.query.get("month") or now.strftime("%Y-%m")).strip()
        try:
            first = datetime.strptime(raw_month, "%Y-%m").date().replace(day=1)
        except ValueError:
            first = now.date().replace(day=1)
        if first.month == 12:
            next_first = first.replace(year=first.year + 1, month=1)
        else:
            next_first = first.replace(month=first.month + 1)
        prev_last = first - timedelta(days=1)
        prev_first = prev_last.replace(day=1)
        last = next_first - timedelta(days=1)

        events: dict[str, list[dict[str, str]]] = {}
        bookings = await self.db.list_booking_submissions(limit=2000, statuses=("confirmed", "paid", "completed"))
        for item in bookings:
            if not item.get("form_id"):
                continue
            questions = await self.db.list_form_questions(int(item["form_id"]))
            window = self._booking_window(item, questions)
            if not window:
                continue
            start, end = window
            if end.date() < first or start.date() > last:
                continue
            link = f"/admin/requests/{int(item['id'])}"
            for date_iso, period in self._event_segments(start, end):
                if first.isoformat() <= date_iso <= last.isoformat():
                    events.setdefault(date_iso, []).append({
                        "kind": "rental", "label": f"{period} · #{int(item['id'])} {item.get('form_name') or 'Бронь'}", "url": link,
                    })
            pricing = await self.db.get_form_pricing(int(item["form_id"]))
            before = max(0, int(pricing.get("buffer_before_minutes") or 0))
            after = max(0, int(pricing.get("buffer_after_minutes") or 0))
            if before:
                for date_iso, period in self._event_segments(start - timedelta(minutes=before), start):
                    if first.isoformat() <= date_iso <= last.isoformat():
                        events.setdefault(date_iso, []).append({
                            "kind": "before", "label": f"{period} · монтаж #{int(item['id'])}", "url": link,
                        })
            if after:
                for date_iso, period in self._event_segments(end, end + timedelta(minutes=after)):
                    if first.isoformat() <= date_iso <= last.isoformat():
                        events.setdefault(date_iso, []).append({
                            "kind": "after", "label": f"{period} · уборка #{int(item['id'])}", "url": link,
                        })

        manual_blocks = await self.db.list_availability_blocks(start_date=first.isoformat(), end_date=last.isoformat(), limit=500)
        for block in manual_blocks:
            if block.get("source_submission_id"):
                continue
            date_iso = str(block.get("date_iso") or "")
            if not (first.isoformat() <= date_iso <= last.isoformat()):
                continue
            period = "весь день" if not block.get("start_time") else f"{block.get('start_time')}–{block.get('end_time')}"
            note = str(block.get("note") or "ручная блокировка")
            events.setdefault(date_iso, []).append({"kind": "manual", "label": f"{period} · {note}", "url": "/admin/availability"})

        recurring = await self.advanced.list_recurring_blocks(None)
        if recurring:
            day = first
            while day <= last:
                for block in recurring:
                    if not block.get("enabled") or int(block.get("weekday") or -1) != day.weekday():
                        continue
                    period = "весь день" if not block.get("start_time") else f"{block.get('start_time')}–{block.get('end_time')}"
                    note = str(block.get("note") or "регулярная занятость")
                    events.setdefault(day.isoformat(), []).append({"kind": "manual", "label": f"{period} · ↻ {note}", "url": "/admin/rules"})
                day += timedelta(days=1)

        cells = []
        weekdays = "".join(f'<div class="weekday">{name}</div>' for name in ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"))
        for week in pycalendar.Calendar(firstweekday=0).monthdatescalendar(first.year, first.month):
            for day in week:
                day_events = events.get(day.isoformat(), [])
                shown = day_events[:6]
                event_html = "".join(
                    f'<a class="cal-event {_e(ev["kind"])}" href="{_e(ev["url"])}" title="{_e(ev["label"])}">{_e(ev["label"])}</a>'
                    for ev in shown
                )
                if len(day_events) > len(shown):
                    event_html += f'<div class="muted small">+ ещё {len(day_events)-len(shown)}</div>'
                cls = "day"
                if day.month != first.month:
                    cls += " other"
                if day == now.date():
                    cls += " today"
                cells.append(f'<div class="{cls}"><div class="day-num">{day.day}</div>{event_html}</div>')
        content = f'''<div class="calendar-head"><a class="btn" href="/admin/calendar?month={prev_first:%Y-%m}">← {_e(prev_first.strftime("%m.%Y"))}</a><h2>{_e(first.strftime("%m.%Y"))}</h2><a class="btn" href="/admin/calendar?month={next_first:%Y-%m}">{_e(next_first.strftime("%m.%Y"))} →</a></div>
<div class="legend"><span><i class="dot rental"></i> аренда</span><span><i class="dot tech"></i> монтаж / уборка</span><span><i class="dot manual"></i> ручная занятость</span></div>
<div class="month-grid">{weekdays}{''.join(cells)}</div>
<div class="section actions"><a class="btn" href="/admin/availability">Управлять занятостью</a><a class="btn" href="/admin/requests">Открыть заявки</a></div>'''
        return self.page(request, "Календарь", content, active="calendar")

    @staticmethod
    def _normalize_hours(raw: str) -> str | None:
        values: list[int] = []
        for token in raw.replace(";", ",").replace("\n", ",").split(","):
            token = token.strip()
            if not token:
                continue
            if not token.isdigit():
                return None
            value = int(token)
            if not 1 <= value <= 24 * 90:
                return None
            if value not in values:
                values.append(value)
        if not values:
            return None
        return ",".join(str(x) for x in sorted(values, reverse=True)[:12])

    async def reminders_get(self, request: web.Request) -> web.Response:
        enabled = (await self.db.get_setting("reminders_enabled", "0")) == "1"
        client_hours = await self.db.get_setting("reminder_client_hours", "168,24,3") or "168,24,3"
        admin_hours = await self.db.get_setting("reminder_admin_hours", "168,24,3") or "168,24,3"
        grace = await self.db.get_setting("reminder_grace_hours", "6") or "6"
        client_template = await self.db.get_setting("reminder_client_template", "") or ""
        admin_template = await self.db.get_setting("reminder_admin_template", "") or ""
        deliveries = await self.db.list_reminder_deliveries(limit=80)
        rows = ""
        status_labels = {"sent": "✅ отправлено", "failed": "❌ ошибка", "missed": "⏭ пропущено", "pending": "⏳ ожидает"}
        for item in deliveries:
            recipient = "Клиент" if item.get("recipient_key") == "client" else str(item.get("recipient_key") or "—")
            err = str(item.get("error") or "")
            rows += (
                f'<tr><td><a href="/admin/requests/{int(item["submission_id"])}">#{int(item["submission_id"])}</a></td>'
                f'<td>{_e(recipient)}</td><td>{int(item.get("hours_before") or 0)} ч</td>'
                f'<td>{_e(status_labels.get(str(item.get("status")), item.get("status")))}</td>'
                f'<td>{_e(_local_dt(item.get("sent_at") or item.get("last_attempt_at"), self.timezone))}</td>'
                f'<td class="small">{_e(err[:180] or "—")}</td></tr>'
            )
        variables = "{id}, {form}, {client}, {username}, {date}, {time}, {hours_before}, {amount}, {prepayment}, {balance}"
        content = f'''<div class="card"><form method="post" action="/admin/reminders"><div class="field"><label><input style="width:auto" type="checkbox" name="enabled" value="1" {'checked' if enabled else ''}> Автоматические напоминания включены</label></div><div class="row3"><div class="field"><label>Клиенту за, часов</label><input name="client_hours" value="{_e(client_hours)}"><div class="muted">Например: 168,24,3</div></div><div class="field"><label>Админу за, часов</label><input name="admin_hours" value="{_e(admin_hours)}"></div><div class="field"><label>Допустимое опоздание, часов</label><input name="grace_hours" value="{_e(grace)}"><div class="muted">После простоя бот не пришлёт давно просроченное напоминание.</div></div></div><div class="field"><label>Шаблон клиенту</label><textarea name="client_template">{_e(client_template)}</textarea></div><div class="field"><label>Шаблон администратору</label><textarea name="admin_template">{_e(admin_template)}</textarea><div class="muted">Переменные: {_e(variables)}</div></div><button class="btn primary">Сохранить</button></form><form method="post" action="/admin/reminders/run" class="section"><button class="btn">Проверить напоминания сейчас</button></form></div><div class="section"><h2>История доставки</h2><div class="table-wrap"><table><thead><tr><th>Заявка</th><th>Кому</th><th>За сколько</th><th>Статус</th><th>Время</th><th>Ошибка</th></tr></thead><tbody>{rows or '<tr><td colspan="6" class="muted">История пока пуста</td></tr>'}</tbody></table></div></div>'''
        return self.page(request, "Напоминания", content, active="reminders")

    async def reminders_post(self, request: web.Request) -> web.StreamResponse:
        data = request["post"]
        client_hours = self._normalize_hours(str(data.get("client_hours") or ""))
        admin_hours = self._normalize_hours(str(data.get("admin_hours") or ""))
        grace = _parse_int(str(data.get("grace_hours") or ""), minimum=1, maximum=72)
        client_template = str(data.get("client_template") or "").strip()[:4096]
        admin_template = str(data.get("admin_template") or "").strip()[:4096]
        if client_hours is None or admin_hours is None or grace is None or not client_template or not admin_template:
            raise web.HTTPFound("/admin/reminders?err=" + quote("Проверьте интервалы и шаблоны"))
        for key, value in {
            "reminders_enabled": "1" if data.get("enabled") else "0",
            "reminder_client_hours": client_hours,
            "reminder_admin_hours": admin_hours,
            "reminder_grace_hours": str(grace),
            "reminder_client_template": client_template,
            "reminder_admin_template": admin_template,
        }.items():
            await self.db.set_setting(key, value)
        raise web.HTTPFound("/admin/reminders?ok=" + quote("Настройки напоминаний сохранены"))

    async def reminders_run(self, request: web.Request) -> web.StreamResponse:
        result = await self.reminder_service.run_once()
        message = f"Проверено: {result['checked']}; отправлено: {result['sent']}; ошибок: {result['failed']}; пропущено: {result['missed']}"
        raise web.HTTPFound("/admin/reminders?ok=" + quote(message))

    async def backups_get(self, request: web.Request) -> web.Response:
        enabled = (await self.db.get_setting("backups_enabled", "1")) == "1"
        hour = await self.db.get_setting("backup_hour_local", "4") or "4"
        retention = await self.db.get_setting("backup_retention", "30") or "30"
        backups = await self.backup_manager.list_backups()
        kind_names = {"auto": "авто", "manual": "ручная", "pre-restore": "до восстановления"}
        rows = ""
        for item in backups:
            dt = datetime.fromtimestamp(item.mtime, tz=self.timezone).strftime("%d.%m.%Y %H:%M")
            size = f"{item.size / 1024:.1f} КБ" if item.size < 1024 * 1024 else f"{item.size / 1024 / 1024:.1f} МБ"
            rows += f'''<tr><td>{_e(dt)}</td><td>{_e(kind_names.get(item.kind, item.kind))}</td><td>{_e(size)}</td><td class="small">{_e(item.name)}</td><td><div class="actions"><a class="btn" href="/admin/backups/{_e(item.name)}/download">Скачать</a><form method="post" action="/admin/backups/{_e(item.name)}/restore" onsubmit="return confirm('Восстановить базу из этой копии? Перед восстановлением будет создана страховочная копия.')"><button class="btn danger">Восстановить</button></form><form method="post" action="/admin/backups/{_e(item.name)}/delete" onsubmit="return confirm('Удалить эту резервную копию?')"><button class="btn ghost">Удалить</button></form></div></td></tr>'''
        content = f'''<div class="card"><form method="post" action="/admin/backups/settings"><div class="field"><label><input style="width:auto" type="checkbox" name="enabled" value="1" {'checked' if enabled else ''}> Ежедневные автобэкапы включены</label></div><div class="row"><div class="field"><label>Час создания по TZ (0–23)</label><input name="hour" value="{_e(hour)}"></div><div class="field"><label>Хранить последних копий</label><input name="retention" value="{_e(retention)}"></div></div><button class="btn primary">Сохранить настройки</button></form><form method="post" action="/admin/backups/create" class="section"><button class="btn">Создать резервную копию сейчас</button></form><p class="muted">Каталог: <code>{_e(str(self.backup_manager.backup_dir))}</code>. Перед каждым восстановлением автоматически создаётся отдельная страховочная копия текущей базы.</p></div><div class="section"><h2>Резервные копии</h2><div class="table-wrap"><table><thead><tr><th>Создана</th><th>Тип</th><th>Размер</th><th>Файл</th><th></th></tr></thead><tbody>{rows or '<tr><td colspan="5" class="muted">Копий пока нет</td></tr>'}</tbody></table></div></div>'''
        return self.page(request, "Резервные копии", content, active="backups")

    async def backups_settings(self, request: web.Request) -> web.StreamResponse:
        data = request["post"]
        hour = _parse_int(str(data.get("hour") or ""), minimum=0, maximum=23)
        retention = _parse_int(str(data.get("retention") or ""), minimum=3, maximum=365)
        if hour is None or retention is None:
            raise web.HTTPFound("/admin/backups?err=" + quote("Проверьте час и количество копий"))
        await self.db.set_setting("backups_enabled", "1" if data.get("enabled") else "0")
        await self.db.set_setting("backup_hour_local", str(hour))
        await self.db.set_setting("backup_retention", str(retention))
        await self.backup_manager.prune()
        raise web.HTTPFound("/admin/backups?ok=" + quote("Настройки резервного копирования сохранены"))

    async def backups_create(self, request: web.Request) -> web.StreamResponse:
        info = await self.backup_manager.create_backup("manual")
        raise web.HTTPFound("/admin/backups?ok=" + quote(f"Создана копия {info.name}"))

    async def backups_download(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        try:
            path = self.backup_manager._safe_path(name)
        except ValueError:
            raise web.HTTPNotFound()
        if not path.exists():
            raise web.HTTPNotFound()
        response = web.FileResponse(path)
        response.headers["Content-Disposition"] = f'attachment; filename="{path.name}"'
        response.headers["Cache-Control"] = "no-store"
        return response

    async def backups_restore(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        try:
            safety = await self.backup_manager.restore_backup(name)
        except (ValueError, FileNotFoundError) as exc:
            raise web.HTTPFound("/admin/backups?err=" + quote(str(exc)))
        except Exception as exc:
            logger.exception("Web backup restore failed")
            raise web.HTTPFound("/admin/backups?err=" + quote(f"Восстановление не выполнено: {exc}"))
        raise web.HTTPFound("/admin/backups?ok=" + quote(f"База восстановлена из {name}. Страховочная копия: {safety}"))

    async def backups_delete(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        try:
            await self.backup_manager.delete_backup(name)
        except ValueError:
            raise web.HTTPNotFound()
        raise web.HTTPFound("/admin/backups?ok=" + quote("Резервная копия удалена"))

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

    async def file_download(self, request: web.Request) -> web.StreamResponse:
        from pathlib import Path
        name = request.match_info["name"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise web.HTTPNotFound()
        path = Path(self.db.path).parent / "uploads" / name
        if not path.is_file():
            raise web.HTTPNotFound(text="Файл не найден")
        return web.FileResponse(path, headers={"Content-Disposition": f'inline; filename="{name}"'})

    async def clients(self, request: web.Request) -> web.Response:
        query = (request.query.get("q") or "").strip()
        items = await self.advanced.list_clients(query=query, limit=300)
        currency = str(await self.db.get_setting("crm_currency", "₽") or "₽")
        rows = ""
        for item in items:
            key = f"u:{int(item['user_id'])}" if item.get("user_id") else f"c:{int(item.get('chat_id') or 0)}"
            name = " ".join(x for x in [item.get("first_name"), item.get("last_name")] if x) or ("@" + str(item.get("username")) if item.get("username") else key)
            rows += f'<tr><td><a href="/admin/clients/{quote(key, safe=":")}">{_e(name)}</a></td><td>@{_e(item.get("username") or "—")}</td><td>{int(item.get("submissions_count") or 0)}</td><td>{int(item.get("successful_count") or 0)}</td><td>{_e(_money(item.get("total_amount"), currency))}</td><td>{_e(_local_dt(item.get("last_submission_at"), self.timezone))}</td></tr>'
        rows = rows or '<tr><td colspan="6" class="muted">Клиентов пока нет</td></tr>'
        content = f"""<form method="get" class="card"><div class="row"><div class="field"><label>Поиск клиента</label><input name="q" value="{_e(query)}" placeholder="Имя, @username, телефон"></div><div class="field"><label>&nbsp;</label><button class="btn primary">Найти</button></div></div></form>
<div class="section actions"><a class="btn" href="/admin/export.csv">⬇️ CSV</a><a class="btn" href="/admin/export.xlsx">⬇️ Excel</a></div>
<div class="section table-wrap"><table><thead><tr><th>Клиент</th><th>Username</th><th>Заявок</th><th>Успешных</th><th>Сумма</th><th>Последняя</th></tr></thead><tbody>{rows}</tbody></table></div>"""
        return self.page(request, "Клиенты", content, active="clients")

    async def client_detail(self, request: web.Request) -> web.Response:
        key = request.match_info["client_key"]
        client = await self.advanced.get_client(key)
        if not client:
            raise web.HTTPNotFound(text="Клиент не найден")
        items = await self.db.list_client_submissions(user_id=client.get("user_id"), chat_id=client.get("chat_id"), limit=50)
        notes = await self.advanced.list_client_notes(key)
        currency = str(await self.db.get_setting("crm_currency", "₽") or "₽")
        name = " ".join(x for x in [client.get("first_name"), client.get("last_name")] if x) or ("@" + str(client.get("username")) if client.get("username") else key)
        request_rows = "".join(self._request_row(x, currency) for x in items) or '<tr><td colspan="6" class="muted">Заявок нет</td></tr>'
        note_rows = "".join(f'<div class="card section"><div class="muted">{_e(_local_dt(n.get("created_at"), self.timezone))} · {_e(n.get("admin_name") or "admin")}</div><div class="answer">{_e(n.get("note"))}</div></div>' for n in notes) or '<div class="muted">Заметок нет</div>'
        content = f"""<div class="grid"><div class="card"><div class="muted">Клиент</div><div class="metric">{_e(name)}</div><div>@{_e(client.get("username") or "—")}</div></div><div class="card"><div class="muted">Заявок</div><div class="metric">{int(client.get("submissions_count") or 0)}</div></div><div class="card"><div class="muted">Сумма</div><div class="metric">{_e(_money(client.get("total_amount"), currency))}</div></div></div>
<div class="section"><h2>Заявки</h2><div class="table-wrap"><table><thead><tr><th>№</th><th>Клиент</th><th>Форма</th><th>Статус</th><th>Сумма</th><th>Создана</th></tr></thead><tbody>{request_rows}</tbody></table></div></div>
<div class="section"><h2>CRM-заметки</h2><form method="post" action="/admin/clients/{quote(key, safe=':')}" class="card"><input type="hidden" name="action" value="note"><div class="field"><textarea name="note" required placeholder="Внутренняя заметка о клиенте"></textarea></div><button class="btn primary">Добавить заметку</button></form>{note_rows}</div>"""
        return self.page(request, f"Клиент: {name}", content, active="clients")

    async def client_note_post(self, request: web.Request) -> web.StreamResponse:
        key = request.match_info["client_key"]
        note = str(request["post"].get("note") or "").strip()
        if not note:
            raise web.HTTPFound(f"/admin/clients/{quote(key, safe=':')}?err=" + quote("Введите заметку"))
        await self.advanced.add_client_note(key, note, str(request.get("admin_user") or "admin"))
        raise web.HTTPFound(f"/admin/clients/{quote(key, safe=':')}?ok=" + quote("Заметка добавлена"))

    async def analytics_view(self, request: web.Request) -> web.Response:
        a = await self.advanced.analytics()
        currency = str(await self.db.get_setting("crm_currency", "₽") or "₽")
        cards = f"""<div class="grid"><div class="card"><div class="muted">Всего заявок</div><div class="metric">{int(a.get('total') or 0)}</div></div><div class="card"><div class="muted">Конверсия в подтверждение</div><div class="metric">{_e(a.get('conversion_percent'))}%</div></div><div class="card"><div class="muted">Выручка по подтверждённым</div><div class="metric">{_e(_money(a.get('revenue'), currency))}</div></div><div class="card"><div class="muted">Средний чек</div><div class="metric">{_e(_money(round(float(a.get('avg_check') or 0)), currency))}</div></div></div>"""
        form_rows = "".join(f'<tr><td>{_e(x.get("form_name"))}</td><td>{int(x.get("c") or 0)}</td><td>{_e(_money(x.get("revenue"), currency))}</td></tr>' for x in a.get("forms", [])) or '<tr><td colspan="3" class="muted">Нет данных</td></tr>'
        month_rows = "".join(f'<tr><td>{_e(x.get("month"))}</td><td>{int(x.get("c") or 0)}</td><td>{_e(_money(x.get("revenue"), currency))}</td></tr>' for x in a.get("months", [])) or '<tr><td colspan="3" class="muted">Нет данных</td></tr>'
        content = cards + f"""<div class="row section"><div><h2>По формам</h2><div class="table-wrap"><table><thead><tr><th>Форма</th><th>Заявок</th><th>Выручка</th></tr></thead><tbody>{form_rows}</tbody></table></div></div><div><h2>По месяцам</h2><div class="table-wrap"><table><thead><tr><th>Месяц</th><th>Заявок</th><th>Выручка</th></tr></thead><tbody>{month_rows}</tbody></table></div></div></div>"""
        return self.page(request, "Аналитика", content, active="analytics")

    async def audit_view(self, request: web.Request) -> web.Response:
        items = await self.advanced.list_audit(300)
        rows = "".join(f'<tr><td>{_e(_local_dt(x.get("created_at"), self.timezone))}</td><td>{_e(x.get("actor"))}</td><td>{_e(x.get("role"))}</td><td>{_e(x.get("action"))}</td><td>{_e(x.get("details") or "")}</td></tr>' for x in items) or '<tr><td colspan="5" class="muted">Журнал пуст</td></tr>'
        content = f'<div class="table-wrap"><table><thead><tr><th>Время</th><th>Кто</th><th>Роль</th><th>Действие</th><th>Детали</th></tr></thead><tbody>{rows}</tbody></table></div>'
        return self.page(request, "Журнал действий", content, active="audit")

    async def rules(self, request: web.Request) -> web.Response:
        forms = await self.db.list_forms()
        rows = ""
        for form in forms:
            rule = await self.advanced.get_rule(int(form["id"]))
            rows += f'<tr><td><a href="/admin/rules/{int(form["id"])}">{_e(form["name"])}</a></td><td>{"✅" if rule.get("enabled") else "⚪"}</td><td>{int(rule.get("min_duration_minutes") or 0)} мин</td><td>{int(rule.get("min_lead_hours") or 0)} ч</td><td>{int(rule.get("max_advance_days") or 0)} дн.</td></tr>'
        content = f'<div class="table-wrap"><table><thead><tr><th>Форма</th><th>Правила</th><th>Минимум</th><th>До события</th><th>Горизонт</th></tr></thead><tbody>{rows}</tbody></table></div>'
        return self.page(request, "Правила бронирования", content, active="rules")

    async def rule_detail(self, request: web.Request) -> web.Response:
        fid = int(request.match_info["fid"])
        form = await self.db.get_form(fid)
        if not form:
            raise web.HTTPNotFound()
        rule = await self.advanced.get_rule(fid)
        blocks = await self.advanced.list_recurring_blocks(fid)
        closed = set(int(x) for x in rule.get("closed_weekdays") or [])
        closed_html = " ".join(f'<label><input style="width:auto" type="checkbox" name="closed_{i}" value="1" {"checked" if i in closed else ""}> {name}</label>' for i,name in enumerate(WEEKDAY_NAMES))
        day_hours = rule.get("day_hours") or {}
        day_lines = "\n".join(f'{i}={v[0]}-{v[1]}' for i,v in sorted(((int(k),v) for k,v in day_hours.items() if isinstance(v,list) and len(v)==2), key=lambda x:x[0]))
        guest_map = rule.get("guest_surcharges") or {}
        guest_options = ["до 50","от 50 до 100","от 100 до 150","от 150 до 250","от 250 до 400","более 400"]
        guest_fields = "".join(f'<div class="field"><label>{_e(opt)}</label><input name="guest_{i}" value="{int(guest_map.get(opt,0) or 0)}"></div>' for i,opt in enumerate(guest_options))
        weekday_map = rule.get("weekday_surcharges") or {}
        weekday_fields = "".join(f'<div class="field"><label>{name}, %</label><input name="weekday_{i}" value="{int(weekday_map.get(str(i),0) or 0)}"></div>' for i,name in enumerate(WEEKDAY_NAMES))
        block_rows = "".join(f'<tr><td>{WEEKDAY_NAMES[int(b.get("weekday") or 0)]}</td><td>{_e((b.get("start_time") or "весь день") + ("–" + str(b.get("end_time")) if b.get("end_time") else ""))}</td><td>{_e(b.get("note") or "")}</td><td><form method="post" action="/admin/rules/{fid}/recurring/{int(b["id"])}/delete"><button class="btn danger">Удалить</button></form></td></tr>' for b in blocks) or '<tr><td colspan="4" class="muted">Нет регулярных блокировок</td></tr>'
        options = ''.join(f'<option value="{i}">{n}</option>' for i,n in enumerate(WEEKDAY_NAMES))
        content = f"""<div class="card"><form method="post" action="/admin/rules/{fid}"><div class="field"><label><input style="width:auto" type="checkbox" name="enabled" value="1" {"checked" if rule.get("enabled") else ""}> Включить правила и динамические надбавки</label></div><div class="row3"><div class="field"><label>Минимум аренды, мин</label><input name="min_duration_minutes" value="{int(rule.get('min_duration_minutes') or 0)}"></div><div class="field"><label>Минимум до события, ч</label><input name="min_lead_hours" value="{int(rule.get('min_lead_hours') or 0)}"></div><div class="field"><label>Бронировать вперёд, дней</label><input name="max_advance_days" value="{int(rule.get('max_advance_days') or 365)}"></div></div><div class="field"><label>Закрытые дни</label><div class="actions">{closed_html}</div></div><div class="field"><label>Рабочее время по дням (0=Пн … 6=Вс)</label><textarea name="day_hours" placeholder="0=10:00-23:59\n5=12:00-24:00">{_e(day_lines)}</textarea></div><h3>Надбавка по количеству гостей</h3><div class="grid">{guest_fields}</div><h3>Надбавка по дню недели, %</h3><div class="grid">{weekday_fields}</div><div class="row3"><div class="field"><label>Ночная зона с</label><input name="night_start" value="{_e(rule.get('night_start') or '23:00')}"></div><div class="field"><label>Ночная доплата, фикс.</label><input name="night_surcharge_amount" value="{int(rule.get('night_surcharge_amount') or 0)}"></div><div class="field"><label>Ночная доплата, %</label><input name="night_surcharge_percent" value="{int(rule.get('night_surcharge_percent') or 0)}"></div></div><button class="btn primary">Сохранить правила</button></form></div>
<div class="section"><h2>Повторяющаяся занятость</h2><div class="table-wrap"><table><thead><tr><th>День</th><th>Время</th><th>Причина</th><th></th></tr></thead><tbody>{block_rows}</tbody></table></div><form method="post" action="/admin/rules/{fid}/recurring" class="card section"><div class="row3"><div class="field"><label>День недели</label><select name="weekday">{options}</select></div><div class="field"><label>Начало (пусто = весь день)</label><input name="start_time" placeholder="18:00"></div><div class="field"><label>Окончание</label><input name="end_time" placeholder="22:00"></div></div><div class="field"><label>Причина</label><input name="note" value="Регулярная занятость"></div><button class="btn">Добавить</button></form></div>"""
        return self.page(request, f"Правила: {form['name']}", content, active="rules")

    async def rule_post(self, request: web.Request) -> web.StreamResponse:
        import json as _jsonlib
        fid = int(request.match_info["fid"])
        data = request["post"]
        def iv(name: str, maximum: int = 1000000) -> int:
            v = _parse_int(str(data.get(name) or "0"), minimum=0, maximum=maximum)
            if v is None:
                raise ValueError(name)
            return v
        try:
            closed = [i for i in range(7) if data.get(f"closed_{i}")]
            day_hours = {}
            for line in str(data.get("day_hours") or "").splitlines():
                line=line.strip()
                if not line:
                    continue
                k, rng = line.split("=",1); start,end = rng.split("-",1)
                idx=int(k.strip())
                if idx not in range(7) or not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d|24:00", start.strip()) or not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d|24:00", end.strip()):
                    raise ValueError("day_hours")
                day_hours[str(idx)] = [start.strip(), end.strip()]
            guest_options = ["до 50","от 50 до 100","от 100 до 150","от 150 до 250","от 250 до 400","более 400"]
            guest_map = {}
            for i,opt in enumerate(guest_options):
                value=iv(f"guest_{i}",10**9)
                if value:
                    guest_map[opt]=value
            weekday_map = {}
            for i in range(7):
                value=iv(f"weekday_{i}",500)
                if value:
                    weekday_map[str(i)]=value
            night_start = str(data.get("night_start") or "23:00").strip()
            if not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", night_start):
                raise ValueError("night_start")
            values = {
                "enabled": 1 if data.get("enabled") else 0,
                "min_duration_minutes": iv("min_duration_minutes", 7*24*60),
                "min_lead_hours": iv("min_lead_hours", 24*365),
                "max_advance_days": iv("max_advance_days", 3650),
                "closed_weekdays_json": _jsonlib.dumps(closed, ensure_ascii=False),
                "day_hours_json": _jsonlib.dumps(day_hours, ensure_ascii=False),
                "guest_surcharges_json": _jsonlib.dumps(guest_map, ensure_ascii=False),
                "weekday_surcharges_json": _jsonlib.dumps(weekday_map, ensure_ascii=False),
                "night_start": night_start,
                "night_surcharge_amount": iv("night_surcharge_amount", 10**9),
                "night_surcharge_percent": iv("night_surcharge_percent", 500),
            }
        except Exception:
            raise web.HTTPFound(f"/admin/rules/{fid}?err=" + quote("Проверьте числовые значения и формат рабочего времени"))
        await self.advanced.update_rule(fid, values)
        raise web.HTTPFound(f"/admin/rules/{fid}?ok=" + quote("Правила сохранены"))

    async def recurring_add(self, request: web.Request) -> web.StreamResponse:
        fid = int(request.match_info["fid"]); data=request["post"]
        weekday = _parse_int(str(data.get("weekday") or ""), minimum=0, maximum=6)
        start = str(data.get("start_time") or "").strip() or None
        end = str(data.get("end_time") or "").strip() or None
        if weekday is None or bool(start) != bool(end):
            raise web.HTTPFound(f"/admin/rules/{fid}?err=" + quote("Укажите оба времени или оставьте оба пустыми"))
        if start and (not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", start) or not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d|24:00", end or "")):
            raise web.HTTPFound(f"/admin/rules/{fid}?err=" + quote("Время должно быть ЧЧ:ММ"))
        await self.advanced.add_recurring_block(form_id=fid, weekday=weekday, start_time=start, end_time=end, note=str(data.get("note") or "Регулярная занятость"))
        raise web.HTTPFound(f"/admin/rules/{fid}?ok=" + quote("Регулярная занятость добавлена"))

    async def recurring_delete(self, request: web.Request) -> web.StreamResponse:
        fid=int(request.match_info["fid"]); bid=int(request.match_info["bid"])
        await self.advanced.delete_recurring_block(bid)
        raise web.HTTPFound(f"/admin/rules/{fid}?ok=" + quote("Блокировка удалена"))

    async def export_csv(self, request: web.Request) -> web.Response:
        import csv, io
        async with self.db.connection() as conn:
            rows = await (await conn.execute("SELECT * FROM form_submissions ORDER BY created_at DESC,id DESC")).fetchall()
        out=io.StringIO(); w=csv.writer(out, delimiter=';')
        w.writerow(["id","created_at","form","status","client","username","chat_id","amount","prepayment","balance","answers","note"])
        for r in rows:
            d=dict(r); total=int(d.get("total_amount") or 0); pre=int(d.get("prepayment_amount") or 0)
            client=" ".join(x for x in [d.get("first_name"),d.get("last_name")] if x)
            w.writerow([d.get("id"),d.get("created_at"),d.get("form_name"),d.get("status"),client,d.get("username"),d.get("chat_id"),total,pre,max(0,total-pre),d.get("answers_json"),d.get("internal_note")])
        body='\ufeff'+out.getvalue()
        return web.Response(text=body, content_type="text/csv", charset="utf-8", headers={"Content-Disposition":"attachment; filename=tgautoreply-export.csv"})

    async def export_xlsx(self, request: web.Request) -> web.Response:
        from io import BytesIO
        from openpyxl import Workbook
        async with self.db.connection() as conn:
            rows = await (await conn.execute("SELECT * FROM form_submissions ORDER BY created_at DESC,id DESC")).fetchall()
        wb=Workbook(); ws=wb.active; ws.title="Заявки"
        headers=["ID","Создана UTC","Форма","Статус","Клиент","Username","Chat ID","Стоимость","Предоплата","Остаток","Ответы JSON","Заметка"]
        ws.append(headers)
        for r in rows:
            d=dict(r); total=int(d.get("total_amount") or 0); pre=int(d.get("prepayment_amount") or 0)
            client=" ".join(x for x in [d.get("first_name"),d.get("last_name")] if x)
            ws.append([d.get("id"),d.get("created_at"),d.get("form_name"),d.get("status"),client,d.get("username"),d.get("chat_id"),total,pre,max(0,total-pre),d.get("answers_json"),d.get("internal_note")])
        ws.freeze_panes="A2"
        for col in ws.columns:
            ws.column_dimensions[col[0].column_letter].width=min(50,max(12,max(len(str(c.value or "")) for c in col)+2))
        stream=BytesIO(); wb.save(stream)
        return web.Response(body=stream.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition":"attachment; filename=tgautoreply-export.xlsx"})

    async def client_requests_view(self, request: web.Request) -> web.Response:
        items=await self.advanced.list_client_requests(limit=200)
        labels={"change":"✏️ Изменение брони","addons":"🧰 Изменение услуг","cancel":"❌ Отмена"}
        rows=""
        for x in items:
            rid=int(x["id"]); current=str(x.get("status") or "new")
            opts="".join(f'<option value="{v}" {"selected" if current==v else ""}>{label}</option>' for v,label in [("new","Новый"),("in_progress","В работе"),("done","Готово"),("rejected","Отклонён")])
            rows += f'<tr><td>#{rid}</td><td><a href="/admin/requests/{int(x["submission_id"])}">заявка #{int(x["submission_id"])}</a></td><td>{_e(labels.get(str(x.get("request_type")),x.get("request_type")))}</td><td>{_e(x.get("message") or "")}</td><td><form method="post" action="/admin/client-requests/{rid}"><select name="status">{opts}</select><button class="btn">OK</button></form></td><td>{_e(_local_dt(x.get("created_at"),self.timezone))}</td></tr>'
        rows=rows or '<tr><td colspan="6" class="muted">Запросов нет</td></tr>'
        return self.page(request,"Запросы клиентов",f'<div class="table-wrap"><table><thead><tr><th>№</th><th>Заявка</th><th>Тип</th><th>Сообщение</th><th>Статус</th><th>Создан</th></tr></thead><tbody>{rows}</tbody></table></div>',active="client_requests")

    async def client_request_status_post(self, request: web.Request) -> web.StreamResponse:
        rid=int(request.match_info["rid"]); status=str(request["post"].get("status") or "")
        try:
            await self.advanced.update_client_request_status(rid,status)
        except ValueError:
            raise web.HTTPFound("/admin/client-requests?err="+quote("Некорректный статус"))
        raise web.HTTPFound("/admin/client-requests?ok="+quote("Статус запроса обновлён"))

    async def integrations_get(self, request: web.Request) -> web.Response:
        enabled=(await self.db.get_setting("calendar_feed_enabled","0"))=="1"
        token=str(await self.db.get_setting("calendar_feed_token","") or "")
        payment=str(await self.db.get_setting("payment_link_template","") or "")
        message=str(await self.db.get_setting("payment_message_template","") or "")
        percent=str(await self.db.get_setting("payment_default_percent","30") or "30")
        feed_url=f"{request.scheme}://{request.host}/calendar.ics?token={quote(token)}"
        content=f"""<div class="card"><h2>Google Calendar / iCalendar</h2><p class="muted">Односторонняя синхронизация: Google Calendar может подписаться на приватный ICS URL. Для внешнего Google Calendar адрес Web Admin должен быть доступен из интернета по HTTPS.</p><form method="post" action="/admin/integrations"><input type="hidden" name="section" value="calendar"><label><input style="width:auto" type="checkbox" name="calendar_feed_enabled" value="1" {"checked" if enabled else ""}> Включить приватный календарный feed</label><div class="field"><label>URL подписки</label><input readonly value="{_e(feed_url)}"></div><button class="btn primary">Сохранить</button></form></div>
<div class="card section"><h2>Ссылка на предоплату</h2><p class="muted">Шаблон URL может использовать <code>{{id}}</code>, <code>{{amount}}</code>, <code>{{prepayment}}</code>, <code>{{balance}}</code>.</p><form method="post" action="/admin/integrations"><input type="hidden" name="section" value="payment"><div class="field"><label>Шаблон URL</label><input name="payment_link_template" value="{_e(payment)}" placeholder="https://pay.example/order/{{id}}?amount={{prepayment}}"></div><div class="field"><label>Предоплата по умолчанию, %</label><input name="payment_default_percent" value="{_e(percent)}"></div><div class="field"><label>Текст сообщения клиенту</label><textarea name="payment_message_template">{_e(message)}</textarea></div><button class="btn primary">Сохранить</button></form></div>"""
        return self.page(request,"Интеграции",content,active="integrations")

    async def integrations_post(self, request: web.Request) -> web.StreamResponse:
        data=request["post"]; section=str(data.get("section") or "")
        if section=="calendar":
            await self.db.set_setting("calendar_feed_enabled","1" if data.get("calendar_feed_enabled") else "0")
        elif section=="payment":
            percent=_parse_int(str(data.get("payment_default_percent") or ""),minimum=1,maximum=100)
            if percent is None:
                raise web.HTTPFound("/admin/integrations?err="+quote("Процент должен быть 1–100"))
            await self.db.set_setting("payment_link_template",str(data.get("payment_link_template") or "").strip()[:2000])
            await self.db.set_setting("payment_default_percent",str(percent))
            await self.db.set_setting("payment_message_template",str(data.get("payment_message_template") or "")[:4000])
        raise web.HTTPFound("/admin/integrations?ok="+quote("Настройки сохранены"))

    async def calendar_ics(self, request: web.Request) -> web.Response:
        if (await self.db.get_setting("calendar_feed_enabled","0"))!="1":
            raise web.HTTPNotFound()
        token=str(await self.db.get_setting("calendar_feed_token","") or "")
        if not token or not hmac.compare_digest(str(request.query.get("token") or ""),token):
            raise web.HTTPForbidden(text="Invalid calendar token")
        items=await self.db.list_booking_submissions(limit=5000,statuses=("confirmed","paid","completed"))
        def esc(v: str)->str:
            return str(v).replace('\\','\\\\').replace(';','\\;').replace(',','\\,').replace('\n','\\n')
        lines=["BEGIN:VCALENDAR","VERSION:2.0","PRODID:-//TG AutoReply//Business Calendar//RU","CALSCALE:GREGORIAN","METHOD:PUBLISH"]
        for item in items:
            if not item.get("form_id"):
                continue
            qs=await self.db.list_form_questions(int(item["form_id"])); window=self._booking_window(item,qs)
            if not window:
                continue
            start,end=window
            uid=f"tgautoreply-{int(item['id'])}@local"
            client=" ".join(x for x in [item.get("first_name"),item.get("last_name")] if x) or ("@"+str(item.get("username")) if item.get("username") else "клиент")
            lines += ["BEGIN:VEVENT",f"UID:{uid}",f"DTSTAMP:{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}",f"DTSTART;TZID={self.timezone.key}:{start.strftime('%Y%m%dT%H%M%S')}",f"DTEND;TZID={self.timezone.key}:{end.strftime('%Y%m%dT%H%M%S')}",f"SUMMARY:{esc('#'+str(item['id'])+' '+str(item.get('form_name') or 'Бронь'))}",f"DESCRIPTION:{esc('Клиент: '+client+' | статус: '+str(item.get('status') or ''))}","END:VEVENT"]
        lines.append("END:VCALENDAR")
        return web.Response(text="\r\n".join(lines)+"\r\n",content_type="text/calendar",charset="utf-8",headers={"Content-Disposition":"inline; filename=bookings.ics"})

    async def payment_send(self, request: web.Request) -> web.StreamResponse:
        sid=int(request.match_info["sid"]); item=await self.db.get_submission(sid)
        if not item:
            raise web.HTTPNotFound()
        template=str(await self.db.get_setting("payment_link_template","") or "").strip()
        if not template or not self.on_payment_link:
            raise web.HTTPFound(f"/admin/requests/{sid}?err="+quote("Сначала настройте ссылку на оплату в Интеграциях"))
        total=max(0,int(item.get("total_amount") or 0)); pre=max(0,int(item.get("prepayment_amount") or 0))
        if not pre:
            percent=int(await self.db.get_setting("payment_default_percent","30") or 30); pre=(total*percent+99)//100 if total else 0
        balance=max(0,total-pre)
        values={"id":str(sid),"amount":str(total),"prepayment":str(pre),"balance":str(balance)}
        url=template
        for k,v in values.items():
            url=url.replace("{"+k+"}",quote(v,safe=""))
        msg=str(await self.db.get_setting("payment_message_template","") or "{url}")
        currency=str(await self.db.get_setting("crm_currency","₽") or "₽")
        display={"id":str(sid),"amount":_money(total,currency),"prepayment":_money(pre,currency),"balance":_money(balance,currency),"url":url}
        for k,v in display.items():
            msg=msg.replace("{"+k+"}",str(v))
        ok,info=await self.on_payment_link(sid,msg[:4000]); key="ok" if ok else "err"
        raise web.HTTPFound(f"/admin/requests/{sid}?{key}="+quote(info))


    def application(self) -> web.Application:
        app = web.Application(middlewares=[self.auth_middleware])
        app.router.add_get("/health", self.health)
        app.router.add_get("/calendar.ics", self.calendar_ics)
        app.router.add_get("/", lambda r: (_ for _ in ()).throw(web.HTTPFound("/admin")))
        app.router.add_get("/admin/login", self.login_get)
        app.router.add_post("/admin/login", self.login_post)
        app.router.add_get("/admin/logout", self.logout)
        app.router.add_get("/admin", self.dashboard)
        app.router.add_get("/admin/requests", self.requests)
        app.router.add_get("/admin/files/{name}", self.file_download)
        app.router.add_get("/admin/clients", self.clients)
        app.router.add_get("/admin/clients/{client_key}", self.client_detail)
        app.router.add_post("/admin/clients/{client_key}", self.client_note_post)
        app.router.add_get("/admin/client-requests", self.client_requests_view)
        app.router.add_post(r"/admin/client-requests/{rid:\d+}", self.client_request_status_post)
        app.router.add_get("/admin/analytics", self.analytics_view)
        app.router.add_get("/admin/audit", self.audit_view)
        app.router.add_get("/admin/export.csv", self.export_csv)
        app.router.add_get("/admin/export.xlsx", self.export_xlsx)
        app.router.add_get("/admin/requests/{sid:\\d+}", self.request_detail)
        app.router.add_post("/admin/requests/{sid:\\d+}/status", self.request_status_post)
        app.router.add_post("/admin/requests/{sid:\\d+}/crm", self.request_crm_post)
        app.router.add_get("/admin/settings", self.settings_get)
        app.router.add_post("/admin/settings", self.settings_post)
        app.router.add_get("/admin/pricing", self.pricing)
        app.router.add_get("/admin/rules", self.rules)
        app.router.add_get(r"/admin/rules/{fid:\d+}", self.rule_detail)
        app.router.add_post(r"/admin/rules/{fid:\d+}", self.rule_post)
        app.router.add_post(r"/admin/rules/{fid:\d+}/recurring", self.recurring_add)
        app.router.add_post(r"/admin/rules/{fid:\d+}/recurring/{bid:\d+}/delete", self.recurring_delete)
        app.router.add_get("/admin/integrations", self.integrations_get)
        app.router.add_post("/admin/integrations", self.integrations_post)
        app.router.add_get("/admin/pricing/{fid:\\d+}", self.pricing_detail)
        app.router.add_post("/admin/pricing/{fid:\\d+}", self.pricing_post)
        app.router.add_post("/admin/pricing/{fid:\\d+}/addon", self.addon_create)
        app.router.add_post("/admin/pricing/{fid:\\d+}/addon/{aid:\\d+}", self.addon_update)
        app.router.add_post("/admin/pricing/{fid:\\d+}/addon/{aid:\\d+}/delete", self.addon_delete)
        app.router.add_get("/admin/calendar", self.calendar_view)
        app.router.add_get("/admin/reminders", self.reminders_get)
        app.router.add_post("/admin/reminders", self.reminders_post)
        app.router.add_post("/admin/reminders/run", self.reminders_run)
        app.router.add_get("/admin/backups", self.backups_get)
        app.router.add_post("/admin/backups/settings", self.backups_settings)
        app.router.add_post("/admin/backups/create", self.backups_create)
        app.router.add_get("/admin/backups/{name}/download", self.backups_download)
        app.router.add_post("/admin/backups/{name}/restore", self.backups_restore)
        app.router.add_post("/admin/backups/{name}/delete", self.backups_delete)
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
    backup_manager: BackupManager,
    reminder_service: ReminderService,
    advanced: AdvancedService,
    users_config: str = "",
    on_payment_link: PaymentLinkCallback | None = None,
) -> WebAdminHandle:
    admin = WebAdmin(
        db=db,
        timezone=timezone,
        username=username,
        password=password,
        on_status_change=on_status_change,
        on_amount_change=on_amount_change,
        concurrency_snapshot=concurrency_snapshot,
        backup_manager=backup_manager,
        reminder_service=reminder_service,
        advanced=advanced,
        users_config=users_config,
        on_payment_link=on_payment_link,
    )
    runner = web.AppRunner(admin.application(), access_log=logger)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    logger.info("Web admin listening on 0.0.0.0:%s (enabled=%s)", port, admin.enabled)
    return WebAdminHandle(runner)
