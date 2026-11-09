import csv
import hashlib
import io
import json
import os
import re
from datetime import datetime, timedelta, timezone

import aiosqlite

from .models import Lead
from .scoring import DEFAULT_INTENT, DEFAULT_NEGATIVE, DEFAULT_TOPIC, Keywords, ScoreResult, normalize

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    source TEXT NOT NULL, external_id TEXT NOT NULL, seen_at TEXT NOT NULL,
    PRIMARY KEY (source, external_id)
);
CREATE TABLE IF NOT EXISTS seen_hashes (hash TEXT PRIMARY KEY, seen_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL, external_id TEXT NOT NULL, url TEXT, title TEXT, text TEXT,
    author TEXT, contact TEXT, score INTEGER NOT NULL, matched TEXT, summary TEXT,
    service TEXT, budget TEXT, urgency TEXT, draft TEXT, chat_key TEXT, chat_title TEXT,
    status TEXT NOT NULL DEFAULT 'new', found_at TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS leads_created ON leads(created_at);
CREATE TABLE IF NOT EXISTS keywords (
    kind TEXT NOT NULL, word TEXT NOT NULL, category TEXT, PRIMARY KEY (kind, word)
);
CREATE TABLE IF NOT EXISTS ignored (kind TEXT NOT NULL, key TEXT NOT NULL, label TEXT, PRIMARY KEY (kind, key));
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def content_hash(text: str) -> str:
    # Один и тот же запрос часто постят в десяток чатов, меняя ссылки/эмодзи — сравниваем только буквы и цифры
    core = re.sub(r"[^\w]+", "", re.sub(r"https?://\S+", "", normalize(text)))
    return hashlib.sha1(core.encode()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database.connect() не вызван"
        return self._conn

    async def connect(self) -> None:
        if os.path.dirname(self.path):
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._seed_keywords()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    async def _seed_keywords(self) -> None:
        cur = await self.conn.execute("SELECT COUNT(*) FROM keywords")
        if (await cur.fetchone())[0]:
            return
        rows = [("topic", w, cat) for w, cat in DEFAULT_TOPIC.items()]
        rows += [("intent", w, None) for w in DEFAULT_INTENT] + [("negative", w, None) for w in DEFAULT_NEGATIVE]
        await self.conn.executemany("INSERT OR IGNORE INTO keywords VALUES (?, ?, ?)", rows)
        await self.conn.commit()

    # --- дедуп ---
    async def mark_seen(self, source: str, external_id: str) -> bool:
        """True, если запись новая."""
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO seen VALUES (?, ?, ?)", (source, external_id, _now())
        )
        await self.conn.commit()
        return cur.rowcount == 1

    async def mark_hash_seen(self, text: str) -> bool:
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO seen_hashes VALUES (?, ?)", (content_hash(text), _now())
        )
        await self.conn.commit()
        return cur.rowcount == 1

    # --- лиды ---
    async def save_lead(self, lead: Lead, score: int, res: ScoreResult, *, summary: str | None = None,
                        service: str = "", budget: str = "", urgency: str = "", draft: str = "") -> int:
        matched = json.dumps({"topic": res.topic, "intent": res.intent, "negative": res.negative}, ensure_ascii=False)
        cur = await self.conn.execute(
            "INSERT INTO leads (source, external_id, url, title, text, author, contact, score, matched, summary,"
            " service, budget, urgency, draft, chat_key, chat_title, found_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (lead.source, lead.external_id, lead.url, lead.title, lead.text, lead.author, lead.contact,
             score, matched, summary, service, budget, urgency, draft,
             lead.extra.get("chat_key"), lead.extra.get("chat_title"), lead.found_at.isoformat(), _now()),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def get_lead(self, lead_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,))
        return await cur.fetchone()

    async def set_status(self, lead_id: int, status: str) -> None:
        await self.conn.execute("UPDATE leads SET status = ? WHERE id = ?", (status, lead_id))
        await self.conn.commit()

    async def recent_leads(self, limit: int = 10) -> list[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM leads ORDER BY id DESC LIMIT ?", (limit,))
        return list(await cur.fetchall())

    async def leads_by_status(self, status: str, limit: int = 5) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM leads WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit)
        )
        return list(await cur.fetchall())

    async def leads_since(self, hours: int) -> list[aiosqlite.Row]:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        cur = await self.conn.execute(
            "SELECT * FROM leads WHERE created_at >= ? ORDER BY score DESC, id DESC", (since,)
        )
        return list(await cur.fetchall())

    async def stats(self, hours: int = 24) -> list[tuple[str, int]]:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        cur = await self.conn.execute(
            "SELECT source, COUNT(*) FROM leads WHERE created_at >= ? GROUP BY source ORDER BY 2 DESC", (since,)
        )
        return [(r[0], r[1]) for r in await cur.fetchall()]

    async def export_csv(self, limit: int = 1000) -> bytes:
        cur = await self.conn.execute(
            "SELECT id, created_at, source, score, status, service, budget, title, text, author, contact, url, summary"
            " FROM leads ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = await cur.fetchall()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([d[0] for d in cur.description])
        w.writerows(tuple(r) for r in rows)
        return buf.getvalue().encode("utf-8-sig")  # BOM — чтобы Excel открыл кириллицу

    # --- ключевые слова ---
    async def get_keywords(self) -> Keywords:
        cur = await self.conn.execute("SELECT kind, word, category FROM keywords")
        kw = Keywords(topic={}, intent=[], negative=[])
        for kind, word, category in await cur.fetchall():
            if kind == "topic":
                kw.topic[word] = category or "другое"
            else:
                getattr(kw, kind).append(word)
        return kw

    async def add_keyword(self, kind: str, word: str, category: str | None = None) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO keywords VALUES (?, ?, ?)",
            (kind, normalize(word), category if kind == "topic" else None),
        )
        await self.conn.commit()

    async def remove_keyword(self, word: str) -> int:
        cur = await self.conn.execute("DELETE FROM keywords WHERE word = ?", (normalize(word),))
        await self.conn.commit()
        return cur.rowcount

    # --- игнор-листы: шумные чаты и авторы-спамеры ---
    async def add_ignored(self, kind: str, key: str, label: str = "") -> None:
        await self.conn.execute("INSERT OR REPLACE INTO ignored VALUES (?, ?, ?)", (kind, key.lower(), label))
        await self.conn.commit()

    async def remove_ignored(self, key: str) -> int:
        cur = await self.conn.execute("DELETE FROM ignored WHERE key = ?", (key.lower(),))
        await self.conn.commit()
        return cur.rowcount

    async def get_ignored(self) -> dict[str, dict[str, str]]:
        cur = await self.conn.execute("SELECT kind, key, label FROM ignored")
        out: dict[str, dict[str, str]] = {"chat": {}, "author": {}}
        for kind, key, label in await cur.fetchall():
            out.setdefault(kind, {})[key] = label or ""
        return out

    # --- настройки ---
    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        cur = await self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row[0] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        await self.conn.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))
        await self.conn.commit()
