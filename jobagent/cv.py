"""CV из data/cv.md: краткая версия для /cv и «Акцент в CV» для карточки вакансии."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .config import DATA_DIR

CV_PATH = DATA_DIR / "cv.md"

_TOKEN = re.compile(r"[a-z0-9][a-z0-9+#./-]*[a-z0-9]|[a-z0-9]")
_STOP = set("""
the and for with that this from into over per all any our their your are was were been being have has had
will can may not but its it's his her you they them who what when how why while across each also
a an of in on to by as at or is be we i my me up out via using use used within under between
run ran caught built provide provided prepared support supports supported
""".split())
MIN_HIGHLIGHT_SCORE = 1.5     # суммарный вес совпавших технических терминов (редкие весят больше)


@dataclass
class Cv:
    title: str = ""
    summary: str = ""
    skills: list[tuple[str, str]] = field(default_factory=list)   # (раздел, значения)
    bullets: list[str] = field(default_factory=list)              # пункты Experience
    weights: dict[str, float] = field(default_factory=dict)       # слово -> вес (чем реже, тем выше)
    bullet_tokens: list[set[str]] = field(default_factory=list)   # только технические термины
    vocab: set[str] = field(default_factory=set)                  # технический словарь: Technical Skills из CV


def tokens(text: str) -> set[str]:
    out: set[str] = set()
    for t in _TOKEN.findall(text.lower()):
        t = t.strip(".-/")
        out.add(t)
        if "/" in t or "-" in t:          # ntp/chrony → ntp, chrony; time-sync → time, sync
            out.update(p for p in re.split(r"[/-]", t) if len(p) > 1)
    return out - _STOP - {""}


def _plain(s: str) -> str:
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    return s.replace("**", "").strip()


def _parse(md: str) -> Cv:
    cv = Cv()
    section = ""
    for raw in md.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            section = line[3:].strip().lower()
            continue
        if not line:
            continue
        if not section and line.startswith("**") and not cv.title:
            cv.title = _plain(line)
        elif section == "summary":
            cv.summary += (" " if cv.summary else "") + _plain(line)
        elif section == "technical skills" and line.startswith("- "):
            name, _, vals = _plain(line[2:]).partition(":")
            cv.skills.append((name.strip(), vals.strip()))
        elif section == "experience" and line.startswith("- "):
            cv.bullets.append(_plain(line[2:]))

    # Сравниваем только технические термины (раздел Technical Skills), иначе совпадают
    # общие слова вроде customers/production и в «Акценте» всегда одни и те же пункты
    cv.vocab = tokens(" ".join(f"{k} {v}" for k, v in cv.skills))
    try:                       # плюс скиллы из profile.yaml (OceanStor, CVE, HyperMetro и др.)
        from .config import load_profile
        sk = load_profile().get("skills") or {}
        cv.vocab |= tokens(" ".join((sk.get("strong") or []) + (sk.get("learning") or [])))
    except Exception:
        pass
    cv.vocab -= {"windows", "server", "servers", "maintenance", "support", "upgrade", "call", "automation", "on"}
    cv.bullet_tokens = [tokens(b) & cv.vocab for b in cv.bullets]
    n = max(len(cv.bullets), 1)
    df: dict[str, int] = {}
    for toks in cv.bullet_tokens:
        for t in toks:
            df[t] = df.get(t, 0) + 1
    # слово из одного пункта весит больше, чем «incident», встречающееся везде
    cv.weights = {t: math.log(1 + n / c) for t, c in df.items()}
    return cv


_cache: tuple[float, Cv] | None = None


def load_cv() -> Cv | None:
    global _cache
    if not CV_PATH.exists():
        return None
    mtime = CV_PATH.stat().st_mtime
    if _cache is None or _cache[0] != mtime:
        _cache = (mtime, _parse(CV_PATH.read_text(encoding="utf-8")))
    return _cache[1]


def short_bullet(b: str, limit: int = 95) -> str:
    """Начало пункта до первого разделителя, не длиннее limit."""
    cut = re.split(r"\s+[—–]\s+|;|:\s", b, maxsplit=1)[0]
    if len(cut) > limit:
        cut = cut[:limit].rsplit(" ", 1)[0] + "…"
    return cut.rstrip(".,")


def highlights(job_text: str, n: int = 2) -> list[str]:
    """1–2 пункта Experience с максимальным пересечением с текстом вакансии (без LLM)."""
    cv = load_cv()
    if not cv or not cv.bullets:
        return []
    job_toks = tokens(job_text)
    scored = []
    for b, toks in zip(cv.bullets, cv.bullet_tokens):
        common = toks & job_toks
        s = sum(cv.weights.get(t, 0) for t in common)
        if s >= MIN_HIGHLIGHT_SCORE:
            scored.append((s, b))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [short_bullet(b) for _, b in scored[:n]]
