"""
Pracuj.pl scraper (Playwright).

Собирает офферы через it.pracuj.pl. Основной паттерн ссылки на оффер — ,oferta,<id>
(старый формат /oferta/ тоже поддерживается).

Возвращает список dict:
  title, company, location, description, url,
  salary_min, salary_max, salary_text
"""
from __future__ import annotations

import json
import os
import re
from urllib.parse import quote

from playwright.async_api import async_playwright, Page, Frame

BASE = "https://it.pracuj.pl"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

_WS = re.compile(r"\s+")

# Основной формат ссылок Pracuj — через запятую: /praca/<slug>,oferta,<id>
# Старый /oferta/ оставляем на всякий случай.
OFFER_HREF_PATTERNS = (",oferta,", "/oferta/")

# Можно переключить в headful через переменную окружения PRACUJ_HEADFUL=1
HEADLESS = os.getenv("PRACUJ_HEADFUL", "0") != "1"


async def search_pracuj(queries: list[str], max_per_query: int = 15) -> list[dict]:
    jobs: list[dict] = []
    if not queries:
        return jobs

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS, slow_mo=50)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            locale="pl-PL",
            viewport={"width": 1366, "height": 900},
        )
        page = await context.new_page()
        try:
            for q in queries:
                try:
                    batch = await _scrape_query(page, q, max_per_query)
                except Exception as e:
                    print(f"[pracuj] '{q}' FAILED: {type(e).__name__}: {e}")
                    batch = []
                print(f"[pracuj] '{q}' -> {len(batch)} offers")
                jobs.extend(batch)
        finally:
            await context.close()
            await browser.close()
    return jobs


async def _scrape_query(page: Page, query: str, max_jobs: int) -> list[dict]:
    url = f"{BASE}/praca/{quote(query)};kw"
    resp = await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    status = resp.status if resp else "?"
    await page.wait_for_timeout(1500)
    await _accept_cookies(page)
    await page.wait_for_timeout(500)

    title = await page.title()

    hrefs = await _collect_offer_links(page, max_jobs)

    if not hrefs:
        # ─── Диагностика ─────────────────────────────────────────────────
        print(f"[pracuj]   status={status}  title={title!r}")
        print(f"[pracuj]   final_url={page.url}")
        total_a = await page.locator("a").count()
        print(f"[pracuj]   total <a> = {total_a}")
        print(f"[pracuj]   sample links:")
        try:
            anchors = await page.locator("a").all()
            shown = 0
            for a in anchors:
                href = await a.get_attribute("href")
                if not href:
                    continue
                if not (href.startswith("http") or href.startswith("/")):
                    continue
                print(f"[pracuj]     {href[:120]}")
                shown += 1
                if shown >= 10:
                    break
        except Exception as e:
            print(f"[pracuj]   sample-links failed: {e}")
        body = await page.locator("body").inner_text()
        print(f"[pracuj]   body[:200]={body[:200]!r}")

    out: list[dict] = []
    for href in hrefs:
        try:
            await page.goto(href, wait_until="domcontentloaded", timeout=30_000)
            job = await _extract_offer(page, href)
            if job:
                out.append(job)
        except Exception:
            continue
    return out


async def _collect_offer_links(page: Page, max_jobs: int) -> list[str]:
    hrefs: list[str] = []
    seen: set[str] = set()
    for pattern in OFFER_HREF_PATTERNS:
        loc = page.locator(f"a[href*='{pattern}']")
        n = await loc.count()
        if n == 0:
            continue
        for a in await loc.all():
            href = await a.get_attribute("href")
            if not href:
                continue
            if href.startswith("/"):
                href = BASE + href
            href = href.split("?", 1)[0].split("#", 1)[0]
            if href in seen:
                continue
            seen.add(href)
            hrefs.append(href)
            if len(hrefs) >= max_jobs:
                return hrefs
        if hrefs:
            return hrefs
    return hrefs


# ────────────────────────────────────────────────────────────────────────
# Cookies
# ────────────────────────────────────────────────────────────────────────

_COOKIE_SELECTORS = (
    "button[data-test='button-accept-all-in-consent-modal']",
    "button[data-test*='accept-all']",
    "button[data-test*='accept']",
    "button:has-text('Akceptuj wszystkie')",
    "button:has-text('Akceptuję wszystkie')",
    "button:has-text('Akceptuj')",
    "button:has-text('Zgadzam się')",
    "button:has-text('Accept all')",
    "button:has-text('Accept')",
    "[id*='onetrust'] button#onetrust-accept-btn-handler",
)


