import asyncio
import html
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from ..http import CaptchaError, NeedLoginError, in_work_hours
from ..models import SOURCE_GROUP_TITLES, SOURCE_SETTINGS, Lead

Emit = Callable[..., Awaitable[bool]]
Alert = Callable[[str], Awaitable[None]]
Enabled = Callable[[], Awaitable[bool]]
log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    text = html.unescape(_TAG_RE.sub(" ", text or "")).replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", text).strip()


def aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class SourceError(Exception):
    """Сбой источника с понятной владельцу причиной (уходит в /sources и алерт как есть)."""


class Source:
    name: str = "base"
    interval: int = 600
    # Источник ходит по сайтам «как человек»: только в рабочие часы, с паузами
    human: bool = False
    work_hours: str = "0-24"
    tz: str = "Europe/Moscow"
    alert: Alert | None = None
    # Включена ли группа источника в настройках (кабинет → «Источники»). None = всегда включён
    enabled: Enabled | None = None
    # Состояние для /sources: когда последний раз получилось и что сломалось
    last_ok: float | None = None
    last_error: str | None = None
    last_warning: str | None = None
    found_total: int = 0
    burst_limit: int = 5  # максимум уведомлений за один проход источника
    # Источник отвечает, но свежих записей нет: после стольких проходов подряд — пометка в /sources и алерт
    fresh_hours: int = 48
    stale_alert_after: int = 3
    off: bool = False
    failures: int = 0
    stale_polls: int = 0

    async def poll(self) -> list[Lead]:
        raise NotImplementedError

    def blocked_reason(self) -> str:
        """Непустая строка — источник не настроен и опрашиваться не будет (причина видна в /sources)."""
        return ""

    async def is_enabled(self) -> bool:
        return await self.enabled() if self.enabled else True

    async def run(self, emit: Emit) -> None:
        """По умолчанию — опрос раз в interval секунд с бэкоффом при ошибках. Realtime-источники переопределяют run()."""
        while True:
            await asyncio.sleep(await self.step(emit))

    async def step(self, emit: Emit) -> float:
        """Один проход. Возвращает паузу до следующего."""
        self.off = not await self.is_enabled()
        if self.off:
            return min(self.interval, 300)  # выключенную группу не опрашиваем, только ждём включения
        if self.blocked_reason():
            return 600
        if self.human and not in_work_hours(self.work_hours, self.tz):
            return 600
        try:
            leads = await self.poll()
            # Первый проход после запуска: страница биржи — это десятки старых заказов. Шлём не больше
            # burst_limit самых свежих, остальные уходят в базу молча (есть в /leads и кабинете).
            leads.sort(key=lambda l: aware(l.found_at), reverse=True)
            found = 0
            for lead in leads:
                if await emit(lead, quiet=found >= self.burst_limit):
                    found += 1
            self.found_total += found
            self.last_ok, self.last_error, self.failures = time.time(), None, 0
            log.info("%s: %d записей, %d новых лидов", self.name, len(leads), found)
            await self._check_fresh(leads)
        except (CaptchaError, NeedLoginError) as e:
            self.failures += 1
            self.last_error = str(e)[:200]
            log.warning("%s: %s", self.name, e)
            await self._alert(f"⚠️ {self.name}: {e}. Пауза {self._backoff(self.failures) // 60} мин.")
        except SourceError as e:
            self.failures += 1
            self.last_error = str(e)[:200]
            log.warning("%s: %s", self.name, e)
            if self.failures == 3:
                await self._alert(f"⚠️ {self.name} не работает третий раз подряд: {self.last_error}")
        except Exception as e:
            self.failures += 1
            self.last_error = f"{type(e).__name__}: {e}"[:200]
            log.exception("%s: ошибка опроса", self.name)
            if self.failures == 3:
                await self._alert(f"⚠️ {self.name} не работает третий раз подряд: {self.last_error}")
        if self.failures:
            return self._backoff(self.failures)
        # Источник отвечает, но пусто: не тратим запросы с прежней частотой
        extra = self.stale_polls - self.stale_alert_after + 1 if self.stale_alert_after else 0
        return self._backoff(extra) if extra > 0 else self.interval

    async def _check_fresh(self, leads: list[Lead]) -> None:
        now = datetime.now(timezone.utc)
        ages = [(now - aware(l.found_at)).total_seconds() / 3600 for l in leads]
        if any(a <= self.fresh_hours for a in ages):
            self.stale_polls, self.last_warning = 0, None
            return
        self.stale_polls += 1
        if not self.stale_alert_after or self.stale_polls < self.stale_alert_after:
            return
        what = f"самая свежая запись — {int(min(ages) // 24)} дн. назад" if ages else "выдача пустая"
        self.last_warning = f"{self.stale_polls} проходов подряд без записей моложе {self.fresh_hours} ч ({what})"
        if self.stale_polls == self.stale_alert_after:
            log.warning("%s: %s", self.name, self.last_warning)
            await self._alert(f"⚠️ {self.name}: {self.last_warning}. Лидов отсюда сейчас нет — "
                              f"проверьте запросы источника или выключите его.")

    def _backoff(self, failures: int) -> int:
        return min(self.interval * (2 ** min(failures, 4)), 6 * 3600)

    async def _alert(self, text: str) -> None:
        if self.alert:
            try:
                await self.alert(text)
            except Exception:
                log.exception("alert failed")

    def status_line(self) -> str:
        blocked = self.blocked_reason()
        if self.off:
            group = SOURCE_GROUP_TITLES.get(SOURCE_SETTINGS.get(self.name, ("",))[0], "")
            state = "⏸ выключен в настройках" + (f" (группа «{group}»)" if group else "") + ", не опрашивается"
        elif blocked:
            state = f"⛔ не запущен: {blocked}"
        elif self.last_error:
            state = f"⚠️ {self.last_error}"
        elif self.last_warning:
            state = f"⚠️ {self.last_warning}"
        elif self.last_ok:
            state = f"✅ {int((time.time() - self.last_ok) / 60)} мин назад"
        else:
            state = "⏳ ещё не опрашивался"
        return f"{self.name} — каждые {self.interval // 60} мин, лидов: {self.found_total}, {state}"
