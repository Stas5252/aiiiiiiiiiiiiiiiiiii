"""HTTP-сервер Mini App: статика + JSON API. Авторизация — подпись initData от Telegram."""
import asyncio
import html
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from aiohttp import web

from ..db import Database
from ..filters import parse_rule
from ..models import Lead
from .auth import validate_init_data
from .landing import render_landing, render_notice, render_thanks
from .landing_guard import MERGE_WINDOW, LandingGuard, client_ip, contact_key
from .validation import (bad, json_object, path_id, query_int, safe_url, to_bool, to_color, to_dict, to_float, to_int,
                         to_str, to_str_list)

log = logging.getLogger(__name__)
STATIC = os.path.join(os.path.dirname(__file__), "static")
STATUSES = ("new", "taken", "contacted", "replied", "deal", "lost", "spam", "hot")
# «Горячий» — пометка неразобранного лида, а не конец работы: он остаётся в открытых
OPEN_STATUSES = ("new", "hot", "taken", "contacted", "replied")
FLAGS = ("paused", "autopilot", "leads_boards", "leads_social", "leads_jobs", "leads_maps", "leads_web", "format_post",
         "format_carousel", "format_reel", "format_toon", "format_aivideo", "format_ideas", "format_magnet",
         "publish_telegram", "publish_instagram", "publish_threads", "publish_vk", "publish_youtube", "schedule_enabled")
# Как и в боте: состав команды, каналы публикации и пауза — решения владельца
OWNER_ONLY = ("team_remove", "channels", "paused")
DEV_HOSTS = ("localhost", "127.0.0.1", "::1")
NO_AI = ("ИИ не подключён. Подключите его в Настройках → Подключения или командой /setup в боте. "
         "Без ИИ можно собрать карусель или видео вручную: «Заголовок | слайд 1 | слайд 2».")

APP_CSP = ("default-src 'self'; script-src 'self' https://telegram.org; style-src 'self' 'unsafe-inline'; "
           "img-src 'self' data: blob: https:; media-src 'self' https:; connect-src 'self'; object-src 'none'; "
           "base-uri 'none'; form-action 'self'; frame-ancestors 'self' https://*.telegram.org")
LANDING_CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; form-action 'self'; "
               "base-uri 'none'; frame-ancestors 'none'")
API_CSP = "default-src 'none'; frame-ancestors 'none'"
_TG_LINK = re.compile(r"(?:https?://)?t\.me/([A-Za-z][A-Za-z0-9_]{3,31})/?")
_TG_TARGET = re.compile(r"@[A-Za-z][A-Za-z0-9_]{3,31}|-?\d{5,20}")
_SECRET_IN_TEXT = re.compile(r"(?i)\b(key|token|access_token|secret|password)=[^&\s'\"]+")


def _row(r) -> dict:
    return dict(r) if r is not None else None


def _lead_row(r) -> dict:
    d = dict(r)
    d["url"] = safe_url(d.get("url"))
    return d


def _manual_content(kind: str, topic: str) -> tuple[str, dict] | None:
    """Без ИИ, как в боте: «Заголовок | слайд 1 | слайд 2» для карусели и «фраза 1 | фраза 2» для видео."""
    parts = [p.strip() for p in topic.split("|") if p.strip()]
    if len(parts) < 2:
        return None
    if kind == "carousel":
        return parts[0], {"title": parts[0], "caption": parts[0], "hashtags": [],
                          "slides": [{"title": parts[0], "text": ""}] + [{"title": "", "text": p} for p in parts[1:]]}
    if kind == "reel":
        return parts[0], {"title": parts[0], "caption": parts[0], "hashtags": [],
                          "scenes": [{"text": p, "voice": "", "duration": 3} for p in parts]}
    return None


