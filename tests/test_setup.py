"""Онбординг, /setup, /status и модуль подключений: сквозь бота с подменённым Bot API + юнит-тесты integrations."""
import httpx
import pytest
from aiogram import Bot, Dispatcher

from leadhunter import integrations as integ_mod
from leadhunter.bot.callbacks import build_callbacks
from leadhunter.bot.content import build_content_router
from leadhunter.bot.guard import Guard
from leadhunter.bot.handlers import BotDeps, build_router
from leadhunter.bot.notify import TelegramNotifier
from leadhunter.bot.setup import build_onboarding_callbacks, build_setup_router
from leadhunter.config import Settings
from leadhunter.content.runtime import build_content
from leadhunter.db import Database
from leadhunter.integrations import INTEGRATIONS, Integrations, load_overrides, mask, validate
from leadhunter.phrases import PhraseProvider
from leadhunter.pipeline import Pipeline
from test_bot_e2e import OTHER, OWNER
from test_content import ContentEnv, FakeLLM, MediaSession


class SetupEnv(ContentEnv):
    def calls_named(self, *names):
        return [d for n, d in self.session.calls if n in names]

    def all_texts(self):
        """И новые сообщения, и правки — мастер часто редактирует «Проверяю…»."""
        return [d.get("text", "") for d in self.calls_named("SendMessage", "EditMessageText")]


@pytest.fixture
async def env(tmp_path):
    db = Database(str(tmp_path / "s.db"))
    await db.connect()
    session = MediaSession()
    bot = Bot("1:TEST", session=session)
    notifier = TelegramNotifier(bot, db, [OWNER], hot_score=8)
    pipeline = Pipeline(db, notifier, default_threshold=5)
    guard = Guard(bot, db, [OWNER])
    deps = BotDeps(db, [], guard, PhraseProvider(db, ["нужен сайт"]), notifier.workspace, None, "Студия", 0, 24)
    deps.extra["pipeline"] = pipeline
    s = Settings(bot_token="1:TEST", content_dir=str(tmp_path / "content"), _env_file=None)
    factory = await build_content(s, db, bot, FakeLLM(), deps, notifier)
    changed = []

    async def on_change():
        changed.append(1)

    deps.extra["integrations"] = Integrations(db, s, on_change)
    deps.extra["settings"] = s
    dp = Dispatcher()
    for r in (build_router(deps), build_callbacks(deps), build_content_router(deps),
              build_setup_router(deps), build_onboarding_callbacks(deps)):
        dp.include_router(r)
    e = SetupEnv(bot, dp, session, db, notifier, pipeline, factory=factory, llm=FakeLLM(), deps=deps)
    e.settings, e.changed = s, changed
    yield e
    await db.close()


def patch_validate(monkeypatch, result):
    async def fake(key, value, s):
        return result
    monkeypatch.setattr(integ_mod, "validate", fake)


# ---------- бот ----------

async def test_start_shows_onboarding_not_help(env):
    texts = await env.cmd("/start")
    assert "Ниша — /niche" in texts[0] and "Ключ ИИ — /setup" in texts[0] and "/status" in texts[0]
    assert "Рабочая группа" not in texts[0]
    assert "Рабочая группа" in (await env.cmd("/help"))[0]


async def test_setup_lists_integrations_and_needs_owner(env):
    texts = await env.cmd("/setup")
    assert "▫️" in texts[0] and "Gemini" in texts[0]
    kb = env.calls_named("SendMessage")[0]["reply_markup"]["inline_keyboard"]
    assert {b["callback_data"] for row in kb for b in row} == {f"su:{k}" for k in INTEGRATIONS}
    assert await env.cmd("/setup", user_id=OTHER) == []


async def test_setup_key_accepted_and_stored(env, monkeypatch):
    patch_validate(monkeypatch, None)
    calls = await env.tap("su:gemini_api_key")
    assert any("значение" in d.get("text", "") for n, d in calls if n == "SendMessage")
    await env.cmd("AIzaSyTESTKEY1234567890")
    assert "DeleteMessage" in env.session.names()  # ключ не остаётся в чате
    assert any("подключено" in t for t in env.all_texts())
    assert await env.db.get_setting("secret:gemini_api_key") == "AIzaSyTESTKEY1234567890"
    assert env.settings.gemini_api_key == "AIzaSyTESTKEY1234567890"
    assert env.changed == [1]
    assert "✅" in (await env.cmd("/setup"))[0]


async def test_setup_key_rejected_not_stored(env, monkeypatch):
    patch_validate(monkeypatch, "плохой ключ")
    await env.tap("su:gemini_api_key")
    await env.cmd("badkey")
    assert "DeleteMessage" in env.session.names()
    assert any("Не принято" in t and "плохой ключ" in t for t in env.all_texts())
    assert not await env.db.get_setting("secret:gemini_api_key")
    assert not env.settings.gemini_api_key and env.changed == []


async def test_setup_remove_key(env, monkeypatch):
    patch_validate(monkeypatch, None)
    await env.tap("su:gemini_api_key")
    await env.cmd("AIzaSyTESTKEY1234567890")
    await env.tap("sux:gemini_api_key")
    assert not env.settings.gemini_api_key
    assert not await env.db.get_setting("secret:gemini_api_key")
    assert env.changed == [1, 1]


async def test_setup_requires_dependency_first(env):
    calls = await env.tap("su:threads_user_id")
    assert any(d.get("show_alert") for n, d in calls if n == "AnswerCallbackQuery")
    assert not any("значение" in d.get("text", "") for n, d in calls if n == "SendMessage")


