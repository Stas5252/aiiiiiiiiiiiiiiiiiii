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
CREATE TABLE IF NOT EXISTS team (user_id INTEGER PRIMARY KEY, name TEXT NOT NULL, added_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lead_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER NOT NULL, status TEXT NOT NULL,
    user_id INTEGER, user_name TEXT, at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS lead_events_lead ON lead_events(lead_id);
CREATE TABLE IF NOT EXISTS rules (id INTEGER PRIMARY KEY AUTOINCREMENT, expr TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chat_stats (
    chat_key TEXT PRIMARY KEY, title TEXT, messages INTEGER NOT NULL DEFAULT 0, leads INTEGER NOT NULL DEFAULT 0,
    last_seen TEXT
);
CREATE TABLE IF NOT EXISTS author_events (author TEXT NOT NULL, chat_key TEXT NOT NULL, at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS author_events_author ON author_events(author, at);
CREATE TABLE IF NOT EXISTS templates (service TEXT PRIMARY KEY, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alerts (key TEXT PRIMARY KEY, text TEXT NOT NULL, created_at TEXT NOT NULL);
"""

# Колонки, добавленные после первой версии: для существующих баз делаем ALTER TABLE
LEAD_COLUMNS = {
    "assigned_to": "INTEGER", "assigned_name": "TEXT", "assigned_at": "TEXT",
    "msg_chat_id": "INTEGER", "msg_id": "INTEGER", "thread_id": "INTEGER",
    "sla_pinged": "INTEGER NOT NULL DEFAULT 0", "followup_at": "TEXT", "followup_count": "INTEGER NOT NULL DEFAULT 0",
    "phrase": "TEXT", "rule": "TEXT", "author_bio": "TEXT", "freshness": "TEXT", "competitors": "INTEGER",
    "why": "TEXT",
    # pending = карточку надо доставить (не «тихий» лид и не пауза); attempts — сколько раз досылали
    "pending": "INTEGER NOT NULL DEFAULT 0", "attempts": "INTEGER NOT NULL DEFAULT 0",
}
BOARD_SQL = "(source IN ('kwork', 'weblancer', 'freelance.ru', 'hh.ru', 'rss') OR source LIKE 'rss:%')"
CSV_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
OPEN_STATUSES = ("new", "taken", "contacted", "replied")


def content_hash(text: str) -> str:
    # Один и тот же запрос часто постят в десяток чатов, меняя ссылки/эмодзи — сравниваем только буквы и цифры
    core = re.sub(r"[^\w]+", "", re.sub(r"https?://\S+", "", normalize(text)))
    return hashlib.sha1(core.encode()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_iso(dt: datetime) -> str:
    """Время в UTC одной формой записи — иначе строки с +03:00 и +00:00 сравниваются неверно."""
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()


def csv_safe(value):
    """Excel выполняет ячейку, начинающуюся с = + - @ — чужой текст превращаем в обычную строку."""
    if isinstance(value, str) and value.startswith(CSV_FORMULA_START):
        return "'" + value
    return value


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
        await self._migrate()
        await self._seed_keywords()

    async def _migrate(self) -> None:
        cur = await self.conn.execute("PRAGMA table_info(leads)")
        existing = {r[1] for r in await cur.fetchall()}
        for col, typ in LEAD_COLUMNS.items():
            if col not in existing:
                await self.conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {typ}")
        cur = await self.conn.execute("PRAGMA table_info(ignored)")
        if "until" not in {r[1] for r in await cur.fetchall()}:
            await self.conn.execute("ALTER TABLE ignored ADD COLUMN until TEXT")  # NULL — бессрочно
        # found_at раньше писался с поясом источника (+03:00): приводим к UTC
        cur = await self.conn.execute("SELECT id, found_at FROM leads WHERE found_at NOT LIKE '%+00:00'")
        for lead_id, found_at in await cur.fetchall():
            try:
                fixed = utc_iso(datetime.fromisoformat(found_at))
            except (TypeError, ValueError):
                continue
            await self.conn.execute("UPDATE leads SET found_at = ? WHERE id = ?", (fixed, lead_id))
        await self.conn.commit()

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
        """True, если запись новая. Повторная встреча продлевает жизнь записи: cleanup() удаляет только то,
        что источник давно не показывает (иначе организация с карт вернулась бы лидом через месяц)."""
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO seen VALUES (?, ?, ?)", (source, external_id, _now())
        )
        if cur.rowcount != 1:
            day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            cur2 = await self.conn.execute(
                "UPDATE seen SET seen_at = ? WHERE source = ? AND external_id = ? AND seen_at < ?",
                (_now(), source, external_id, day_ago))
            if cur2.rowcount:
                await self.conn.commit()
            return False
        await self.conn.commit()
        return True

    async def mark_hash_seen(self, text: str) -> bool:
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO seen_hashes VALUES (?, ?)", (content_hash(text), _now())
        )
        await self.conn.commit()
        return cur.rowcount == 1

    async def cleanup(self, days: int = 30) -> dict[str, int]:
        """Удаляет отработавшие служебные записи: дедуп старше days дней, события авторов старше 2 суток,
        истёкшие временные игноры и неотправленные алерты старше суток."""
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=days)).isoformat()
        removed = {}
        for table, sql, arg in (
            ("seen", "DELETE FROM seen WHERE seen_at < ?", old),
            ("seen_hashes", "DELETE FROM seen_hashes WHERE seen_at < ?", old),
            ("author_events", "DELETE FROM author_events WHERE at < ?", (now - timedelta(days=2)).isoformat()),
            ("ignored", "DELETE FROM ignored WHERE until IS NOT NULL AND until < ?", now.isoformat()),
            ("alerts", "DELETE FROM alerts WHERE created_at < ?", (now - timedelta(days=1)).isoformat()),
        ):
            cur = await self.conn.execute(sql, (arg,))
            removed[table] = cur.rowcount
        await self.conn.commit()
        return removed

    # --- лиды ---
    async def save_lead(self, lead: Lead, score: int, res: ScoreResult, *, summary: str | None = None,
                        service: str = "", budget: str = "", urgency: str = "", draft: str = "",
                        rule: str = "", author_bio: str = "", freshness: str = "", why: str = "",
                        pending: bool = False) -> int:
        """pending — карточку надо доставить: пока у лида нет msg_id, её досылает фоновый цикл."""
        matched = json.dumps({"topic": res.topic, "intent": res.intent, "negative": res.negative}, ensure_ascii=False)
        cur = await self.conn.execute(
            "INSERT INTO leads (source, external_id, url, title, text, author, contact, score, matched, summary,"
            " service, budget, urgency, draft, chat_key, chat_title, phrase, rule, author_bio, freshness, competitors,"
            " why, pending, found_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (lead.source, lead.external_id, lead.url, lead.title, lead.text, lead.author, lead.contact,
             score, matched, summary, service, budget, urgency, draft,
             lead.extra.get("chat_key"), lead.extra.get("chat_title"), lead.extra.get("phrase"), rule, author_bio,
             freshness, lead.competitors, why, int(pending), utc_iso(lead.found_at), _now()),
        )
        await self.conn.commit()
        lead_id = cur.lastrowid
        await self.add_event(lead_id, "new")
        if lead.extra.get("chat_key"):
            await self.bump_chat_stats(lead.extra["chat_key"], lead.extra.get("chat_title", ""), leads=1)
        return lead_id

    async def get_lead(self, lead_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,))
        return await cur.fetchone()

    async def set_status(self, lead_id: int, status: str, user_id: int | None = None, user_name: str = "") -> None:
        await self.conn.execute("UPDATE leads SET status = ? WHERE id = ?", (status, lead_id))
        await self.conn.commit()
        await self.add_event(lead_id, status, user_id, user_name)

    async def add_event(self, lead_id: int, status: str, user_id: int | None = None, user_name: str = "") -> None:
        await self.conn.execute(
            "INSERT INTO lead_events (lead_id, status, user_id, user_name, at) VALUES (?, ?, ?, ?, ?)",
            (lead_id, status, user_id, user_name, _now()),
        )
        await self.conn.commit()

    async def assign(self, lead_id: int, user_id: int, name: str) -> bool:
        """Закрепить лида за менеджером. False — уже взят кем-то другим."""
        row = await self.get_lead(lead_id)
        if row is None:
            return False
        if row["assigned_to"] == user_id:
            return True
        cur = await self.conn.execute(
            "UPDATE leads SET assigned_to = ?, assigned_name = ?, assigned_at = ?, status = 'taken'"
            " WHERE id = ? AND assigned_to IS NULL",
            (user_id, name, _now(), lead_id),
        )
        await self.conn.commit()
        if cur.rowcount:
            await self.add_event(lead_id, "taken", user_id, name)
        return cur.rowcount == 1

    async def set_lead_message(self, lead_id: int, chat_id: int, msg_id: int, thread_id: int | None) -> None:
        await self.conn.execute(
            "UPDATE leads SET msg_chat_id = ?, msg_id = ?, thread_id = ?, pending = 0 WHERE id = ?",
            (chat_id, msg_id, thread_id, lead_id))
        await self.conn.commit()

    async def sla_overdue(self, minutes: int, min_score: int, *, max_age_minutes: int | None = None,
                          limit: int = 5) -> list[aiosqlite.Row]:
        """Лиды, которые владелец видел (карточка доставлена), но никто не взял за minutes минут.
        Старше max_age_minutes не возвращаем: после простоя пинговать протухшее поздно."""
        now = datetime.now(timezone.utc)
        if max_age_minutes is None:
            max_age_minutes = max(minutes * 3, 60)
        cur = await self.conn.execute(
            "SELECT * FROM leads WHERE status = 'new' AND sla_pinged = 0 AND msg_id IS NOT NULL AND score >= ?"
            " AND created_at <= ? AND created_at >= ? ORDER BY score DESC, id DESC LIMIT ?",
            (min_score, (now - timedelta(minutes=minutes)).isoformat(),
             (now - timedelta(minutes=max_age_minutes)).isoformat(), limit),
        )
        return list(await cur.fetchall())

    async def mark_sla_pinged(self, lead_id: int) -> None:
        await self.conn.execute("UPDATE leads SET sla_pinged = 1 WHERE id = ?", (lead_id,))
        await self.conn.commit()

    # --- follow-up ---
    async def schedule_followup(self, lead_id: int, hours: int) -> None:
        at = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
        await self.conn.execute("UPDATE leads SET followup_at = ?, followup_count = followup_count + 1 WHERE id = ?",
                                (at, lead_id))
        await self.conn.commit()

    async def clear_followup(self, lead_id: int) -> None:
        await self.conn.execute("UPDATE leads SET followup_at = NULL WHERE id = ?", (lead_id,))
        await self.conn.commit()

    async def followups_due(self) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM leads WHERE followup_at IS NOT NULL AND followup_at <= ? AND status = 'contacted'", (_now(),)
        )
        return list(await cur.fetchall())

    # --- команда ---
    async def add_member(self, user_id: int, name: str) -> None:
        await self.conn.execute("INSERT OR REPLACE INTO team VALUES (?, ?, ?)", (user_id, name, _now()))
        await self.conn.commit()

    async def remove_member(self, user_id: int) -> int:
        cur = await self.conn.execute("DELETE FROM team WHERE user_id = ?", (user_id,))
        await self.conn.commit()
        return cur.rowcount

    async def members(self) -> list[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM team ORDER BY added_at")
        return list(await cur.fetchall())

    async def is_member(self, user_id: int) -> bool:
        cur = await self.conn.execute("SELECT 1 FROM team WHERE user_id = ?", (user_id,))
        return await cur.fetchone() is not None

    async def team_stats(self, days: int = 7) -> list[aiosqlite.Row]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = await self.conn.execute(
            "SELECT user_name, status, COUNT(*) AS n FROM lead_events WHERE at >= ? AND user_id IS NOT NULL"
            " GROUP BY user_name, status", (since,)
        )
        return list(await cur.fetchall())

    # --- правила-фильтры ---
    async def add_rule(self, expr: str) -> None:
        await self.conn.execute("INSERT OR IGNORE INTO rules (expr, created_at) VALUES (?, ?)", (expr.strip(), _now()))
        await self.conn.commit()

    async def remove_rule(self, rule_id: int) -> int:
        cur = await self.conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
        await self.conn.commit()
        return cur.rowcount

    async def rules(self) -> list[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM rules ORDER BY id")
        return list(await cur.fetchall())

    # --- статистика чатов и авторов ---
    async def bump_chat_stats(self, chat_key: str, title: str, messages: int = 0, leads: int = 0) -> None:
        await self.conn.execute(
            "INSERT INTO chat_stats (chat_key, title, messages, leads, last_seen) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(chat_key) DO UPDATE SET messages = messages + ?, leads = leads + ?, last_seen = ?,"
            " title = COALESCE(NULLIF(excluded.title, ''), chat_stats.title)",
            (chat_key, title, messages, leads, _now(), messages, leads, _now()),
        )
        await self.conn.commit()

    async def chat_rating(self, limit: int = 50) -> list[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM chat_stats ORDER BY leads DESC, messages DESC LIMIT ?", (limit,))
        return list(await cur.fetchall())

    async def author_seen(self, author: str, chat_key: str, window_hours: int = 1) -> int:
        """Записывает запрос автора и возвращает, в скольких разных чатах он оставил запросы за окно.
        Вызывается только для сообщений, прошедших оценку: обычные реплики в счёт не идут."""
        await self.conn.execute("INSERT INTO author_events VALUES (?, ?, ?)", (author.lower(), chat_key, _now()))
        since = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
        cur = await self.conn.execute(
            "SELECT COUNT(DISTINCT chat_key) FROM author_events WHERE author = ? AND at >= ?", (author.lower(), since)
        )
        await self.conn.commit()
        return (await cur.fetchone())[0]

    # --- шаблоны ответов ---
    async def set_template(self, service: str, text: str) -> None:
        await self.conn.execute("INSERT OR REPLACE INTO templates VALUES (?, ?)", (service, text))
        await self.conn.commit()

    async def get_template(self, service: str) -> str | None:
        cur = await self.conn.execute("SELECT text FROM templates WHERE service = ?", (service,))
        row = await cur.fetchone()
        return row[0] if row else None

    async def templates(self) -> dict[str, str]:
        cur = await self.conn.execute("SELECT service, text FROM templates")
        return {r[0]: r[1] for r in await cur.fetchall()}

    # --- аналитика ---
    async def funnel(self, days: int = 30, by: str = "source") -> list[aiosqlite.Row]:
        """Воронка: найдено → взято → написали → ответил → сделка, в разрезе source / phrase / chat_title."""
        col = {"source": "source", "phrase": "phrase", "chat": "chat_title", "service": "service"}[by]
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = await self.conn.execute(
            f"SELECT COALESCE({col}, '—') AS k, COUNT(*) AS found,"
            " SUM(status IN ('taken','contacted','replied','deal','lost')) AS taken,"
            " SUM(status IN ('contacted','replied','deal','lost')) AS contacted,"
            " SUM(status IN ('replied','deal','lost')) AS replied,"
            " SUM(status = 'deal') AS deal, SUM(status = 'spam') AS spam"
            f" FROM leads WHERE created_at >= ? GROUP BY {col} ORDER BY deal DESC, replied DESC, found DESC", (since,)
        )
        return list(await cur.fetchall())

    async def response_times(self, days: int = 7) -> list[float]:
        """Минуты от появления лида до «взял» — метрика speed-to-lead."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = await self.conn.execute(
            "SELECT created_at, assigned_at FROM leads WHERE assigned_at IS NOT NULL AND created_at >= ?", (since,)
        )
        out = []
        for created, assigned in await cur.fetchall():
            out.append((datetime.fromisoformat(assigned) - datetime.fromisoformat(created)).total_seconds() / 60)
        return out

    async def recent_leads(self, limit: int = 10) -> list[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM leads ORDER BY id DESC LIMIT ?", (limit,))
        return list(await cur.fetchall())

    async def undelivered_leads(self, limit: int = 20, board_max_age_hours: float = 3.0) -> list[aiosqlite.Row]:
        """Недоставленные лиды; с бирж — только те, что ещё можно забрать (моложе N часов)."""
        since = (datetime.now(timezone.utc) - timedelta(hours=board_max_age_hours)).isoformat()
        cur = await self.conn.execute(
            f"SELECT * FROM leads WHERE msg_id IS NULL AND status = 'new' AND (NOT {BOARD_SQL} OR found_at >= ?)"
            " ORDER BY score DESC, id DESC LIMIT ?", (since, limit)
        )
        return list(await cur.fetchall())

    async def pending_leads(self, limit: int = 5, board_max_age_hours: float = 3.0, max_age_hours: float = 48.0,
                            max_attempts: int = 5) -> list[aiosqlite.Row]:
        """Лиды, чью карточку не удалось отправить (сбой Telegram, не было владельца) — для фоновой досылки.
        «Тихие» лиды (сверх лимита пачки, пауза) сюда не входят. Заказы бирж — пока их можно забрать,
        остальное — не старше max_age_hours, заявки с лендинга — без срока."""
        now = datetime.now(timezone.utc)
        cur = await self.conn.execute(
            "SELECT * FROM leads WHERE msg_id IS NULL AND pending = 1 AND status = 'new' AND attempts < ?"
            f" AND (source = 'landing' OR ({BOARD_SQL} AND found_at >= ?) OR (NOT {BOARD_SQL} AND created_at >= ?))"
            " ORDER BY (source = 'landing') DESC, score DESC, id DESC LIMIT ?",
            (max_attempts, (now - timedelta(hours=board_max_age_hours)).isoformat(),
             (now - timedelta(hours=max_age_hours)).isoformat(), limit)
        )
        return list(await cur.fetchall())

    async def bump_attempts(self, lead_id: int) -> int:
        await self.conn.execute("UPDATE leads SET attempts = attempts + 1 WHERE id = ?", (lead_id,))
        await self.conn.commit()
        cur = await self.conn.execute("SELECT attempts FROM leads WHERE id = ?", (lead_id,))
        return (await cur.fetchone())[0]

    async def recent_lead_texts(self, hours: int = 48, limit: int = 2000) -> list[tuple[str, datetime]]:
        """Тексты недавних лидов — чтобы после перезапуска семантический дедуп помнил, что уже присылал."""
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        cur = await self.conn.execute(
            "SELECT title, text, created_at FROM leads WHERE created_at >= ? ORDER BY id DESC LIMIT ?", (since, limit))
        rows = await cur.fetchall()
        return [(f"{r[0] or ''}\n{r[1] or ''}".strip(), datetime.fromisoformat(r[2])) for r in reversed(rows)]

    # --- служебные алерты: кладёт любой модуль, доставляет фоновый цикл ---
    async def add_alert(self, key: str, text: str) -> None:
        await self.conn.execute("INSERT OR REPLACE INTO alerts VALUES (?, ?, ?)", (key, text, _now()))
        await self.conn.commit()

    async def alerts(self) -> list[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT key, text FROM alerts ORDER BY created_at")
        return list(await cur.fetchall())

    async def remove_alert(self, key: str) -> None:
        await self.conn.execute("DELETE FROM alerts WHERE key = ?", (key,))
        await self.conn.commit()

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
        w.writerows([csv_safe(v) for v in r] for r in rows)
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
    async def add_ignored(self, kind: str, key: str, label: str = "", *, hours: float | None = None) -> None:
        """hours — временный игнор: через столько часов запись перестаёт действовать сама."""
        until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat() if hours else None
        await self.conn.execute("INSERT OR REPLACE INTO ignored (kind, key, label, until) VALUES (?, ?, ?, ?)",
                                (kind, key.lower(), label, until))
        await self.conn.commit()

    async def remove_ignored(self, key: str) -> int:
        cur = await self.conn.execute("DELETE FROM ignored WHERE key = ?", (key.lower(),))
        await self.conn.commit()
        return cur.rowcount

    async def get_ignored(self) -> dict[str, dict[str, str]]:
        cur = await self.conn.execute(
            "SELECT kind, key, label FROM ignored WHERE until IS NULL OR until > ?", (_now(),))
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
