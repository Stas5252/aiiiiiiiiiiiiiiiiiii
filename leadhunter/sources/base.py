import asyncio
import logging
import re
from collections.abc import Awaitable, Callable

from ..models import Lead

Emit = Callable[[Lead], Awaitable[bool]]
log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    text = _TAG_RE.sub(" ", text or "")
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    return re.sub(r"[ \t]+", " ", text).strip()


class Source:
    name: str = "base"
    interval: int = 600

    async def poll(self) -> list[Lead]:
        raise NotImplementedError

    async def run(self, emit: Emit) -> None:
        """По умолчанию — опрос раз в interval секунд. Realtime-источники переопределяют run()."""
        while True:
            try:
                leads = await self.poll()
                found = 0
                for lead in leads:
                    if await emit(lead):
                        found += 1
                log.info("%s: %d записей, %d новых лидов", self.name, len(leads), found)
            except Exception:
                log.exception("%s: ошибка опроса", self.name)
            await asyncio.sleep(self.interval)
