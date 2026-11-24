"""Проверка доступа: владельцы из OWNER_IDS или по одноразовому коду из лога + команда.
В рабочей группе любой её участник становится членом команды при первом нажатии кнопки;
кто вышел из группы — теряет доступ (перепроверка с кэшем, событие chat_member, фоновый обход)."""
import asyncio
import hmac
import logging
import secrets
import time

from aiogram import Bot
from aiogram.types import ChatMemberUpdated, User

from ..db import Database
from .workspace import WORKSPACE_KEY

log = logging.getLogger(__name__)

# Общие id Telegram: анонимный админ группы, «канал от имени», служебный аккаунт — это не люди
ANONYMOUS_IDS = frozenset({1087968824, 136817688, 777000})
MEMBER_TTL = 15 * 60  # сколько секунд верим прошлому ответу Telegram о членстве в группе
MAX_CLAIM_FAILS = 5  # столько неверных кодов может прислать один человек; дальше его коды не принимаются до перезапуска
MAX_CLAIM_FAILS_TOTAL = 1000  # столько неверных кодов со всех аккаунтов — и код меняется (защита от перебора толпой)


def _present(member) -> bool:
    status = getattr(member, "status", "")
    return status in ("member", "administrator", "creator") or (status == "restricted" and bool(getattr(member, "is_member", False)))


