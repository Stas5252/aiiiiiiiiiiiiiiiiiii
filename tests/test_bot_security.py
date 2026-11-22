"""Модуль C «Бот и безопасность»: по тесту на каждую находку аудита (docs/audit/02_bot_miniapp.md).
Бот гоняется через Dispatcher.feed_update с фиктивным токеном, подменённой сессией и временной SQLite.
Ответы Telegram (разбор HTML, лимит 4096 символов) смоделированы в SecSession — как в аудите, «модель TG»."""
import asyncio
import errno
import html
import logging
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import Command
from aiogram.types import Chat, ChatMemberBanned, ChatMemberLeft, ChatMemberMember, ChatMemberUpdated, User

from leadhunter import integrations as integ_mod
from leadhunter import main as main_mod
from leadhunter.bot import app as app_mod
from leadhunter.bot.app import build_dispatcher
from leadhunter.bot.guard import ANONYMOUS_IDS, MAX_CLAIM_FAILS, Guard
from leadhunter.bot.handlers import BotDeps
from leadhunter.bot.notify import TelegramNotifier
from leadhunter.bot.text import parse_int, split_text
from leadhunter.config import Settings
from leadhunter.content.runtime import build_content
from leadhunter.db import Database
from leadhunter.integrations import Integrations, validate
from leadhunter.models import Lead
from leadhunter.phrases import PhraseProvider
from leadhunter.pipeline import Pipeline
from leadhunter.tunnel import CloudflareTunnel
from test_bot_e2e import OTHER, OWNER, seed
from test_content import ContentEnv, FakeLLM, MediaSession

GROUP = -1001234567890
MEMBER = 777
ANON = 1087968824  # общий id всех анонимных админов Telegram
SECRET = "SECRETKEY_AUDIT_0123456789abcdef"
TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre", "blockquote", "tg-spoiler", "span"}
TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)((?:\s[^<>]*)?)>")


def tg_html_error(text: str) -> str | None:
    """Модель Telegram: что он ответит на такой HTML-текст (None — примет)."""
    stack = []
    for close, tag, _ in TAG_RE.findall(text):
        if tag.lower() not in TAGS:
            return f"can't parse entities: Unsupported start tag \"{tag}\""
        if not close:
            stack.append(tag.lower())
        elif not stack or stack.pop() != tag.lower():
            return "can't parse entities: Unexpected end tag"
    if stack:
        return "can't parse entities: Can't find end tag"
    plain = TAG_RE.sub("", text)
    if "<" in plain:
        return "can't parse entities: Unsupported start tag"
    if len(html.unescape(plain).encode("utf-16-le")) // 2 > 4096:
        return "message is too long"
    return None


class SecSession(MediaSession):
    def __init__(self):
        super().__init__()
        self.member_status: dict[int, str] = {}  # кто в каком статусе в рабочей группе
        self.member_error = False  # Telegram недоступен
        self.delete_fails = False  # нет права удалять сообщения

    async def _request(self, bot, method, timeout=None):
        name = type(method).__name__
        if name == "GetChatMember" and (self.member_error or method.user_id in self.member_status):
            self.calls.append((name, {"chat_id": method.chat_id, "user_id": method.user_id}))
            if self.member_error:
                raise TelegramNetworkError(method=method, message="timeout")
            user = User(id=method.user_id, is_bot=False, first_name="x")
            status = self.member_status[method.user_id]
            if status == "left":
                return ChatMemberLeft(user=user)
            if status == "kicked":
                return ChatMemberBanned(user=user, until_date=datetime.now(timezone.utc))
            return ChatMemberMember(user=user)
        if name == "DeleteMessage" and self.delete_fails:
            self.calls.append((name, {}))
            raise TelegramBadRequest(method=method, message="Bad Request: message can't be deleted")
        if name == "SetChatMenuButton":
            self.calls.append((name, method.model_dump(exclude_none=True)))
            return True
        if name in ("SendMessage", "EditMessageText") and method.parse_mode is not None:
            problem = tg_html_error(method.text)
            if problem:
                self.calls.append((name + ":REJECTED", {"text": method.text}))
                raise TelegramBadRequest(method=method, message=f"Bad Request: {problem}")
        return await super()._request(bot, method, timeout)


