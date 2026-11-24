"""Хранилище контент-фабрики: материалы, правки владельца (обучение), каналы публикации, лиды с лендингов."""
import json
import secrets
from datetime import datetime, timedelta, timezone

import aiosqlite

from ..db import Database

SCHEMA = """
CREATE TABLE IF NOT EXISTS content_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,              -- post | carousel | reel | magnet | idea
    topic TEXT NOT NULL,
    title TEXT, body TEXT,           -- body: текст поста либо JSON (слайды / сцены / пункты)
    caption TEXT, hashtags TEXT,
    image_prompt TEXT, media TEXT,   -- JSON: {"images": [пути], "video": путь, "cover": путь}
    status TEXT NOT NULL DEFAULT 'draft',  -- draft | scheduled | published | rejected
    scheduled_at TEXT, published_at TEXT, published TEXT,  -- published: JSON [{platform, url}]
    platforms TEXT,                  -- через запятую; пусто = все настроенные
    lead_id INTEGER, parent_id INTEGER, slug TEXT UNIQUE,
    msg_chat_id INTEGER, msg_id INTEGER,
    created_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS content_items_status ON content_items(status, scheduled_at);
CREATE TABLE IF NOT EXISTS content_edits (
    id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER, kind TEXT NOT NULL,
    original TEXT NOT NULL, edited TEXT NOT NULL, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS content_channels (
    platform TEXT PRIMARY KEY, target TEXT NOT NULL, label TEXT, added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS landing_leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT NOT NULL, name TEXT, contact TEXT, message TEXT,
    lead_id INTEGER, at TEXT NOT NULL
);
"""

KINDS = {"post": "✍️ Пост", "carousel": "🎠 Карусель", "reel": "🎬 Видео", "toon": "🎨 Мультик (код)", "aivideo": "🤖 ИИ-мультик",
         "magnet": "🧲 Лид-магнит", "idea": "💡 Идея"}
STATUSES = {"draft": "черновик", "scheduled": "запланировано", "publishing": "публикуется",
            "published": "опубликовано", "rejected": "отклонено"}
