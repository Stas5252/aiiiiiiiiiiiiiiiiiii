import html
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from ..db import Database
from ..llm import LLMClassifier
from ..phrases import PhraseProvider
from ..sources.base import Source
from ..sources.telegram_chats import TelegramChatsSource

HELP = """Я ищу лидов на сайты, ботов, ИИ-автоматизацию и ИИ-мультики и присылаю их сюда.

<b>Лиды</b>
/stats — лиды за сутки по источникам
/leads [N] — последние N лидов
/digest — сводка за 24 часа прямо сейчас
/export — выгрузить лидов в CSV

<b>Фильтры</b>
/keywords — словарь
/kw_add topic|intent|negative слово [услуга] — добавить слово
/kw_del слово — удалить слово
/threshold N — минимальный балл для уведомления (0–10)
/profile [текст] — что продаёт студия (для ИИ)
/phrases — фразы, по которым ищем в Threads/VK/поисковиках
/phrases_set фраза; фраза; … — задать свои
/phrases_gen — попросить ИИ сгенерировать фразы под профиль
/block @username — не показывать автора; /unblock @username
/ignored — игнор-лист чатов и авторов
/pause и /resume — пауза уведомлений

<b>Telegram-чаты</b> (если подключён аккаунт)
/tg_find запрос — найти публичные чаты по названию
/tg_join @chat — вступить в чат (или по ссылке-приглашению)
/tg_chats — где состоит аккаунт

/sources — состояние источников"""


@dataclass
class BotDeps:
    db: Database
    sources: list[Source]
    static_owners: list[int]
    phrases: PhraseProvider
    llm: LLMClassifier | None = None
    default_profile: str = ""
    outreach_limit: int = 0
    tz: str = "Europe/Moscow"