class SecEnv(ContentEnv):
    def named(self, *names):
        return [d for n, d in self.session.calls if n in names]

    def texts(self) -> list[str]:
        """Всё, что бот написал: новые сообщения и правки."""
        return [d.get("text", "") for d in self.named("SendMessage", "EditMessageText")]

    def said(self) -> str:
        return "\n".join(self.texts())

    def alerts(self) -> list[str]:
        return [d.get("text", "") for d in self.named("AnswerCallbackQuery")]

    async def say(self, text: str, user_id: int = OWNER, chat_id: int | None = None) -> str:
        await self.cmd(text, user_id, chat_id)
        return self.said()

    async def press(self, data: str, user_id: int = OWNER, chat_id: int | None = None) -> list[str]:
        await self.tap(data, user_id, chat_id)
        return self.alerts()

    async def member_event(self, user_id: int, status: str, chat_id: int = GROUP) -> None:
        user = User(id=user_id, is_bot=False, first_name="Участник")
        new = {"left": ChatMemberLeft(user=user), "kicked": ChatMemberBanned(user=user, until_date=datetime.now(timezone.utc)),
               "member": ChatMemberMember(user=user)}[status]
        ev = ChatMemberUpdated(chat=Chat(id=chat_id, type="supergroup"), from_user=user, date=datetime.now(timezone.utc),
                               old_chat_member=ChatMemberMember(user=user), new_chat_member=new)
        await self.dp.feed_update(self.bot, self._update(chat_member=ev))


