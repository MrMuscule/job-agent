"""
Telegram-бот для job-agent.

Запуск:
  python bot.py              — запустить бота (long-polling). Ctrl+C — стоп.
  python bot.py --once       — прогнать pipeline и запушить Top-N в TELEGRAM_CHAT_ID.
  python bot.py --once --top 20

Команды бота:
  /start            — приветствие + покажет chat_id
  /top [N]          — запустить поиск, прислать Top-N (default 10) с кнопками
  /new              — что нового с прошлого запуска
  /stats            — статистика БД
  /list STATUS      — вакансии с нужным статусом
  /mark STATUS URL  — вручную поставить статус

Кнопки под каждой вакансией:
  ⭐ В шортлист  — status=SHORTLIST
  📨 Отклик      — status=APPLIED
  ❌ Скип        — status=REJECTED
  🔗 Открыть     — открыть вакансию в браузере

Токен и chat_id берутся из .env:
  TELEGRAM_BOT_TOKEN=123456:ABC...
  TELEGRAM_CHAT_ID=123456789
"""
from __future__ import annotations

import argparse
import asyncio
import html
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from app import PROFILE_PATH, run_pipeline_quiet
from db import Store, VALID_STATUSES

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID_ENV = os.getenv("TELEGRAM_CHAT_ID", "").strip()

if not TOKEN:
    raise SystemExit(
        "TELEGRAM_BOT_TOKEN не задан в .env.\n"
        "1. Найди @BotFather в Telegram\n"
        "2. /newbot → получи токен\n"
        "3. Добавь в .env: TELEGRAM_BOT_TOKEN=123456:ABC-DEF..."
    )

STATUS_SHORT = {
    "SHORTLIST": "SL",
    "APPLIED": "AP",
    "REJECTED": "RJ",
    "SEEN": "SN",
}
SHORT_TO_STATUS = {v: k for k, v in STATUS_SHORT.items()}


# ────────────────────────────────────────────────────────────────────────
# Форматирование
# ────────────────────────────────────────────────────────────────────────

def esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


def esc_attr(s) -> str:
    return html.escape(str(s or ""), quote=True)


def fmt_salary(job) -> str:
    lo, hi = job.salary_min, job.salary_max
    if lo and hi:
        return f"~{int(lo)}–{int(hi)} PLN/мес"
    if hi:
        return f"~{int(hi)} PLN/мес"
    if lo:
        return f"~{int(lo)} PLN/мес"
    return "зарплата не указана"


def fmt_job(i: int, s) -> str:
    b = s.breakdown
    tag = "🆕 " if s.is_new else ""
    status = "" if s.status == "NEW" else f" · [{s.status}]"
    return (
        f"<b>{i}. [{s.total}]</b> {tag}{esc(s.job.title)}{status}\n"
        f"<i>{esc(s.job.company)}</i> · {esc(s.job.location)}\n"
        f"💰 {esc(fmt_salary(s.job))}\n"
        f"R {b.role} · T {b.tech} · S {b.seniority} · $ {b.salary}"
    )


def kb_for(jid: str, url: str, status: str) -> InlineKeyboardMarkup:
    sl_label = "✅ Шортлист" if status == "SHORTLIST" else "⭐ В шортлист"
    ap_label = "✅ Отклик" if status == "APPLIED" else "📨 Отклик"
    rj_label = "✅ Скип" if status == "REJECTED" else "❌ Скип"
    rows = [[
        InlineKeyboardButton(sl_label, callback_data=f"m:SL:{jid}"),
        InlineKeyboardButton(ap_label, callback_data=f"m:AP:{jid}"),
        InlineKeyboardButton(rj_label, callback_data=f"m:RJ:{jid}"),
    ]]
    if url:
        rows.append([InlineKeyboardButton("🔗 Открыть", url=url)])
    return InlineKeyboardMarkup(rows)