class Guard:
    def __init__(self, bot: Bot, db: Database, static_owners: list[int], member_ttl: float = MEMBER_TTL):
        self.bot = bot
        self.db = db
        self.static_owners = static_owners
        self.member_ttl = member_ttl
        self.claim_code: str | None = None
        self._claim_fails: dict[int, int] = {}  # user_id -> сколько неверных кодов прислал
        # user_id -> (когда спросили Telegram, в группе ли, про какую группу спрашивали)
        self._checked: dict[int, tuple[float, bool, int]] = {}
        # (группа, user_id) -> запрос к Telegram, который уже идёт: параллельные обращения ждут его, а не шлют свои
        self._asking: dict[tuple[int, int], asyncio.Future] = {}

    # --- владельцы ---
    async def owners(self) -> list[int]:
        if self.static_owners:
            return self.static_owners
        stored = await self.db.get_setting("owner_ids", "")
        ids = [int(x) for x in stored.split(",") if x.strip().isdigit()]
        return [i for i in ids if i not in ANONYMOUS_IDS]

    async def is_owner(self, user_id: int) -> bool:
        return user_id in await self.owners()

    async def needs_owner(self) -> bool:
        return not await self.owners()

    def new_claim_code(self) -> str:
        """Одноразовый код: кто пришлёт его боту в личку (/start КОД), станет владельцем."""
        self.claim_code = secrets.token_hex(4)
        self._claim_fails = {}
        return self.claim_code

    async def claim(self, user: User, chat_type: str, code: str) -> bool:
        if not self.claim_code or not await self.needs_owner():
            return False
        if chat_type != "private" or user.is_bot or user.id in ANONYMOUS_IDS:
            return False
        code = code.strip().lower()
        # Дальше до записи владельца нет ни одного await: два одновременных запроса не пройдут проверку оба
        if not code or not self.claim_code or self._claim_fails.get(user.id, 0) >= MAX_CLAIM_FAILS:
            return False  # /start без кода — не попытка; перебиравший коды заблокирован
        if not hmac.compare_digest(code.encode(), self.claim_code.encode()):
            self._claim_fails[user.id] = self._claim_fails.get(user.id, 0) + 1
            if self._claim_fails[user.id] == MAX_CLAIM_FAILS:
                log.warning("id %s прислал %s неверных кодов владельца — его коды больше не принимаются до перезапуска бота",
                            user.id, MAX_CLAIM_FAILS)
            if sum(self._claim_fails.values()) >= MAX_CLAIM_FAILS_TOTAL:
                log.warning("Слишком много неверных кодов владельца — код сменён. Новый: /start %s", self.new_claim_code())
            return False
        valid, self.claim_code = self.claim_code, None
        try:
            await self.db.set_setting("owner_ids", str(user.id))
        except BaseException:
            self.claim_code = valid  # владелец не записан — код остаётся в силе
            raise
        log.info("владелец бота назначен по коду: id %s", user.id)
        return True

    # --- команда ---
    async def _workspace(self) -> int | None:
        raw = await self.db.get_setting(WORKSPACE_KEY)
        return int(raw) if raw else None

    async def _in_workspace(self, chat_id: int, user_id: int) -> bool | None:
        """True/False — что ответил Telegram; None — узнать не удалось."""
        try:
            return _present(await self.bot.get_chat_member(chat_id, user_id))
        except Exception as e:
            log.warning("не удалось проверить участника %s в рабочей группе: %s", user_id, type(e).__name__)
            return None

    async def _ask_once(self, chat_id: int, user_id: int) -> bool | None:
        """Один запрос к Telegram на человека, даже если о нём спросили несколько раз одновременно
        (кабинет при открытии шлёт сразу несколько запросов)."""
        key = (chat_id, user_id)
        asking = self._asking.get(key)
        if asking is None:
            asking = self._asking[key] = asyncio.ensure_future(self._in_workspace(chat_id, user_id))
            asking.add_done_callback(lambda _: self._asking.pop(key, None))
        # shield: если один из ждущих отменён (человек закрыл кабинет), остальные всё равно получат ответ
        return await asyncio.shield(asking)

    async def member_ok(self, user_id: int, force: bool = False) -> bool:
        """Участник команды, который всё ещё состоит в рабочей группе. Вышедшего убираем из команды."""
        if not await self.db.is_member(user_id):
            return False
        ws = await self._workspace()
        if ws is None or self.bot is None:
            return True  # группы нет — команду ведёт владелец через /team_del
        hit = self._checked.get(user_id)
        if hit and hit[2] != ws:
            hit = None  # ответ был про прежнюю рабочую группу — в новой он ничего не значит
        if hit and not force and time.monotonic() - hit[0] < self.member_ttl:
            return hit[1]
        present = await self._ask_once(ws, user_id)
        if present is None:
            return hit[1] if hit else False  # Telegram недоступен: верим прошлому ответу, без него не пускаем
        if not present:
            await self.drop_member(user_id)
            return False
        self._checked[user_id] = (time.monotonic(), True, ws)
        return True

    async def drop_member(self, user_id: int) -> None:
        self._checked.pop(user_id, None)
        if await self.db.remove_member(user_id):
            log.info("участник %s вышел из рабочей группы — убран из команды", user_id)

    async def allowed_id(self, user_id: int) -> bool:
        """Доступ по одному id (для кабинета): владелец или действующий участник команды."""
        return await self.is_owner(user_id) or await self.member_ok(user_id)

    async def allowed(self, user: User, chat_id: int | None = None) -> bool:
        if await self.is_owner(user.id):
            return True
        if user.is_bot or user.id in ANONYMOUS_IDS:
            return False
        if await self.member_ok(user.id):
            return True
        ws = await self._workspace()
        if ws is not None and chat_id == ws and await self._in_workspace(ws, user.id):
            await self.db.add_member(user.id, user.full_name)
            self._checked[user.id] = (time.monotonic(), True, ws)
            return True
        return False

    async def on_chat_member(self, event: ChatMemberUpdated) -> None:
        """Событие Telegram «участник группы изменился»: вышел или удалён — сразу теряет доступ."""
        if event.chat.id != await self._workspace():
            return
        user_id = event.new_chat_member.user.id
        if _present(event.new_chat_member):
            if await self.db.is_member(user_id):
                self._checked[user_id] = (time.monotonic(), True, event.chat.id)
        else:
            await self.drop_member(user_id)

    async def sweep(self, pause: float = 0.5) -> list[int]:
        """Перепроверяет всю команду (нужно кабинету: он смотрит только в таблицу команды). Возвращает убранных."""
        removed = []
        for row in await self.db.members():
            uid = row["user_id"]
            if not await self.member_ok(uid, force=True) and not await self.db.is_member(uid):
                removed.append(uid)
            await asyncio.sleep(pause)
        return removed

    async def sweep_loop(self, interval: float | None = None) -> None:
        while True:
            await asyncio.sleep(interval or self.member_ttl)
            try:
                await self.sweep()
            except Exception:
                log.exception("перепроверка команды не удалась")

    @staticmethod
    def name(user: User) -> str:
        return user.full_name or (f"@{user.username}" if user.username else str(user.id))
