"""
Telegram-бот для job-agent.
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

from app import run_pipeline_quiet
from db import Store, VALID_STATUSES

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID_ENV = os.getenv("TELEGRAM_CHAT_ID", "").strip()

PROFILE_PATH = Path(__file__).resolve().parent / "profile.yaml"

if not TOKEN:
    raise SystemExit(
        "TELEGRAM_BOT_TOKEN не задан в .env.\n"
        "Найди @BotFather → /newbot → скопируй токен."
    )

PAGE_STATE: dict[int, dict] = {}
PAGE_SIZE = 5


def esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


def esc_attr(s) -> str:
    return html.escape(str(s or ""), quote=True)


def fmt_salary(job) -> str:
    lo, hi = job.salary_min, job.salary_max
    if lo and hi:
        return f"~{int(lo)}–{int(hi)} PLN"
    if hi:
        return f"~{int(hi)} PLN"
    if lo:
        return f"~{int(lo)} PLN"
    return "зп не указана"


def src_tag(source: str) -> str:
    if source == "adzuna":
        return "🌐 Adz"
    if source == "justjoin":
        return "🇵🇱 JJ"
    return source[:3]


def fmt_job_card(i: int, s, total: int) -> str:
    b = s.breakdown
    tag = "🆕 " if s.is_new else ""
    status = "" if s.status == "NEW" else f" · <b>{s.status}</b>"
    loc = s.job.location or "—"
    return (
        f"<b>{i}/{total} · [{s.total}]</b> {tag}{esc(s.job.title)}{status}\n"
        f"<i>{esc(s.job.company)}</i>\n"
        f"📍 {esc(loc)} · {src_tag(s.job.source)}\n"
        f"💰 {esc(fmt_salary(s.job))}\n"
        f"<code>R {b.role} · T {b.tech} · S {b.seniority} · $ {b.salary} · L {b.location}</code>"
    )


def kb_for(jid: str, url: str, status: str) -> InlineKeyboardMarkup:
    sl = "✅ Шортлист" if status == "SHORTLIST" else "⭐ Шортлист"
    ap = "✅ Отклик" if status == "APPLIED" else "📨 Отклик"
    rj = "✅ Скип" if status == "REJECTED" else "❌ Скип"
    rows = [
        [
            InlineKeyboardButton(sl, callback_data=f"m:SHORTLIST:{jid}"),
            InlineKeyboardButton(ap, callback_data=f"m:APPLIED:{jid}"),
            InlineKeyboardButton(rj, callback_data=f"m:REJECTED:{jid}"),
        ],
    ]
    if url:
        rows.append([InlineKeyboardButton("🔗 Открыть", url=url)])
    return InlineKeyboardMarkup(rows)


def kb_pagination(chat_id: int, total: int, page: int) -> InlineKeyboardMarkup | None:
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    if pages <= 1:
        return None
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Назад", callback_data=f"pg:{chat_id}:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="pg:noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("Вперёд ▶", callback_data=f"pg:{chat_id}:{page+1}"))
    return InlineKeyboardMarkup([nav])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    text = (
        "👋 <b>Job-Agent</b>\n\n"
        "<b>Команды:</b>\n"
        "/top [N] — Top-N (default 10)\n"
        "/all — все вакансии с пагинацией\n"
        "/new — что нового с прошлого запуска\n"
        "/search СЛОВО — поиск по базе\n"
        "/stats — статистика\n"
        "/list STATUS — вакансии со статусом\n"
        "/mark STATUS URL — вручную статус\n\n"
        f"<b>Твой chat_id:</b> <code>{chat_id}</code>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    n = 10
    if context.args:
        try:
            n = max(1, min(50, int(context.args[0])))
        except ValueError:
            pass

    msg = await update.message.reply_text(f"⏳ Ищу (Top {n}), жди ~30 сек…")

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
        f"📊 Найдено <b>{len(scored)}</b>. Top {len(top)}:",
        parse_mode=ParseMode.HTML,
    )
    store.close()

    for i, s in enumerate(top, 1):
        try:
            await update.message.reply_text(
                fmt_job_card(i, s, len(top)),
                parse_mode=ParseMode.HTML,
                reply_markup=kb_for(s.job_id, s.job.url, s.status),
                disable_web_page_preview=True,
            )
        except Exception as e:
            await update.message.reply_text(f"⚠ #{i}: {e}")


async def cmd_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    msg = await update.message.reply_text("⏳ Готовлю базу…")

    store = Store()
    try:
        profile = yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8"))
        scored = await run_pipeline_quiet(profile, store)
    except Exception as e:
        await msg.edit_text(f"❌ Ошибка: {e}")
        store.close()
        return
    store.close()

    PAGE_STATE[chat_id] = {"items": scored, "page": 0}
    await msg.edit_text(f"📚 Всего: <b>{len(scored)}</b>. По {PAGE_SIZE}.",
                        parse_mode=ParseMode.HTML)
    await _send_page(update, chat_id, page=0)


async def _send_page(update: Update, chat_id: int, page: int) -> None:
    state = PAGE_STATE.get(chat_id)
    if not state:
        return
    items = state["items"]
    state["page"] = page
    start = page * PAGE_SIZE
    chunk = items[start:start + PAGE_SIZE]
    total = len(items)

    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    header = f"<b>Стр. {page+1}/{pages}</b> ({start+1}–{start+len(chunk)} из {total})\n"
    lines = [header]
    for i, s in enumerate(chunk, start + 1):
        tag = "🆕 " if s.is_new else ""
        status = f" · <b>{s.status}</b>" if s.status != "NEW" else ""
        lines.append(
            f"<b>{i}.</b> [{s.total}] {tag}{esc(s.job.title[:70])}{status}\n"
            f"   <i>{esc(s.job.company[:40])}</i> · {esc(s.job.location[:40])} · {src_tag(s.job.source)}\n"
            f"   💰 {esc(fmt_salary(s.job))}"
        )

    kb = kb_pagination(chat_id, total, page)
    if update.callback_query:
        await update.callback_query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    else:
        await update.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
            disable_web_page_preview=True,
        )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store = Store()
    try:
        rows = store.list_new_since_last_run(limit=30)
    finally:
        store.close()

    if not rows:
        await update.message.reply_text("Нет новых вакансий.")
        return

    parts = [f"<b>🆕 Новые ({len(rows)}):</b>", ""]
    for i, r in enumerate(rows, 1):
        src = (r["source"] or "")[:3]
        parts.append(f"{i}. [{src}] {esc(r['title'])}")
        parts.append(f"   <i>{esc(r['company'])}</i> · {esc(r['location'])}")
        parts.append(f'   <a href="{esc_attr(r["url"])}">открыть</a>')
    text = "\n".join(parts)
    for c in range(0, len(text), 3500):
        await update.message.reply_text(
            text[c:c + 3500], parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Использование: /search terraform")
        return
    query = " ".join(context.args).strip().lower()

    store = Store()
    try:
        rows = store.conn.execute(
            """SELECT * FROM jobs
            WHERE LOWER(title) LIKE ? OR LOWER(company) LIKE ?
                  OR LOWER(description) LIKE ?
            ORDER BY last_seen DESC LIMIT 30""",
            (f"%{query}%", f"%{query}%", f"%{query}%"),
        ).fetchall()
    finally:
        store.close()

    if not rows:
        await update.message.reply_text(f"Ничего по '{query}'.")
        return

    parts = [f"<b>Поиск '{esc(query)}' ({len(rows)}):</b>", ""]
    for i, r in enumerate(rows, 1):
        parts.append(f"{i}. {esc(r['title'])}")
        parts.append(f"   <i>{esc(r['company'])}</i> · {esc(r['location'])}")
        parts.append(f'   <a href="{esc_attr(r["url"])}">открыть</a>')
    text = "\n".join(parts)
    for c in range(0, len(text), 3500):
        await update.message.reply_text(
            text[c:c + 3500], parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store = Store()
    try:
        s = store.stats()
    finally:
        store.close()

    lines = [
        f"<b>Total:</b> {s['total_jobs']}",
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
            f"Использование: /list STATUS\nЕсть: {', '.join(VALID_STATUSES)}"
        )
        return
    status = context.args[0].upper()
    if status not in VALID_STATUSES:
        await update.message.reply_text(f"Неизвестный. Есть: {', '.join(VALID_STATUSES)}")
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
        src = (r["source"] or "")[:3]
        parts.append(f"{i}. [{src}] {esc(r['title'])}")
        parts.append(f"   <i>{esc(r['company'])}</i>")
        parts.append(f'   <a href="{esc_attr(r["url"])}">открыть</a>')
    text = "\n".join(parts)
    for c in range(0, len(text), 3500):
        await update.message.reply_text(
            text[c:c + 3500], parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


async def cmd_mark(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        await update.message.reply_text(
            f"Использование: /mark STATUS URL\nЕсть: {', '.join(VALID_STATUSES)}"
        )
        return
    status = context.args[0].upper()
    target = context.args[1]
    if status not in VALID_STATUSES:
        await update.message.reply_text(f"Неизвестный. Есть: {', '.join(VALID_STATUSES)}")
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

    if data.startswith("m:"):
        try:
            _, status, jid = data.split(":", 2)
        except ValueError:
            await q.answer("bad data")
            return
        if status not in VALID_STATUSES:
            await q.answer("unknown status")
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
            await q.edit_message_reply_markup(reply_markup=kb_for(jid, url, status))
        except Exception:
            pass
        return

    if data.startswith("pg:"):
        parts = data.split(":")
        if len(parts) == 3 and parts[1] != "noop":
            try:
                _, chat_s, page_s = parts
                chat_id = int(chat_s)
                page = int(page_s)
            except ValueError:
                await q.answer()
                return
            await q.answer()
            await _send_page(update, chat_id, page)
            return

    await q.answer()


async def push_once(n: int) -> None:
    if not CHAT_ID_ENV:
        print("TELEGRAM_CHAT_ID не задан в .env.")
        return
    try:
        chat_id = int(CHAT_ID_ENV)
    except ValueError:
        print(f"TELEGRAM_CHAT_ID не число: {CHAT_ID_ENV!r}")
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
        chat_id, f"📊 <b>Top {len(top)}</b> из {len(scored)}",
        parse_mode=ParseMode.HTML,
    )
    for i, s in enumerate(top, 1):
        await app.bot.send_message(
            chat_id,
            fmt_job_card(i, s, len(top)),
            parse_mode=ParseMode.HTML,
            reply_markup=kb_for(s.job_id, s.job.url, s.status),
            disable_web_page_preview=True,
        )
    print("Готово.")


def main() -> None:
    p = argparse.ArgumentParser(prog="bot.py")
    p.add_argument("--once", action="store_true", help="push и выход")
    p.add_argument("--top", type=int, default=10)
    args = p.parse_args()

    if args.once:
        asyncio.run(push_once(args.top))
        return

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("all", cmd_all))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("mark", cmd_mark))
    app.add_handler(CallbackQueryHandler(on_button))

    print("🤖 Бот запущен. Ctrl+C — остановить.")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()