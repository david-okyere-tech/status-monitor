"""Append-only JSONL plus a SQLite store for uptime and percentiles."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Optional

from status_monitor.models import HISTORY_FILENAME, SQLITE_FILENAME, CheckResult


class HistoryStore:
    def __init__(self, log_dir: str):
        os.makedirs(log_dir, exist_ok=True)
        self.path = os.path.join(log_dir, SQLITE_FILENAME)
        self.jsonl_path = os.path.join(log_dir, HISTORY_FILENAME)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._migrate()
        self._jsonl = open(self.jsonl_path, "a", encoding="utf-8")

    def close(self) -> None:
        with self._lock:
            try:
                self._jsonl.close()
            except OSError:
                pass
            self._conn.close()

    def record_results(self, results: list[CheckResult]) -> None:
        with self._lock:
            self._write_jsonl(results)
            try:
                self._conn.execute("BEGIN")
                for result in results:
                    self._insert_check(result)
                    self._touch_incident(result)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def prune(self, before_unix: float) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM checks WHERE ts_unix < ?", (before_unix,))
            self._conn.execute(
                "DELETE FROM incidents WHERE ended_unix IS NOT NULL AND ended_unix < ?",
                (before_unix,),
            )
            self._conn.commit()

    def counts(self, url: Optional[str], since: Optional[float]) -> dict[str, int]:
        query = "SELECT status, COUNT(*) AS n FROM checks WHERE 1=1"
        args: list = []
        if url is not None:
            query += " AND url = ?"
            args.append(url)
        if since is not None:
            query += " AND ts_unix >= ?"
            args.append(since)
        query += " GROUP BY status"
        with self._lock:
            rows = self._conn.execute(query, args).fetchall()
        return {row["status"]: int(row["n"]) for row in rows}

    def latencies(self, url: Optional[str], since: Optional[float], limit: int = 2000) -> list[float]:
        query = "SELECT response_time_ms FROM checks WHERE response_time_ms IS NOT NULL"
        args: list = []
        if url is not None:
            query += " AND url = ?"
            args.append(url)
        if since is not None:
            query += " AND ts_unix >= ?"
            args.append(since)
        query += " ORDER BY ts_unix DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(query, args).fetchall()
        return [float(row["response_time_ms"]) for row in rows]

    def recent(self, url: str, limit: int = 40) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT status, response_time_ms, ts
                FROM checks
                WHERE url = ?
                ORDER BY ts_unix DESC
                LIMIT ?
                """,
                (url, limit),
            ).fetchall()
        items = [
            {"status": row["status"], "ms": row["response_time_ms"], "ts": row["ts"]}
            for row in rows
        ]
        items.reverse()
        return items

    def latest(self, url: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT status, status_code, response_time_ms, error, ts, ssl_days_remaining, name
                FROM checks WHERE url = ? ORDER BY ts_unix DESC LIMIT 1
                """,
                (url,),
            ).fetchone()
        if row is None:
            return None
        return dict(row)

    def open_incident_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM incidents WHERE ended_unix IS NULL"
            ).fetchone()
        return int(row["n"])

    def incidents(self, limit: int = 8) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT url, name, started_at, ended_at, error
                FROM incidents
                ORDER BY (ended_unix IS NULL) DESC, started_unix DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def urls(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT url, name, check_type
                FROM checks
                WHERE id IN (SELECT MAX(id) FROM checks GROUP BY url)
                ORDER BY url
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def _migrate(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS checks (
                id INTEGER PRIMARY KEY,
                url TEXT NOT NULL,
                name TEXT NOT NULL,
                check_type TEXT NOT NULL,
                ts TEXT NOT NULL,
                ts_unix REAL NOT NULL,
                status TEXT NOT NULL,
                status_code INTEGER,
                response_time_ms REAL,
                error TEXT,
                attempt INTEGER NOT NULL,
                ssl_days_remaining INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_checks_url_ts ON checks (url, ts_unix);
            CREATE INDEX IF NOT EXISTS idx_checks_ts ON checks (ts_unix);
            CREATE TABLE IF NOT EXISTS incidents (
                id INTEGER PRIMARY KEY,
                url TEXT NOT NULL,
                name TEXT NOT NULL,
                started_at TEXT NOT NULL,
                started_unix REAL NOT NULL,
                ended_at TEXT,
                ended_unix REAL,
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_incidents_url ON incidents (url, ended_unix);
            """
        )
        self._conn.commit()

    def _insert_check(self, result: CheckResult) -> None:
        self._conn.execute(
            """
            INSERT INTO checks (
                url, name, check_type, ts, ts_unix, status, status_code,
                response_time_ms, error, attempt, ssl_days_remaining
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.url,
                result.name or result.url,
                result.check_type,
                result.timestamp,
                result.ts_unix,
                result.status,
                result.status_code,
                result.response_time_ms,
                result.error,
                result.attempt,
                result.ssl_days_remaining,
            ),
        )

    def _touch_incident(self, result: CheckResult) -> None:
        open_row = self._conn.execute(
            "SELECT id FROM incidents WHERE url = ? AND ended_unix IS NULL LIMIT 1",
            (result.url,),
        ).fetchone()
        if result.status == "DOWN":
            if open_row is None:
                self._conn.execute(
                    """
                    INSERT INTO incidents (url, name, started_at, started_unix, error)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        result.url,
                        result.name or result.url,
                        result.timestamp,
                        result.ts_unix,
                        result.error,
                    ),
                )
            return
        if open_row is not None:
            self._conn.execute(
                """
                UPDATE incidents
                SET ended_at = ?, ended_unix = ?
                WHERE url = ? AND ended_unix IS NULL
                """,
                (result.timestamp, result.ts_unix, result.url),
            )

    def _write_jsonl(self, results: list[CheckResult]) -> None:
        if not results:
            return
        payload = "".join(json.dumps(result.to_dict()) + "\n" for result in results)
        self._jsonl.write(payload)
        self._jsonl.flush()
