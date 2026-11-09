import asyncio
import logging
import re
import time
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
    # Состояние для /sources: когда последний раз получилось и что сломалось
    last_ok: float | None = None
    last_error: str | None = None
    found_total: int = 0

    async def poll(self) -> list[Lead]:
        raise NotImplementedError

    async def run(self, emit: Emit) -> None:
        """По умолчанию — опрос раз в interval секунд с бэкоффом при ошибках. Realtime-источники переопределяют run()."""
        failures = 0
        while True:
            try:
                leads = await self.poll()
                found = 0
                for lead in leads:
                    if await emit(lead):
                        found += 1
                self.found_total += found
                self.last_ok, self.last_error, failures = time.time(), None, 0
                log.info("%s: %d записей, %d новых лидов", self.name, len(leads), found)
            except Exception as e:
                failures += 1
                self.last_error = f"{type(e).__name__}: {e}"[:200]
                log.exception("%s: ошибка опроса", self.name)
            await asyncio.sleep(min(self.interval * (2 ** min(failures, 4)), 6 * 3600) if failures else self.interval)

    def status_line(self) -> str:
        if self.last_error:
            state = f"⚠️ {self.last_error}"
        elif self.last_ok:
            state = f"✅ {int((time.time() - self.last_ok) / 60)} мин назад"
        else:
            state = "⏳ ещё не опрашивался"
        return f"{self.name} — каждые {self.interval // 60} мин, лидов: {self.found_total}, {state}"
