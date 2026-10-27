"""JustJoin.it: публичный JSON API кандидата.

Список:  GET /api/candidate-api/offers?from=N[&categories=devops][&keywords=storage]
Деталь:  GET /api/candidate-api/offers/{slug}  (поле body = полное описание, HTML)
"""
from __future__ import annotations

import asyncio

import httpx

BASE = "https://justjoin.it/api/candidate-api/offers"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json",
}
PAGE_DELAY = 0.3
MAX_PAGES = 40            # по 10 вакансий на страницу

# Что тянем списком: категории и поисковые слова
CATEGORIES = ("devops", "admin", "support")
KEYWORDS = ("storage", "linux", "administrator", "infrastructure", "noc", "telecom")


async def _fetch_list(client: httpx.AsyncClient, extra: dict, max_pages: int,
                      log) -> list[dict]:
    out: list[dict] = []
    cursor = 0
    for _ in range(max_pages):
        try:
            resp = await client.get(BASE, params={"from": cursor, **extra})
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:
            log(f"[justjoin] {extra} from={cursor}: {e}")
            break
        data = payload.get("data") or []
        if not data:
            break
        out.extend(data)
        nxt = ((payload.get("meta") or {}).get("next") or {}).get("cursor")
        if nxt is None:
            break
        cursor = int(nxt)
        await asyncio.sleep(PAGE_DELAY)
    return out


async def fetch_justjoin_list(max_pages: int = MAX_PAGES, log=print) -> list[dict]:
    """Список офферов (без описания), дедуп по slug."""
    seen: dict[str, dict] = {}
    async with httpx.AsyncClient(headers=HEADERS, timeout=30.0) as client:
        queries = [{"categories": c} for c in CATEGORIES] + [{"keywords": k} for k in KEYWORDS]
        results = await asyncio.gather(
            *[_fetch_list(client, q, max_pages, log) for q in queries]
        )
    for chunk in results:
        for o in chunk:
            slug = o.get("slug")
            if slug and slug not in seen:
                seen[slug] = o
    return list(seen.values())


async def fetch_justjoin_bodies(slugs: list[str], log=print,
                                concurrency: int = 5) -> dict[str, str]:
    """slug -> HTML-описание."""
    sem = asyncio.Semaphore(concurrency)
    out: dict[str, str] = {}

    async with httpx.AsyncClient(headers=HEADERS, timeout=30.0) as client:
        async def one(slug: str) -> None:
            async with sem:
                try:
                    r = await client.get(f"{BASE}/{slug}")
                    r.raise_for_status()
                    out[slug] = r.json().get("body") or ""
                except Exception as e:
                    log(f"[justjoin] detail {slug}: {e}")
                await asyncio.sleep(0.15)

        await asyncio.gather(*[one(s) for s in slugs])
    return out
