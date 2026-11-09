"""Картинки и озвучка через OpenAI-совместимый API (gpt-image / DALL·E, tts). Без ключа — обложки рисуются шаблоном."""
import base64
import logging

import httpx

log = logging.getLogger(__name__)


class MediaAI:
    def __init__(self, api_key: str | None, base_url: str, *, image_model: str = "", tts_model: str = "",
                 tts_voice: str = "alloy"):
        self.image_model = image_model if api_key else ""
        self.tts_model = tts_model if api_key else ""
        self.tts_voice = tts_voice
        self.client = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers={"Authorization": f"Bearer {api_key or ''}"},
                                        timeout=180)

    @property
    def images_enabled(self) -> bool:
        return bool(self.image_model)

    @property
    def tts_enabled(self) -> bool:
        return bool(self.tts_model)

    async def image(self, prompt: str, size: str = "1024x1024") -> bytes | None:
        if not self.image_model:
            return None
        payload = {"model": self.image_model, "prompt": prompt[:3000], "n": 1, "size": size}
        if self.image_model.startswith("dall-e"):
            payload["response_format"] = "b64_json"  # gpt-image-* этот параметр не принимает, у него всегда b64
        r = await self.client.post("/images/generations", json=payload)
        r.raise_for_status()
        item = r.json()["data"][0]
        if item.get("b64_json"):
            return base64.b64decode(item["b64_json"])
        if item.get("url"):
            dl = await self.client.get(item["url"])
            dl.raise_for_status()
            return dl.content
        return None

    async def speech(self, text: str) -> bytes | None:
        if not self.tts_model or not text.strip():
            return None
        r = await self.client.post("/audio/speech", json={"model": self.tts_model, "voice": self.tts_voice,
                                                          "input": text[:4000], "response_format": "mp3"})
        r.raise_for_status()
        return r.content
