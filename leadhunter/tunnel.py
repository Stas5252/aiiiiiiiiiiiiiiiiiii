"""Публичный HTTPS для Mini App без домена: Cloudflare quick tunnel (trycloudflare.com, бесплатно, без аккаунта).
Адрес случайный и меняется при каждом запуске — бот сам переставляет кнопку меню.
Если cloudflared умер, watch() поднимает его заново и сообщает новый адрес."""
import asyncio
import logging
import re
import shutil

log = logging.getLogger(__name__)
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


class CloudflareTunnel:
    def __init__(self, port: int, binary: str = "cloudflared"):
        self.port = port
        self.binary = binary
        self.proc: asyncio.subprocess.Process | None = None
        self.url: str | None = None
        self._drain_task: asyncio.Task | None = None

    @staticmethod
    def available(binary: str = "cloudflared") -> bool:
        return shutil.which(binary) is not None

    async def start(self, timeout: float = 40.0) -> str:
        if not self.available(self.binary):
            raise RuntimeError("cloudflared не установлен: https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation/")
        self.proc = await asyncio.create_subprocess_exec(
            self.binary, "tunnel", "--url", f"http://localhost:{self.port}", "--no-autoupdate",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        assert self.proc.stderr is not None
        try:
            async with asyncio.timeout(timeout):
                while True:
                    line = await self.proc.stderr.readline()
                    if not line:
                        raise RuntimeError("cloudflared завершился, не выдав адрес")
                    m = URL_RE.search(line.decode(errors="ignore"))
                    if m:
                        self.url = m.group(0)
                        break
        except TimeoutError:
            await self.stop()
            raise RuntimeError(f"cloudflared не выдал адрес за {timeout:g} секунд")
        except BaseException:
            await self.stop()
            raise
        self._drain_task = asyncio.create_task(self._drain(), name="cloudflared-log")
        log.info("Cloudflare tunnel: %s → localhost:%s", self.url, self.port)
        return self.url

    async def _drain(self) -> None:
        # Читаем stderr до конца, иначе буфер переполнится и процесс встанет
        assert self.proc and self.proc.stderr
        while await self.proc.stderr.readline():
            pass

    async def watch(self, on_url, restart_delay: float = 5.0, max_delay: float = 300.0, timeout: float = 40.0) -> None:
        """Следит за процессом: умер — пишет в лог, поднимает заново (с растущей паузой) и вызывает on_url(новый адрес)."""
        delay = restart_delay
        while True:
            assert self.proc is not None
            started = asyncio.get_running_loop().time()
            code = await self.proc.wait()
            lived = asyncio.get_running_loop().time() - started
            log.warning("cloudflared завершился (код %s) — адрес %s больше не работает, перезапускаю туннель", code, self.url)
            delay = restart_delay if lived > 60 else delay
            while True:
                await asyncio.sleep(delay)
                try:
                    url = await self.start(timeout)
                    break
                except Exception as e:
                    delay = min(delay * 2, max_delay)
                    log.error("туннель не поднялся: %s; следующая попытка через %g с", e, delay)
            delay = min(delay * 2, max_delay)  # если снова умрёт сразу — ждём дольше
            try:
                await on_url(url)
            except Exception:
                log.exception("туннель перезапущен, но новый адрес применить не удалось")

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except TimeoutError:
                self.proc.kill()