def build_router(deps: BotDeps) -> Router:
    router = Router()
    db, sources, static_owners = deps.db, deps.sources, deps.static_owners
    tg = next((s for s in sources if isinstance(s, TelegramChatsSource)), None)

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
        await m.answer("\n".join(_lead_line(r) for r in rows))

    @router.message(Command("digest"))
    async def digest(m: Message):
        if await is_owner(m.from_user.id):
            await m.answer(await build_digest(db))

    @router.message(Command("export"))
    async def export(m: Message):
        if not await is_owner(m.from_user.id):
            return
        data = await db.export_csv()
        await m.answer_document(BufferedInputFile(data, filename="leads.csv"), caption="Последние 1000 лидов")

    @router.message(Command("sources"))
    async def sources_cmd(m: Message):
        if await is_owner(m.from_user.id):
            await m.answer("Источники:\n" + "\n".join(f"• {html.escape(s.status_line())}" for s in sources))

    @router.message(Command("keywords"))
    async def keywords(m: Message):
        if not await is_owner(m.from_user.id):
            return
        kw = await db.get_keywords()
        by_cat: dict[str, list[str]] = {}
        for w, cat in kw.topic.items():
            by_cat.setdefault(cat, []).append(w)
        topic = "\n".join(f"  {cat}: {', '.join(ws)}" for cat, ws in by_cat.items())
        await m.answer(
            f"<b>Тема</b>:\n{html.escape(topic)}\n\n<b>Намерение</b>: {html.escape(', '.join(kw.intent))}\n\n"
            f"<b>Стоп-слова</b>: {html.escape(', '.join(kw.negative))}"
        )

    @router.message(Command("kw_add"))
    async def kw_add(m: Message, command: CommandObject):
        if not await is_owner(m.from_user.id):
            return
        parts = (command.args or "").split()
        if len(parts) < 2 or parts[0] not in ("topic", "intent", "negative"):
            await m.answer("Формат: /kw_add topic|intent|negative слово [услуга]\nНапример: /kw_add topic квиз сайт")
            return
        kind, word = parts[0], parts[1]
        category = parts[2] if kind == "topic" and len(parts) > 2 else None
        await db.add_keyword(kind, word, category)
        await m.answer(f"Добавил «{word}» в {kind}" + (f" ({category})" if category else ""))

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

    @router.message(Command("profile"))
    async def profile(m: Message, command: CommandObject):
        if not await is_owner(m.from_user.id):
            return
        if not command.args:
            cur = await db.get_setting("profile") or "(из .env)"
            await m.answer(f"Профиль студии для ИИ:\n{html.escape(cur)}\n\nИзменить: /profile текст")
            return
        await db.set_setting("profile", command.args.strip())
        await m.answer("Профиль обновлён — ИИ будет оценивать лидов с учётом него.")

    @router.message(Command("phrases"))
    async def phrases_show(m: Message):
        if await is_owner(m.from_user.id):
            ps = await deps.phrases.get()
            await m.answer("Фразы для поиска:\n" + "\n".join(f"• {html.escape(p)}" for p in ps)
                           + "\n\nИзменить: /phrases_set фраза; фраза; … или /phrases_gen")

    @router.message(Command("phrases_set"))
    async def phrases_set(m: Message, command: CommandObject):
        if not await is_owner(m.from_user.id):
            return
        if not command.args:
            await m.answer("Формат: /phrases_set нужен сайт; ищу разработчика бота; …")
            return
        ps = [p for p in command.args.split(";") if p.strip()]
        await deps.phrases.set(ps)
        await m.answer(f"Сохранил {len(ps)} фраз — источники подхватят на следующем проходе.")

    @router.message(Command("phrases_gen"))
    async def phrases_gen(m: Message):
        if not await is_owner(m.from_user.id):
            return
        if not deps.llm:
            await m.answer("ИИ не подключён (OPENAI_API_KEY).")
            return
        profile = await db.get_setting("profile", deps.default_profile) or deps.default_profile
        try:
            ps = await deps.phrases.generate(deps.llm, profile)
        except Exception as e:
            await m.answer(f"Не получилось: {html.escape(str(e))}")
            return
        await m.answer("ИИ предложил и сохранил:\n" + "\n".join(f"• {html.escape(p)}" for p in ps))

    @router.message(Command("block"))
    async def block(m: Message, command: CommandObject):
        if not await is_owner(m.from_user.id):
            return
        if not command.args:
            await m.answer("Формат: /block @username")
            return
        await db.add_ignored("author", command.args.strip())
        await m.answer(f"Автор {html.escape(command.args.strip())} в игноре")

    @router.message(Command("unblock"))
    async def unblock(m: Message, command: CommandObject):
        if not await is_owner(m.from_user.id):
            return
        n = await db.remove_ignored((command.args or "").strip())
        await m.answer("Убрал из игнора" if n else "Такого в игноре нет")

    @router.message(Command("ignored"))
    async def ignored(m: Message):
        if not await is_owner(m.from_user.id):
            return
        ig = await db.get_ignored()
        chats = "\n".join(f"  {html.escape(label or key)} ({key})" for key, label in ig["chat"].items()) or "  —"
        authors = ", ".join(html.escape(k) for k in ig["author"]) or "—"
        await m.answer(f"<b>Чаты</b>:\n{chats}\n<b>Авторы</b>: {authors}\n\nСнять: /unblock ключ")

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

    # --- Telegram-чаты через userbot ---
    @router.message(Command("tg_find"))
    async def tg_find(m: Message, command: CommandObject):
        if not await is_owner(m.from_user.id):
            return
        if not tg:
            await m.answer("Telegram-аккаунт не подключён (TG_API_ID / TG_API_HASH).")
            return
        if not command.args:
            await m.answer("Формат: /tg_find фриланс заказы")
            return
        found = await tg.find_chats(command.args)
        if not found:
            await m.answer("Ничего не нашёл.")
            return
        lines = [
            f"{'👥' if c['is_group'] else '📢'} {html.escape(c['title'])} — @{c['username']}"
            + (f" ({c['members']})" if c["members"] else "")
            for c in found
        ]
        await m.answer("\n".join(lines) + "\n\nВступить: /tg_join @username")

    @router.message(Command("tg_join"))
    async def tg_join(m: Message, command: CommandObject):
        if not await is_owner(m.from_user.id):
            return
        if not tg:
            await m.answer("Telegram-аккаунт не подключён.")
            return
        if not command.args:
            await m.answer("Формат: /tg_join @chat или ссылка-приглашение")
            return
        try:
            title = await tg.join(command.args.strip())
            await m.answer(f"Вступил: {html.escape(title)}. Новые сообщения уже отслеживаются.")
        except Exception as e:
            await m.answer(f"Не получилось: {html.escape(str(e))}")

    @router.message(Command("tg_chats"))
    async def tg_chats(m: Message):
        if not await is_owner(m.from_user.id):
            return
        if not tg:
            await m.answer("Telegram-аккаунт не подключён.")
            return
        chats = await tg.list_chats()
        lines = [f"• {html.escape(c['title'])}" + (f" (@{c['username']})" if c["username"] else "") for c in chats[:80]]
        await m.answer(f"Чатов: {len(chats)}\n" + "\n".join(lines))

    # --- кнопки под карточкой ---
    @router.callback_query(F.data.startswith("st:"))
    async def set_status(cq: CallbackQuery):
        if not await is_owner(cq.from_user.id):
            await cq.answer()
            return
        _, lead_id, status = cq.data.split(":")
        await db.set_status(int(lead_id), status)
        labels = {"contacted": "✅ написал", "hot": "🔥 горячий — ИИ учтёт как пример", "spam": "🚫 не лид — ИИ учтёт как пример"}
        await cq.message.edit_reply_markup(reply_markup=None)
        await cq.answer(f"#{lead_id}: {labels.get(status, status)}", show_alert=False)

    @router.callback_query(F.data.startswith("dr:"))
    async def show_draft(cq: CallbackQuery):
        if not await is_owner(cq.from_user.id):
            await cq.answer()
            return
        row = await db.get_lead(int(cq.data.split(":")[1]))
        if not row or not row["draft"]:
            await cq.answer("Черновика нет")
            return
        # Отдельным сообщением без разметки — чтобы скопировать одним тапом
        await cq.message.answer(f"✍️ Черновик для #{row['id']}:\n\n<code>{html.escape(row['draft'])}</code>")
        await cq.answer()

    @router.callback_query(F.data.startswith("send:"))
    async def send_draft(cq: CallbackQuery):
        if not await is_owner(cq.from_user.id):
            await cq.answer()
            return
        row = await db.get_lead(int(cq.data.split(":")[1]))
        if not tg or not row or not row["draft"] or not row["contact"]:
            await cq.answer("Нечего отправлять")
            return
        today = datetime.now(ZoneInfo(deps.tz)).strftime("%Y-%m-%d")
        key = f"outreach:{today}"
        sent_today = int(await db.get_setting(key, "0"))
        if sent_today >= deps.outreach_limit:
            await cq.answer(f"Лимит {deps.outreach_limit} сообщений в день исчерпан — остальное руками", show_alert=True)
            return
        try:
            await tg.send_dm(row["contact"], row["draft"])
        except Exception as e:
            await cq.answer(f"Не отправилось: {str(e)[:150]}", show_alert=True)
            return
        await db.set_setting(key, str(sent_today + 1))
        await db.set_status(row["id"], "contacted")
        await cq.message.edit_reply_markup(reply_markup=None)
        await cq.answer(f"Отправлено {row['contact']} ({sent_today + 1}/{deps.outreach_limit} за сегодня)")

    @router.callback_query(F.data.startswith("mute:"))
    async def mute_chat(cq: CallbackQuery):
        if not await is_owner(cq.from_user.id):
            await cq.answer()
            return
        row = await db.get_lead(int(cq.data.split(":")[1]))
        if not row or not row["chat_key"]:
            await cq.answer("У этого лида нет чата")
            return
        await db.add_ignored("chat", row["chat_key"], row["chat_title"] or "")
        await cq.answer(f"Чат «{row['chat_title']}» в игноре. Снять: /unblock {row['chat_key']}", show_alert=True)

    return router


