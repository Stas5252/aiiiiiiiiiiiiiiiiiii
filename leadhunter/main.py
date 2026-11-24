import asyncio
import errno
import html
import logging
import traceback

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties

from .bot.app import build_dispatcher
from .bot.guard import Guard
from .bot.handlers import BotDeps
from .bot.notify import TelegramNotifier
from .config import Settings
from .analysis import DossierService
from .bot.dossier import auto_dossier
from .content.runtime import build_content, build_llm, reconfigure_ai
from .content.scheduler import autopilot_loop, content_loop
from .db import Database
from .integrations import Integrations, load_overrides, scrub
from .loops import digest_loop, followup_loop, sla_loop
from .phrases import PhraseProvider, default_phrases
from .pipeline import Pipeline
from .sources import build_sources
from .sources.telegram_chats import TelegramChatsSource
from .webhook import MultiNotifier, WebhookNotifier

log = logging.getLogger("leadhunter")


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx на INFO пишет полный адрес каждого запроса, а в адресах бывают ключи и токены
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def describe_failure(name: str, exc: BaseException, s: Settings) -> str:
    """Короткое объяснение для владельца, почему остановилась фоновая задача (без секретов)."""
    if name == "miniapp" and isinstance(exc, OSError) and exc.errno == errno.EADDRINUSE:
        return (f"порт {s.miniapp_port} занят другой программой — кабинет не запущен. "
                "Освободите порт или задайте другой MINIAPP_PORT в .env и перезапустите бота")
    return scrub(f"{type(exc).__name__}: {exc}", s)[:300]


def watch_task(task: asyncio.Task, s: Settings, on_fail=None) -> asyncio.Task:
    """Фоновая задача не должна умирать молча: падение — в лог и (через on_fail) владельцу."""
    def done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is None:
            log.warning("фоновая задача %s завершилась и больше не работает", t.get_name())
            return
        reason = describe_failure(t.get_name(), exc, s)
        # трассировку пишем сами и через scrub: в тексте исключения бывает адрес запроса с ключом
        log.error("фоновая задача %s упала: %s\n%s", t.get_name(), reason, scrub("".join(traceback.format_exception(exc)), s))
        if on_fail:
            alert = asyncio.get_running_loop().create_task(on_fail(t.get_name(), reason))
            _alerts.add(alert)
            alert.add_done_callback(_alerts.discard)

    task.add_done_callback(done)
    return task


_alerts: set[asyncio.Task] = set()


async def apply_miniapp_url(base_url: str, s: Settings, deps: BotDeps, bot: Bot, factory=None) -> None:
    """Новый адрес туннеля: кнопка меню, /app и ссылки на файлы контента должны вести на него."""
    from aiogram.types import MenuButtonWebApp, WebAppInfo

    s.miniapp_url = base_url.rstrip("/") + "/app"
    deps.extra["miniapp_url"] = s.miniapp_url
    media = getattr(factory, "media", None)
    if media is not None and hasattr(media, "public_base"):
        media.public_base = base_url.rstrip("/")
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Кабинет", web_app=WebAppInfo(url=s.miniapp_url)))
    except Exception:
        log.exception("не удалось обновить кнопку меню Mini App")
    log.info("Mini App: новый адрес %s", s.miniapp_url)


async def announce_owner_code(guard: Guard, bot: Bot) -> None:
    """Владельца нет ни в .env, ни в базе: печатаем одноразовый код — владельцем станет тот, кто пришлёт его боту в личку."""
    if not await guard.needs_owner():
        return
    code = guard.new_claim_code()
    link = ""
    try:
        me = await bot.me()
        if me.username:
            link = f" (или откройте https://t.me/{me.username}?start={code})"
    except Exception as e:
        log.warning("не удалось узнать имя бота: %s", type(e).__name__)
    log.warning("Владелец бота не задан. Код владельца: %s — напишите боту в личку: /start %s%s. "
                "Код одноразовый; вместо него можно вписать OWNER_IDS=ваш_id в .env.", code, code, link)