async def make_env(tmp_path, owners=(OWNER,)) -> SecEnv:
    db = Database(str(tmp_path / "sec.db"))
    await db.connect()
    session = SecSession()
    bot = Bot("123456:FAKE", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    notifier = TelegramNotifier(bot, db, list(owners), hot_score=8)
    pipeline = Pipeline(db, notifier, default_threshold=5)
    guard = Guard(bot, db, list(owners))
    deps = BotDeps(db, [], guard, PhraseProvider(db, ["нужен сайт"]), notifier.workspace, None, "Студия", 0, 24)
    deps.extra["pipeline"] = pipeline
    deps.extra["redeliver"] = notifier.notify_row
    s = Settings(bot_token="123456:FAKE", content_dir=str(tmp_path / "content"), _env_file=None)
    factory = await build_content(s, db, bot, FakeLLM(), deps, notifier)

    async def on_change():
        pass

    deps.extra["integrations"] = Integrations(db, s, on_change)
    deps.extra["settings"] = s
    e = SecEnv(bot, build_dispatcher(deps), session, db, notifier, pipeline, factory=factory, llm=FakeLLM(), deps=deps)
    e.guard, e.settings = guard, s
    return e


@pytest.fixture
async def env(tmp_path):
    e = await make_env(tmp_path)
    yield e
    await e.db.close()


@pytest.fixture
async def fresh(tmp_path):
    """Чистая установка: OWNER_IDS пуст, в базе владельца нет, бот при запуске напечатал код."""
    e = await make_env(tmp_path, owners=())
    e.guard.new_claim_code()
    yield e
    await e.db.close()


async def with_group(env, *members: int):
    """Рабочая группа настроена, участники нажали кнопку под лидом и попали в команду."""
    await env.db.set_setting("workspace_chat_id", str(GROUP))
    await seed(env, 1)
    for uid in members:
        env.session.member_status[uid] = "member"
        assert "Ваш лид" in "".join(await env.press("take:1", uid, GROUP)) or await env.db.is_member(uid)


# ---------- V1: владелец ----------

async def test_v1_first_start_does_not_make_stranger_owner(fresh):
    await seed(fresh)
    said = await fresh.say("/start", OTHER)
    assert "Код владельца" in said and str(OTHER) in said  # подсказка, как стать владельцем, — и больше ничего
    assert "нашёл" not in said and "@user" not in said
    assert not await fresh.db.get_setting("owner_ids")
    for cmd in ("/export", "/setup", "/leads", "/help"):
        await fresh.cmd(cmd, OTHER)
        assert fresh.session.calls == [], cmd


async def test_v1_code_from_log_makes_owner_once(fresh):
    code = fresh.guard.claim_code
    await seed(fresh)
    said = await fresh.say(f"/start {code}")
    assert "ищу вам клиентов" in said and "нашёл 3 лидов" in said
    assert await fresh.db.get_setting("owner_ids") == str(OWNER)
    assert fresh.guard.claim_code is None  # код одноразовый
    assert await fresh.say(f"/start {code}", OTHER) == ""
    assert await fresh.db.get_setting("owner_ids") == str(OWNER)


async def test_v1_wrong_codes_rotate_the_code(fresh):
    code = fresh.guard.claim_code
    for i in range(MAX_CLAIM_FAILS):
        assert "Код владельца" in await fresh.say(f"/start deadbee{i}", OTHER)
    assert fresh.guard.claim_code and fresh.guard.claim_code != code
    await fresh.cmd(f"/start {code}", OTHER)  # старый код уже не действует
    assert not await fresh.db.get_setting("owner_ids")


async def test_v1_group_and_anonymous_admin_cannot_become_owner(fresh):
    code = fresh.guard.claim_code
    assert await fresh.say("/start", ANON, GROUP) == ""
    assert await fresh.say(f"/start {code}", ANON, GROUP) == ""
    assert await fresh.say(f"/start {code}", OWNER, GROUP) == ""  # код принимается только в личке
    await fresh.cmd(f"/start {code}", ANON)
    assert not await fresh.db.get_setting("owner_ids") and fresh.guard.claim_code == code
    # если в старой базе владельцем записан анонимный админ — он не владелец, код снова нужен
    await fresh.db.set_setting("owner_ids", str(ANON))
    assert ANON in ANONYMOUS_IDS and await fresh.guard.needs_owner() and not await fresh.guard.is_owner(ANON)
    assert "ищу вам клиентов" in await fresh.say(f"/start {code}")
    assert await fresh.db.get_setting("owner_ids") == str(OWNER)


async def test_v1_startup_prints_code_only_without_owner(fresh, env, caplog):
    caplog.set_level(logging.INFO)
    fresh.guard.claim_code = None
    await main_mod.announce_owner_code(fresh.guard, fresh.bot)
    code = fresh.guard.claim_code
    assert code and f"/start {code}" in caplog.text and f"t.me/testbot?start={code}" in caplog.text
    caplog.clear()
    await main_mod.announce_owner_code(env.guard, env.bot)  # OWNER_IDS задан — код не нужен
    assert env.guard.claim_code is None and "Код владельца" not in caplog.text


# ---------- V2: членство в рабочей группе ----------

async def test_v2_member_who_left_loses_access_on_event(env):
    await with_group(env, MEMBER)
    assert "#1" in await env.say("/leads", MEMBER)  # в личке — доступ есть, пока он в группе
    env.session.member_status[MEMBER] = "left"
    await env.member_event(MEMBER, "left")
    assert not await env.db.is_member(MEMBER)  # кабинет смотрит в эту таблицу
    for cmd in ("/leads", "/export"):
        await env.cmd(cmd, MEMBER)
        assert not env.named("SendMessage", "SendDocument"), cmd


async def test_v2_membership_is_rechecked_with_cache(env):
    await with_group(env, MEMBER)
    for _ in range(3):
        assert "#1" in await env.say("/leads", MEMBER)
        assert not env.named("GetChatMember")  # свежий ответ Telegram — в кэше, лишних запросов нет
    env.session.member_status[MEMBER] = "kicked"  # событие не дошло, но кэш устарел
    env.guard.member_ttl = 0
    assert await env.say("/leads", MEMBER) == ""
    assert not await env.db.is_member(MEMBER)
    assert await env.press("take:1", MEMBER) == ["Нет доступа"]


async def test_v2_telegram_down_keeps_last_answer_but_never_grants(env):
    await with_group(env, MEMBER)
    env.guard.member_ttl = 0
    env.session.member_error = True
    assert "#1" in await env.say("/leads", MEMBER)  # сеть моргнула — участника не выкидываем
    assert await env.db.is_member(MEMBER)
    env.guard._checked.clear()  # после перезапуска кэша нет: не знаем — не пускаем, но и из команды не удаляем
    assert await env.say("/leads", MEMBER) == ""
    assert await env.db.is_member(MEMBER)


async def test_v2_sweep_cleans_team_for_cabinet(env):
    await with_group(env, MEMBER, OTHER)
    env.session.member_status[MEMBER] = "left"
    assert await env.guard.sweep(pause=0) == [MEMBER]
    assert not await env.db.is_member(MEMBER) and await env.db.is_member(OTHER)
    assert not await env.guard.allowed_id(MEMBER) and await env.guard.allowed_id(OTHER) and await env.guard.allowed_id(OWNER)
    assert "chat_member" in env.dp.resolve_used_update_types()  # иначе Telegram не пришлёт событие о выходе


# ---------- V4–V6, V9, V10: секреты ----------

async def test_v4_proxy_password_is_never_shown(env):
    env.settings.proxy_url = "http://user:PASSw0rd@proxy.example:8080"
    said = await env.say("/setup")
    assert "PASSw0rd" not in said and "proxy.example:8080" in said
    st = {x["key"]: x for x in await env.deps.extra["integrations"].status()}
    assert st["proxy_url"]["secret"] is True and "PASSw0rd" not in str(st["proxy_url"])
    assert "PASSw0rd" not in await env.say("/status")


REAL_CLIENT = httpx.AsyncClient


def real_http(monkeypatch, handler):
    """Настоящий httpx (со всем его логированием), но ответы — из handler."""
    seen: list[httpx.Request] = []
    real = REAL_CLIENT

    def reply(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(integ_mod.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(reply), **kw))
    return seen


@pytest.mark.parametrize("key", ["threads_access_token", "ig_access_token"])
async def test_v5_token_is_not_in_url_or_log(env, monkeypatch, caplog, key):
    caplog.set_level(logging.INFO)
    seen = real_http(monkeypatch, lambda r: httpx.Response(200, json={"id": "1"}))
    assert await validate(key, SECRET, env.settings) is None
    assert SECRET not in str(seen[0].url) and seen[0].headers["authorization"] == f"Bearer {SECRET}"
    assert SECRET not in caplog.text


def test_v5_httpx_request_log_is_silenced():
    logging.getLogger("httpx").setLevel(logging.NOTSET)
    main_mod.setup_logging()
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
    assert not logging.getLogger("httpcore").isEnabledFor(logging.INFO)


async def test_v6_key_is_not_echoed_back(env, monkeypatch, caplog):
    caplog.set_level(logging.INFO)

    class Boom:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            raise httpx.LocalProtocolError(f"Illegal header value b'AIzaSy{SECRET}'")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(integ_mod.httpx, "AsyncClient", Boom)
    await env.press("su:gemini_api_key")
    said = await env.say(f"AIzaSy{SECRET[:10]}\n{SECRET[10:]}")  # ключ вставлен в две строки
    assert "Не принято" in said and "сеть" in said
    assert SECRET not in said and SECRET[:10] not in said and SECRET not in caplog.text
    # сервис сам повторил ключ в тексте ошибки (так делает Meta) — вырезаем
    real_http(monkeypatch, lambda r: httpx.Response(400, json={"error": {"message": f"Malformed access token {SECRET}"}}))
    err = await validate("ig_access_token", SECRET, env.settings)
    assert err and SECRET not in err and "Malformed access token" in err


async def test_v6_two_line_key_is_joined(env, monkeypatch):
    seen = real_http(monkeypatch, lambda r: httpx.Response(200, json={}))
    await env.press("su:gemini_api_key")
    assert "подключено" in await env.say("AIzaSyFIRST\nSECONDLINE12345")
    assert seen[0].headers["x-goog-api-key"] == "AIzaSyFIRSTSECONDLINE12345"
    assert env.settings.gemini_api_key == "AIzaSyFIRSTSECONDLINE12345"


async def test_v7_non_ascii_key_is_rejected_and_input_mode_ends(env, monkeypatch):
    real_http(monkeypatch, lambda r: httpx.Response(200, json={}))
    await env.press("su:gemini_api_key")
    said = await env.say(f"ключ {SECRET}")
    assert "Не принято" in said and "русские буквы" in said and "Проверяю" not in env.texts()[-1]
    assert not env.settings.gemini_api_key
    await env.cmd("коллеги, созвон в 15:00")  # обычное сообщение больше не считается ключом
    assert env.session.calls == []


async def test_v7_crash_inside_check_still_ends_input_mode(env, monkeypatch):
    async def boom(key, value, s):
        raise RuntimeError(f"упало на {value}")

    monkeypatch.setattr(integ_mod, "validate", boom)
    await env.press("su:gemini_api_key")
    said = await env.say(SECRET)
    assert "Не принято" in said and SECRET not in said
    await env.cmd("ещё одно сообщение")
    assert env.session.calls == []


async def test_v8_any_command_cancels_key_input(env):
    await env.press("su:gemini_api_key")
    assert "Лидов пока нет" in await env.say("/leads")
    await env.cmd("коллеги, созвон в 15:00")
    assert env.session.calls == []  # не удалено и не проверено как ключ


async def test_v8_key_input_expires_after_five_minutes(env, monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(app_mod.time, "time", lambda: now[0])
    await env.press("su:gemini_api_key")
    now[0] += app_mod.STATE_TTL + 1
    await env.cmd("коллеги, созвон в 15:00")
    assert env.session.calls == []
    # а в пределах пяти минут значение принимается
    real_http(monkeypatch, lambda r: httpx.Response(200, json={}))
    await env.press("su:gemini_api_key")
    now[0] += app_mod.STATE_TTL - 5
    assert "подключено" in await env.say("AIzaSyTESTKEY1234567890")


async def test_m11_command_cancels_waiting_for_post_topic(env):
    assert "о чём" in await env.say("/post")
    await env.say("/leads")
    await env.cmd("это просто сообщение, а не тема поста")
    assert env.session.calls == [] and await env.factory.store.recent(10) == []
    # сам режим по-прежнему работает
    await env.say("/post")
    await env.cmd("сколько стоит сайт")
    assert len(await env.factory.store.recent(10)) == 1


async def test_v9_keys_are_accepted_only_in_private_chat(env):
    env.settings.gemini_api_key = "AIzaSyTESTKEY1234567890"
    said = await env.say("/setup", chat_id=GROUP)
    assert "только в личном чате" in said and "AIza" not in said and not env.named("SendMessage")[0].get("reply_markup")
    alerts = await env.press("su:gemini_api_key", chat_id=GROUP)
    assert "только в личном чате" in alerts[0] and not env.named("SendMessage")
    await env.cmd(SECRET, chat_id=GROUP)  # режим ввода в группе не включился — сообщение не трогаем
    assert env.session.calls == []
    assert "только в личном чате" in (await env.press("sux:gemini_api_key", chat_id=GROUP))[0]
    assert env.settings.gemini_api_key


async def test_v9_failed_delete_is_reported(env, monkeypatch):
    real_http(monkeypatch, lambda r: httpx.Response(200, json={}))
    env.session.delete_fails = True
    await env.press("su:gemini_api_key")
    said = await env.say("AIzaSyTESTKEY1234567890")
    assert "подключено" in said and "удалите его из чата вручную" in said


async def test_v10_checks_are_real_or_honestly_absent(env, monkeypatch):
    seen = real_http(monkeypatch, lambda r: httpx.Response(403, text="Invalid key"))
    s = env.settings
    assert await validate("threads_user_id", "не число", s)
    assert "число" in await validate("threads_user_id", "not-a-number", s)
    assert "число" in await validate("ig_user_id", "12ab", s)
    assert await validate("threads_user_id", "17841400000000000", s) is None
    assert "googleusercontent" in await validate("yt_client_id", "anything-goes", s)
    assert await validate("yt_client_id", "123-abc.apps.googleusercontent.com", s) is None
    assert seen == []  # всё это — проверка формата, без сети
    err = await validate("yandex_geosearch_key", "garbage-not-a-key", s)
    assert "Яндекс не принял ключ" in err and "403" in err and len(seen) == 1
    # что нельзя проверить — так и говорим, а не «уже работает»
    await env.press("su:yt_client_id")
    assert "Проверен только формат" in await env.say("123-abc.apps.googleusercontent.com")
    await env.press("su:yt_client_secret")
    prompt = env.said()
    assert "проверить его отдельно нельзя" in prompt and "живым запросом" not in prompt
    said = await env.say("GOCSPX-anything")
    assert "сохранено без проверки" in said and "уже работает" not in said
    await env.press("su:proxy_url")
    assert "Проверен только формат" in await env.say("http://u:p@host:8080")


# ---------- M10: кнопки без проверки доступа ----------

async def test_m10_stranger_cannot_cancel_setup_or_open_niches(env):
    await env.press("su:gemini_api_key")
    assert await env.press("suc", OTHER) == ["Только владелец"]
    assert not env.named("DeleteMessage", "SendMessage")
    assert await env.press("ob:niche", OTHER) == ["Нет доступа"]
    assert not env.named("SendMessage")
    assert await env.press("sux:gemini_api_key", OTHER) == ["Только владелец"]


def all_commands(dp) -> list[str]:
    cmds = set()
    for router in dp.sub_routers:
        for h in router.message.handlers:
            for f in h.filters or []:
                if isinstance(f.callback, Command):
                    cmds.update(f.callback.commands)
    return sorted(cmds)


def all_buttons() -> list[str]:
    """Все кнопки бота — прямо из исходников, чтобы новая кнопка без проверки доступа не проскочила."""
    data = set()
    for path in (Path(integ_mod.__file__).parent / "bot").glob("*.py"):
        src = path.read_text(encoding="utf-8")
        data.update(p + "1" for p in re.findall(r'F\.data\.startswith\("([^"]+)"\)', src))
        data.update(re.findall(r'F\.data == "([^"]+)"', src))
    data.update({"st:1:deal", "fu:1:24", "su:gemini_api_key", "sux:gemini_api_key", "nz:realtor", "cs:1:1:10"})
    return sorted(data)


async def test_stranger_gets_no_answer_to_any_command_or_button(env):
    await seed(env, 1)
    await env.say("/post тема")  # есть лид №1 и материал №1 — кнопкам было бы что показать
    commands, buttons = all_commands(env.dp), all_buttons()
    assert len(commands) + len(buttons) >= 54 and "start" in commands and "take:1" in buttons
    before = (await env.db.get_lead(1))["status"], (await env.factory.store.get(1))["status"]
    for c in commands:
        for text in (f"/{c}", f"/{c} 1", f"/{c} @x текст"):
            await env.cmd(text, OTHER)
            assert env.session.calls == [], text
            await env.cmd(text, OTHER, GROUP)  # и в группе, которая не рабочая
            assert env.session.calls == [], text
    for b in buttons:
        calls = await env.tap(b, OTHER)
        assert {n for n, _ in calls} <= {"AnswerCallbackQuery"}, b
        assert all(d.get("text") in ("Нет доступа", "Только владелец") for _, d in calls), (b, calls)
    await env.cmd("просто текст", OTHER)
    assert env.session.calls == []
    assert before == ((await env.db.get_lead(1))["status"], (await env.factory.store.get(1))["status"])
    assert not await env.db.is_member(OTHER)


# ---------- V17, M5–M9: длинные ответы, экранирование, проверка ввода ----------

async def test_v17_long_lead_list_is_sent_in_parts(env):
    from leadhunter.scoring import ScoreResult
    for i in range(30):
        lead = Lead(source="rss:very-long-board-name-example.com/projects/web", external_id=f"L:{i}", contact=f"@u{i}", url=f"https://t.me/c/1/{i}",
                    title=f"Нужен сайт для сети салонов красоты «Орхидея» №{i} " + "очень подробно " * 5, text="Ищу кто сделает сайт")
        await env.db.save_lead(lead, 8, ScoreResult(8, ["сайт"], ["ищу"], []), service="сайт")
    await env.cmd("/leads 30")
    parts = env.texts()
    assert len(parts) > 1 and not [n for n, _ in env.session.calls if n.endswith(":REJECTED")]
    assert all(f"#{i} " in "\n".join(parts) for i in range(1, 31))
    assert "внутренняя ошибка" not in env.said()


async def test_v17_source_and_service_are_escaped(env):
    await seed(env, 2)
    await env.db.conn.execute("UPDATE leads SET service = ?, source = ? WHERE id = 1", ("сайт <срочно>", "rss:<x>&y"))
    await env.db.conn.execute("UPDATE leads SET service = ? WHERE id = 2", ('<a href="https://evil.example">жми</a>',))
    await env.db.conn.commit()
    for cmd in ("/leads", "/digest", "/stats"):
        said = await env.say(cmd)
        assert "внутренняя ошибка" not in said and "evil.example\">жми</a>" not in said, cmd
        assert "rss:&lt;x&gt;&amp;y" in said, cmd
    assert "сайт &lt;срочно&gt;" in await env.say("/leads")


def test_split_text_keeps_lines_and_markup_whole():
    text = "\n".join(f"#{i} <b>строка</b> &lt;тег&gt; 🔥 " + "я" * 150 for i in range(100))
    parts = split_text(text)
    assert len(parts) > 3 and "\n".join(parts) == text
    assert all(tg_html_error(p) is None for p in parts)
    one_line = "<b>" + "&lt;я&gt;🔥" * 2000 + "</b>"  # одна строка длиннее лимита — режем, не разрывая &сущности;
    chunks = split_text(one_line)
    assert "".join(chunks) == one_line and all(len(c.encode("utf-16-le")) // 2 <= 4000 for c in chunks)
    assert all(not re.search(r"&[a-z]*$", c) for c in chunks)
    assert split_text("") == [] and split_text("коротко") == ["коротко"]


async def test_m5_strange_numbers_get_an_answer(env):
    await seed(env, 1)
    assert "Воронка" in await env.say("/funnel 99999999999")
    assert "Воронка" in await env.say("/funnel ²")
    assert "#1" in await env.say("/leads ²") and "#1" in await env.say("/leads 99999999999999999999999")
    assert "Формат" in await env.say("/team_del 99999999999999999999999")
    assert "Формат" in await env.say("/rule_del ²")
    for bad in ("/fresh nan 5", "/fresh inf 5", "/fresh 2 99999999999999999999", "/fresh -1 5", "/fresh 2 ²"):
        assert "Формат" in await env.say(bad), bad
    assert await env.db.get_setting("board_max_age_hours") is None
    assert "моложе 2 ч" in await env.say("/fresh 2 10")
    assert parse_int("²", 0, 10) is None and parse_int("٣", 0, 10) is None and parse_int(" 7 ", 0, 10) == 7


async def test_m6_kw_add_and_template_set_escape_input(env):
    said = await env.say("/kw_add topic <b>x сайт")
    assert "&lt;b&gt;x" in said and "внутренняя ошибка" not in said
    said = await env.say("/template_set <i>сайт Здравствуйте!")
    assert "&lt;i&gt;сайт" in said and "сохранён" in said
    assert "&lt;i&gt;сайт" in await env.say("/templates")


async def test_m7_threshold_only_zero_to_ten(env):
    assert "Порог: 6" in await env.say("/threshold 6")
    for bad in ("/threshold 99", "/threshold 11", "/threshold ²", "/threshold -1", "/threshold шесть"):
        assert "от 0 до 10" in await env.say(bad), bad
        assert await env.db.get_setting("threshold") == "6"
    assert "Порог: 0" in await env.say("/threshold 0") and "Порог: 10" in await env.say("/threshold 10")


async def test_m8_empty_phrase_list_keeps_old_phrases(env):
    await env.say("/phrases_set нужен лендинг; ищу разработчика")
    for bad in ("/phrases_set ;;;", "/phrases_set  ;  ; "):
        assert "прежние оставил" in await env.say(bad)
    assert await env.deps.phrases.get() == ["нужен лендинг", "ищу разработчика"]


async def test_m9_unbalanced_rule_is_rejected(env):
    for bad in ("/rule_add +(a|", "/rule_add +сайт )", "/rule_add +((а|б))", '/rule_add +"под ключ'):
        assert "Ошибка в правиле" in await env.say(bad), bad
    assert await env.db.rules() == []
    assert "добавлено" in await env.say('/rule_add +сайт +(нужен|ищу) -"под ключ"')


# ---------- «ни одна команда не падает молча» ----------

async def test_crashed_handler_tells_the_owner_and_logs(env, monkeypatch, caplog):
    async def boom(*a, **k):
        raise RuntimeError("база сломалась")

    monkeypatch.setattr(env.db, "stats", boom)
    said = await env.say("/stats")
    assert "внутренняя ошибка (RuntimeError)" in said and "база сломалась" not in said
    assert "обработчик упал" in caplog.text and "база сломалась" in caplog.text
    alerts = await env.press("take:не-число")
    assert "внутренняя ошибка" in alerts[0]


# ---------- V19: фоновые задачи и туннель ----------

async def test_v19_dead_background_task_is_logged_and_reported(env, caplog):
    failed = []

    async def on_fail(name, reason):
        failed.append((name, reason))

    async def busy_port():
        raise OSError(errno.EADDRINUSE, "error while attempting to bind on address ('0.0.0.0', 8080): address already in use")

    async def leaky():
        raise RuntimeError(f"запрос https://api.example/?access_token={SECRET} не прошёл")

    env.settings.threads_access_token = SECRET
    tasks = [main_mod.watch_task(asyncio.create_task(busy_port(), name="miniapp"), env.settings, on_fail),
             main_mod.watch_task(asyncio.create_task(leaky(), name="threads"), env.settings, on_fail)]
    await asyncio.wait(tasks)
    for _ in range(5):
        await asyncio.sleep(0)
    assert "фоновая задача miniapp упала" in caplog.text and f"порт {env.settings.miniapp_port} занят" in caplog.text
    by_name = dict(failed)
    assert "MINIAPP_PORT" in by_name["miniapp"]
    assert SECRET not in by_name["threads"] and "RuntimeError" in by_name["threads"]
    # отменённая при остановке бота задача — не авария
    caplog.clear()
    quiet = main_mod.watch_task(asyncio.create_task(asyncio.sleep(60), name="sla"), env.settings, on_fail)
    quiet.cancel()
    await asyncio.wait([quiet])
    await asyncio.sleep(0)
    assert "sla" not in caplog.text and len(failed) == 2


async def test_v19_app_command_explains_dead_cabinet(env):
    env.deps.extra["miniapp_url"] = "https://old.trycloudflare.com/app"
    env.deps.extra["miniapp_error"] = "порт 8080 занят другой программой — кабинет не запущен"
    said = await env.say("/app")
    assert "порт 8080 занят" in said and not env.named("SendMessage")[0].get("reply_markup")
    assert "порт 8080 занят" in await env.say("/status")


def fake_cloudflared(tmp_path, lifetime: float) -> str:
    """Подставной cloudflared: печатает новый адрес при каждом запуске и через lifetime секунд умирает."""
    counter = tmp_path / "runs"
    script = tmp_path / "cloudflared"
    script.write_text(f"""#!{sys.executable}
import pathlib, sys, time
p = pathlib.Path({str(counter)!r})
n = int(p.read_text()) + 1 if p.exists() else 1
p.write_text(str(n))
sys.stderr.write(f"INF |  https://fake-tunnel-{{n}}.trycloudflare.com  |\\n")
sys.stderr.flush()
time.sleep({lifetime} if n == 1 else 60)
sys.exit(1)
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


async def test_v19_tunnel_restarts_and_updates_menu_button(env, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    tunnel = CloudflareTunnel(8095, binary=fake_cloudflared(tmp_path, 0.3))
    assert await tunnel.start() == "https://fake-tunnel-1.trycloudflare.com"
    s, deps = env.settings, env.deps
    await main_mod.apply_miniapp_url(tunnel.url, s, deps, env.bot, env.factory)
    env.session.reset()
    got = asyncio.Event()

    async def on_url(url):
        await main_mod.apply_miniapp_url(url, s, deps, env.bot, env.factory)
        got.set()

    watcher = asyncio.create_task(tunnel.watch(on_url, restart_delay=0.05))
    try:
        await asyncio.wait_for(got.wait(), 15)
    finally:
        watcher.cancel()
        await tunnel.stop()
    assert "cloudflared завершился (код 1)" in caplog.text
    new = "https://fake-tunnel-2.trycloudflare.com/app"
    assert s.miniapp_url == new and deps.extra["miniapp_url"] == new
    assert env.factory.media.public_base == "https://fake-tunnel-2.trycloudflare.com"
    buttons = env.named("SetChatMenuButton")
    assert len(buttons) == 1 and buttons[0]["menu_button"]["web_app"]["url"] == new
    env.session.reset()
    await env.cmd("/app")
    assert env.named("SendMessage")[0]["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"] == new
