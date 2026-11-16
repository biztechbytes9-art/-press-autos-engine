"""SQLite memory: what we have seen, drafted and spent. Lives in state/engine.db (committed by the workflow)."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    url TEXT PRIMARY KEY,
    title TEXT,
    domain TEXT,
    source TEXT,
    published TEXT,
    seen_at TEXT
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_key TEXT,
    title TEXT,
    category TEXT,
    wp_id INTEGER,
    status TEXT,
    notes TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS rejected (
    cluster_key TEXT PRIMARY KEY,
    reason TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS spend (
    day TEXT,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    usd REAL
);
"""


def now_iso() -> str:
    # Same format as SQLite's datetime('now') so string comparisons work.
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(drafts)")}
        if "edition" not in cols:  # upgrade older memory files
            self.db.execute("ALTER TABLE drafts ADD COLUMN edition TEXT DEFAULT 'global'")

    # items -----------------------------------------------------------
    def is_seen(self, url: str) -> bool:
        return self.db.execute("SELECT 1 FROM items WHERE url=?", (url,)).fetchone() is not None

    def add_item(self, it: dict) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO items(url,title,domain,source,published,seen_at) VALUES(?,?,?,?,?,?)",
            (it["url"], it["title"], it["domain"], it["source"], it.get("published", ""), now_iso()),
        )

    # drafts ----------------------------------------------------------
    def recent_draft_titles(self, days: int = 7) -> list[str]:
        rows = self.db.execute(
            "SELECT title FROM drafts WHERE created_at >= datetime('now', ?)", (f"-{days} days",)
        ).fetchall()
        return [r["title"] for r in rows]

    def drafted_keys(self) -> set[str]:
        keys = {r["cluster_key"] for r in self.db.execute("SELECT cluster_key FROM drafts")}
        keys |= {r["cluster_key"] for r in self.db.execute("SELECT cluster_key FROM rejected")}
        return keys

    def drafts_today(self, edition: str | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM drafts WHERE substr(created_at,1,10)=? AND status='posted'"
        args: tuple = (today(),)
        if edition is not None:
            q += " AND COALESCE(edition,'global')=?"
            args += (edition,)
        return self.db.execute(q, args).fetchall()

    def add_draft(self, key: str, title: str, category: str, wp_id: int | None, status: str, notes: list[str],
                  edition: str = "global") -> None:
        self.db.execute(
            "INSERT INTO drafts(cluster_key,title,category,wp_id,status,notes,created_at,edition) VALUES(?,?,?,?,?,?,?,?)",
            (key, title, category, wp_id, status, json.dumps(notes), now_iso(), edition),
        )

    def reject(self, key: str, reason: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO rejected(cluster_key,reason,created_at) VALUES(?,?,?)", (key, reason, now_iso())
        )

    # spend -----------------------------------------------------------
    def add_spend(self, model: str, inp: int, out: int, usd: float) -> None:
        self.db.execute("INSERT INTO spend(day,model,input_tokens,output_tokens,usd) VALUES(?,?,?,?,?)",
                        (today(), model, inp, out, usd))

    def usage_today(self, model: str) -> tuple[int, int]:
        r = self.db.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(input_tokens+output_tokens),0) AS t FROM spend WHERE day=? AND model=?",
            (today(), model)).fetchone()
        return int(r["n"]), int(r["t"])

    def spend_today(self) -> float:
        r = self.db.execute("SELECT COALESCE(SUM(usd),0) AS s FROM spend WHERE day=?", (today(),)).fetchone()
        return float(r["s"])

    # housekeeping ----------------------------------------------------
    def prune(self, days: int = 30) -> None:
        self.db.execute("DELETE FROM items WHERE seen_at < datetime('now', ?)", (f"-{days} days",))
        self.db.execute("DELETE FROM rejected WHERE created_at < datetime('now', ?)", (f"-{days} days",))

    def commit(self) -> None:
        self.db.commit()

    def close(self) -> None:
        self.db.commit()
        self.db.close()
