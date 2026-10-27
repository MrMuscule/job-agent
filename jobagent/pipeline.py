"""Сбор → нормализация → фильтры → скоринг → SQLite."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .filters import hard_exclude, prefilter_justjoin
from .normalize import Job, dedupe, from_adzuna, from_justjoin, html_to_text
from .scoring import score_job
from .sources.adzuna import fetch_adzuna
from .sources.justjoin import fetch_justjoin_bodies, fetch_justjoin_list
from .store import Store, now

MIN_CACHED_DESC = 200     # короче — считаем, что описание не загружено


@dataclass
class CollectResult:
    raw_adzuna: int = 0
    raw_justjoin: int = 0
    unique: int = 0
    kept: int = 0
    new: int = 0
    excluded: int = 0
    closed: int = 0
    bodies_fetched: int = 0


async def collect(profile: dict, store: Store, log=print) -> CollectResult:
    res = CollectResult()
    bot_cfg = profile.get("bot") or {}
    eur, usd = float(bot_cfg.get("eur_pln", 4.3)), float(bot_cfg.get("usd_pln", 3.9))

    adz_raw, jj_raw = await asyncio.gather(
        fetch_adzuna(profile, log), fetch_justjoin_list(log=log))
    res.raw_adzuna, res.raw_justjoin = len(adz_raw), len(jj_raw)

    adz = [j for j in map(from_adzuna, adz_raw) if j]
    jj = [j for j in (from_justjoin(o, eur, usd) for o in jj_raw) if j]

    # JustJoin: полное описание тянем только для тех, кто прошёл предфильтр
    candidates = [j for j in jj if not prefilter_justjoin(j, profile)]
    need = [j.ext_id for j in candidates
            if len(store.known_description(j.id)) < MIN_CACHED_DESC]
    bodies = await fetch_justjoin_bodies(need, log) if need else {}
    res.bodies_fetched = len(bodies)
    for j in candidates:
        if j.ext_id in bodies:
            j.description = html_to_text(bodies[j.ext_id])
        else:
            j.description = store.known_description(j.id)

    # JustJoin раньше Adzuna: при дубле остаётся запись с полным описанием
    pool = candidates + adz
    for j in jj:
        if j not in candidates:
            store.mark_excluded(j.id, "предфильтр")
            res.excluded += 1

    unique = dedupe(pool)
    res.unique = len(unique)

    for job in unique:
        reason = hard_exclude(job, profile)
        if reason:
            store.mark_excluded(job.id, reason)
            res.excluded += 1
            continue
        if store.upsert(job, score_job(job, profile)):
            res.new += 1
        res.kept += 1

    res.closed = store.close_stale(14)
    store.set_meta("last_collect", now())
    return res
