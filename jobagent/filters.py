"""Жёсткие фильтры: что не показывать вообще."""
from __future__ import annotations

from .matching import best_role, has, hits
from .normalize import Job, SalaryOffer


def salary_threshold(kind: str, profile: dict) -> float:
    cfg = profile.get("salary") or {}
    if kind == "b2b":
        return float(cfg.get("min_b2b_net", 16000))
    # uop и неизвестный тип сравниваем с порогом UoP (он ниже — не режем лишнего)
    return float(cfg.get("min_uop_gross", 13000))


def offer_top(o: SalaryOffer) -> float | None:
    return o.hi if o.hi is not None else None


def below_salary_min(job: Job, profile: dict) -> str | None:
    """Сравниваем ВЕРХ вилки с минимумом. Нет вилки или нет верха → не режем."""
    cfg = profile.get("salary") or {}
    if cfg.get("below_min", "exclude") != "exclude" or not job.offers:
        return None
    tops = []
    for o in job.offers:
        thr = salary_threshold(o.kind, profile)
        top = offer_top(o)
        if top is None:          # «от X» без верха — неизвестно
            return None
        if top >= thr:
            return None
        tops.append(f"{int(top)}<{int(thr)} {o.kind}")
    return "зарплата ниже минимума (" + ", ".join(tops) + ")"


def excluded_title(job: Job, profile: dict) -> str | None:
    t = job.title.lower()
    for kw in profile.get("exclude_titles") or []:
        if has(t, kw):
            return f"title содержит '{kw}'"
    return None


def relevant(job: Job, profile: dict) -> str | None:
    """None, если вакансия по профилю подходит; иначе причина отказа."""
    if best_role(job.title.lower(), profile):
        return None
    strong = hits(job.text, (profile.get("skills") or {}).get("strong") or [])
    if best_role(job.text, profile) and len(strong) >= 3:
        return None
    return "нет подходящей роли в title"


def hard_exclude(job: Job, profile: dict) -> str | None:
    return (excluded_title(job, profile)
            or below_salary_min(job, profile)
            or relevant(job, profile))


def prefilter_justjoin(job: Job, profile: dict) -> str | None:
    """До загрузки полного описания: title/зарплата + роль в title или 3+ скилла из списка."""
    reason = excluded_title(job, profile) or below_salary_min(job, profile)
    if reason:
        return reason
    if best_role(job.title.lower(), profile):
        return None
    strong = hits(job.text, (profile.get("skills") or {}).get("strong") or [])
    return None if len(strong) >= 3 else "нет подходящей роли в title"
