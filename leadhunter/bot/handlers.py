import html

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from ..db import Database
from ..sources.base import Source

HELP = """Я ищу лидов на сайты, ботов, ИИ-автоматизацию и ИИ-мультики и присылаю их сюда.

/stats — сколько лидов за сутки по источникам
/leads [N] — последние N лидов
/sources — какие источники включены
/keywords — текущие ключевые слова
/kw_add topic|intent|negative слово — добавить слово
/kw_del слово — удалить слово
/threshold N — минимальный балл для уведомления
/pause и /resume — приостановить/возобновить уведомления"""


def build_router(db: Database, sources: list[Source], static_owners: list[int]) -> Router:
    router = Router()

    async def is_owner(user_id: int) -> bool:
        if static_owners:
            return user_id in static_owners
        stored = await db.get_setting("owner_ids", "")
        return str(user_id) in stored.split(",")

    @router.message(Command("start"))
    async def start(m: Message):
        if not static_owners and not await db.get_setting("owner_ids"):
            # Первый, кто написал боту, становится владельцем — дальше чужих игнорируем
            await db.set_setting("owner_ids", str(m.from_user.id))
        if not await is_owner(m.from_user.id):
            return
        await m.answer(HELP)

    @router.message(Command("help"))
    async def help_cmd(m: Message):
        if await is_owner(m.from_user.id):
            await m.answer(HELP)

    @router.message(Command("stats"))
    async def stats(m: Message):
        if not await is_owner(m.from_user.id):
            return
        rows = await db.stats(24)
        if not rows:
            await m.answer("За сутки лидов нет.")
            return
        total = sum(c for _, c in rows)
        await m.answer(f"Лидов за 24ч: {total}\n" + "\n".join(f"• {s}: {c}" for s, c in rows))

    @router.message(Command("leads"))
    async def leads(m: Message, command: CommandObject):
        if not await is_owner(m.from_user.id):
            return
        n = int(command.args) if command.args and command.args.isdigit() else 10
        rows = await db.recent_leads(min(n, 30))
        if not rows:
            await m.answer("Лидов пока нет.")
            return
        lines = []
        for r in rows:
            head = html.escape((r["title"] or r["text"] or "")[:80])
            link = f' — <a href="{html.escape(r["url"])}">ссылка</a>' if r["url"] else ""
            lines.append(f"#{r['id']} [{r['status']}] {r['score']}/10 {r['source']}: {head}{link}")
        await m.answer("\n".join(lines), disable_web_page_preview=True)

    @router.message(Command("sources"))
    async def sources_cmd(m: Message):
        if await is_owner(m.from_user.id):
            await m.answer("Источники:\n" + "\n".join(f"• {s.name} (каждые {s.interval}с)" for s in sources))

    @router.message(Command("keywords"))
    async def keywords(m: Message):
        if not await is_owner(m.from_user.id):
            return
        kw = await db.get_keywords()
        await m.answer(
            f"<b>Тема</b>: {', '.join(kw.topic)}\n\n<b>Намерение</b>: {', '.join(kw.intent)}\n\n"
            f"<b>Стоп-слова</b>: {', '.join(kw.negative)}"
        )

    @router.message(Command("kw_add"))
    async def kw_add(m: Message, command: CommandObject):
        if not await is_owner(m.from_user.id):
            return
        parts = (command.args or "").split(maxsplit=1)
        if len(parts) != 2 or parts[0] not in ("topic", "intent", "negative"):
            await m.answer("Формат: /kw_add topic|intent|negative слово")
            return
        await db.add_keyword(parts[0], parts[1])
        await m.answer(f"Добавил «{parts[1]}» в {parts[0]}")

    @router.message(Command("kw_del"))
    async def kw_del(m: Message, command: CommandObject):
        if not await is_owner(m.from_user.id):
            return
        if not command.args:
            await m.answer("Формат: /kw_del слово")
            return
        n = await db.remove_keyword(command.args.strip())
        await m.answer("Удалил" if n else "Такого слова нет")

    @router.message(Command("threshold"))
    async def threshold(m: Message, command: CommandObject):
        if not await is_owner(m.from_user.id):
            return
        if not command.args or not command.args.isdigit():
            await m.answer(f"Текущий порог: {await db.get_setting('threshold', 'по умолчанию')}\nФормат: /threshold N")
            return
        await db.set_setting("threshold", command.args)
        await m.answer(f"Порог: {command.args}")

    @router.message(Command("pause"))
    async def pause(m: Message):
        if await is_owner(m.from_user.id):
            await db.set_setting("paused", "1")
            await m.answer("Уведомления на паузе, лиды копятся в базе.")

    @router.message(Command("resume"))
    async def resume(m: Message):
        if await is_owner(m.from_user.id):
            await db.set_setting("paused", "0")
            await m.answer("Уведомления включены.")

    @router.callback_query(F.data.startswith("st:"))
    async def set_status(cq: CallbackQuery):
        if not await is_owner(cq.from_user.id):
            await cq.answer()
            return
        _, lead_id, status = cq.data.split(":")
        await db.set_status(int(lead_id), status)
        labels = {"contacted": "✅ написал", "hot": "🔥 горячий", "spam": "🚫 не лид"}
        await cq.message.edit_reply_markup(reply_markup=None)
        await cq.answer(f"#{lead_id}: {labels.get(status, status)}")

    return router
