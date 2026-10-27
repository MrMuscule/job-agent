"""Скоринг вакансии по profile.yaml. Детерминированный, с объяснением."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .matching import best_role, has, hits
from .normalize import Job

ROLE_TITLE_MULT = 4        # вес роли × 4 при совпадении в title
ROLE_DESC_MULT = 2         # × 2 при совпадении только в описании
STRONG_PTS, STRONG_CAP = 3, 15
LEARN_PTS, LEARN_CAP = 1, 5
GAP_PTS, GAP_CAP = -5, -10
SENIOR_PENALTY = -8
YEARS_PENALTY = -6
LEVEL_OK_BONUS = 2

_YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:years?|yrs?|lat|lata|roku)\b")
# В описании (а не в поле workplace) принимаем только однозначные формулировки
_REMOTE_STRONG = ("fully remote", "100% remote", "100 % remote", "100% zdaln", "100 % zdaln",
                  "w pełni zdaln", "całkowicie zdaln", "remote-first", "remote first")


@dataclass
class Score:
    total: int = 0
    reasons: list[tuple[int, str]] = field(default_factory=list)

    def add(self, pts: int, why: str) -> None:
        self.total += pts
        self.reasons.append((pts, why))

    def top_reasons(self, n: int = 3) -> list[tuple[int, str]]:
        return sorted(self.reasons, key=lambda r: abs(r[0]), reverse=True)[:n]


def _clean_title(title_l: str, profile: dict) -> str:
    """Убираем из title фразы вида 'mid/senior', чтобы они не давали штраф за senior."""
    for ok in (profile.get("seniority") or {}).get("ok") or []:
        if "/" in ok:
            title_l = title_l.replace(ok.lower(), " ")
    return title_l


def is_remote(job: Job, profile: dict) -> bool:
    if job.workplace == "remote":
        return True
    loc = (profile.get("location") or {})
    head = f"{job.title}\n{job.location}".lower()
    modes = [a.lower() for a in loc.get("accept") or []
             if a.lower() not in ("hybrid", "hybrydowo") and a.lower() not in _home_tokens(profile)]
    if any(has(head, m) for m in modes):
        return True
    return any(m in job.description.lower() for m in _REMOTE_STRONG)


def _home_tokens(profile: dict) -> list[str]:
    home = ((profile.get("location") or {}).get("home") or "").lower()
    toks = {home, "warsaw", "warszawa"} if home in ("warszawa", "warsaw") else {home}
    return [t for t in toks if t]


def score_job(job: Job, profile: dict) -> Score:
    sc = Score()
    title_l = job.title.lower()
    text_l = job.text
    skills = profile.get("skills") or {}

    # ── роль ──
    in_title = best_role(title_l, profile)
    if in_title:
        sc.add(in_title[0] * ROLE_TITLE_MULT, f"роль «{in_title[1]}» в названии")
    else:
        in_desc = best_role(text_l, profile)
        if in_desc:
            sc.add(in_desc[0] * ROLE_DESC_MULT, f"роль «{in_desc[1]}» в описании")

    # ── скиллы ──
    strong = hits(text_l, skills.get("strong") or [])
    if strong:
        sc.add(min(len(strong) * STRONG_PTS, STRONG_CAP), "скиллы: " + ", ".join(strong[:6]))
    learn = hits(text_l, skills.get("learning") or [])
    if learn:
        sc.add(min(len(learn) * LEARN_PTS, LEARN_CAP), "изучаю: " + ", ".join(learn[:4]))

    head = f"{title_l}\n{' '.join(job.skills).lower()}\n{job.description[:500].lower()}"
    gaps = [g for g in hits(head, skills.get("gaps") or [])]
    if gaps:
        sc.add(max(len(gaps) * GAP_PTS, GAP_CAP), "не мой стек в требованиях: " + ", ".join(gaps))

    # ── уровень ──
    sen = profile.get("seniority") or {}
    t_clean = _clean_title(title_l, profile)
    pen = hits(t_clean, sen.get("penalty") or [])
    level_senior = job.level in ("senior", "lead", "c_level", "expert", "manager")
    if pen:
        sc.add(SENIOR_PENALTY, "уровень: " + pen[0])
    elif level_senior:
        sc.add(SENIOR_PENALTY, f"уровень: {job.level}")
    elif job.level in ("junior", "mid", "regular") or hits(t_clean, sen.get("ok") or []):
        sc.add(LEVEL_OK_BONUS, "уровень подходит")

    max_years = int(sen.get("max_years_required", 4))
    req = max((int(m.group(1)) for m in _YEARS.finditer(job.description.lower())
               if 1 <= int(m.group(1)) <= 20), default=0)
    if req > max_years:
        sc.add(YEARS_PENALTY, f"требуют {req}+ лет")

    # ── зарплата ──
    sal = profile.get("salary") or {}
    bonus_cfg = sal.get("above_target_bonus") or {}
    for o in job.offers:
        top = o.hi if o.hi is not None else o.lo
        if top is None:
            continue
        target = float(bonus_cfg.get("from_b2b_net" if o.kind == "b2b" else "from_uop_gross", 10 ** 9))
        if top >= target:
            sc.add(int(bonus_cfg.get("points", 4)), f"вилка до {int(top)} ({o.kind})")
            break

    # ── локация ──
    loc_l = job.location.lower()
    home = _home_tokens(profile)
    if any(h in loc_l for h in home):
        sc.reasons.append((0, "Варшава"))
    elif is_remote(job, profile):
        sc.reasons.append((0, "remote"))
    elif loc_l in ("", "polska", "poland"):
        pass
    else:
        sc.add(int((profile.get("location") or {}).get("far_onsite_penalty", -10)),
               f"офис не в Варшаве ({job.location})")

    # ── бонусы ──
    bonus_text = f"{text_l}\n{job.company.lower()}"
    for b in profile.get("bonus") or []:
        if re.search(b["pattern"], bonus_text, re.IGNORECASE):
            sc.add(int(b["points"]), b.get("why", b["pattern"]))

    return sc
