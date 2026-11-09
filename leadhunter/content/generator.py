"""Генерация контента ИИ: посты, карусели, сценарии видео, лид-магниты, идеи.

Всё «обучение» — в промпте: профиль студии, тон голоса (/voice), последние правки владельца («было → стало»),
уже опубликованные посты как образцы стиля и боли клиентов из найденных лидов."""
import json
import logging
from datetime import datetime, timedelta, timezone

from ..db import Database
from ..llm import LLMClassifier
from .store import ContentStore

log = logging.getLogger(__name__)

SYSTEM = """Ты — контент-маркетолог студии. Пишешь по-русски, живым языком, без канцелярита, без воды и без
клише вроде «в современном мире». Конкретика, цифры, примеры, польза для читателя. Эмодзи — умеренно.
Профиль студии (что продаём): {profile}
{voice}{examples}{edits}{pains}
Ответ — строго JSON по схеме, без пояснений."""

SCHEMAS = {
    "post": """Напиши пост для соцсетей{platform} на тему: «{topic}».
Структура: цепляющая первая строка (хук), 3–6 коротких абзацев, в конце мягкий призыв написать в личку / оставить заявку.
Длина 600–1200 знаков. Хэштеги — 5–8 релевантных, по-русски и по-английски.
JSON: {{"title": "короткий заголовок до 60 знаков", "body": "текст поста с переносами строк",
"hashtags": ["#тег", ...], "image_prompt": "описание картинки к посту на английском, без текста на картинке, 1 предложение"}}""",
    "carousel": """Сделай карусель (слайды) на тему: «{topic}».
7–9 слайдов. Слайд 1 — обложка: заголовок до 8 слов + подзаголовок до 12 слов. Слайды 2–N: заголовок до 6 слов
и 1–3 коротких предложения (до 220 знаков). Последний слайд — призыв к действию и что получит человек.
JSON: {{"title": "название карусели", "slides": [{{"title": "...", "text": "..."}}, ...],
"caption": "подпись под каруселью 300–600 знаков с призывом", "hashtags": ["#тег", ...],
"image_prompt": "описание обложки на английском, без текста"}}""",
    "reel": """Напиши сценарий короткого вертикального видео (Reels / Shorts / TikTok) на 20–40 секунд на тему: «{topic}».
Первые 3 секунды — хук, который останавливает скролл. 5–8 сцен. Для каждой: текст на экране (до 60 знаков),
текст озвучки (1–2 предложения, разговорно), длительность в секундах (2–6).
JSON: {{"title": "название ролика", "hook": "первая фраза", "scenes": [{{"text": "на экране", "voice": "озвучка", "duration": 4}}, ...],
"caption": "подпись к видео 200–400 знаков с призывом", "hashtags": ["#тег", ...]}}""",
    "magnet": """Сделай лид-магнит на тему: «{topic}» — чек-лист или мини-гайд, за который человек готов оставить контакт.
7–12 пунктов, каждый: заголовок до 8 слов и 1–3 предложения практики (не теория).
JSON: {{"title": "название лид-магнита (до 60 знаков)", "subtitle": "что получит человек, 1 предложение",
"items": [{{"title": "...", "text": "..."}}, ...], "cta": "что написать, чтобы получить консультацию, 1 предложение",
"post": "анонс лид-магнита для соцсетей 400–700 знаков: боль → что внутри → как получить (написать слово в личку / ссылка)",
"hashtags": ["#тег", ...], "image_prompt": "обложка на английском, без текста"}}""",
    "ideas": """Предложи {n} идей для контента студии на ближайшую неделю. Опирайся на боли клиентов ниже, если они есть:
каждая идея должна отвечать на реальный вопрос или возражение клиента. Чередуй форматы: post, carousel, reel, toon, magnet.
JSON: {{"ideas": [{{"topic": "тема в 5–10 словах", "kind": "post|carousel|reel|toon|magnet", "hook": "первая фраза, которая цепляет",
"why": "почему это сработает, 1 предложение"}}, ...]}}""",
    "toon": """Напиши сценарий короткого вертикального мультика (20–40 секунд) на тему: «{topic}».
Это 2D-мультик с простыми персонажами, который рендерится кодом: 2–3 персонажа, 5–9 сцен, диалог живой и смешной, с конфликтом и развязкой,
в конце — вывод и мягкий призыв. Реплики короткие (до 90 знаков), разговорные. Персонажи: ключ латиницей, имя, цвет (hex), форма.
JSON: {{"title": "название", "style": "flat|chalk",
"characters": {{"boss": {{"name": "Директор", "color": "#F2C14E", "shape": "round|tall|square"}}, "dev": {{"name": "Разработчик", "color": "#6FB9F0", "shape": "tall"}}}},
"scenes": [{{"bg": "office|street|home|abstract|none", "duration": 4,
  "characters": [{{"id": "boss", "pos": "left|center|right", "emotion": "neutral|happy|angry|sad|surprised|thinking", "action": "idle|talk|walk|jump|point|shake|nod"}}],
  "line": {{"speaker": "boss", "text": "реплика"}}, "caption": "короткий титр сверху или пусто"}}, ...],
"caption": "подпись к видео 200–400 знаков с призывом", "hashtags": ["#тег", ...]}}""",
    "aivideo": """Придумай короткий ИИ-мультик (видео генерирует нейросеть Veo клипами по 8 секунд) на тему: «{topic}».
1–3 клипа, единый герой и стиль во всех клипах (повтори описание героя и стиля в каждом промпте — так нейросеть сохранит персонажа).
Промпты на английском, кинематографичные: стиль (например, "3D Pixar-style animation" или "2D flat cartoon"), герой, действие, камера,
свет, настроение; без текста на экране. Отдельно — озвучка по-русски для каждого клипа (1–2 предложения, читается за 8 секунд).
JSON: {{"title": "название", "style": "общее описание стиля на английском",
"clips": [{{"prompt": "английский промпт клипа", "voice": "русская озвучка клипа"}}, ...],
"caption": "подпись к видео 200–400 знаков с призывом", "hashtags": ["#тег", ...]}}""",
}

