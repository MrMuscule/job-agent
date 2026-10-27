"""
JustJoin.it scraper через публичный JSON API.

Endpoint: GET https://justjoin.it/api/candidate-api/offers?from=N
Возвращает { "data": [...], "meta": { "next": {"cursor": N} } }
Постранично по 10 вакансий.

Нормализует в общую структуру Job (см. app.py).
"""
from __future__ import annotations

import asyncio
import re

import httpx

JUSTJOIN_URL = "https://justjoin.it/api/candidate-api/offers"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

_WS = re.compile(r"\s+")
HOURS_PER_MONTH = 168

# Сколько страниц по 10 вакансий тянуть. 30 страниц = ~300 вакансий.
MAX_PAGES = 30
PAGE_DELAY = 0.4  # секунд между запросами, чтобы не банили


async def fetch_justjoin(max_pages: int = MAX_PAGES) -> list[dict]:
    out: list[dict] = []
    cursor = 0

    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=30.0,
    ) as client:
        for _ in range(max_pages):
            try:
                resp = await client.get(JUSTJOIN_URL, params={"from": cursor})
                resp.raise_for_status()
                payload = resp.json()
            except Exception as e:
                print(f"[justjoin] page cursor={cursor} failed: {e}")
                break

            data = payload.get("data") if isinstance(payload, dict) else payload
            if not data:
                break

            for offer in data:
                try:
                    item = _normalize(offer)
                    if item:
                        out.append(item)
                except Exception:
                    continue

            meta = payload.get("meta") or {}
            nxt = (meta.get("next") or {}).get("cursor")
            if nxt is None:
                break
            cursor = int(nxt)
            await asyncio.sleep(PAGE_DELAY)

    return out


def _normalize(o: dict) -> dict | None:
    title = (o.get("title") or "").strip()
    if not title:
        return None

    slug = (o.get("slug") or o.get("guid") or "").strip()
    url = f"https://justjoin.it/job-offer/{slug}" if slug else ""

    company = (o.get("companyName") or "").strip()

    loc_parts: list[str] = []
    if o.get("workplaceType"):
        loc_parts.append(str(o["workplaceType"]).capitalize())
    city = (o.get("city") or "").strip()
    if city:
        loc_parts.append(city)
    location = ", ".join(loc_parts) if loc_parts else ""

    skills = _extract_skills(o)
    parts: list[str] = []
    if skills:
        parts.append("Skills: " + ", ".join(skills))
    desc = _WS.sub(" ", " ".join(parts)).strip()

    salary_min, salary_max, salary_text = _salary(o)

    return {
        "title": title,
        "company": company,
        "location": location,
        "description": desc,
        "url": url,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_text": salary_text,
        "experience_level": (o.get("experienceLevel") or "unknown").lower(),
        "skills": skills,
        "raw": o,
    }


def _extract_skills(o: dict) -> list[str]:
    out: list[str] = []
    for s in (o.get("requiredSkills") or []):
        name = (s.get("name") if isinstance(s, dict) else str(s)) or ""
        name = name.strip()
        if name:
            out.append(name)
    return out


def _salary(o: dict) -> tuple[float | None, float | None, str]:
    """Парсит employmentTypes.

    В PLN-записи поле unit говорит период:
      - "Hour" → умножаем на 168 (рабочих часов в месяц)
      - "Month" / "month" → уже месячная
    Берём только currency=PLN (там отдаются реальные цифры, остальное — конверсия).
    """
    lo: float | None = None
    hi: float | None = None

    for s in (o.get("employmentTypes") or []):
        if not isinstance(s, dict):
            continue
        if (s.get("currency") or "").upper() != "PLN":
            continue

        f = _num(s.get("from"))
        t = _num(s.get("to"))
        unit = (s.get("unit") or "").lower()

        if unit == "hour":
            if f is not None:
                f = f * HOURS_PER_MONTH
            if t is not None:
                t = t * HOURS_PER_MONTH

        if f is None and t is None:
            continue

        if f is not None:
            lo = min(lo, f) if lo is not None else f
        if t is not None:
            hi = max(hi, t) if hi is not None else t

    if lo is None and hi is None:
        return None, None, ""

    # отсеиваем мусор (например, часовая ставка без unit → 90 "PLN")
    if lo and lo < 2000:
        lo = None
    if hi and hi < 2000:
        hi = None

    if lo and hi:
        text = f"{int(lo)}-{int(hi)} PLN/мес"
    elif hi:
        text = f"до {int(hi)} PLN/мес"
    elif lo:
        text = f"от {int(lo)} PLN/мес"
    else:
        text = ""

    return lo, hi, text


def _num(x) -> float | None:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None