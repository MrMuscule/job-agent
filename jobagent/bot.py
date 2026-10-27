"""Telegram-бот. Отвечает только владельцу (TELEGRAM_CHAT_ID).

Данные читаются из SQLite (мгновенно); сбор вакансий идёт в фоне через JobQueue.
"""
from __future__ import annotations

import asyncio
import functools
import html
import json
import logging
from datetime import datetime, timedelta, timezone

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes)

from .config import env, load_profile, owner_chat_id
from .pipeline import CollectResult, collect
from .store import HIDDEN_STATUSES, VALID_STATUSES, Store

log = logging.getLogger("jobagent.bot")

PAGE_SIZE = 5
MAX_PUSH_PER_RUN = 15
HTML = ParseMode.HTML

STATUS_CODES = {"SL": "SHORTLIST", "AP": "APPLIED", "SK": "SKIP"}
CODE_OF = {v: k for k, v in STATUS_CODES.items()}
COLLECT_LOCK = asyncio.Lock()


# ── утилиты ─────────────────────────────────────────────────────────────

def esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


def owner_only(fn):
    """Чужим не отвечаем вообще."""
    @functools.wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        if chat is None or chat.id != context.application.bot_data["owner"]:
            log.warning("ignored update from chat %s", chat.id if chat else None)
            return
        return await fn(update, context)
    return wrapper


def short_location(row) -> str:
    cities = [c.strip() for c in (row["location"] or "").split(",") if c.strip()]
    loc = ", ".join(cities[:3]) + (f" +{len(cities) - 3}" if len(cities) > 3 else "")
    wp = {"remote": "remote", "hybrid": "hybrid", "office": "office"}.get(row["workplace"] or "")
    return " · ".join(x for x in (loc, wp) if x) or "—"


def reasons_of(row, n: int = 3) -> list[str]:
    pairs = json.loads(row["reasons_json"] or "[]")
    pairs = [p for p in pairs if p[0] != 0] or pairs
    pairs.sort(key=lambda p: abs(p[0]), reverse=True)
    return [f"{p:+d} {t}" if p else t for p, t in pairs[:n]]


def card(row, idx: int | None = None, total: int | None = None) -> str:
    head = f"{idx}/{total} · " if idx else ""
    status = "" if row["status"] in ("NEW", "SEEN") else f" · <b>{row['status']}</b>"
    lines = [
        f"<b>{head}{esc(row['title'])}</b>{status}",
        f"🏢 {esc(row['company'] or '—')}",
        f"📍 {esc(short_location(row))}",
        f"💰 {esc(row['salary_label']) or 'вилка не указана'}",
        f"🎯 <b>score {row['score']}</b>",
    ]
    lines += [f"   • {esc(r)}" for r in reasons_of(row)]
    return "\n".join(lines)


def keyboard(row) -> InlineKeyboardMarkup:
    cur = row["status"]

    def btn(label: str, status: str) -> InlineKeyboardButton:
        mark = "✓ " if cur == status else ""
        return InlineKeyboardButton(mark + label, callback_data=f"m:{CODE_OF[status]}:{row['rid']}")

    rows = [[btn("⭐ Shortlist", "SHORTLIST"), btn("✅ Applied", "APPLIED"), btn("❌ Skip", "SKIP")]]
    if (row["url"] or "").startswith("http"):
        rows.append([InlineKeyboardButton("🔗 Open", url=row["url"])])
    return InlineKeyboardMarkup(rows)


async def send_cards(bot, chat_id: int, rows, numbered: bool = True) -> None:
    for i, r in enumerate(rows, 1):
        await bot.send_message(chat_id, card(r, i if numbered else None, len(rows)),
                               parse_mode=HTML, reply_markup=keyboard(r),
                               disable_web_page_preview=True)


def compact_line(i: int, r) -> str:
    sal = f" · 💰 {esc(r['salary_label'])}" if r["salary_label"] else ""
    return (f"<b>{i}.</b> [{r['score']}] <a href=\"{html.escape(r['url'] or '', quote=True)}\">"
            f"{esc(r['title'][:70])}</a>\n"
            f"    <i>{esc((r['company'] or '')[:35])}</i> · {esc(short_location(r))}{sal}")


