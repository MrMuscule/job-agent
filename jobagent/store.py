"""SQLite-хранилище."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import DB_PATH
from .normalize import Job, SalaryOffer, salary_label
from .scoring import Score

VALID_STATUSES = ("NEW", "SEEN", "SHORTLIST", "APPLIED", "SKIP",
                  "INTERVIEW", "OFFER", "CLOSED")
# Эти статусы не попадают в /top и /all
HIDDEN_STATUSES = ("SKIP", "APPLIED", "INTERVIEW", "OFFER", "CLOSED")
# Только эти статусы автоматически закрываются, когда вакансия пропала из выдачи
AUTO_CLOSE_FROM = ("NEW", "SEEN")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,           -- <source>:<ext_id>
    source        TEXT NOT NULL,
    ext_id        TEXT NOT NULL,
    title         TEXT NOT NULL,
    company       TEXT,
    location      TEXT,
    workplace     TEXT,
    url           TEXT NOT NULL,
    description   TEXT,
    level         TEXT,
    offers_json   TEXT,
    salary_label  TEXT,
    score         INTEGER,
    reasons_json  TEXT,
    excluded      TEXT,                        -- причина жёсткого отсева или NULL
    notified_at   TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS status (
    job_id     TEXT PRIMARY KEY REFERENCES jobs(id),
    status     TEXT NOT NULL DEFAULT 'NEW',
    notes      TEXT DEFAULT '',
    updated_at TEXT NOT NULL,
    applied_at TEXT
);
CREATE TABLE IF NOT EXISTS legacy_skip (url TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(score);
CREATE INDEX IF NOT EXISTS idx_status_status ON status(status);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def url_key(url: str) -> str:
    return (url or "").strip().lower().split("#")[0].split("?")[0].rstrip("/")


def _migrate_legacy(path: Path) -> list[str]:
    """Старая схема (id = sha1, без ext_id) → переименовать, вернуть URL'ы со статусом REJECTED."""
    if not path.exists():
        return []
    con = sqlite3.connect(path)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(jobs)")]
        if not cols or "ext_id" in cols:
            return []
        rows = con.execute(
            "SELECT j.url FROM jobs j JOIN status s ON s.job_id=j.id WHERE s.status='REJECTED'"
        ).fetchall()
        urls = [r[0] for r in rows]
    finally:
        con.close()
    legacy = path.with_name("jobs.legacy.db")
    if legacy.exists():
        legacy.unlink()
    path.rename(legacy)
    return urls