# ────────────────────────────────────────────────────────────────────────
# Handlers
# ────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    text = (
        "👋 <b>Job-Agent</b>\n\n"
        "<b>Команды:</b>\n"
        "/top [N] — искать вакансии, показать Top-N (default 10)\n"
        "/new — что нового с прошлого запуска\n"
        "/stats — статистика БД\n"
        "/list STATUS — вакансии со статусом\n"
        "/mark STATUS URL — вручную поставить статус\n\n"
        f"<b>Твой chat_id:</b> <code>{chat_id}</code>\n\n"
        "Скопируй его в <code>.env</code> как\n"
        "<code>TELEGRAM_CHAT_ID=" + str(chat_id) + "</code>\n"
        "чтобы получать push-уведомления из <code>python bot.py --once</code>."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    n = 10
    if context.args:
        try:
            n = max(1, min(30, int(context.args[0])))
        except ValueError:
            pass

    msg = await update.message.reply_text(f"⏳ Ищу вакансии (Top {n}), подожди ~30 сек…")

    store = Store()
    try:
        profile = yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8"))
        scored = await run_pipeline_quiet(profile, store)
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {type(e).__name__}: {e}")
        store.close()
        return

    top = scored[:n]
    await msg.edit_text(
        f"📊 <b>Найдено:</b> {len(scored)} подходящих вакансий\n"
        f"Показываю Top {len(top)}:",
        parse_mode=ParseMode.HTML,
    )
    store.close()

    for i, s in enumerate(top, 1):
        try:
            await update.message.reply_text(
                fmt_job(i, s),
                parse_mode=ParseMode.HTML,
                reply_markup=kb_for(s.job_id, s.job.url, s.status),
                disable_web_page_preview=True,
            )
        except Exception as e:
            await update.message.reply_text(f"⚠ Ошибка отправки #{i}: {e}")


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store = Store()
    try:
        rows = store.list_new_since_last_run(limit=30)
    finally:
        store.close()

    if not rows:
        await update.message.reply_text("Нет новых вакансий с прошлого запуска.")
        return

    parts = [f"<b>🆕 Новые ({len(rows)}):</b>", ""]
    for i, r in enumerate(rows, 1):
        parts.append(f"{i}. {esc(r['title'])}")
        parts.append(f"   <i>{esc(r['company'])}</i> · {esc(r['location'])}")
        parts.append(f'   <a href="{esc_attr(r["url"])}">открыть</a>')
    text = "\n".join(parts)
    for chunk_start in range(0, len(text), 3500):
        await update.message.reply_text(
            text[chunk_start:chunk_start + 3500],
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store = Store()
    try:
        s = store.stats()
    finally:
        store.close()

    lines = [
        f"<b>Total jobs:</b> {s['total_jobs']}",
        f"<b>Last scored:</b> {esc(s['last_score_at'] or '—')}",
        "",
        "<b>По статусам:</b>",
    ]
    for k, v in sorted(s["by_status"].items()):
        lines.append(f"  <code>{k:10}</code> {v}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Использование: /list STATUS\n"
            f"Статусы: {', '.join(VALID_STATUSES)}"
        )
        return
    status = context.args[0].upper()
    if status not in VALID_STATUSES:
        await update.message.reply_text(f"Неизвестный статус. Есть: {', '.join(VALID_STATUSES)}")
        return

    store = Store()
    try:
        rows = store.list_by_status(status, limit=30)
    finally:
        store.close()

    if not rows:
        await update.message.reply_text(f"Пусто для {status}.")
        return

    parts = [f"<b>{status} ({len(rows)}):</b>", ""]
    for i, r in enumerate(rows, 1):
        parts.append(f"{i}. {esc(r['title'])}")
        parts.append(f"   <i>{esc(r['company'])}</i>")
        parts.append(f'   <a href="{esc_attr(r["url"])}">открыть</a>')
    text = "\n".join(parts)
    for chunk_start in range(0, len(text), 3500):
        await update.message.reply_text(
            text[chunk_start:chunk_start + 3500],
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


async def cmd_mark(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        await update.message.reply_text(
            "Использование: /mark STATUS URL_или_ID\n"
            f"Статусы: {', '.join(VALID_STATUSES)}"
        )
        return
    status = context.args[0].upper()
    target = context.args[1]
    if status not in VALID_STATUSES:
        await update.message.reply_text(f"Неизвестный статус. Есть: {', '.join(VALID_STATUSES)}")
        return

    store = Store()
    try:
        row = store.find_job_by_url(target)
        if not row:
            await update.message.reply_text(f"❌ Не найдено: {target}")
            return
        store.set_status(row["id"], status)
        await update.message.reply_text(
            f"✓ {esc(row['title'][:80])}\n→ <b>{status}</b>",
            parse_mode=ParseMode.HTML,
        )
    finally:
        store.close()


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    data = q.data or ""
    if not data.startswith("m:"):
        await q.answer()
        return

    try:
        _, code, jid = data.split(":", 2)
    except ValueError:
        await q.answer("bad data")
        return

    status = SHORT_TO_STATUS.get(code)
    if not status:
        await q.answer("unknown")
        return

    store = Store()
    try:
        store.set_status(jid, status)
        row = store.conn.execute("SELECT url FROM jobs WHERE id=?", (jid,)).fetchone()
        url = row["url"] if row else ""
    finally:
        store.close()

    await q.answer(f"✓ {status}")

    try:
        await q.edit_message_reply_markup(
            reply_markup=kb_for(jid, url, status),
        )
    except Exception:
        pass


# ────────────────────────────────────────────────────────────────────────
# Push mode (--once)
# ────────────────────────────────────────────────────────────────────────

async def push_once(n: int) -> None:
    if not CHAT_ID_ENV:
        print("TELEGRAM_CHAT_ID не задан в .env — push невозможен.")
        print("Открой бота в TG, отправь /start, скопируй chat_id в .env.")
        return
    try:
        chat_id = int(CHAT_ID_ENV)
    except ValueError:
        print(f"TELEGRAM_CHAT_ID должен быть числом, получено: {CHAT_ID_ENV!r}")
        return

    print("Ищу вакансии…")
    store = Store()
    try:
        profile = yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8"))
        scored = await run_pipeline_quiet(profile, store)
    finally:
        store.close()

    top = scored[:n]
    print(f"Отправляю Top {len(top)} в chat {chat_id}…")

    app = Application.builder().token(TOKEN).build()
    await app.bot.send_message(
        chat_id,
        f"📊 <b>Top {len(top)}</b> из {len(scored)} подходящих",
        parse_mode=ParseMode.HTML,
    )
    for i, s in enumerate(top, 1):
        await app.bot.send_message(
            chat_id,
            fmt_job(i, s),
            parse_mode=ParseMode.HTML,
            reply_markup=kb_for(s.job_id, s.job.url, s.status),
            disable_web_page_preview=True,
        )
    print("Готово.")


# ────────────────────────────────────────────────────────────────────────
# Entry point
# ────────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(prog="bot.py")
    p.add_argument("--once", action="store_true",
                   help="один прогон pipeline + push Top-N, потом выход")
    p.add_argument("--top", type=int, default=10,
                   help="сколько вакансий в push (default 10)")
    args = p.parse_args()

    if args.once:
        asyncio.run(push_once(args.top))
        return

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("mark", cmd_mark))
    app.add_handler(CallbackQueryHandler(on_button))

    print("🤖 Бот запущен. Ctrl+C — остановить.")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()