async def send_long(message, text: str) -> None:
    for c in range(0, len(text), 3800):
        await message.reply_text(text[c:c + 3800], parse_mode=HTML,
                                 disable_web_page_preview=True)


# ── сбор в фоне ─────────────────────────────────────────────────────────

def summary(res: CollectResult) -> str:
    return (f"Adzuna {res.raw_adzuna} · JustJoin {res.raw_justjoin} · "
            f"подошло {res.kept} (новых {res.new}) · отсеяно {res.excluded}")


async def run_collect(application: Application, notify: bool = True) -> CollectResult | None:
    """Собрать вакансии и прислать новые с высоким score. None — если сбор уже идёт."""
    if COLLECT_LOCK.locked():
        return None
    async with COLLECT_LOCK:
        profile = load_profile()
        cfg = profile.get("bot") or {}
        owner = application.bot_data["owner"]
        store = Store()
        try:
            res = await collect(profile, store, log=log.warning)
            if store.get_meta("push_baseline") is None:
                # первый запуск бота: старую базу не рассылаем
                store.baseline_notified()
                store.set_meta("push_baseline", "1")
                return res
            if not notify:
                return res
            fresh = store.to_notify(int(cfg.get("push_min_score", 25)))
            batch = fresh[:MAX_PUSH_PER_RUN]
            if batch:
                await application.bot.send_message(
                    owner, f"🆕 Новых вакансий (score ≥ {cfg.get('push_min_score', 25)}): "
                           f"<b>{len(fresh)}</b>", parse_mode=HTML)
                await send_cards(application.bot, owner, batch, numbered=False)
                if len(fresh) > len(batch):
                    await application.bot.send_message(
                        owner, f"…и ещё {len(fresh) - len(batch)} — смотри /new")
                store.mark_notified([r["id"] for r in batch])
            return res
        finally:
            store.close()


async def scheduled_collect(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await run_collect(context.application)
    except Exception:
        log.exception("scheduled collect failed")


# ── команды ─────────────────────────────────────────────────────────────

HELP = (
    "👋 <b>Job-Agent</b>\n\n"
    "/top [N] — лучшие N (по умолчанию 10)\n"
    "/all — все вакансии постранично\n"
    "/new — новые, ещё не просмотренные\n"
    "/refresh — собрать вакансии сейчас\n"
    "/stats — статистика и цель X/10\n"
    "/applied — отклики и follow-up\n"
    "/list STATUS — вакансии со статусом\n"
    "/search слово — поиск по базе\n"
    "/mark STATUS URL — задать статус вручную\n"
    "/cv — краткий профиль"
)


@owner_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP, parse_mode=HTML)


@owner_only
async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    n = 10
    if context.args:
        try:
            n = max(1, min(30, int(context.args[0])))
        except ValueError:
            pass
    store = Store()
    try:
        rows, total = store.ranked(n), store.count_ranked()
    finally:
        store.close()
    if not rows:
        await update.message.reply_text("База пуста. Запусти /refresh.")
        return
    await update.message.reply_text(f"📊 Top {len(rows)} из {total}")
    await send_cards(context.bot, update.effective_chat.id, rows)


async def render_page(page: int) -> tuple[str, InlineKeyboardMarkup | None]:
    store = Store()
    try:
        total = store.count_ranked()
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        rows = store.ranked(PAGE_SIZE, page * PAGE_SIZE)
    finally:
        store.close()
    start = page * PAGE_SIZE
    text = f"<b>Стр. {page + 1}/{pages}</b> · всего {total}\n\n" + "\n".join(
        compact_line(start + i, r) for i, r in enumerate(rows, 1))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀", callback_data=f"pg:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("▶", callback_data=f"pg:{page + 1}"))
    return text, InlineKeyboardMarkup([nav]) if pages > 1 else None


@owner_only
async def cmd_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = await render_page(0)
    await update.message.reply_text(text, parse_mode=HTML, reply_markup=kb,
                                    disable_web_page_preview=True)


@owner_only
async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store = Store()
    try:
        rows = store.new_jobs(30)
    finally:
        store.close()
    if not rows:
        await update.message.reply_text("Новых нет.")
        return
    await send_long(update.message, f"<b>🆕 Новые ({len(rows)}):</b>\n\n" +
                    "\n".join(compact_line(i, r) for i, r in enumerate(rows, 1)))


