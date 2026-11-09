import logging
from datetime import datetime, timezone
from urllib.parse import quote

from ..browser.session import BrowserSession
from ..http import NeedLoginError, human_pause
from ..models import Lead
from .base import Source

log = logging.getLogger(__name__)
TAG_INFO = "https://www.instagram.com/api/v1/tags/web_info/?tag_name={tag}"
HEADERS = {"x-ig-app-id": "936619743392459", "x-requested-with": "XMLHttpRequest",
           "referer": "https://www.instagram.com/explore/tags/"}


def parse_tag_info(data: dict, tag: str) -> list[Lead]:
    """Ответ внутреннего эндпоинта страницы хэштега: data.recent.sections[].layout_content.medias[].media"""
    leads = []
    recent = (data.get("data") or {}).get("recent") or {}
    for section in recent.get("sections") or []:
        for m in ((section.get("layout_content") or {}).get("medias") or []):
            media = m.get("media") or {}
            text = ((media.get("caption") or {}).get("text") or "").strip()
            code = media.get("code")
            if not text or not code:
                continue
            user = (media.get("user") or {}).get("username") or ""
            ts = media.get("taken_at")
            leads.append(Lead(
                source="instagram",
                external_id=str(media.get("pk") or code),
                title=f"#{tag}",
                text=text,
                url=f"https://www.instagram.com/p/{code}/",
                author=f"@{user}" if user else "",
                contact=f"https://www.instagram.com/{user}/" if user else "",
                found_at=datetime.fromtimestamp(ts, tz=timezone.utc) if ts else datetime.now(timezone.utc),
            ))
    return leads


class InstagramWebSource(Source):
    """Instagram через залогиненный браузерный профиль: свежие посты по хэштегам.
    Осторожно: Meta банит автоматизацию — отдельный аккаунт, прокси, редкие запросы."""

    name = "instagram"
    human = True

    def __init__(self, hashtags: list[str], interval: int, profiles_dir: str, proxy: str | None, headless: bool,
                 per_poll: int = 3):
        self.hashtags = hashtags
        self.interval = interval
        self.profiles_dir = profiles_dir
        self.proxy = proxy
        self.headless = headless
        self.per_poll = per_poll
        self._cursor = 0

    async def poll(self) -> list[Lead]:
        # За один проход — несколько хэштегов по кругу, чтобы не выглядеть как скрипт
        batch = [self.hashtags[(self._cursor + i) % len(self.hashtags)] for i in range(min(self.per_poll, len(self.hashtags)))]
        self._cursor = (self._cursor + len(batch)) % len(self.hashtags)
        leads: list[Lead] = []
        async with BrowserSession(self.profiles_dir, "instagram", self.proxy, self.headless) as b:
            for tag in batch:
                data = await b.api_json(TAG_INFO.format(tag=quote(tag)), HEADERS)
                if data is None:
                    raise NeedLoginError("Instagram разлогинил — python -m leadhunter.browser.login instagram")
                leads += parse_tag_info(data, tag)
                await human_pause(20, 60)
        return leads