def _lead_line(r) -> str:
    head = html.escape((r["title"] or r["text"] or "")[:80])
    link = f' — <a href="{html.escape(r["url"])}">ссылка</a>' if r["url"] else ""
    service = f" 🎯{r['service']}" if r["service"] else ""
    return f"#{r['id']} [{r['status']}] {r['score']}/10 {r['source']}{service}: {head}{link}"


async def build_digest(db: Database, hours: int = 24) -> str:
    rows = await db.leads_since(hours)
    if not rows:
        return f"За {hours}ч лидов нет."
    by_source: dict[str, int] = {}
    by_service: dict[str, int] = {}
    for r in rows:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
        if r["service"]:
            by_service[r["service"]] = by_service.get(r["service"], 0) + 1
    untouched = [r for r in rows if r["status"] == "new"]
    top = sorted(untouched, key=lambda r: -r["score"])[:5]
    lines = [f"📊 <b>Дайджест за {hours}ч</b>: {len(rows)} лидов, не разобрано {len(untouched)}"]
    lines.append("По источникам: " + ", ".join(f"{s} {c}" for s, c in sorted(by_source.items(), key=lambda x: -x[1])))
    if by_service:
        lines.append("По услугам: " + ", ".join(f"{s} {c}" for s, c in sorted(by_service.items(), key=lambda x: -x[1])))
    if top:
        lines.append("\n🔥 <b>Топ неразобранных</b>:")
        lines += [_lead_line(r) for r in top]
    return "\n".join(lines)