OPEN_STATUSES = ("draft", "scheduled")  # только из них материал можно публиковать и ставить в расписание
STUCK_MINUTES = 10  # дольше одна площадка не публикуется (таймаут 5 минут) — значит, процесс умер
INTERRUPTED = {"platform": "", "label": "остальные площадки", "error": "публикация прервалась"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ContentStore:
    def __init__(self, db: Database):
        self.db = db

    @property
    def conn(self) -> aiosqlite.Connection:
        return self.db.conn

    async def init(self) -> None:
        await self.conn.executescript(SCHEMA)
        cur = await self.conn.execute("PRAGMA table_info(content_items)")
        if "publish_errors" not in {r[1] for r in await cur.fetchall()}:
            # JSON [{platform, label, error}] — площадки, куда материал не ушёл при последней публикации
            await self.conn.execute("ALTER TABLE content_items ADD COLUMN publish_errors TEXT")
        cur = await self.conn.execute("PRAGMA table_info(content_items)")
        have = {r[1] for r in await cur.fetchall()}
        if "threads_text" not in have:
            # отдельная версия текста для Threads (до 500 знаков, без хэштегов)
            await self.conn.execute("ALTER TABLE content_items ADD COLUMN threads_text TEXT")
        if "checks" not in have:
            # JSON-список: что владельцу проверить перед публикацией (цифры, которых нет в брифе)
            await self.conn.execute("ALTER TABLE content_items ADD COLUMN checks TEXT")
        # идеи плана не публикуются сами: раньше им ставился статус scheduled
        await self.conn.execute("UPDATE content_items SET status = 'draft' WHERE kind = 'idea' AND status = 'scheduled'")
        await self.conn.commit()
        await self.release_stuck()

    async def release_stuck(self, minutes: float = STUCK_MINUTES) -> int:
        """Материал завис в «публикуется» (бот упал или перезапущен посреди публикации) — снимаем захват.
        Что успело выйти, записано в published: такой материал считается опубликованным, а остальные площадки
        владелец доотправит кнопкой. Ничего не вышло — обратно в черновики (сам второй раз не уйдёт)."""
        before = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        cur = await self.conn.execute(
            "UPDATE content_items SET scheduled_at = NULL,"
            " status = CASE WHEN COALESCE(published, '') IN ('', '[]') THEN 'draft' ELSE 'published' END,"
            " publish_errors = CASE WHEN COALESCE(published, '') IN ('', '[]') THEN NULL ELSE ? END"
            " WHERE status = 'publishing' AND updated_at < ?", (json.dumps([INTERRUPTED], ensure_ascii=False), before))
        await self.conn.commit()
        return cur.rowcount

    # --- материалы ---
    async def add(self, kind: str, topic: str, *, title: str = "", body: str = "", caption: str = "",
                  hashtags: list[str] | None = None, image_prompt: str = "", lead_id: int | None = None,
                  parent_id: int | None = None, created_by: str = "", scheduled_at: str | None = None,
                  platforms: str = "") -> int:
        slug = secrets.token_urlsafe(6) if kind == "magnet" else None
        cur = await self.conn.execute(
            "INSERT INTO content_items (kind, topic, title, body, caption, hashtags, image_prompt, lead_id, parent_id,"
            " slug, created_by, scheduled_at, platforms, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, topic, title, body, caption, " ".join(hashtags or []), image_prompt, lead_id, parent_id, slug,
             created_by, scheduled_at, platforms, "scheduled" if scheduled_at and kind != "idea" else "draft", _now(), _now()))
        await self.conn.commit()
        return cur.lastrowid

    async def get(self, item_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute("SELECT * FROM content_items WHERE id = ?", (item_id,))
        return await cur.fetchone()

    async def by_slug(self, slug: str) -> aiosqlite.Row | None:
        cur = await self.conn.execute("SELECT * FROM content_items WHERE slug = ?", (slug,))
        return await cur.fetchone()

    async def update(self, item_id: int, **fields) -> None:
        if not fields:
            return
        if "hashtags" in fields and isinstance(fields["hashtags"], list):
            fields["hashtags"] = " ".join(fields["hashtags"])
        for k in ("media", "published", "publish_errors", "checks"):
            if k in fields and not isinstance(fields[k], (str, type(None))):
                fields[k] = json.dumps(fields[k], ensure_ascii=False)
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        await self.conn.execute(f"UPDATE content_items SET {cols} WHERE id = ?", (*fields.values(), item_id))
        await self.conn.commit()

    async def set_message(self, item_id: int, chat_id: int, msg_id: int) -> None:
        await self.update(item_id, msg_chat_id=chat_id, msg_id=msg_id)

    async def schedule(self, item_id: int, at: datetime | None) -> bool:
        """В расписание (или обратно в черновики) — только неопубликованное, не удалённое и на будущее время.
        False — ничего не изменилось."""
        if at is not None and at.astimezone(timezone.utc) <= datetime.now(timezone.utc):
            return False
        status, when = ("draft", None) if at is None else ("scheduled", at.astimezone(timezone.utc).isoformat())
        cur = await self.conn.execute(
            "UPDATE content_items SET status = ?, scheduled_at = ?, updated_at = ? WHERE id = ? AND kind != 'idea'"
            " AND status IN ('draft', 'scheduled')", (status, when, _now(), item_id))
        await self.conn.commit()
        return cur.rowcount == 1

    async def reject(self, item_id: int) -> bool:
        """Удалить можно только черновик или запланированное: вышедшее и то, что сейчас публикуется, не трогаем."""
        cur = await self.conn.execute(
            "UPDATE content_items SET status = 'rejected', scheduled_at = NULL, updated_at = ? WHERE id = ?"
            " AND (status IN ('draft', 'scheduled') OR kind = 'idea')", (_now(), item_id))
        await self.conn.commit()
        return cur.rowcount == 1

    async def claim(self, item_id: int) -> bool:
        """Атомарно забрать материал в публикацию: из двух одновременных попыток пройдёт одна.
        Берётся черновик, запланированное или опубликованное частично (есть площадки, куда не ушло)."""
        cur = await self.conn.execute(
            "UPDATE content_items SET status = 'publishing', updated_at = ? WHERE id = ? AND kind != 'idea' AND"
            " (status IN ('draft', 'scheduled')"
            "  OR (status = 'published' AND COALESCE(publish_errors, '') NOT IN ('', '[]')))", (_now(), item_id))
        await self.conn.commit()
        return cur.rowcount == 1

    async def finish_publish(self, item_id: int, status: str, failed: list[dict]) -> None:
        """Снять захват: итоговый статус и площадки, куда не ушло."""
        fields = {"status": status, "publish_errors": failed or None}
        if status == "published":
            fields["scheduled_at"] = None
        cols = ", ".join(f"{k} = ?" for k in fields)
        await self.conn.execute(
            f"UPDATE content_items SET {cols}, updated_at = ? WHERE id = ? AND status = 'publishing'",
            (*(json.dumps(v, ensure_ascii=False) if isinstance(v, list) else v for v in fields.values()), _now(), item_id))
        await self.conn.commit()

    async def add_published(self, item_id: int, result: dict) -> None:
        """Записать площадку сразу после успеха — чтобы повтор или перезапуск не отправили туда второй раз."""
        row = await self.get(item_id)
        done = json.loads(row["published"] or "[]") + [result]
        await self.update(item_id, published=done, published_at=_now())

    async def mark_published(self, item_id: int, results: list[dict]) -> None:
        await self.update(item_id, status="published", published_at=_now(), published=results)

    async def due(self, limit: int | None = None) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM content_items WHERE status = 'scheduled' AND kind != 'idea' AND scheduled_at <= ?"
            " ORDER BY scheduled_at, id LIMIT ?", (_now(), limit if limit else -1))
        return list(await cur.fetchall())

    async def queue(self, limit: int = 30) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM content_items WHERE status IN ('draft', 'scheduled') AND kind != 'idea'"
            " ORDER BY status DESC, scheduled_at, id DESC LIMIT ?", (limit,))
        return list(await cur.fetchall())

    async def recent(self, limit: int = 30, status: str | None = None, kind: str | None = None) -> list[aiosqlite.Row]:
        where, params = ["1=1"], []
        if status:
            where.append("status = ?"); params.append(status)
        if kind:
            where.append("kind = ?"); params.append(kind)
        cur = await self.conn.execute(
            f"SELECT * FROM content_items WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?", (*params, limit))
        return list(await cur.fetchall())

    async def published_texts(self, limit: int = 5) -> list[str]:
        """Что владелец реально опубликовал — лучшие примеры стиля для ИИ."""
        cur = await self.conn.execute(
            "SELECT body FROM content_items WHERE status = 'published' AND kind = 'post' ORDER BY published_at DESC LIMIT ?",
            (limit,))
        return [r[0] for r in await cur.fetchall() if r[0]]

    async def stats(self, days: int = 30) -> dict:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = await self.conn.execute(
            "SELECT kind, status, COUNT(*) AS n FROM content_items WHERE created_at >= ? AND kind != 'idea'"
            " GROUP BY kind, status", (since,))
        out: dict[str, dict[str, int]] = {}
        for r in await cur.fetchall():
            out.setdefault(r["kind"], {})[r["status"]] = r["n"]
        cur = await self.conn.execute("SELECT COUNT(*) FROM landing_leads WHERE at >= ?", (since,))
        return {"by_kind": out, "landing_leads": (await cur.fetchone())[0]}

    # --- обучение на правках ---
    async def add_edit(self, item_id: int | None, kind: str, original: str, edited: str) -> None:
        if original.strip() == edited.strip():
            return
        await self.conn.execute("INSERT INTO content_edits (item_id, kind, original, edited, at) VALUES (?, ?, ?, ?, ?)",
                                (item_id, kind, original, edited, _now()))
        await self.conn.commit()

    async def edits(self, kind: str | None = None, limit: int = 4) -> list[tuple[str, str]]:
        if kind:
            cur = await self.conn.execute(
                "SELECT original, edited FROM content_edits WHERE kind = ? ORDER BY id DESC LIMIT ?", (kind, limit))
        else:
            cur = await self.conn.execute("SELECT original, edited FROM content_edits ORDER BY id DESC LIMIT ?", (limit,))
        return [(r[0], r[1]) for r in await cur.fetchall()]

    # --- каналы публикации ---
    async def set_channel(self, platform: str, target: str, label: str = "") -> None:
        await self.conn.execute("INSERT OR REPLACE INTO content_channels VALUES (?, ?, ?, ?)",
                                (platform, target, label, _now()))
        await self.conn.commit()

    async def remove_channel(self, platform: str) -> int:
        cur = await self.conn.execute("DELETE FROM content_channels WHERE platform = ?", (platform,))
        await self.conn.commit()
        return cur.rowcount

    async def channels(self) -> dict[str, str]:
        cur = await self.conn.execute("SELECT platform, target FROM content_channels")
        return {r[0]: r[1] for r in await cur.fetchall()}

    # --- лиды с лендингов лид-магнитов ---
    async def add_landing_lead(self, slug: str, name: str, contact: str, message: str, lead_id: int | None) -> int:
        cur = await self.conn.execute(
            "INSERT INTO landing_leads (slug, name, contact, message, lead_id, at) VALUES (?, ?, ?, ?, ?, ?)",
            (slug, name, contact, message, lead_id, _now()))
        await self.conn.commit()
        return cur.lastrowid


def body_json(row) -> dict | list:
    """body карусели/видео/магнита хранится как JSON; для поста — обычный текст."""
    try:
        return json.loads(row["body"] or "")
    except (ValueError, TypeError):
        return {}


def errors_json(row) -> list[dict]:
    """Площадки, куда материал не ушёл при последней публикации: [{platform, label, error}]."""
    try:
        raw = row["publish_errors"]
    except (KeyError, IndexError):
        return []
    try:
        data = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def checks_json(row) -> list[str]:
    """Что владельцу проверить перед публикацией: цифры, которых нет в брифе, «мы» вместо «я»."""
    try:
        data = json.loads(row["checks"] or "[]")
    except (KeyError, IndexError, ValueError, TypeError):
        return []
    return [str(x) for x in data if isinstance(x, str) and x.strip()] if isinstance(data, list) else []


def threads_text(row) -> str:
    """Отдельный текст для Threads; у старых материалов его нет."""
    try:
        return str(row["threads_text"] or "")
    except (KeyError, IndexError):
        return ""


def media_json(row) -> dict:
    try:
        return json.loads(row["media"] or "{}")
    except ValueError:
        return {}