class MiniAppAPI:
    def __init__(self, db: Database, bot_token: str, guard, deps, sources, dev_user_id: int | None = None):
        self.db = db
        self.bot_token = bot_token
        self.guard = guard
        self.deps = deps
        self.sources = sources
        self.dev_user_id = dev_user_id
        self.landing_guard = LandingGuard(bot_token)
        self._landing_lock = asyncio.Lock()

    # --- auth ---
    async def user(self, request: web.Request) -> dict:
        # Только заголовок: адрес с ?initData= оседал бы в логах доступа и истории браузера
        init_data = request.headers.get("X-Init-Data", "")
        user = validate_init_data(init_data, self.bot_token) if init_data else None
        if user is None and self.dev_user_id and not init_data:
            # Режим разработки без подписи — только при обращении по локальному имени (защита от DNS rebinding)
            if request.url.host not in DEV_HOSTS:
                raise web.HTTPForbidden(text="режим разработки доступен только с localhost")
            user = {"id": self.dev_user_id, "first_name": "dev"}
        if not isinstance(user, dict) or isinstance(user.get("id"), bool) or not isinstance(user.get("id"), int):
            raise web.HTTPUnauthorized(text="bad initData")
        uid = int(user["id"])
        # Доступ решает Guard бота: владелец или участник, который всё ещё состоит в рабочей группе
        if not await self.guard.allowed_id(uid):
            raise web.HTTPForbidden(text="Нет доступа: вы не в команде. Нажмите любую кнопку бота в рабочей группе "
                                         "или попросите владельца.")
        user["is_owner"] = await self.guard.is_owner(uid)
        return user

    @staticmethod
    def _name(user: dict) -> str:
        return " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or user.get("username") or str(user["id"])

    # --- handlers ---
    async def me(self, request):
        user = await self.user(request)
        return web.json_response({"user": user, "sources": [s.status_line() for s in self.sources]})

    async def leads(self, request):
        user = await self.user(request)
        q = request.query
        where, params = ["1=1"], []
        if q.get("status"):
            # open — все открытые; иначе один статус или несколько через запятую
            statuses = OPEN_STATUSES if q["status"] == "open" else tuple(x for x in q["status"].split(",") if x)
            if not statuses or any(x not in STATUSES for x in statuses):
                raise bad("status: неизвестный статус")
            where.append(f"status IN ({', '.join('?' * len(statuses))})"); params += statuses
        if q.get("source"):
            where.append("source = ?"); params.append(q["source"])
        if q.get("service"):
            where.append("service = ?"); params.append(q["service"])
        if q.get("q"):
            where.append("(text LIKE ? OR title LIKE ? OR author LIKE ?)")
            params += [f"%{q['q']}%"] * 3
        if q.get("mine") and q.get("mine") != "0":
            where.append("assigned_to = ?"); params.append(int(user["id"]))
        limit = query_int(request, "limit", 50, 1, 200)
        offset = query_int(request, "offset", 0, 0, 10**9)
        cur = await self.db.conn.execute(
            f"SELECT id, source, score, status, service, urgency, budget, title, text, author, contact, url, summary,"
            f" assigned_name, created_at, chat_title FROM leads WHERE {' AND '.join(where)}"
            f" ORDER BY id DESC LIMIT ? OFFSET ?", (*params, limit, offset))
        rows = [_lead_row(r) for r in await cur.fetchall()]
        for r in rows:
            r["text"] = (r["text"] or "")[:300]
        return web.json_response({"leads": rows})

    async def lead(self, request):
        await self.user(request)
        row = await self.db.get_lead(path_id(request))
        if not row:
            raise web.HTTPNotFound()
        cur = await self.db.conn.execute("SELECT status, user_name, at FROM lead_events WHERE lead_id = ? ORDER BY id",
                                         (row["id"],))
        events = [dict(r) for r in await cur.fetchall()]
        data = _lead_row(row)
        if not data.get("draft") and data.get("service"):
            data["draft"] = await self.db.get_template(data["service"]) or ""
        return web.json_response({"lead": data, "events": events})

    async def set_status(self, request):
        user = await self.user(request)
        lead_id = path_id(request)
        body = await json_object(request)
        status = body.get("status")
        if not isinstance(status, str) or status not in STATUSES:
            raise bad("bad status")
        row = await self.db.get_lead(lead_id)
        if not row:
            raise web.HTTPNotFound()
        name = self._name(user)
        if status == "hot" and row["status"] not in ("new", "hot"):
            raise web.HTTPConflict(text="лид уже в работе — «горячим» помечают только новый")
        if status in ("taken", "contacted", "replied", "deal", "lost") and not row["assigned_to"]:
            await self.db.assign(lead_id, int(user["id"]), name)
        if status == "taken":
            if not await self.db.assign(lead_id, int(user["id"]), name):
                raise web.HTTPConflict(text=f"уже взял {row['assigned_name']}")
            # «Вернуть в работу» свой закрытый лид: он уже закреплён, assign статус не трогает
            if (await self.db.get_lead(lead_id))["status"] != "taken":
                await self.db.set_status(lead_id, "taken", int(user["id"]), name)
        else:
            await self.db.set_status(lead_id, status, int(user["id"]), name)
        if status == "contacted":
            await self.db.schedule_followup(lead_id, self.deps.followup_hours)
        elif status != "hot":
            await self.db.clear_followup(lead_id)
        return web.json_response({"lead": _lead_row(await self.db.get_lead(lead_id))})

    async def stats(self, request):
        await self.user(request)
        days = query_int(request, "days", 30, 1, 365)
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = await self.db.conn.execute(
            "SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS n, SUM(status='deal') AS deals FROM leads"
            " WHERE created_at >= ? GROUP BY day ORDER BY day", (since,))
        per_day = [dict(r) for r in await cur.fetchall()]
        cur = await self.db.conn.execute("SELECT status, COUNT(*) AS n FROM leads WHERE created_at >= ? GROUP BY status", (since,))
        by_status = {r["status"]: r["n"] for r in await cur.fetchall()}
        times = await self.db.response_times(days)
        times.sort()
        return web.json_response({
            "days": days,
            "per_day": per_day,
            "by_status": by_status,
            "funnel_source": [dict(r) for r in await self.db.funnel(days, "source")],
            "funnel_service": [dict(r) for r in await self.db.funnel(days, "service")],
            "funnel_phrase": [dict(r) for r in await self.db.funnel(days, "phrase")][:15],
            "funnel_chat": [dict(r) for r in await self.db.funnel(days, "chat")][:15],
            "response": {"n": len(times), "median_min": times[len(times) // 2] if times else None,
                         "under5_pct": (sum(1 for t in times if t <= 5) * 100 // len(times)) if times else None},
            "team": [dict(r) for r in await self.db.team_stats(days)],
            "sources": [s.status_line() for s in self.sources],
        })

    async def settings_get(self, request):
        user = await self.user(request)
        kw = await self.db.get_keywords()
        from ..freshness import Freshness
        from ..niches import NICHES
        from ..content.factory import brand_from_setting
        fr = Freshness.from_settings({"board_max_age_hours": await self.db.get_setting("board_max_age_hours"),
                                      "max_competitors": await self.db.get_setting("max_competitors")})
        brand = brand_from_setting(await self.db.get_setting("brand"))
        factory = self.deps.extra.get("factory")
        channels = await factory.store.channels() if factory else {}
        integ = self.deps.extra.get("integrations")
        return web.json_response({
            "threshold": await self.db.get_setting("threshold", str(self.deps.extra.get("threshold", 5))),
            "profile": await self.db.get_setting("profile") or self.deps.default_profile,
            "geo": await self.db.get_setting("geo", ""),
            "format_post": await self.db.get_setting("format_post", "1") == "1",
            "format_carousel": await self.db.get_setting("format_carousel", "1") == "1",
            "format_reel": await self.db.get_setting("format_reel", "1") == "1",
            "format_toon": await self.db.get_setting("format_toon", "1") == "1",
            "format_aivideo": await self.db.get_setting("format_aivideo", "1") == "1",
            "format_ideas": await self.db.get_setting("format_ideas", "1") == "1",
            "format_magnet": await self.db.get_setting("format_magnet", "1") == "1",
            "publish_telegram": await self.db.get_setting("publish_telegram", "1") == "1",
            "publish_instagram": await self.db.get_setting("publish_instagram", "1") == "1",
            "publish_threads": await self.db.get_setting("publish_threads", "1") == "1",
            "publish_vk": await self.db.get_setting("publish_vk", "1") == "1",
            "publish_youtube": await self.db.get_setting("publish_youtube", "1") == "1",
            "schedule_enabled": await self.db.get_setting("schedule_enabled", "1") == "1",
            "leads_boards": await self.db.get_setting("leads_boards", "1") == "1",
            "leads_social": await self.db.get_setting("leads_social", "1") == "1",
            "leads_jobs": await self.db.get_setting("leads_jobs", "0") == "1",
            "leads_maps": await self.db.get_setting("leads_maps", "1") == "1",
            "leads_web": await self.db.get_setting("leads_web", "0") == "1",
            "paused": (await self.db.get_setting("paused")) == "1",
            "phrases": await self.deps.phrases.get(),
            "rules": [dict(r) for r in await self.db.rules()],
            "keywords": {"topic": kw.topic, "intent": kw.intent, "negative": kw.negative},
            "templates": await self.db.templates(),
            "ignored": await self.db.get_ignored(),
            "team": [dict(r) for r in await self.db.members()],
            "workspace": await self.db.get_setting("workspace_chat_id", ""),
            "niche": await self.db.get_setting("niche", ""),
            "niches": [{"key": k, "title": n.title} for k, n in NICHES.items()],
            "fresh": {"board_max_age_hours": fr.board_max_age_hours, "max_competitors": fr.max_competitors},
            "voice": await self.db.get_setting("voice", ""),
            "brand": {"name": brand.name, "handle": brand.handle, "primary": brand.primary, "accent": brand.accent},
            "autopilot": (await self.db.get_setting("autopilot")) == "1",
            "pillars": await self.db.get_setting("pillars", ""),
            "channels": channels,
            # ключи и прокси (пусть и под маской) — только владельцу
            "integrations": await integ.status() if integ and user["is_owner"] else [],
            "summary": integ.summary() if integ else {},
            "miniapp_url": self.deps.extra.get("miniapp_url"),
        })

    async def settings_set(self, request):
        """Сначала проверяем весь запрос, потом применяем: ошибка в одном поле не оставляет настройки наполовину изменёнными."""
        user = await self.user(request)
        body = await json_object(request)
        factory = self.deps.extra.get("factory")
        db = self.db
        ops = []  # отложенные записи: (функция, аргументы)
        for key, value in body.items():
            if key in OWNER_ONLY and not user["is_owner"]:
                raise web.HTTPForbidden(text="это может только владелец")
            if key == "threshold":
                ops.append((db.set_setting, ("threshold", str(to_int(value, key, 0, 10)))))
            elif key in ("profile", "geo", "voice", "pillars"):
                ops.append((db.set_setting, (key, to_str(value, key))))
            elif key in FLAGS:
                ops.append((db.set_setting, (key, "1" if to_bool(value, key) else "0")))
            elif key == "phrases":
                ops.append((self.deps.phrases.set, (to_str_list(value, key),)))
            elif key == "templates":
                for service, text in to_dict(value, key).items():
                    ops.append((db.set_template, (to_str(service, key, 50), to_str(text, key, 4000))))
            elif key == "niche":
                from ..niches import NICHES, apply
                if not isinstance(value, str) or value not in NICHES:
                    raise bad("unknown niche")
                ops.append((apply, (db, self.deps.phrases, NICHES[value])))
            elif key == "fresh":
                v = to_dict(value, key)
                if "board_max_age_hours" in v:
                    hours = to_float(v["board_max_age_hours"], "board_max_age_hours", 0.1, 72.0)
                    ops.append((db.set_setting, ("board_max_age_hours", str(hours))))
                if "max_competitors" in v:
                    ops.append((db.set_setting, ("max_competitors", str(to_int(v["max_competitors"], "max_competitors", 0, 500)))))
            elif key == "brand":
                raw = to_dict(value, key)
                v = {k: to_str(raw.get(k, ""), k, 100) for k in ("name", "handle")}
                v.update({k: to_color(raw.get(k, ""), k) for k in ("primary", "accent")})
                ops.append((db.set_setting, ("brand", json.dumps({k: x for k, x in v.items() if x}, ensure_ascii=False))))
            elif key == "channels":
                if not factory:
                    raise bad("контент-фабрика выключена")
                for platform, target in to_dict(value, key).items():
                    if platform not in ("telegram", "vk"):
                        raise bad("канал задаётся только для telegram и vk")
                    target = "" if target is None else to_str(target, platform, 100)
                    if platform == "telegram":
                        m = _TG_LINK.fullmatch(target)
                        target = "@" + m.group(1) if m else target
                    if not target:
                        ops.append((factory.store.remove_channel, (platform,)))
                    elif platform == "vk" and not re.fullmatch(r"-?\d{1,20}", target):
                        raise bad("VK: числовой id, для группы со знаком минус")
                    elif platform == "telegram" and not _TG_TARGET.fullmatch(target):
                        raise bad("Telegram-канал: @имя канала или его числовой id")
                    else:
                        ops.append((factory.store.set_channel, (platform, target)))
            elif key == "team_remove":
                ops.append((db.remove_member, (to_int(value, key),)))
            else:
                raise bad(f"unknown setting {key}")
        for fn, args in ops:
            await fn(*args)
        log.info("settings changed by %s: %s", user["id"], list(body))
        return await self.settings_get(request)

    async def integration_set(self, request):
        """Ключи API: проверка живым запросом, сохранение в базе, применение без перезапуска. Только владелец."""
        user = await self.user(request)
        if not user.get("is_owner"):
            raise web.HTTPForbidden(text="только владелец")
        integ = self.deps.extra.get("integrations")
        if not integ:
            raise web.HTTPServiceUnavailable(text="недоступно")
        body = await json_object(request)
        key, value = body.get("key"), body.get("value")
        from ..integrations import INTEGRATIONS
        if not isinstance(key, str) or key not in INTEGRATIONS:
            raise bad("неизвестное подключение")
        if value is not None and not isinstance(value, str):
            raise bad("value: нужна строка")
        if not (value or "").strip():
            await integ.unset(key)
        else:
            err = await integ.set(key, value)
            if err:
                raise bad(err)
        return web.json_response({"integrations": await integ.status(), "summary": integ.summary()})

    async def rule_add(self, request):
        await self.user(request)
        expr = to_str((await json_object(request)).get("expr", ""), "expr", 500)
        try:
            parse_rule(expr)
        except ValueError as e:
            raise bad(str(e))
        await self.db.add_rule(expr)
        return web.json_response({"rules": [dict(r) for r in await self.db.rules()]})

    async def rule_del(self, request):
        await self.user(request)
        await self.db.remove_rule(path_id(request))
        return web.json_response({"rules": [dict(r) for r in await self.db.rules()]})

    async def keyword_add(self, request):
        await self.user(request)
        body = await json_object(request)
        kind = body.get("kind")
        if kind not in ("topic", "intent", "negative"):
            raise bad("kind: topic, intent или negative")
        # Пустое слово совпадает с любым текстом и ломает оценку всех лидов
        word = to_str(body.get("word"), "word", 100)
        if not word:
            raise bad("word: напишите слово")
        category = body.get("category")
        if kind == "topic":
            category = to_str(category, "category", 50) if category is not None else ""
            category = category or "другое"
        await self.db.add_keyword(kind, word, category if kind == "topic" else None)
        return await self.settings_get(request)

    async def keyword_del(self, request):
        await self.user(request)
        await self.db.remove_keyword(request.match_info["word"])
        return await self.settings_get(request)

    async def ignored_del(self, request):
        await self.user(request)
        await self.db.remove_ignored(request.match_info["key"])
        return await self.settings_get(request)

    async def index(self, request):
        return web.FileResponse(os.path.join(STATIC, "index.html"))

    # --- контент-фабрика ---
    @property
    def factory(self):
        f = self.deps.extra.get("factory")
        if not f:
            raise web.HTTPServiceUnavailable(text="контент-фабрика не инициализирована")
        return f

    def _content_row(self, r) -> dict:
        from ..content.factory import compose_text
        from ..content.store import errors_json, media_json

        d = dict(r)
        media = media_json(r)
        store = self.factory.media
        d["images"] = [store.url(n) or f"/media/{n}" for n in media.get("images") or []]
        d["video"] = (store.url(media["video"]) or f"/media/{media['video']}") if media.get("video") else None
        # compose_text отдаёт HTML для Telegram; кабинету нужен обычный текст — экранирует он сам, один раз
        d["text"] = html.unescape(re.sub(r"<[^>]+>", "", compose_text(r)))
        d["published"] = json.loads(r["published"] or "[]")
        # площадки, куда не ушло: причина — без ключей и токенов, если они попали в текст ошибки
        d["publish_errors"] = [{"platform": str(x.get("platform") or ""), "label": str(x.get("label") or x.get("platform") or ""),
                                "error": self._safe_error(str(x.get("error") or ""))} for x in errors_json(r)]
        return d

    def _safe_error(self, text: str) -> str:
        from ..integrations import scrub
        settings = self.deps.extra.get("settings")
        text = scrub(text, settings) if settings else text
        return _SECRET_IN_TEXT.sub(r"\1=***", text)[:300]

    async def content_list(self, request):
        await self.user(request)
        q = request.query
        status = q.get("status") or None
        rows = await self.factory.store.recent(query_int(request, "limit", 50, 1, 200), status=status, kind=q.get("kind") or None)
        # удалённое показываем, только если о нём спросили прямо
        return web.json_response({"items": [self._content_row(r) for r in rows
                                            if (r["kind"] != "idea" or q.get("kind") == "idea")
                                            and (r["status"] != "rejected" or status == "rejected")]})

    async def content_item(self, request):
        await self.user(request)
        row = await self.factory.store.get(path_id(request))
        if not row:
            raise web.HTTPNotFound()
        return web.json_response({"item": self._content_row(row)})

    async def content_generate(self, request):
        user = await self.user(request)
        body = await json_object(request)
        kind, topic = body.get("kind", "post"), to_str(body.get("topic") or "", "topic", 2000)
        if kind not in ("post", "carousel", "reel", "magnet") or not topic:
            raise bad("нужны kind и topic")
        factory, data = self.factory, None
        gen = getattr(factory, "gen", None)
        if gen is not None and not gen.available:
            manual = _manual_content(kind, topic)
            if manual is None:
                raise bad(NO_AI)
            topic, data = manual
        try:
            row = await factory.create(kind, topic, created_by=self._name(user), data=data)
        except Exception as e:
            log.warning("контент %s не создан: %s", kind, _SECRET_IN_TEXT.sub(r"\1=***", str(e)))
            raise bad(_SECRET_IN_TEXT.sub(r"\1=***", str(e))[:300])
        return web.json_response({"item": self._content_row(row)})

    async def content_action(self, request):
        """action: publish | schedule (at: ISO) | draft | reject | edit (body/caption/hashtags)."""
        await self.user(request)
        item_id = path_id(request)
        row = await self.factory.store.get(item_id)
        if not row:
            raise web.HTTPNotFound()
        body = await json_object(request)
        action = body.get("action")
        if action in ("publish", "schedule") and row["status"] == "rejected":
            raise bad("материал удалён")
        if action == "publish":
            pubs = await self.deps.extra["publishers"]() if self.deps.extra.get("publishers") else []
            if not pubs:
                raise bad("площадки не настроены")
            out = await self.factory.publish(row, pubs)
            return web.json_response({"item": self._content_row(await self.factory.store.get(item_id)),
                                      "results": [{"platform": r.platform, "url": r.url} for r in out.results],
                                      "errors": [self._safe_error(e) for e in out.errors]})
        # Всё остальное меняет материал — это можно только с черновиком или запланированным (как в боте)
        editable = row["status"] in ("draft", "scheduled")
        if action == "schedule":
            try:
                at = datetime.fromisoformat(body["at"])
            except (KeyError, ValueError, TypeError):
                raise bad("нужно время at в ISO")
            if at.tzinfo is None:
                from zoneinfo import ZoneInfo
                at = at.replace(tzinfo=ZoneInfo(self.deps.tz))
            if not editable:
                raise web.HTTPConflict(text="В расписание можно поставить только черновик — этот уже опубликован или публикуется")
            if not await self.factory.store.schedule(item_id, at):
                raise bad("Это время уже прошло — выберите время в будущем")
        elif action == "draft":
            if not editable or not await self.factory.store.schedule(item_id, None):
                raise web.HTTPConflict(text="Снять с плана можно только запланированное — этот материал уже опубликован или публикуется")
        elif action == "reject":
            if not await self.factory.store.reject(item_id):
                raise web.HTTPConflict(text="Удалить можно только черновик — этот уже опубликован или публикуется")
        elif action == "edit":
            if not editable:
                raise web.HTTPConflict(text="Править можно только черновик — этот уже опубликован, публикуется или удалён")
            fields = {}
            for k in ("body", "caption"):
                if isinstance(body.get(k), str):
                    await self.factory.store.add_edit(item_id, row["kind"], row[k] or "", body[k])
                    fields[k] = body[k]
            if "hashtags" in body:
                tags = body["hashtags"]
                fields["hashtags"] = to_str(tags, "hashtags", 500) if isinstance(tags, str) else to_str_list(tags, "hashtags", 30, 60)
            await self.factory.store.update(item_id, **fields)
        else:
            raise bad("bad action")
        return web.json_response({"item": self._content_row(await self.factory.store.get(item_id))})

    async def content_channels(self, request):
        await self.user(request)
        pubs = await self.deps.extra["publishers"]() if self.deps.extra.get("publishers") else []
        return web.json_response({"channels": [{"platform": p.platform, "label": p.label} for p in pubs]})

    # --- публичный лендинг лид-магнита: заявка превращается в горячий лид ---
    async def landing(self, request):
        slug = request.match_info["slug"]
        row = await self.factory.store.by_slug(slug)
        if not row or row["status"] == "rejected":
            raise web.HTTPNotFound(text="страница не найдена")
        from ..content.store import body_json
        page = render_landing(row, body_json(row), await self.factory.brand(), self.landing_guard.tokens.issue(slug))
        return web.Response(text=page, content_type="text/html")

    async def landing_submit(self, request):
        from ..content.store import body_json
        slug, guard, ip = request.match_info["slug"], self.landing_guard, client_ip(request)

        async def too_many(limiter, key):
            wait = limiter.retry_after(key)
            page = render_notice("Слишком много заявок", "Попробуйте ещё раз через несколько минут.", await self.factory.brand())
            return web.Response(text=page, status=429, content_type="text/html", headers={"Retry-After": str(wait)})

        if not guard.attempts.take(ip):
            return await too_many(guard.attempts, ip)
        row = await self.factory.store.by_slug(slug)
        if not row or row["status"] == "rejected":
            raise web.HTTPNotFound(text="страница не найдена")
        form = await request.post()
        brand = await self.factory.brand()
        values = {"name": str(form.get("name", "")).strip()[:100], "contact": str(form.get("contact", "")).strip()[:100],
                  "message": str(form.get("message", "")).strip()[:1000]}
        name, contact, message = values["name"], values["contact"], values["message"]
        token = str(form.get("token", ""))

        def again(error: str):
            """Вернуть форму с введённым текстом и новым токеном — человеку не нужно набирать заново."""
            page = render_landing(row, body_json(row), brand, guard.tokens.issue(slug), values, error)
            return web.Response(text=page, status=400, content_type="text/html")

        if str(form.get("website", "")):  # website — скрытое поле-ловушка для ботов
            raise bad("укажите контакт")
        if len(contact) < 3:
            return again("Укажите телефон или Telegram — иначе мы не сможем ответить.")
        # От проверки токена до его погашения нет ни одного await: параллельные запросы не проскочат
        if not guard.tokens.valid(slug, token):
            return again("Страница устарела. Проверьте данные и нажмите «Получить» ещё раз.")
        if not guard.per_ip.take(ip):
            return await too_many(guard.per_ip, ip)
        if not guard.per_slug.take(slug):
            return await too_many(guard.per_slug, slug)
        guard.tokens.consume(token)

        store = self.factory.store
        thanks = web.Response(text=render_thanks(row, brand), content_type="text/html")
        async with self._landing_lock:
            since = (datetime.now(timezone.utc) - timedelta(seconds=MERGE_WINDOW)).isoformat()
            cur = await store.conn.execute("SELECT contact, message FROM landing_leads WHERE slug = ? AND at >= ?", (slug, since))
            same = [r for r in await cur.fetchall() if contact_key(r["contact"] or "") == contact_key(contact)]
            if same:
                # Тот же человек на той же странице: второго лида и уведомления не будет, новый текст сохраняем
                if message and all((r["message"] or "") != message for r in same):
                    await store.add_landing_lead(slug, name, contact, message, None)
                return thanks
            n = await store.add_landing_lead(slug, name, contact, message, None)
        pipeline = self.deps.extra.get("pipeline")
        if pipeline:
            title = row["title"] or row["topic"]
            text = (f"Заявка с лендинга «{title}»" + (f": {message}" if message else "")
                    + f"\nИмя: {name or '—'}. Контакт: {contact}. Хочет получить материал и консультацию.")
            created = False
            try:
                created = await pipeline.handle(Lead(source="landing", external_id=f"{slug}:{n}", text=text,
                                                     title=row["title"] or "", author=name, contact=contact, preset_score=9))
            except Exception:
                log.exception("заявка с лендинга %s (№%s) не дошла до лидов", slug, n)
            if not created:
                log.warning("заявка с лендинга %s (№%s) не стала лидом; она есть в кабинете: Контент → Заявки со страниц", slug, n)
        return thanks

    async def landing_leads(self, request):
        """Все заявки со страниц лид-магнитов — даже те, что не стали лидами."""
        await self.user(request)
        cur = await self.factory.store.conn.execute(
            "SELECT l.id, l.slug, l.name, l.contact, l.message, l.at, COALESCE(NULLIF(c.title, ''), c.topic, '') AS page"
            " FROM landing_leads l LEFT JOIN content_items c ON c.slug = l.slug ORDER BY l.id DESC LIMIT ?",
            (query_int(request, "limit", 50, 1, 200),))
        return web.json_response({"items": [dict(r) for r in await cur.fetchall()]})


def build_app(api: MiniAppAPI) -> web.Application:
    app = web.Application()
    app.router.add_get("/", api.index)
    app.router.add_get("/app", api.index)
    app.router.add_get("/app/", api.index)
    app.router.add_static("/static/", STATIC, show_index=False)
    r = app.router
    r.add_get("/api/me", api.me)
    r.add_get("/api/leads", api.leads)
    r.add_get("/api/leads/{id:\\d+}", api.lead)
    r.add_post("/api/leads/{id:\\d+}/status", api.set_status)
    r.add_get("/api/stats", api.stats)
    r.add_get("/api/settings", api.settings_get)
    r.add_post("/api/settings", api.settings_set)
    r.add_post("/api/integrations", api.integration_set)
    r.add_post("/api/rules", api.rule_add)
    r.add_delete("/api/rules/{id:\\d+}", api.rule_del)
    r.add_post("/api/keywords", api.keyword_add)
    r.add_delete("/api/keywords/{word}", api.keyword_del)
    r.add_delete("/api/ignored/{key}", api.ignored_del)
    r.add_get("/api/content", api.content_list)
    r.add_post("/api/content", api.content_generate)
    r.add_get("/api/content/channels", api.content_channels)
    r.add_get(r"/api/content/{id:\d+}", api.content_item)
    r.add_post(r"/api/content/{id:\d+}", api.content_action)
    r.add_get("/api/landing-leads", api.landing_leads)
    r.add_get("/lp/{slug}", api.landing)
    r.add_post("/lp/{slug}", api.landing_submit)
    if api.deps.extra.get("factory"):
        app.router.add_static("/media/", api.deps.extra["factory"].media.root, show_index=False)

    @web.middleware
    async def errors(request, handler):
        """Ошибки API — всегда JSON с полем error: кабинет показывает его человеку."""
        try:
            return await handler(request)
        except web.HTTPException as e:
            if e.status >= 400 and request.path.startswith("/api/"):
                return web.json_response({"error": e.text or e.reason}, status=e.status)
            raise
        except Exception:
            if not request.path.startswith("/api/"):
                raise
            log.exception("ошибка API %s %s", request.method, request.path)
            return web.json_response({"error": "Что-то сломалось на сервере. Попробуйте ещё раз."}, status=500)
    app.middlewares.append(errors)

    async def security_headers(request, response):
        path, h = request.path, response.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("Referrer-Policy", "no-referrer")
        if path in ("/", "/app", "/app/") or path.startswith("/static/"):
            # Кабинет открывается внутри Telegram, в том числе во фрейме web.telegram.org:
            # X-Frame-Options список сайтов не умеет, поэтому здесь работает только frame-ancestors
            h.setdefault("Content-Security-Policy", APP_CSP)
            h.setdefault("Cache-Control", "no-cache")
            return
        h.setdefault("X-Frame-Options", "DENY")
        if path.startswith("/lp/"):
            h.setdefault("Content-Security-Policy", LANDING_CSP)
        elif not path.startswith("/media/"):
            h.setdefault("Content-Security-Policy", API_CSP)
    app.on_response_prepare.append(security_headers)
    return app


async def start_miniapp(s, db: Database, bot, deps) -> None:
    from aiogram.types import MenuButtonWebApp, WebAppInfo

    api = MiniAppAPI(db, s.bot_token, deps.guard, deps, deps.sources, dev_user_id=None)
    deps.extra["threshold"] = s.score_threshold
    app = build_app(api)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", s.miniapp_port)
    await site.start()
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Кабинет", web_app=WebAppInfo(url=s.miniapp_url)))
    except Exception:
        log.exception("не удалось поставить кнопку меню Mini App")
    log.info("Mini App: http://0.0.0.0:%s → %s", s.miniapp_port, s.miniapp_url)
    try:
        while True:
            await __import__("asyncio").sleep(3600)
    finally:
        await runner.cleanup()
