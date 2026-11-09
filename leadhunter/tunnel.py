"""Публичный HTTPS для Mini App без домена: Cloudflare quick tunnel (trycloudflare.com, бесплатно, без аккаунта).
Адрес случайный и меняется при каждом запуске — бот сам переставляет кнопку меню."""
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
            raise RuntimeError("cloudflared не выдал адрес за 40 секунд")
        asyncio.create_task(self._drain(), name="cloudflared-log")
        log.info("Cloudflare tunnel: %s → localhost:%s", self.url, self.port)
        return self.url

    async def _drain(self) -> None:
        # Читаем stderr до конца, иначе буфер переполнится и процесс встанет
        assert self.proc and self.proc.stderr
        while await self.proc.stderr.readline():
            pass

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except TimeoutError:
                self.proc.kill()