async def _accept_cookies(page: Page) -> None:
    if await _try_click(page):
        return
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            if await _try_click(frame):
                return
        except Exception:
            continue


async def _try_click(ctx: Page | Frame) -> bool:
    for sel in _COOKIE_SELECTORS:
        try:
            loc = ctx.locator(sel).first
            if await loc.count() > 0:
                await loc.click(timeout=1500)
                try:
                    await ctx.wait_for_timeout(400)
                except Exception:
                    pass
                return True
        except Exception:
            continue
    return False


# ────────────────────────────────────────────────────────────────────────
# Извлечение оффера
# ────────────────────────────────────────────────────────────────────────

async def _extract_offer(page: Page, url: str) -> dict | None:
    ld = await _extract_jsonld(page)
    if ld:
        return _from_jsonld(ld, url)
    return await _from_dom(page, url)


async def _extract_jsonld(page: Page) -> dict | None:
    try:
        scripts = await page.locator("script[type='application/ld+json']").all()
    except Exception:
        return None
    for s in scripts:
        try:
            data = json.loads(await s.inner_text())
        except Exception:
            continue
        for item in _iter_ld(data):
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def _iter_ld(data):
    if isinstance(data, list):
        for x in data:
            yield from _iter_ld(x)
    elif isinstance(data, dict):
        yield data
        graph = data.get("@graph")
        if isinstance(graph, list):
            for x in graph:
                yield from _iter_ld(x)


def _from_jsonld(ld: dict, url: str) -> dict:
    title = (ld.get("title") or "").strip()

    company = ""
    org = ld.get("hiringOrganization")
    if isinstance(org, dict):
        company = (org.get("name") or "").strip()

    location = ""
    locs = ld.get("jobLocation")
    if isinstance(locs, list) and locs:
        locs = locs[0]
    if isinstance(locs, dict):
        addr = locs.get("address") or {}
        if isinstance(addr, dict):
            parts = [
                addr.get("addressLocality") or "",
                addr.get("addressRegion") or "",
                addr.get("addressCountry") or "",
            ]
            location = ", ".join(p for p in parts if p)

    desc = ld.get("description") or ""
    desc = _WS.sub(" ", re.sub(r"<[^>]+>", " ", desc)).strip()

    salary_text = ""
    salary_min = None
    salary_max = None
    sal = ld.get("baseSalary")
    if isinstance(sal, dict):
        val = sal.get("value")
        cur = sal.get("currency") or ""
        if isinstance(val, dict):
            salary_min = _num(val.get("minValue"))
            salary_max = _num(val.get("maxValue"))
            if salary_min and salary_max:
                salary_text = f"{int(salary_min)}-{int(salary_max)} {cur}".strip()
            elif salary_min:
                salary_text = f"{int(salary_min)}+ {cur}".strip()
        elif isinstance(val, (int, float)):
            salary_min = float(val)
            salary_text = f"{int(val)} {cur}".strip()

    return {
        "title": title,
        "company": company,
        "location": location,
        "description": desc,
        "url": url,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_text": salary_text,
    }


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


async def _from_dom(page: Page, url: str) -> dict | None:
    async def txt(sel: str) -> str:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                return (await loc.inner_text()).strip()
        except Exception:
            pass
        return ""

    title = await txt("h1")
    if not title:
        return None

    company = ""
    for sel in (
        "[data-test='text-employer-name']",
        "[data-test='text-company-name']",
        "h2",
    ):
        company = await txt(sel)
        if company:
            break

    location = ""
    for sel in (
        "[data-test='text-region']",
        "[data-test='text-location']",
    ):
        location = await txt(sel)
        if location:
            break

    desc = ""
    for sel in (
        "[data-test='section-text']",
        "[data-test='text-description']",
        "section[data-test='section-description']",
    ):
        desc = await txt(sel)
        if desc:
            break

    if not desc:
        desc = await txt("body")

    return {
        "title": title,
        "company": company,
        "location": location,
        "description": _WS.sub(" ", desc)[:8000],
        "url": url,
        "salary_min": None,
        "salary_max": None,
        "salary_text": "",
    }