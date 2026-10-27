"""Adzuna API (Польша). Возвращает сырые записи."""
from __future__ import annotations

import asyncio

import httpx

from ..config import env

RETRY_STATUSES = {429, 502, 503, 504}
RETRY_DELAYS = [2, 5, 10]
URL = "https://api.adzuna.com/v1/api/jobs/pl/search/{page}"


async def _get(client: httpx.AsyncClient, url: str, params: dict) -> httpx.Response:
    last: Exception | None = None
    for delay in [0, *RETRY_DELAYS]:
        if delay:
            await asyncio.sleep(delay)
        try:
            resp = await client.get(url, params=params, timeout=30.0)
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last = e
            continue
        if resp.status_code in RETRY_STATUSES:
            last = httpx.HTTPStatusError(f"HTTP {resp.status_code}",
                                         request=resp.request, response=resp)
            continue
        resp.raise_for_status()
        return resp
    raise last or RuntimeError("adzuna request failed")


async def fetch_adzuna(profile: dict, log=print) -> list[dict]:
    app_id, app_key = env("ADZUNA_APP_ID"), env("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        log("[adzuna] ключи не заданы, пропускаю")
        return []

    cfg = profile.get("search") or {}
    pages = int(cfg.get("adzuna_pages", 1))
    out: list[dict] = []
    async with httpx.AsyncClient() as client:
        for query in cfg.get("adzuna_queries") or []:
            for page in range(1, pages + 1):
                params = {"app_id": app_id, "app_key": app_key,
                          "results_per_page": 50, "what": query,
                          "content-type": "application/json"}
                try:
                    resp = await _get(client, URL.format(page=page), params)
                except Exception as e:
                    log(f"[adzuna] '{query}' p{page}: {e}")
                    break
                results = resp.json().get("results", [])
                if not results:
                    break
                out.extend(results)
    return out