async def run() -> None:
    s = Settings()
    db = Database(s.db_path)
    await db.connect()
    applied = await load_overrides(db, s)  # ключи, введённые через /setup или кабинет
    if applied:
        log.info("подключения из базы: %s", ", ".join(applied))

    bot = Bot(s.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    tg_notifier = TelegramNotifier(bot, db, s.owner_ids, outreach_enabled=s.tg_outreach_limit > 0,
                                   hot_score=s.hot_score, deliver_dm=s.deliver_dm)
    notifier = MultiNotifier(tg_notifier, WebhookNotifier(s.webhook_url) if s.webhook_url else None)
    llm = build_llm(s)
    pipeline = Pipeline(db, notifier, s.score_threshold, llm, max_age_hours=s.max_age_hours,
                        llm_min_score=s.llm_min_score, profile=s.profile, spam_chats=s.spam_chats_per_hour)

    phrases = PhraseProvider(db, default_phrases(s))
    sources = build_sources(s, db, phrases)
    for src in sources:
        src.alert = tg_notifier.send
    tg = next((x for x in sources if isinstance(x, TelegramChatsSource)), None)
    if tg:
        tg.on_message_stats = db.bump_chat_stats
        pipeline.author_bio = tg.get_bio
    log.info("источники: %s; ИИ: %s", ", ".join(x.name for x in sources) or "нет", llm.model if llm else "выкл")

    guard = Guard(bot, db, s.owner_ids)
    deps = BotDeps(db, sources, guard, phrases, tg_notifier.workspace, llm, s.profile, s.tg_outreach_limit,
                   s.followup_hours, s.digest_tz)
    deps.extra["redeliver"] = tg_notifier.notify_row
    deps.extra["threshold"] = s.score_threshold
    if tg:
        from .discovery import ChatDiscovery
        deps.discover = ChatDiscovery(tg, db, llm, s.discover_join_per_day)

    dossier = DossierService(db, lambda: deps.llm, proxy=s.proxy_url, get_serper_key=lambda: s.serper_api_key,
                             default_profile=s.profile)
    deps.extra["dossier"] = dossier
    pipeline.on_delivered = auto_dossier(dossier, db, bot, s.hot_score)

    dp = build_dispatcher(deps)
    deps.extra["pipeline"] = pipeline
    await announce_owner_code(guard, bot)

    tunnel = None
    if s.miniapp_tunnel == "cloudflare" and not s.miniapp_url:
        from .tunnel import CloudflareTunnel
        tunnel = CloudflareTunnel(s.miniapp_port)
        try:
            s.miniapp_url = await tunnel.start() + "/app"
        except Exception as e:
            log.error("туннель не поднялся, Mini App выключен: %s", e)
            tunnel = None
    deps.extra["miniapp_url"] = s.miniapp_url
    factory = await build_content(s, db, bot, llm, deps, tg_notifier)

    async def on_change():
        reconfigure_ai(s, deps, factory, pipeline)

    deps.extra["integrations"] = Integrations(db, s, on_change)
    deps.extra["settings"] = s

    async def on_task_fail(name: str, reason: str) -> None:
        if name == "miniapp":
            deps.extra["miniapp_error"] = reason
        await tg_notifier.send(f"⚠️ Фоновая задача «{html.escape(name)}» остановилась: {html.escape(reason)}.\n"
                               "Подробности — в логе бота.")

    tasks: list[asyncio.Task] = []

    def spawn(coro, name: str) -> None:
        tasks.append(watch_task(asyncio.create_task(coro, name=name), s, on_task_fail))

    for src in sources:
        spawn(src.run(pipeline.handle), src.name)
    spawn(sla_loop(db, tg_notifier, s.sla_minutes, s.sla_min_score), "sla")
    spawn(followup_loop(db, tg_notifier), "followup")
    spawn(content_loop(factory, deps.extra["publishers"], deps.extra["content_notify"]), "content")
    spawn(autopilot_loop(factory, deps.extra["deliver_to_team"], s.digest_tz, s.content_hour), "autopilot")
    spawn(guard.sweep_loop(), "team-check")
    if s.digest_hour is not None:
        spawn(digest_loop(db, tg_notifier, s.digest_hour, s.digest_tz), "digest")
    if s.miniapp_url:
        from .miniapp.server import start_miniapp
        spawn(start_miniapp(s, db, bot, deps), "miniapp")
    if tunnel:
        async def on_tunnel_url(url: str) -> None:
            await apply_miniapp_url(url, s, deps, bot, factory)
            await tg_notifier.send("Туннель кабинета перезапустился, адрес изменился. Кнопка «Кабинет» в меню уже обновлена; "
                                   "старые кнопки в сообщениях не откроются — напишите /app.")

        spawn(tunnel.watch(on_tunnel_url), "tunnel")
    try:
        await dp.start_polling(bot)
    finally:
        for t in tasks:
            t.cancel()
        if tunnel:
            await tunnel.stop()
        await db.close()


def main() -> None:
    setup_logging()
    asyncio.run(run())


if __name__ == "__main__":
    main()
