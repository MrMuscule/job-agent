"""
JOB-AGENT — deterministic job search pipeline + SQLite storage.

Источники: Adzuna + JustJoin.it
Профиль: Infrastructure / Platform Engineer (Kubernetes, Storage, Linux).
Уровень: mid / regular (не senior).
Локация: Warszawa + окрестности + full-remote (ПОЛЬША).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from functools import lru_cache
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from db import Store, job_id, VALID_STATUSES
from justjoin import fetch_justjoin

load_dotenv()

console = Console()

TOP_JOBS = 30
RETRY_STATUSES = {429, 502, 503, 504}
RETRY_DELAYS = [2, 5, 10]
MAX_REASONABLE_MONTHLY = 30000

HIDDEN_STATUSES = ("REJECTED", "APPLIED", "INTERVIEW", "OFFER")

HERE = Path(__file__).resolve().parent
PROFILE_PATH = HERE / "profile.yaml"
DATA_DIR = HERE / "data"
DATA_DIR.mkdir(exist_ok=True)


# ────────────────────────────────────────────────────────────────────────
# Локация
# ────────────────────────────────────────────────────────────────────────

LOCATION_WHITELIST = (
    "warszawa", "warsaw", "mazowieckie",
    "piaseczno", "raszyn", "pruszków", "pruszkow",
    "legionowo", "marki", "otwock", "wołomin", "wolomin",
    "żyrardów", "zyrardow", "nowy dwór mazowiecki", "nowy dwor mazowiecki",
    "siedlce", "radom", "płock", "plock", "ciechanów", "ciechanow",
    "kraków", "krakow", "katowice", "wrocław", "wroclaw",
    "poznań", "poznan", "gdańsk", "gdansk", "gdynia", "sopot",
    "łódź", "lodz", "szczecin", "bydgoszcz", "lublin",
    "białystok", "bialystok", "rzeszów", "rzeszow",
    "toruń", "torun", "kielce", "olsztyn",
    "częstochowa", "czestochowa", "sosnowiec", "gliwice", "zabrze",
    "polska", "poland",
)

LOCATION_FAR_FROM_WARSAW = (
    "kraków", "krakow", "katowice", "wrocław", "wroclaw",
    "poznań", "poznan", "gdańsk", "gdansk", "gdynia", "sopot",
    "łódź", "lodz", "szczecin", "bydgoszcz", "lublin",
    "białystok", "bialystok", "rzeszów", "rzeszow",
    "toruń", "torun", "kielce", "olsztyn",
    "częstochowa", "czestochowa", "sosnowiec", "gliwice", "zabrze",
)

_WORK_MODE_PREFIXES = ("remote", "hybrid", "office", "zdalna", "zdalnie",
                       "hybrydowa", "stacjonarna", "praca zdalna")


def _normalize_location(loc: str) -> str:
    if not loc:
        return ""
    s = loc.strip().lower()
    for prefix in _WORK_MODE_PREFIXES:
        if s.startswith(prefix + ","):
            s = s[len(prefix) + 1:].strip()
            break
        if s.startswith(prefix + " "):
            s = s[len(prefix) + 1:].strip()
            break
    return s


def _is_warsaw(job) -> bool:
    loc_l = _normalize_location(job.location)
    markers = (
        "warszawa", "warsaw", "mazowieckie",
        "piaseczno", "raszyn", "pruszków", "pruszkow",
        "legionowo", "marki", "otwock", "wołomin", "wolomin",
    )
    return any(tok in loc_l for tok in markers)


def _is_poland_generic(job) -> bool:
    loc_l = _normalize_location(job.location)
    return "polska" in loc_l or "poland" in loc_l or loc_l == ""


def _is_in_whitelist(job) -> bool:
    loc_l = _normalize_location(job.location)
    if not loc_l:
        return True
    return any(city in loc_l for city in LOCATION_WHITELIST)


def _is_far_polish_city(job) -> str | None:
    loc_l = _normalize_location(job.location)
    for city in LOCATION_FAR_FROM_WARSAW:
        if city in loc_l:
            return city
    return None


def _is_fully_remote(job, profile: dict) -> str | None:
    loc_cfg = profile.get("location") or {}
    markers = loc_cfg.get("remote_markers") or []
    text_l = f"{job.title}\n{job.description}".lower()
    for m in markers:
        if m and m.lower() in text_l:
            return m
    return None


def _location_reject(job, profile: dict) -> tuple[bool, str]:
    loc_l = _normalize_location(job.location)

    if _is_fully_remote(job, profile):
        return False, ""

    if loc_l and not _is_in_whitelist(job):
        return True, f"локация не Польша: '{job.location}'"

    far_city = _is_far_polish_city(job)
    if far_city:
        return True, f"далёкий город: '{far_city}'"

    if _is_warsaw(job) or _is_poland_generic(job):
        return False, ""

    return False, ""


def _location_score(job, profile: dict, b: "Breakdown") -> int:
    loc_cfg = profile.get("location") or {}
    warsaw_bonus = int(loc_cfg.get("warsaw_bonus", 5))
    remote_bonus = int(loc_cfg.get("remote_bonus", 3))

    if _is_warsaw(job):
        b.notes.append(f"location=+{warsaw_bonus} (Warszawa)")
        return warsaw_bonus

    marker = _is_fully_remote(job, profile)
    if marker:
        b.notes.append(f"location=+{remote_bonus} (full remote: '{marker}')")
        return remote_bonus

    if _is_poland_generic(job):
        b.notes.append("location=0 (Polska — общенац.)")
        return 0

    b.notes.append(f"location=0 ({job.location or '—'})")
    return 0


# ────────────────────────────────────────────────────────────────────────
# Модель данных
# ────────────────────────────────────────────────────────────────────────

@dataclass
class Job:
    source: str
    title: str
    company: str
    location: str
    description: str
    url: str
    salary_min: float | None = None
    salary_max: float | None = None
    salary_text: str = ""
    level: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class Breakdown:
    role: int = 0
    tech: int = 0
    seniority: int = 0
    salary: int = 0
    location: int = 0
    penalties: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return (self.role + self.tech + self.seniority
                + self.salary + self.location + self.penalties)


@dataclass
class ScoredJob:
    job: Job
    breakdown: Breakdown
    job_id: str = ""
    is_new: bool = False
    status: str = "NEW"

    @property
    def total(self) -> int:
        return self.breakdown.total


@lru_cache(maxsize=None)
def _kw_re(kw: str) -> re.Pattern:
    return re.compile(r"(?<![a-z0-9])" + re.escape(kw.lower()) + r"(?![a-z0-9])")


def _has_kw(text_lower: str, kw: str) -> bool:
    return bool(_kw_re(kw).search(text_lower))


# ────────────────────────────────────────────────────────────────────────
# Adzuna
# ────────────────────────────────────────────────────────────────────────

async def _get_with_retry(client: httpx.AsyncClient, url: str, params: dict) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt, delay in enumerate([0, *RETRY_DELAYS]):
        if delay:
            console.print(f"  [dim]retry in {delay}s ({attempt}/{len(RETRY_DELAYS)})…[/dim]")
            await asyncio.sleep(delay)
        try:
            resp = await client.get(url, params=params, timeout=30.0)
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last_exc = e
            continue
        if resp.status_code in RETRY_STATUSES:
            last_exc = httpx.HTTPStatusError(
                f"HTTP {resp.status_code}", request=resp.request, response=resp
            )
            continue
        resp.raise_for_status()
        return resp
    if last_exc is None:
        raise RuntimeError("request failed without exception")
    raise last_exc


async def collect_adzuna(client: httpx.AsyncClient, profile: dict) -> list[Job]:
    app_id = os.getenv("ADZUNA_APP_ID")
    app_key = os.getenv("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        console.print("[yellow]Adzuna: ключи отсутствуют.[/yellow]")
        return []

    cfg = profile.get("search") or {}
    where = cfg.get("adzuna_where") or ""
    pages = int(cfg.get("adzuna_pages", 1))
    queries = cfg.get("adzuna_queries") or []

    out: list[Job] = []
    for query in queries:
        for page in range(1, pages + 1):
            params = {
                "app_id": app_id,
                "app_key": app_key,
                "results_per_page": 50,
                "what": query,
                "content-type": "application/json",
            }
            if where:
                params["where"] = where
            url = f"https://api.adzuna.com/v1/api/jobs/pl/search/{page}"
            try:
                resp = await _get_with_retry(client, url, params)
            except Exception as e:
                console.print(f"[red]Adzuna '{query}' p{page}: {e}[/red]")
                break
            results = resp.json().get("results", [])
            if not results:
                break
            for r in results:
                out.append(_normalize_adzuna(r))
    return out


_YEARLY_MARKERS = (
    "rocznie", "per year", "annual", "yearly", "annually",
    "brutto rocznie", "netto rocznie", "pln/year", "pln / year",
)
_MONTHLY_MARKERS = (
    "miesięcznie", "miesiac", "per month", "monthly",
    "miesiąc", "brutto miesięcznie", "netto miesięcznie",
    "pln/month", "pln / month",
)


def _to_monthly(v, desc: str = "") -> float | None:
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None

    dl = (desc or "").lower()
    has_yearly = any(m in dl for m in _YEARLY_MARKERS)
    has_monthly = any(m in dl for m in _MONTHLY_MARKERS)

    if has_yearly and not has_monthly:
        result = v / 12.0
    elif has_monthly and not has_yearly:
        result = v
    else:
        result = v / 12.0 if v > 30000 else v

    if result > MAX_REASONABLE_MONTHLY:
        return None
    return result


def _normalize_adzuna(r: dict) -> Job:
    company = ((r.get("company") or {}).get("display_name") or "").strip()
    location = ((r.get("location") or {}).get("display_name") or "").strip()
    desc = re.sub(r"<[^>]+>", " ", r.get("description") or "")
    clean_desc = re.sub(r"\s+", " ", desc).strip()

    smin = _to_monthly(r.get("salary_min"), clean_desc)
    smax = _to_monthly(r.get("salary_max"), clean_desc)

    if smax is not None and smax > MAX_REASONABLE_MONTHLY:
        smax = None
    if smin is not None and smin > MAX_REASONABLE_MONTHLY:
        smin = None

    return Job(
        source="adzuna",
        title=(r.get("title") or "").strip(),
        company=company,
        location=location,
        description=clean_desc,
        url=(r.get("redirect_url") or "").strip(),
        salary_min=smin,
        salary_max=smax,
        raw=r,
    )


# ────────────────────────────────────────────────────────────────────────
# JustJoin
# ────────────────────────────────────────────────────────────────────────

async def collect_justjoin(profile: dict) -> list[Job]:
    raw = await fetch_justjoin(max_pages=30)
    console.print(f"[cyan]JustJoin:[/cyan] {len(raw)}")
    out: list[Job] = []
    for r in raw:
        out.append(Job(
            source="justjoin",
            title=(r.get("title") or "").strip(),
            company=(r.get("company") or "").strip(),
            location=(r.get("location") or "").strip(),
            description=(r.get("description") or "").strip(),
            url=(r.get("url") or "").strip(),
            salary_min=r.get("salary_min"),
            salary_max=r.get("salary_max"),
            salary_text=(r.get("salary_text") or "").strip(),
            level=(r.get("level") or "").strip().lower(),
            raw=r,
        ))
    return out


# ────────────────────────────────────────────────────────────────────────
# Dedupe
# ────────────────────────────────────────────────────────────────────────

_WS = re.compile(r"\s+")


def _norm_key(s: str) -> str:
    return _WS.sub(" ", (s or "").strip().lower())


def _url_key(url: str) -> str:
    if not url:
        return ""
    return url.split("#", 1)[0].split("?", 1)[0].rstrip("/").lower()


def dedupe(jobs: list[Job]) -> list[Job]:
    by_url: dict[str, Job] = {}
    by_tc: dict[tuple[str, str], Job] = {}
    out: list[Job] = []
    for job in jobs:
        uk = _url_key(job.url)
        tk = (_norm_key(job.title), _norm_key(job.company))

        if uk and uk in by_url:
            existing = by_url[uk]
            if len(job.description) > len(existing.description):
                existing.description = job.description
            continue
        if tk[0] and tk[1] and tk in by_tc:
            existing = by_tc[tk]
            if len(job.description) > len(existing.description):
                existing.description = job.description
            continue

        out.append(job)
        if uk:
            by_url[uk] = job
        if tk[0] and tk[1]:
            by_tc[tk] = job
    return out


# ────────────────────────────────────────────────────────────────────────
# Hard reject
# ────────────────────────────────────────────────────────────────────────

def hard_reject(job: Job, profile: dict) -> tuple[bool, str]:
    title_l = job.title.lower()
    desc_l = job.description.lower()
    role_cfg = profile.get("role_keywords") or {}

    rej, reason = _location_reject(job, profile)
    if rej:
        return True, reason

    strong = role_cfg.get("strong") or []
    medium = role_cfg.get("medium") or []
    reject_list = profile.get("reject_role_keywords") or []

    if any(_has_kw(title_l, kw) for kw in strong):
        return False, ""

    for kw in reject_list:
        if _has_kw(title_l, kw):
            return True, f"title содержит '{kw}'"

    has_role = (
        any(_has_kw(title_l, kw) for kw in medium)
        or any(_has_kw(desc_l, kw) for kw in strong)
    )
    if has_role:
        return False, ""

    sk = profile.get("skills") or {}
    net = sum(1 for kw in (sk.get("network") or []) if _has_kw(desc_l, kw))
    infra = sum(1 for kw in (sk.get("infra") or []) if _has_kw(desc_l, kw))
    st = sum(1 for kw in (sk.get("storage") or []) if _has_kw(desc_l, kw))
    if (net + st) >= 4 and infra >= 2:
        return False, ""

    return True, "нет совпадений role/tech"


# ────────────────────────────────────────────────────────────────────────
# Scoring
# ────────────────────────────────────────────────────────────────────────

_SOFTWARE_MARKERS = (
    "software", "developer", "programmer",
    "java", "python", ".net", "c++", "golang", "kotlin",
    "frontend", "front-end", "backend", "back-end",
    "full stack", "fullstack", "mobile",
    "react", "angular", "vue", "node.js",
    "control systems", "space systems", "electrical systems",
    "embedded systems", "mechanical systems", "automotive",
    "avionics", "rf engineer", "hardware engineer",
    "databricks", "mlops", "spark engineer",
    "ai engineer", "ml engineer", "machine learning engineer",
    "data platform engineer", "data engineer",
    "ai platform", "ml platform",
)

_DEVOPS_CLOUD_MARKERS = (
    "devops aws", "devops azure", "devops gcp",
    "aws devops", "azure devops", "gcp devops",
    "cloud devops", "devops cloud",
    "devops engineer azure", "devops engineer aws", "devops engineer gcp",
    "devops (azure)", "devops (aws)", "devops (gcp)",
    "devops/cloud", "cloud/devops",
)


def score_job(job: Job, profile: dict) -> Breakdown:
    b = Breakdown()
    title_l = job.title.lower()
    text_l = f"{job.title}\n{job.description}".lower()

    role_cfg = profile.get("role_keywords") or {}
    strong = role_cfg.get("strong") or []
    medium = role_cfg.get("medium") or []

    strong_title = next((kw for kw in strong if _has_kw(title_l, kw)), None)
    medium_title = next((kw for kw in medium if _has_kw(title_l, kw)), None)
    strong_desc = next((kw for kw in strong if _has_kw(text_l, kw)), None)

    is_software = any(m in title_l for m in _SOFTWARE_MARKERS)
    is_devops_cloud = any(m in title_l for m in _DEVOPS_CLOUD_MARKERS)

    if strong_title and (is_software or is_devops_cloud):
        b.role = 16
        b.notes.append(f"role=16 (title '{strong_title}', specialty/software/devops-cloud)")
    elif strong_title:
        b.role = 40
        b.notes.append(f"role=40 (title: '{strong_title}')")
    elif medium_title and is_software:
        b.role = 8
        b.notes.append(f"role=8 (title '{medium_title}', specialty/software)")
    elif medium_title and is_devops_cloud:
        b.role = 10
        b.notes.append(f"role=10 (title '{medium_title}', devops-cloud)")
    elif medium_title:
        b.role = 24
        b.notes.append(f"role=24 (title: '{medium_title}')")
    elif strong_desc:
        b.role = 16
        b.notes.append(f"role=16 (desc: '{strong_desc}')")
    else:
        b.role = 6
        b.notes.append("role=6 (weak)")

    sk = profile.get("skills") or {}
    net_hits = [kw for kw in (sk.get("network") or []) if _has_kw(text_l, kw)]
    infra_hits = [kw for kw in (sk.get("infra") or []) if _has_kw(text_l, kw)]
    storage_hits = [kw for kw in (sk.get("storage") or []) if _has_kw(text_l, kw)]
    hw_hits = [kw for kw in (sk.get("huawei") or []) if _has_kw(text_l, kw)]

    net_pts = min(len(net_hits) * 2, 8)
    infra_pts = min(len(infra_hits) * 2, 10)
    storage_pts = min(len(storage_hits) * 3, 12)
    hw_pts = min(len(hw_hits) * 3, 9)
    b.tech = min(net_pts + infra_pts + storage_pts + hw_pts, 30)
    b.notes.append(
        f"tech={b.tech} (net {len(net_hits)}→{net_pts}, "
        f"infra {len(infra_hits)}→{infra_pts}, "
        f"storage {len(storage_hits)}→{storage_pts}, "
        f"huawei {len(hw_hits)}→{hw_pts})"
    )

    b.seniority = _seniority_score(job, profile, text_l, b)
    b.salary = _salary_score(job, profile.get("salary") or {}, b)
    b.location = _location_score(job, profile, b)

    for kw in (profile.get("penalty_keywords") or []):
        if _has_kw(text_l, kw):
            b.penalties -= 3
            b.notes.append(f"penalty -3 ('{kw}')")

    return b


_YEARS_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:years?|lat|років|роки|рок)", re.IGNORECASE)


def _seniority_score(job: Job, profile: dict, text_l: str, b: Breakdown) -> int:
    user_years = int((profile.get("seniority") or {}).get("user_years", 3))
    title_l = job.title.lower()

    # 1. JustJoin level
    level = (job.level or "").lower().strip()
    if level in ("senior", "lead", "principal", "staff"):
        b.notes.append(f"seniority=-8 (JustJoin: {level})")
        return -8
    if level == "junior":
        b.notes.append("seniority=-5 (JustJoin: junior)")
        return -5
    if level in ("mid", "regular", "middle"):
        b.notes.append("seniority=+15 (JustJoin: mid)")
        return 15

    # 2. "N+ years"
    required = None
    for m in _YEARS_RE.finditer(text_l):
        v = int(m.group(1))
        if 1 <= v <= 30:
            required = max(required or 0, v)

    if required is not None:
        if required >= 10:
            b.notes.append(f"seniority=-20 (требует {required}+ лет)")
            return -20
        if required >= 7:
            b.notes.append(f"seniority=-15 (требует {required}+ лет)")
            return -15
        if required >= 5:
            b.notes.append(f"seniority=-8 (требует {required}+ лет)")
            return -8
        if required <= user_years:
            b.notes.append(f"seniority=+12 (требует {required}+ лет, у тебя {user_years})")
            return 12
        b.notes.append(f"seniority=0 (требует {required}+ лет)")
        return 0

    # 3. По title
    sen = profile.get("seniority_keywords") or {}
    over = sen.get("overqualified") or []
    senior = sen.get("senior") or []
    junior = sen.get("junior") or []
    match = sen.get("match") or []

    if any(_has_kw(title_l, kw) for kw in over):
        b.notes.append("seniority=-15 (principal/architect/head)")
        return -15
    if any(_has_kw(title_l, kw) for kw in senior):
        b.notes.append("seniority=-8 (senior/lead в title)")
        return -8
    if any(_has_kw(title_l, kw) for kw in junior):
        b.notes.append("seniority=-5 (junior в title)")
        return -5
    if any(_has_kw(title_l, kw) for kw in match):
        b.notes.append("seniority=+15 (mid/regular)")
        return 15

    b.notes.append("seniority=+12 (без уровня → mid)")
    return 12


def _salary_score(job: Job, cfg: dict, b: Breakdown) -> int:
    lo = float(cfg.get("minimum", 12000))
    tgt = float(cfg.get("target", 16000))
    hi = float(cfg.get("very_good", 20000))

    if job.salary_max:
        val: float | None = float(job.salary_max)
    elif job.salary_min:
        val = float(job.salary_min)
    else:
        desc_for_salary = job.salary_text or job.description
        raw = _parse_salary(desc_for_salary)
        val = _to_monthly(raw, desc_for_salary) if raw is not None else None

    if val is None or val > MAX_REASONABLE_MONTHLY:
        b.notes.append("salary=0 (UNKNOWN → нейтрально)")
        return 0

    if val < lo:
        b.notes.append(f"salary=-15 (~{int(val)} < {int(lo)})")
        return -15
    if val < tgt:
        pts = round(-15 + 20 * (val - lo) / (tgt - lo))
        b.notes.append(f"salary={pts} (~{int(val)})")
        return pts
    if val < hi:
        pts = round(5 + 10 * (val - tgt) / (hi - tgt))
        b.notes.append(f"salary={pts} (~{int(val)})")
        return pts
    b.notes.append(f"salary=+15 (~{int(val)})")
    return 15


_NUM_RE = re.compile(r"(?<!\d)(\d{1,3}(?:[  \.,]\d{3})+|\d{4,7})(?!\d)")


def _parse_salary(text: str) -> float | None:
    if not text:
        return None
    tl = text.lower().replace("\xa0", " ")
    if not any(tok in tl for tok in ("pln", "zł", "zl", "zlot", "brutto", "netto")):
        return None
    cands: list[int] = []
    for m in _NUM_RE.finditer(tl):
        raw = re.sub(r"[^\d]", "", m.group(1))
        if not raw:
            continue
        try:
            v = int(raw)
        except ValueError:
            continue
        if 3000 <= v <= 1000000:
            cands.append(v)
    return float(max(cands)) if cands else None


# ────────────────────────────────────────────────────────────────────────
# Pipeline
# ────────────────────────────────────────────────────────────────────────

async def collect_all(profile: dict) -> list[Job]:
    jobs: list[Job] = []
    async with httpx.AsyncClient() as client:
        adz = await collect_adzuna(client, profile)
        console.print(f"[cyan]Adzuna:[/cyan] {len(adz)}")
        jobs.extend(adz)

    try:
        jj = await collect_justjoin(profile)
        jobs.extend(jj)
    except Exception as e:
        console.print(f"[yellow]JustJoin failed: {e}[/yellow]")

    return jobs


def process_jobs(raw_jobs: list[Job], profile: dict) -> tuple[list[Job], int]:
    unique = dedupe(raw_jobs)
    kept: list[Job] = []
    rejected = 0
    for job in unique:
        is_rej, _ = hard_reject(job, profile)
        if is_rej:
            rejected += 1
        else:
            kept.append(job)
    return kept, rejected


async def run_pipeline_quiet(profile: dict, store: Store) -> list[ScoredJob]:
    jobs: list[Job] = []
    async with httpx.AsyncClient() as client:
        adz = await collect_adzuna(client, profile)
        jobs.extend(adz)
    try:
        jj = await collect_justjoin(profile)
        jobs.extend(jj)
    except Exception as e:
        print(f"[warn] JustJoin failed: {e}")

    kept, _ = process_jobs(jobs, profile)
    scored: list[ScoredJob] = []
    for j in kept:
        b = score_job(j, profile)
        jid, is_new = store.upsert_job(j)
        store.save_score(jid, b, b.total)
        st = store.get_status(jid)
        if st in HIDDEN_STATUSES:
            continue
        scored.append(ScoredJob(job=j, breakdown=b, job_id=jid,
                                is_new=is_new, status=st))
    scored.sort(key=lambda s: s.total, reverse=True)
    return scored


async def run_pipeline(profile: dict, store: Store) -> None:
    console.print("[bold]Collecting…[/bold]")
    jobs = await collect_all(profile)
    console.print(f"[bold]Raw:[/bold] {len(jobs)}")

    unique = dedupe(jobs)
    console.print(f"[bold]Unique:[/bold] {len(unique)}")

    kept, rejected = process_jobs(jobs, profile)
    console.print(f"[bold]Rejected:[/bold] {rejected}  [bold]Kept:[/bold] {len(kept)}")

    scored: list[ScoredJob] = []
    new_count = 0
    for j in kept:
        b = score_job(j, profile)
        jid, is_new = store.upsert_job(j)
        store.save_score(jid, b, b.total)
        st = store.get_status(jid)
        if is_new:
            new_count += 1
        scored.append(ScoredJob(job=j, breakdown=b, job_id=jid,
                                is_new=is_new, status=st))

    scored.sort(key=lambda s: s.total, reverse=True)
    console.print(f"[bold]New since last run:[/bold] {new_count}")

    top = scored[:TOP_JOBS]
    print_table(top)
    print_details(top, n=10)
    save_json(scored)


# ────────────────────────────────────────────────────────────────────────
# Render
# ────────────────────────────────────────────────────────────────────────

_SRC_LABEL = {"adzuna": "Adz", "justjoin": "JJ"}


def _src_short(source: str) -> str:
    return _SRC_LABEL.get(source, source[:3])


def print_table(scored: list[ScoredJob]) -> None:
    table = Table(title=f"Top {len(scored)}", show_lines=False)
    table.add_column("#", justify="right", style="dim")
    table.add_column("Score", justify="right", style="bold green")
    table.add_column("Src", style="cyan")
    table.add_column("St", justify="left", style="magenta")
    table.add_column("Title")
    table.add_column("Company")
    table.add_column("Location")
    table.add_column("R/T/S/$/L/P", justify="right", style="dim")

    for i, s in enumerate(scored, 1):
        b = s.breakdown
        st = "NEW" if s.is_new else s.status[:4]
        table.add_row(
            str(i), str(s.total), _src_short(s.job.source), st,
            s.job.title[:55], s.job.company[:25], s.job.location[:25],
            f"{b.role}/{b.tech}/{b.seniority}/{b.salary}/{b.location}/{b.penalties}",
        )
    console.print(table)


def print_details(scored: list[ScoredJob], n: int = 10) -> None:
    console.rule("[bold]Details[/bold]")
    for i, s in enumerate(scored[:n], 1):
        b = s.breakdown
        st = "NEW" if s.is_new else s.status
        console.print(
            f"\n[bold cyan]{i}. {s.job.title}[/bold cyan]  "
            f"[bold green]{s.total}[/bold green]  "
            f"[magenta]{st}[/magenta]  [cyan]{s.job.source}[/cyan]"
        )
        console.print(f"   [dim]{s.job.company} — {s.job.location}[/dim]")
        console.print(f"   [link]{s.job.url}[/link]")
        console.print(
            f"   Role {b.role:+d} | Tech {b.tech:+d} | Sen {b.seniority:+d} | "
            f"$ {b.salary:+d} | Loc {b.location:+d} | Pen {b.penalties:+d}"
        )
        for note in b.notes:
            console.print(f"     [dim]• {note}[/dim]")


def save_json(scored: list[ScoredJob]) -> None:
    payload = []
    for s in scored:
        payload.append({
            "job_id": s.job_id,
            "job": {
                "source": s.job.source,
                "title": s.job.title,
                "company": s.job.company,
                "location": s.job.location,
                "url": s.job.url,
                "description": s.job.description[:4000],
                "salary_min": s.job.salary_min,
                "salary_max": s.job.salary_max,
                "salary_text": s.job.salary_text,
                "level": s.job.level,
            },
            "breakdown": asdict(s.breakdown),
            "total": s.total,
            "is_new": s.is_new,
            "status": s.status,
        })
    path = DATA_DIR / "last_run.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print(f"[dim]saved → {path}[/dim]")


# ────────────────────────────────────────────────────────────────────────
# CLI commands
# ────────────────────────────────────────────────────────────────────────

def cmd_list_new(store: Store) -> None:
    rows = store.list_new_since_last_run(limit=100)
    if not rows:
        console.print("[yellow]нет новых вакансий[/yellow]")
        return
    table = Table(title=f"New ({len(rows)})")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Src", style="cyan")
    table.add_column("Title")
    table.add_column("Company")
    table.add_column("Location")
    table.add_column("First seen", style="dim")
    for i, r in enumerate(rows, 1):
        table.add_row(str(i), (r["source"] or "")[:3], r["title"][:55],
                      (r["company"] or "")[:25], (r["location"] or "")[:25],
                      r["first_seen"][:10])
    console.print(table)


def cmd_list_status(store: Store, status: str) -> None:
    rows = store.list_by_status(status, limit=200)
    if not rows:
        console.print(f"[yellow]нет вакансий со статусом {status}[/yellow]")
        return
    table = Table(title=f"{status} ({len(rows)})")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Src", style="cyan")
    table.add_column("Title")
    table.add_column("Company")
    table.add_column("URL", style="dim")
    for i, r in enumerate(rows, 1):
        table.add_row(str(i), (r["source"] or "")[:3], r["title"][:50],
                      (r["company"] or "")[:25], r["url"][:60])
    console.print(table)


def cmd_mark(store: Store, url: str, status: str, notes: str) -> None:
    row = store.find_job_by_url(url)
    if not row:
        console.print(f"[red]не найдено вакансии с url ~ {url}[/red]")
        sys.exit(1)
    store.set_status(row["id"], status, notes)
    console.print(f"[green]✓[/green] {row['title'][:60]} → [bold]{status.upper()}[/bold]")
    if notes:
        console.print(f"  [dim]notes: {notes}[/dim]")


def cmd_stats(store: Store) -> None:
    s = store.stats()
    console.print(f"[bold]Total jobs:[/bold] {s['total_jobs']}")
    console.print(f"[bold]Last scored:[/bold] {s['last_score_at'] or '—'}")
    table = Table(title="By status")
    table.add_column("Status")
    table.add_column("Count", justify="right")
    for st, c in sorted(s["by_status"].items()):
        table.add_row(st, str(c))
    console.print(table)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="app.py", description="job-agent")
    p.add_argument("--list-new", action="store_true")
    p.add_argument("--list", metavar="STATUS")
    p.add_argument("--mark", nargs=2, metavar=("STATUS", "URL"))
    p.add_argument("--notes", default="")
    p.add_argument("--stats", action="store_true")
    p.add_argument("--close-stale", type=int, metavar="DAYS")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    store = Store()

    try:
        if args.stats:
            cmd_stats(store)
            return
        if args.list_new:
            cmd_list_new(store)
            return
        if args.list:
            cmd_list_status(store, args.list)
            return
        if args.mark:
            status, url = args.mark
            cmd_mark(store, url, status, args.notes)
            return
        if args.close_stale:
            n = store.mark_stale_as_closed(days=args.close_stale)
            console.print(f"[green]✓[/green] помечено REJECTED: {n}")
            return

        console.rule("[bold]JOB-AGENT[/bold]")
        profile = yaml.safe_load(PROFILE_PATH.read_text(encoding="utf-8"))
        asyncio.run(run_pipeline(profile, store))
    finally:
        store.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("[yellow]interrupted[/yellow]")
        sys.exit(1)