async def test_setup_callbacks_owner_only(env):
    calls = await env.tap("su:gemini_api_key", user_id=OTHER)
    assert "Только владелец" in [d for n, d in calls if n == "AnswerCallbackQuery"][0]["text"]


async def test_onboarding_niche_flow(env):
    calls = await env.tap("ob:niche")
    kb = [d for n, d in calls if n == "SendMessage"][0]["reply_markup"]["inline_keyboard"]
    assert any(b["callback_data"] == "nz:realtor" for row in kb for b in row)
    await env.tap("nz:realtor")
    assert "Риелтор" in await env.db.get_setting("profile")
    assert await env.db.get_setting("niche") == "realtor"


async def test_status(env):
    texts = await env.cmd("/status")
    assert "Состояние системы" in texts[0] and "Публикую в" in texts[0]


# ---------- integrations: юнит ----------

def test_mask():
    assert mask(None) == "" and mask("") == ""
    assert mask("short") == "•••••"
    assert mask("AIzaSyABCDEFGH123") == "AIza…123"


async def test_load_overrides_fills_only_empty(tmp_path):
    db = Database(str(tmp_path / "o.db"))
    await db.connect()
    await db.set_setting("secret:gemini_api_key", "from-db")
    await db.set_setting("secret:vk_token", "vk-from-db")
    s = Settings(bot_token="1:T", gemini_api_key="from-env", _env_file=None)
    applied = await load_overrides(db, s)
    assert s.gemini_api_key == "from-env" and s.vk_token == "vk-from-db"
    assert applied == ["vk_token"]
    await db.close()


class FakeResp:
    def __init__(self, status=200, data=None, text=""):
        self.status_code, self._data, self.text = status, data if data is not None else {}, text

    def json(self):
        return self._data


class FakeClient:
    def __init__(self, resp):
        self.resp, self.requests = resp, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kw):
        self.requests.append(("GET", url, kw))
        return self.resp

    post = get


def patch_http(monkeypatch, resp):
    client = FakeClient(resp)
    monkeypatch.setattr(integ_mod.httpx, "AsyncClient", lambda **kw: client)
    return client


async def test_validate_gemini(monkeypatch):
    s = Settings(bot_token="1:T", _env_file=None)
    c = patch_http(monkeypatch, FakeResp(200))
    assert await validate("gemini_api_key", " key123 ", s) is None
    assert c.requests[0][2]["headers"]["x-goog-api-key"] == "key123"
    patch_http(monkeypatch, FakeResp(401, {"error": {"message": "API key not valid"}}))
    err = await validate("gemini_api_key", "bad", s)
    assert "401" in err and "API key not valid" in err


async def test_validate_vk_error_and_ok(monkeypatch):
    s = Settings(bot_token="1:T", _env_file=None)
    patch_http(monkeypatch, FakeResp(200, {"error": {"error_msg": "User authorization failed"}}))
    assert "User authorization failed" in await validate("vk_token", "t", s)
    patch_http(monkeypatch, FakeResp(200, {"response": 1}))
    assert await validate("vk_token", "t", s) is None


async def test_validate_network_error_and_empty(monkeypatch):
    s = Settings(bot_token="1:T", _env_file=None)

    class Boom:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            raise httpx.ConnectError("нет сети")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(integ_mod.httpx, "AsyncClient", Boom)
    assert "сеть" in await validate("gemini_api_key", "k", s)
    assert await validate("gemini_api_key", "  ", s) == "пустое значение"


async def test_validate_proxy_format_without_network(monkeypatch):
    s = Settings(bot_token="1:T", _env_file=None)

    monkeypatch.setattr(integ_mod.httpx, "AsyncClient", lambda **kw: FakeClient(FakeResp()))
    assert "формат" in await validate("proxy_url", "host:8080", s)
    assert await validate("proxy_url", "http://u:p@host:8080", s) is None
    assert await validate("proxy_url", "socks5://host:1080", s) is None


async def test_validate_yt_refresh_needs_client(monkeypatch):
    patch_http(monkeypatch, FakeResp(200))
    assert "Client ID" in await validate("yt_refresh_token", "r", Settings(bot_token="1:T", _env_file=None))
    s = Settings(bot_token="1:T", yt_client_id="i", yt_client_secret="s", _env_file=None)
    assert await validate("yt_refresh_token", "r", s) is None


async def test_integrations_summary_and_status(tmp_path):
    db = Database(str(tmp_path / "i.db"))
    await db.connect()
    s = Settings(bot_token="1:T", _env_file=None)
    it = Integrations(db, s)
    assert not any(it.summary().values())
    s.gemini_api_key, s.threads_access_token = "k" * 12, "t"
    sm = it.summary()
    assert sm["ai"] and sm["media_ai"] and not sm["threads"]
    s.threads_user_id = "42"
    s.openai_api_key, s.gemini_api_key = "o", None
    sm = it.summary()
    assert sm["threads"] and sm["ai"] and not sm["media_ai"]
    s.yt_client_id = s.yt_client_secret = "x"
    assert not it.summary()["youtube"]
    s.yt_refresh_token = "r"
    assert it.summary()["youtube"]
    st = {x["key"]: x for x in await it.status()}
    assert st["openai_api_key"]["set"] and st["openai_api_key"]["masked"] == "•"
    assert st["threads_user_id"]["masked"] == "42" and st["vk_token"]["set"] is False
    assert await it.set("nope", "x") == "неизвестное подключение"
    await db.close()
