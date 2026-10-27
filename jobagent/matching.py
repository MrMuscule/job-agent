"""Общие функции сопоставления ключевых слов."""
from __future__ import annotations

import re
from functools import lru_cache

_WORD = "a-z0-9ąćęłńóśźż"


@lru_cache(maxsize=None)
def _re(kw: str) -> re.Pattern:
    return re.compile(rf"(?<![{_WORD}])" + re.escape(kw.strip().lower()) + rf"(?![{_WORD}])")


def has(text_lower: str, kw: str) -> bool:
    return bool(_re(kw).search(text_lower))


def hits(text_lower: str, kws: list[str]) -> list[str]:
    seen: list[str] = []
    for kw in kws or []:
        k = kw.strip().lower()
        if k and k not in seen and has(text_lower, k):
            seen.append(k)
    return seen


def best_role(text_lower: str, profile: dict) -> tuple[int, str] | None:
    """(вес, паттерн) самого высокого совпавшего target_role."""
    best: tuple[int, str] | None = None
    for role in profile.get("target_roles") or []:
        w = int(role.get("weight", 0))
        for p in role.get("patterns") or []:
            if has(text_lower, p) and (best is None or w > best[0]):
                best = (w, p)
    return best
