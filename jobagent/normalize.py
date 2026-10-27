"""Единая модель вакансии и нормализация из источников."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

_TAG = re.compile(r"<[^>]+>")
_BR = re.compile(r"</(p|li|h\d|div|ul|ol)>|<br\s*/?>", re.IGNORECASE)
_WS = re.compile(r"\s+")

MAX_REASONABLE_MONTHLY = 60000


@dataclass
class SalaryOffer:
    """Вилка в PLN за месяц. kind: uop (брутто) | b2b (нетто) | unknown."""
    kind: str
    lo: float | None
    hi: float | None


@dataclass
class Job:
    source: str
    ext_id: str
    title: str
    company: str = ""
    location: str = ""
    workplace: str = ""          # remote | hybrid | office | ""
    url: str = ""
    description: str = ""
    level: str = ""              # junior | mid | senior | ... (если источник знает)
    skills: list[str] = field(default_factory=list)
    offers: list[SalaryOffer] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.source}:{self.ext_id}"

    @property
    def text(self) -> str:
        """Всё, по чему ищем ключевые слова (в нижнем регистре)."""
        return f"{self.title}\n{' '.join(self.skills)}\n{self.description}".lower()


def html_to_text(s: str) -> str:
    s = _BR.sub(" \n ", s or "")
    s = _TAG.sub(" ", s)
    return _WS.sub(" ", html.unescape(s)).strip()


# ── Adzuna ──────────────────────────────────────────────────────────────

def _adzuna_monthly(v) -> float | None:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    # Adzuna отдаёт годовую сумму; всё, что похоже на месячную, оставляем как есть
    v = v / 12.0 if v > 30000 else v
    return v if v <= MAX_REASONABLE_MONTHLY else None


def from_adzuna(r: dict) -> Job | None:
    title = (r.get("title") or "").strip()
    ext = str(r.get("id") or "").strip()
    url = (r.get("redirect_url") or "").strip()
    if not title or not (ext or url):
        return None

    offers: list[SalaryOffer] = []
    # predicted = оценка Adzuna, а не реальная вилка → игнорируем
    if str(r.get("salary_is_predicted")) != "1":
        lo = _adzuna_monthly(r.get("salary_min"))
        hi = _adzuna_monthly(r.get("salary_max"))
        if lo or hi:
            offers.append(SalaryOffer("unknown", lo, hi))

    return Job(
        source="adzuna",
        ext_id=ext or url,
        title=title,
        company=((r.get("company") or {}).get("display_name") or "").strip(),
        location=((r.get("location") or {}).get("display_name") or "").strip(),
        url=url,
        description=html_to_text(r.get("description") or ""),
        offers=offers,
    )


# ── JustJoin ────────────────────────────────────────────────────────────

_JJ_KIND = {"b2b": "b2b", "permanent": "uop", "mandate_contract": "uop"}


def _jj_offers(o: dict, eur_pln: float, usd_pln: float) -> list[SalaryOffer]:
    rates = {"PLN": 1.0, "EUR": eur_pln, "USD": usd_pln}
    out: list[SalaryOffer] = []
    for e in o.get("employmentTypes") or []:
        if not isinstance(e, dict):
            continue
        # *conversion* — пересчёт самого JJ, берём только оригинальную валюту
        if e.get("currencySource") not in (None, "original"):
            continue
        kind = _JJ_KIND.get((e.get("type") or "").lower())
        if kind is None:
            if (e.get("type") or "").lower() == "internship":
                continue
            kind = "unknown"
        rate = rates.get((e.get("currency") or "").upper())
        if rate is None:
            continue
        # from/to уже приведены JJ к месяцу (для часовых: ставка × 168)
        lo, hi = e.get("from"), e.get("to")
        try:
            lo = float(lo) * rate if lo is not None else None
            hi = float(hi) * rate if hi is not None else None
        except (TypeError, ValueError):
            continue
        if lo is None and hi is None:
            continue
        out.append(SalaryOffer(kind, lo, hi))
    return out


def from_justjoin(o: dict, eur_pln: float = 4.3, usd_pln: float = 3.9) -> Job | None:
    title = (o.get("title") or "").strip()
    slug = (o.get("slug") or "").strip()
    if not title or not slug:
        return None

    wp = (o.get("workplaceType") or "").lower()
    workplace = {"remote": "remote", "hybrid": "hybrid", "office": "office"}.get(wp, "")
    skills = []
    for s in o.get("requiredSkills") or []:
        name = (s.get("name") if isinstance(s, dict) else str(s)) or ""
        if name.strip():
            skills.append(name.strip())

    cities: list[str] = []
    for loc in o.get("locations") or []:
        c = (loc.get("city") or "").strip() if isinstance(loc, dict) else ""
        if c and c not in cities:
            cities.append(c)
    city = (o.get("city") or "").strip()
    if city and city not in cities:
        cities.insert(0, city)

    level = (o.get("experienceLevel") or "").lower()
    return Job(
        source="justjoin",
        ext_id=slug,
        title=title,
        company=(o.get("companyName") or "").strip(),
        location=", ".join(cities),
        workplace=workplace,
        url=f"https://justjoin.it/job-offer/{slug}",
        level=level,
        skills=skills,
        offers=_jj_offers(o, eur_pln, usd_pln),
    )


# ── Отображение вилки ───────────────────────────────────────────────────

_KIND_LABEL = {"uop": "UoP брутто", "b2b": "B2B нетто", "unknown": ""}


def _k(v: float) -> str:
    return f"{v / 1000:.1f}".rstrip("0").rstrip(".") + "k"


def salary_label(offers: list[SalaryOffer]) -> str:
    parts = []
    for o in offers:
        if o.lo and o.hi and round(o.lo) != round(o.hi):
            rng = f"{_k(o.lo)}–{_k(o.hi)}"
        else:
            v = o.hi or o.lo
            rng = _k(v) if v else ""
        if rng:
            parts.append(f"{rng} PLN {_KIND_LABEL.get(o.kind, '')}".strip())
    return " · ".join(parts)


# ── Dedupe ──────────────────────────────────────────────────────────────

def _key(s: str) -> str:
    return _WS.sub(" ", (s or "").strip().lower())


def dedupe(jobs: list[Job]) -> list[Job]:
    by_id: dict[str, Job] = {}
    by_tc: dict[tuple[str, str], Job] = {}
    out: list[Job] = []
    for j in jobs:
        tc = (_key(j.title), _key(j.company))
        prev = by_id.get(j.id) or (by_tc.get(tc) if all(tc) else None)
        if prev is not None:
            if len(j.description) > len(prev.description):
                prev.description = j.description
            if not prev.offers and j.offers:
                prev.offers = j.offers
            continue
        out.append(j)
        by_id[j.id] = j
        if all(tc):
            by_tc[tc] = j
    return out