class Store:
    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(exist_ok=True)
        legacy_urls = _migrate_legacy(path)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        for u in legacy_urls:
            self.conn.execute("INSERT OR IGNORE INTO legacy_skip(url) VALUES (?)", (url_key(u),))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ── запись ──

    def known_description(self, jid: str) -> str:
        row = self.conn.execute("SELECT description FROM jobs WHERE id=?", (jid,)).fetchone()
        return (row["description"] or "") if row else ""

    def upsert(self, job: Job, score: Score) -> bool:
        """Сохраняет вакансию и оценку. True, если вакансия новая."""
        ts = now()
        offers = json.dumps([asdict(o) for o in job.offers], ensure_ascii=False)
        reasons = json.dumps(score.reasons, ensure_ascii=False)
        label = salary_label(job.offers)
        exists = self.conn.execute("SELECT 1 FROM jobs WHERE id=?", (job.id,)).fetchone()

        if exists:
            self.conn.execute(
                """UPDATE jobs SET title=?, company=?, location=?, workplace=?, url=?,
                       description=CASE WHEN length(?) >= length(COALESCE(description,'')) THEN ? ELSE description END,
                       level=?, offers_json=?, salary_label=?, score=?, reasons_json=?,
                       excluded=NULL, last_seen=? WHERE id=?""",
                (job.title, job.company, job.location, job.workplace, job.url,
                 job.description, job.description, job.level, offers, label,
                 score.total, reasons, ts, job.id),
            )
        else:
            self.conn.execute(
                """INSERT INTO jobs (id, source, ext_id, title, company, location, workplace,
                       url, description, level, offers_json, salary_label, score, reasons_json,
                       first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job.id, job.source, job.ext_id, job.title, job.company, job.location,
                 job.workplace, job.url, job.description, job.level, offers, label,
                 score.total, reasons, ts, ts),
            )
            skipped = self.conn.execute(
                "SELECT 1 FROM legacy_skip WHERE url=?", (url_key(job.url),)).fetchone()
            self.conn.execute(
                "INSERT INTO status (job_id, status, updated_at) VALUES (?,?,?)",
                (job.id, "SKIP" if skipped else "NEW", ts),
            )
        self.conn.commit()
        return not exists

    def mark_excluded(self, jid: str, reason: str) -> None:
        """Вакансия, которая раньше проходила, но по текущему профилю уже нет."""
        self.conn.execute("UPDATE jobs SET excluded=? WHERE id=?", (reason, jid))
        self.conn.commit()

    def set_status(self, jid: str, status: str, notes: str = "") -> None:
        status = status.upper()
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status '{status}'. valid: {', '.join(VALID_STATUSES)}")
        ts = now()
        applied = ts if status == "APPLIED" else None
        self.conn.execute(
            """INSERT INTO status (job_id, status, notes, updated_at, applied_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(job_id) DO UPDATE SET
                 status=excluded.status,
                 notes=CASE WHEN excluded.notes != '' THEN excluded.notes ELSE status.notes END,
                 updated_at=excluded.updated_at,
                 applied_at=CASE WHEN excluded.status='APPLIED'
                                 THEN COALESCE(status.applied_at, excluded.applied_at)
                                 ELSE status.applied_at END""",
            (jid, status, notes, ts, applied),
        )
        self.conn.commit()

    def close_stale(self, days: int = 14) -> int:
        """Пропавшие из выдачи → CLOSED. Решения пользователя не трогаем."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        marks = ",".join("?" * len(AUTO_CLOSE_FROM))
        cur = self.conn.execute(
            f"""UPDATE status SET status='CLOSED', updated_at=?
                WHERE status IN ({marks})
                  AND job_id IN (SELECT id FROM jobs WHERE last_seen < ?)""",
            (now(), *AUTO_CLOSE_FROM, cutoff),
        )
        self.conn.commit()
        return cur.rowcount

    def mark_notified(self, ids: list[str]) -> None:
        self.conn.executemany("UPDATE jobs SET notified_at=? WHERE id=?",
                              [(now(), i) for i in ids])
        self.conn.commit()

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, value))
        self.conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    # ── чтение ──

    _SELECT = ("SELECT j.*, s.status AS status, s.notes AS notes, s.applied_at AS applied_at "
               "FROM jobs j JOIN status s ON s.job_id = j.id ")

    def get(self, jid: str) -> sqlite3.Row | None:
        return self.conn.execute(self._SELECT + "WHERE j.id=?", (jid,)).fetchone()

    def find_by_url(self, url: str) -> sqlite3.Row | None:
        key = url_key(url)
        if not key:
            return None
        return self.conn.execute(
            self._SELECT + "WHERE LOWER(j.url) LIKE ? LIMIT 1", (f"{key}%",)).fetchone()

    def ranked(self, limit: int, offset: int = 0) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(HIDDEN_STATUSES))
        return self.conn.execute(
            self._SELECT + f"""WHERE j.excluded IS NULL AND s.status NOT IN ({marks})
                ORDER BY j.score DESC, j.first_seen DESC LIMIT ? OFFSET ?""",
            (*HIDDEN_STATUSES, limit, offset),
        ).fetchall()

    def count_ranked(self) -> int:
        marks = ",".join("?" * len(HIDDEN_STATUSES))
        return self.conn.execute(
            f"""SELECT COUNT(*) FROM jobs j JOIN status s ON s.job_id=j.id
                WHERE j.excluded IS NULL AND s.status NOT IN ({marks})""",
            HIDDEN_STATUSES,
        ).fetchone()[0]

    def list_by_status(self, status: str, limit: int = 30) -> list[sqlite3.Row]:
        return self.conn.execute(
            self._SELECT + "WHERE s.status=? ORDER BY s.updated_at DESC LIMIT ?",
            (status.upper(), limit),
        ).fetchall()

    def applied(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            self._SELECT + "WHERE s.status IN ('APPLIED','INTERVIEW','OFFER') "
            "ORDER BY s.applied_at DESC"
        ).fetchall()

    def new_jobs(self, limit: int = 30) -> list[sqlite3.Row]:
        """Новые с последнего сбора (ещё не просмотренные)."""
        return self.conn.execute(
            self._SELECT + """WHERE j.excluded IS NULL AND s.status='NEW'
               ORDER BY j.first_seen DESC, j.score DESC LIMIT ?""",
            (limit,),
        ).fetchall()

    def to_notify(self, min_score: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            self._SELECT + """WHERE j.excluded IS NULL AND j.notified_at IS NULL
               AND s.status='NEW' AND j.score >= ? ORDER BY j.score DESC""",
            (min_score,),
        ).fetchall()

    def search(self, query: str, limit: int = 30) -> list[sqlite3.Row]:
        q = f"%{query.lower()}%"
        return self.conn.execute(
            self._SELECT + """WHERE j.excluded IS NULL AND (LOWER(j.title) LIKE ?
               OR LOWER(j.company) LIKE ? OR LOWER(j.description) LIKE ?)
               ORDER BY j.score DESC LIMIT ?""",
            (q, q, q, limit),
        ).fetchall()

    def stats(self) -> dict:
        c = self.conn
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
        by_status = {r["status"]: r["n"] for r in c.execute(
            """SELECT s.status, COUNT(*) n FROM status s JOIN jobs j ON j.id=s.job_id
               WHERE j.excluded IS NULL GROUP BY s.status""")}
        return {
            "total": sum(by_status.values()),
            "by_status": by_status,
            "applied_week": c.execute(
                "SELECT COUNT(*) FROM status WHERE applied_at >= ?", (week_ago,)).fetchone()[0],
            "last_collect": self.get_meta("last_collect"),
        }


def offers_of(row: sqlite3.Row) -> list[SalaryOffer]:
    return [SalaryOffer(**o) for o in json.loads(row["offers_json"] or "[]")]
