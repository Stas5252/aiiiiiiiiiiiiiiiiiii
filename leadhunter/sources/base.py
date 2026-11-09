import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable

from ..http import CaptchaError, NeedLoginError, in_work_hours
from ..models import Lead

Emit = Callable[..., Awaitable[bool]]
Alert = Callable[[str], Awaitable[None]]
log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    text = _TAG_RE.sub(" ", text or "")
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    return re.sub(r"[ \t]+", " ", text).strip()


class Source:
    name: str = "base"
    interval: int = 600
    # Источник ходит по сайтам «как человек»: только в рабочие часы, с паузами
    human: bool = False
    work_hours: str = "0-24"
    tz: str = "Europe/Moscow"
    alert: Alert | None = None
    # Состояние для /sources: когда последний раз получилось и что сломалось
    last_ok: float | None = None
    last_error: str | None = None
    found_total: int = 0
    burst_limit: int = 5  # максимум уведомлений за один проход источника

    async def poll(self) -> list[Lead]:
        raise NotImplementedError

    async def run(self, emit: Emit) -> None:
        """По умолчанию — опрос раз в interval секунд с бэкоффом при ошибках. Realtime-источники переопределяют run()."""
        failures = 0
        while True:
            if self.human and not in_work_hours(self.work_hours, self.tz):
                await asyncio.sleep(600)
                continue
            try:
                leads = await self.poll()
                # Первый проход после запуска: страница биржи — это десятки старых заказов. Шлём не больше
                # burst_limit самых свежих, остальные уходят в базу молча (есть в /leads и кабинете).
                leads.sort(key=lambda l: l.found_at, reverse=True)
                found = 0
                for lead in leads:
                    if await emit(lead, quiet=found >= self.burst_limit):
                        found += 1
                self.found_total += found
                self.last_ok, self.last_error, failures = time.time(), None, 0
                log.info("%s: %d записей, %d новых лидов", self.name, len(leads), found)
            except (CaptchaError, NeedLoginError) as e:
                failures += 1
                self.last_error = str(e)[:200]
                log.warning("%s: %s", self.name, e)
                await self._alert(f"⚠️ {self.name}: {e}. Пауза {self._backoff(failures) // 60} мин.")
            except Exception as e:
                failures += 1
                self.last_error = f"{type(e).__name__}: {e}"[:200]
                log.exception("%s: ошибка опроса", self.name)
                if failures == 3:
                    await self._alert(f"⚠️ {self.name} не работает третий раз подряд: {self.last_error}")
            await asyncio.sleep(self._backoff(failures) if failures else self.interval)

    def _backoff(self, failures: int) -> int:
        return min(self.interval * (2 ** min(failures, 4)), 6 * 3600)

    async def _alert(self, text: str) -> None:
        if self.alert:
            try:
                await self.alert(text)
            except Exception:
                log.exception("alert failed")

    def status_line(self) -> str:
        if self.last_error:
            state = f"⚠️ {self.last_error}"
        elif self.last_ok:
            state = f"✅ {int((time.time() - self.last_ok) / 60)} мин назад"
        else:
            state = "⏳ ещё не опрашивался"
        return f"{self.name} — каждые {self.interval // 60} мин, лидов: {self.found_total}, {state}"