@owner_only
async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Использование: /search terraform")
        return
    q = " ".join(context.args).strip()
    store = Store()
    try:
        rows = store.search(q, 20)
    finally:
        store.close()
    if not rows:
        await update.message.reply_text(f"Ничего по «{q}».")
        return
    await send_long(update.message, f"<b>Поиск «{esc(q)}» ({len(rows)}):</b>\n\n" +
                    "\n".join(compact_line(i, r) for i, r in enumerate(rows, 1)))


@owner_only
async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await update.message.reply_text("⏳ Собираю вакансии…")
    try:
        res = await run_collect(context.application)
    except Exception as e:
        log.exception("refresh failed")
        await msg.edit_text(f"❌ {type(e).__name__}: {e}")
        return
    if res is None:
        await msg.edit_text("Сбор уже идёт, подожди.")
        return
    await msg.edit_text("✅ " + summary(res) + "\n/top — посмотреть лучшие")


def progress_bar(done: int, goal: int, width: int = 10) -> str:
    filled = min(width, round(width * done / goal)) if goal else 0
    return "▰" * filled + "▱" * (width - filled)


@owner_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    goal = int((load_profile().get("bot") or {}).get("weekly_applied_goal", 10))
    store = Store()
    try:
        s = store.stats()
    finally:
        store.close()
    by = s["by_status"]
    active = sum(v for k, v in by.items() if k not in HIDDEN_STATUSES)
    last = (s["last_collect"] or "—").replace("T", " ")[:16]
    lines = [
        f"📚 Найдено в базе: <b>{s['total']}</b> (активных: {active})",
        f"⭐ В шортлисте: <b>{by.get('SHORTLIST', 0)}</b>",
        f"✅ Откликов всего: <b>{by.get('APPLIED', 0) + by.get('INTERVIEW', 0) + by.get('OFFER', 0)}</b>",
        f"🎯 Откликов за 7 дней: <b>{s['applied_week']}/{goal}</b>  {progress_bar(s['applied_week'], goal)}",
        f"❌ Пропущено: {by.get('SKIP', 0)} · 🔒 Закрыто: {by.get('CLOSED', 0)}",
        f"🕒 Последний сбор (UTC): {last}",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode=HTML)


