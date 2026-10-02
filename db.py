"""
SQLite storage для job-agent.

Таблицы:
  jobs    — все вакансии, что мы видели
  scores  — история скоринга
  status  — состояние по вакансии (NEW/SEEN/APPLIED/...)

URL Adzuna содержит session-токены (utm_source, se, v), которые меняются
между запусками. Поэтому job_id строится на числовом ID вакансии, а не на
полном URL.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "jobs.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    title           TEXT NOT NULL,
    company         TEXT,
    location        TEXT,
    url             TEXT NOT NULL,
    description     TEXT,
    salary_min      REAL,
    salary_max      REAL,
    salary_text     TEXT,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scores (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL,
    score           INTEGER NOT NULL,
    role            INTEGER NOT NULL,
    tech            INTEGER NOT NULL,
    seniority       INTEGER NOT NULL,
    salary          INTEGER NOT NULL,
    penalties       INTEGER NOT NULL,
    timestamp       TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);

CREATE TABLE IF NOT EXISTS status (
    job_id          TEXT PRIMARY KEY,
    status          TEXT NOT NULL DEFAULT 'NEW',
    notes           TEXT DEFAULT '',
    updated_at      TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);

CREATE INDEX IF NOT EXISTS idx_scores_job ON scores(job_id);
CREATE INDEX IF NOT EXISTS idx_status_status ON status(status);
CREATE INDEX IF NOT EXISTS idx_jobs_last_seen ON jobs(last_seen);
"""

VALID_STATUSES = ("NEW", "SEEN", "SHORTLIST", "APPLIED",
                  "REJECTED", "INTERVIEW", "OFFER")

# Числовой ID вакансии в URL Adzuna: /details/5876475415 или /land/ad/5876475415
_NUMERIC_ID_RE = re.compile(r"/(\d{6,})(?:[/?#]|$)")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _extract_numeric_id(url: str) -> str | None:
    """Достаёт стабильный числовой ID вакансии из URL Adzuna."""
    if not url:
        return None
    m = _NUMERIC_ID_RE.search(url)
    return m.group(1) if m else None


def _url_key(url: str) -> str:
    """Нормализованный URL: без query/fragment, lowercase, без trailing slash."""
    if not url:
        return ""
    return url.strip().lower().split("#")[0].split("?")[0].rstrip("/")


def job_id(url: str, title: str, company: str) -> str:
    """Стабильный id вакансии.

    Приоритет:
      1. Числовой ID из URL Adzuna — не зависит от session-токенов.
      2. Fallback — нормализованный URL + title + company.
    """
    num = _extract_numeric_id(url)
    if num:
        raw = f"adzuna:{num}"
    else:
        ukey = _url_key(url)
        raw = f"{ukey}|{(title or '').strip().lower()}|{(company or '').strip().lower()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