REWRITE = """Ниже материал ({kind}) в JSON. Перепиши его по указанию владельца: «{instruction}».
Сохрани ту же JSON-структуру и ключи, верни только JSON.

{data}"""


class ContentGenerator:
    def __init__(self, llm: LLMClassifier | None, store: ContentStore, db: Database, default_profile: str):
        self.llm = llm
        self.store = store
        self.db = db
        self.default_profile = default_profile

    @property
    def available(self) -> bool:
        return self.llm is not None

    async def pains(self, limit: int = 12) -> list[str]:
        """Что реально спрашивают клиенты за две недели — самая честная контент-стратегия."""
        since = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        cur = await self.db.conn.execute(
            "SELECT summary, title, text FROM leads WHERE created_at >= ? AND status != 'spam'"
            " ORDER BY score DESC, id DESC LIMIT ?", (since, limit))
        out = []
        for summary, title, text in await cur.fetchall():
            line = (summary or title or text or "").strip().replace("\n", " ")
            if line:
                out.append(line[:160])
        return out

    async def _system(self, kind: str) -> str:
        profile = await self.db.get_setting("profile", self.default_profile) or self.default_profile
        voice = await self.db.get_setting("voice", "")
        voice = f"Тон голоса (обязательно соблюдать): {voice}\n" if voice else ""
        examples = ""
        if kind in ("post", "magnet", "ideas"):
            pub = await self.store.published_texts(3)
            if pub:
                examples = "Образцы стиля — посты, которые владелец уже опубликовал:\n" + "\n---\n".join(p[:700] for p in pub) + "\n"
        edits = await self.store.edits(kind if kind != "ideas" else None, 4)
        edits_txt = ""
        if edits:
            edits_txt = ("Как владелец правил предыдущие тексты (учись на этом: пиши сразу как в «стало»):\n"
                         + "\n".join(f"БЫЛО: {a[:400]}\nСТАЛО: {b[:400]}" for a, b in edits) + "\n")
        pains_txt = ""
        if kind in ("ideas", "post", "carousel", "reel", "toon"):
            pains = await self.pains()
            if pains:
                pains_txt = "Боли и запросы клиентов за последние 2 недели:\n" + "\n".join(f"- {p}" for p in pains) + "\n"
            pillars = await self.db.get_setting("pillars", "")
            if pillars:
                pains_txt += f"Рубрики контента (чередуй их): {pillars}\n"
        return SYSTEM.format(profile=profile, voice=voice, examples=examples, edits=edits_txt, pains=pains_txt)

    async def generate(self, kind: str, topic: str, *, platform: str = "", n: int = 7) -> dict:
        if not self.llm:
            raise RuntimeError("ИИ не подключён — задайте OPENAI_API_KEY в .env")
        if kind not in SCHEMAS:
            raise ValueError(f"неизвестный формат {kind}")
        system = await self._system(kind)
        user = SCHEMAS[kind].format(topic=topic, n=n, platform=f" для {platform}" if platform else "")
        data = await self.llm.complete_json(system, user, temperature=0.8)
        return self._clean(kind, data)

    async def rewrite(self, kind: str, data: dict, instruction: str) -> dict:
        if not self.llm:
            raise RuntimeError("ИИ не подключён — задайте OPENAI_API_KEY в .env")
        system = await self._system(kind)
        user = REWRITE.format(kind=kind, instruction=instruction, data=json.dumps(data, ensure_ascii=False))
        return self._clean(kind, await self.llm.complete_json(system, user, temperature=0.7))

    @staticmethod
    def _clean(kind: str, data: dict) -> dict:
        """ИИ иногда возвращает строку вместо списка или лишние ключи — приводим к ожидаемой форме."""
        tags = data.get("hashtags") or []
        if isinstance(tags, str):
            tags = tags.split()
        data["hashtags"] = [t if t.startswith("#") else f"#{t}" for t in (str(x).strip() for x in tags) if t][:10]
        if kind == "carousel":
            data["slides"] = [{"title": str(s.get("title", "")), "text": str(s.get("text", ""))}
                              for s in data.get("slides") or [] if isinstance(s, dict)][:10]
        if kind == "reel":
            scenes = []
            for s in data.get("scenes") or []:
                if not isinstance(s, dict):
                    continue
                try:
                    dur = float(s.get("duration") or 3)
                except (TypeError, ValueError):
                    dur = 3.0
                scenes.append({"text": str(s.get("text", "")), "voice": str(s.get("voice", "")),
                               "duration": max(1.5, min(8.0, dur))})
            data["scenes"] = scenes[:10]
        if kind == "magnet":
            data["items"] = [{"title": str(i.get("title", "")), "text": str(i.get("text", ""))}
                             for i in data.get("items") or [] if isinstance(i, dict)][:15]
        if kind == "ideas":
            data["ideas"] = [i for i in data.get("ideas") or [] if isinstance(i, dict) and i.get("topic")]
            for i in data["ideas"]:
                if i.get("kind") not in ("post", "carousel", "reel", "toon", "magnet"):
                    i["kind"] = "post"
        if kind == "toon":
            from .toon import normalize_script
            data.update(normalize_script(data))
        if kind == "aivideo":
            clips = [c for c in data.get("clips") or [] if isinstance(c, dict) and c.get("prompt")][:3]
            if not clips:
                raise ValueError("ИИ не вернул ни одного клипа")
            data["clips"] = [{"prompt": str(c["prompt"]), "voice": str(c.get("voice", ""))} for c in clips]
        return data


def magnet_text(data: dict) -> str:
    """Лид-магнит как обычный текст — для файла и страницы."""
    lines = [data.get("title", ""), data.get("subtitle", ""), ""]
    for n, item in enumerate(data.get("items") or [], 1):
        lines.append(f"{n}. {item['title']}")
        lines.append(f"   {item['text']}")
        lines.append("")
    if data.get("cta"):
        lines.append(data["cta"])
    return "\n".join(lines).strip()