@owner_only
async def cmd_applied(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    follow_days = int((load_profile().get("bot") or {}).get("followup_days", 7))
    store = Store()
    try:
        rows = store.applied()
    finally:
        store.close()
    if not rows:
        await update.message.reply_text("Откликов пока нет.")
        return
    now = datetime.now(timezone.utc)
    out = [f"<b>✅ Отклики ({len(rows)}):</b>", ""]
    for i, r in enumerate(rows, 1):
        d = datetime.fromisoformat(r["applied_at"]) if r["applied_at"] else None
        when, flag = "—", ""
        if d:
            when = d.astimezone().strftime("%d.%m")
            if r["status"] == "APPLIED" and now - d > timedelta(days=follow_days):
                flag = f" ⏰ <b>напомнить / follow-up</b> ({(now - d).days} дн.)"
        extra = "" if r["status"] == "APPLIED" else f" · {r['status']}"
        out.append(f"{i}. {when} · <a href=\"{html.escape(r['url'] or '', quote=True)}\">"
                   f"{esc(r['title'][:60])}</a> — <i>{esc(r['company'] or '')}</i>{extra}{flag}")
    await send_long(update.message, "\n".join(out))


@owner_only
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or context.args[0].upper() not in VALID_STATUSES:
        await update.message.reply_text("Использование: /list STATUS\nЕсть: " + ", ".join(VALID_STATUSES))
        return
    status = context.args[0].upper()
    store = Store()
    try:
        rows = store.list_by_status(status, 30)
    finally:
        store.close()
    if not rows:
        await update.message.reply_text(f"Пусто для {status}.")
        return
    await send_long(update.message, f"<b>{status} ({len(rows)}):</b>\n\n" +
                    "\n".join(compact_line(i, r) for i, r in enumerate(rows, 1)))


@owner_only
async def cmd_mark(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2 or context.args[0].upper() not in VALID_STATUSES:
        await update.message.reply_text("Использование: /mark STATUS URL\nЕсть: " + ", ".join(VALID_STATUSES))
        return
    status = context.args[0].upper()
    store = Store()
    try:
        row = store.find_by_url(context.args[1])
        if not row:
            await update.message.reply_text(f"❌ Не найдено: {context.args[1]}")
            return
        store.set_status(row["id"], status)
    finally:
        store.close()
    await update.message.reply_text(f"✓ {row['title'][:80]} → {status}")


@owner_only
async def cmd_cv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    p = load_profile()
    c = p["candidate"]
    roles = []
    for r in sorted(p["target_roles"], key=lambda r: -r["weight"])[:3]:
        roles.append(f"{r['patterns'][0]} ({r['weight']})")
    strong = [s.strip() for s in p["skills"]["strong"]][:18]
    sal = p["salary"]
    text = (
        f"👤 <b>{esc(c['name'])}</b>\n"
        f"{esc(c['current_title'])}\n"
        f"Опыт: {c['experience_years']} года · языки: {esc(', '.join(c['languages']))}\n\n"
        f"🎯 <b>Целевые роли:</b> {esc('; '.join(roles))}\n"
        f"🛠 <b>Топ-скиллы:</b> {esc(', '.join(strong))}\n"
        f"📚 <b>Изучаю:</b> {esc(', '.join(p['skills']['learning'][:7]))}\n"
        f"💰 <b>Минимум:</b> {sal['min_uop_gross']} UoP брутто / {sal['min_b2b_net']} B2B нетто\n"
        f"📍 {esc(p['location']['home'].title())} или remote"
    )
    await update.message.reply_text(text, parse_mode=HTML)


# ── кнопки ──────────────────────────────────────────────────────────────

@owner_only
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    data = q.data or ""

    if data.startswith("m:"):
        try:
            _, code, rid_s = data.split(":")
            status, rid = STATUS_CODES[code], int(rid_s)
        except (ValueError, KeyError):
            await q.answer("bad data")
            return
        store = Store()
        try:
            row = store.get_by_rid(rid)
            if not row:
                await q.answer("вакансия не найдена")
                return
            store.set_status(row["id"], status)
            row = store.get_by_rid(rid)
        finally:
            store.close()
        await q.answer(f"✓ {status}")
        try:
            await q.edit_message_reply_markup(reply_markup=keyboard(row))
        except Exception:
            pass
        return

    if data.startswith("pg:"):
        try:
            page = int(data.split(":")[1])
        except ValueError:
            await q.answer()
            return
        text, kb = await render_page(page)
        await q.answer()
        try:
            await q.edit_message_text(text, parse_mode=HTML, reply_markup=kb,
                                      disable_web_page_preview=True)
        except Exception:
            pass
        return

    await q.answer()


async def on_error(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("handler error", exc_info=context.error)


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("top", "лучшие вакансии"), BotCommand("all", "все постранично"),
        BotCommand("new", "новые"), BotCommand("refresh", "собрать сейчас"),
        BotCommand("stats", "статистика, цель X/10"), BotCommand("applied", "отклики"),
        BotCommand("list", "по статусу"), BotCommand("search", "поиск"),
        BotCommand("cv", "мой профиль"), BotCommand("help", "помощь"),
    ])


def run_bot() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    token = env("TELEGRAM_BOT_TOKEN")
    owner = owner_chat_id()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан в .env")
    if owner is None:
        raise SystemExit("TELEGRAM_CHAT_ID не задан в .env: бот отвечает только владельцу")

    every_h = float((load_profile().get("bot") or {}).get("collect_every_hours", 3))
    app = Application.builder().token(token).post_init(post_init).build()
    app.bot_data["owner"] = owner

    for name, fn in (("start", cmd_start), ("help", cmd_start), ("top", cmd_top),
                     ("all", cmd_all), ("new", cmd_new), ("search", cmd_search),
                     ("refresh", cmd_refresh), ("stats", cmd_stats),
                     ("applied", cmd_applied), ("list", cmd_list),
                     ("mark", cmd_mark), ("cv", cmd_cv)):
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(on_error)

    # сбор при старте и затем каждые N часов
    app.job_queue.run_repeating(scheduled_collect, interval=timedelta(hours=every_h),
                                first=timedelta(seconds=5), name="collect")

    print(f"🤖 Бот запущен (владелец {owner}, сбор каждые {every_h:g} ч). Ctrl+C — остановить.")
    app.run_polling(allowed_updates=["message", "callback_query"])