class Store:
    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ── jobs ────────────────────────────────────────────────────────────

    def upsert_job(self, job) -> tuple[str, bool]:
        """Возвращает (job_id, is_new)."""
        jid = job_id(job.url, job.title, job.company)
        now = _now()
        row = self.conn.execute("SELECT first_seen FROM jobs WHERE id=?", (jid,)).fetchone()
        is_new = row is None

        if is_new:
            self.conn.execute(
                """INSERT INTO jobs (id, source, title, company, location, url,
                    description, salary_min, salary_max, salary_text,
                    first_seen, last_seen)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (jid, job.source, job.title, job.company, job.location, job.url,
                 job.description, job.salary_min, job.salary_max, job.salary_text,
                 now, now),
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO status (job_id, status, notes, updated_at) VALUES (?,?,?,?)",
                (jid, "NEW", "", now),
            )
        else:
            self.conn.execute(
                """UPDATE jobs SET last_seen=?, url=?, salary_min=?, salary_max=?,
                    salary_text=?, description=?
                WHERE id=?""",
                (now, job.url, job.salary_min, job.salary_max, job.salary_text,
                 job.description, jid),
            )
        self.conn.commit()
        return jid, is_new

    def save_score(self, jid: str, b, total: int) -> None:
        self.conn.execute(
            """INSERT INTO scores (job_id, score, role, tech, seniority,
                salary, penalties, timestamp)
            VALUES (?,?,?,?,?,?,?,?)""",
            (jid, total, b.role, b.tech, b.seniority, b.salary, b.penalties, _now()),
        )
        self.conn.commit()

    # ── status ──────────────────────────────────────────────────────────

    def set_status(self, jid: str, status: str, notes: str = "") -> None:
        status = status.upper()
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid status '{status}'. valid: {', '.join(VALID_STATUSES)}")
        self.conn.execute(
            """INSERT INTO status (job_id, status, notes, updated_at)
            VALUES (?,?,?,?)
            ON CONFLICT(job_id) DO UPDATE SET
                status=excluded.status,
                notes=CASE WHEN excluded.notes != '' THEN excluded.notes ELSE status.notes END,
                updated_at=excluded.updated_at""",
            (jid, status, notes, _now()),
        )
        self.conn.commit()

    def get_status(self, jid: str) -> str:
        row = self.conn.execute("SELECT status FROM status WHERE job_id=?", (jid,)).fetchone()
        return row["status"] if row else "NEW"

    # ── queries ─────────────────────────────────────────────────────────

    def find_job_by_url(self, url: str) -> sqlite3.Row | None:
        """Ищет вакансию по URL. Игнорирует query-параметры и session-токены."""
        # 1. По нормализованному URL (без query)
        key = _url_key(url)
        if key:
            row = self.conn.execute(
                "SELECT * FROM jobs WHERE LOWER(url) LIKE ? LIMIT 1",
                (f"{key}%",),
            ).fetchone()
            if row:
                return row

        # 2. По числовому ID вакансии — работает даже если в БД URL с другими query
        num = _extract_numeric_id(url)
        if num:
            row = self.conn.execute(
                "SELECT * FROM jobs WHERE url LIKE ? LIMIT 1",
                (f"%/{num}%",),
            ).fetchone()
            if row:
                return row

        # 3. По job_id — на случай, если передан уже хэш
        if len(url) == 16 and all(c in "0123456789abcdef" for c in url.lower()):
            return self.conn.execute("SELECT * FROM jobs WHERE id=?", (url.lower(),)).fetchone()

        return None

    def list_by_status(self, status: str, limit: int = 100) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT j.*, s.status AS job_status, s.notes AS job_notes
            FROM jobs j JOIN status s ON s.job_id = j.id
            WHERE s.status = ?
            ORDER BY j.last_seen DESC LIMIT ?""",
            (status.upper(), limit),
        ).fetchall()

    def list_new_since_last_run(self, limit: int = 50) -> list[sqlite3.Row]:
        """Вакансии, у которых first_seen = last_seen (появились только что)."""
        return self.conn.execute(
            """SELECT j.*, s.status AS job_status
            FROM jobs j JOIN status s ON s.job_id = j.id
            WHERE j.first_seen = j.last_seen
            ORDER BY j.last_seen DESC LIMIT ?""",
            (limit,),
        ).fetchall()

    def stats(self) -> dict:
        total = self.conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"]
        by_status = {
            row["status"]: row["c"]
            for row in self.conn.execute(
                "SELECT status, COUNT(*) AS c FROM status GROUP BY status"
            ).fetchall()
        }
        last_run = self.conn.execute(
            "SELECT MAX(timestamp) AS t FROM scores"
        ).fetchone()["t"]
        return {"total_jobs": total, "by_status": by_status, "last_score_at": last_run}

    def mark_stale_as_closed(self, days: int = 14) -> int:
        """Вакансии, не появлявшиеся N дней, — помечаем REJECTED (если статус SEEN)."""
        cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
        cur = self.conn.execute(
            """UPDATE status SET status='REJECTED', updated_at=?
            WHERE status='SEEN' AND job_id IN (
                SELECT id FROM jobs WHERE last_seen < ?
            )""",
            (_now(), datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat(timespec="seconds")),
        )
        self.conn.commit()
        return cur.rowcount