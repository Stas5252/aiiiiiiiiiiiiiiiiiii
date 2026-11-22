"""Файлы контента на диске (data/content) и их публичные адреса через Mini App (нужны Threads/Instagram)."""
import asyncio
import io
import logging
import os
import secrets
import time
from contextlib import contextmanager
from contextvars import ContextVar

log = logging.getLogger(__name__)
TEMP_PREFIX = "pub."  # временные копии для Threads/Instagram; в обычных именах точки до расширения нет
_scope: ContextVar[list | None] = ContextVar("content_temp_files", default=None)


def to_jpeg(data: bytes) -> bytes:
    """Instagram и Threads принимают по ссылке JPEG, а слайды и обложки у нас PNG."""
    if data[:3] == b"\xff\xd8\xff":
        return data
    from PIL import Image
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        log.warning("картинку не удалось перекодировать в JPEG — отдаю как есть")
        return data
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, "#FFFFFF")
        bg.paste(img, mask=img.split()[-1])
        img = bg
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=92)
    return buf.getvalue()


class MediaStore:
    def __init__(self, root: str, public_base: str | None = None):
        self.root = root
        # MINIAPP_URL вида https://host/app → https://host
        self.public_base = public_base.rstrip("/").removesuffix("/app") if public_base else None
        os.makedirs(root, exist_ok=True)
        self.sweep()

    def sweep(self, older_than: float = 3600) -> int:
        """Убрать временные копии, оставшиеся после оборванной публикации."""
        n = 0
        for name in os.listdir(self.root):
            path = self.path(name)
            try:
                if name.startswith(TEMP_PREFIX) and time.time() - os.path.getmtime(path) > older_than:
                    os.remove(path)
                    n += 1
            except OSError:
                pass
        return n

    def remove(self, name: str) -> None:
        try:
            os.remove(self.path(os.path.basename(name)))
        except OSError:
            pass

    @contextmanager
    def temporary(self):
        """Всё, что публикатор выложил через public_url внутри блока, удаляется на выходе."""
        files: list[str] = []
        token = _scope.set(files)
        try:
            yield files
        finally:
            _scope.reset(token)
            for name in files:
                self.remove(name)

    def path(self, name: str) -> str:
        return os.path.join(self.root, name)

    def save(self, data: bytes, ext: str, prefix: str = "") -> str:
        """Случайное имя — файлы раздаются без авторизации, адрес не должен угадываться."""
        name = f"{prefix}{secrets.token_urlsafe(12)}{ext if ext.startswith('.') else '.' + ext}"
        with open(self.path(name), "wb") as f:
            f.write(data)
        return name

    def read(self, name: str) -> bytes:
        with open(self.path(name), "rb") as f:
            return f.read()

    def url(self, name: str) -> str | None:
        return f"{self.public_base}/media/{name}" if self.public_base else None

    async def public_url(self, data: bytes, ext: str) -> str:
        if not self.public_base:
            raise RuntimeError("Для публикации картинок и видео в Threads/Instagram нужен MINIAPP_URL (публичный HTTPS)")
        if ext.lower().lstrip(".") in ("jpg", "jpeg"):
            data = await asyncio.to_thread(to_jpeg, data)  # расширение в ссылке должно совпадать с содержимым
        name = self.save(data, ext, TEMP_PREFIX)
        files = _scope.get()
        if files is not None:
            files.append(name)
        return self.url(name)
