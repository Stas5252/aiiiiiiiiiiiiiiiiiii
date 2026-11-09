import base64
import json
import struct

import httpx
import pytest

from leadhunter.content import gemini as g
from leadhunter.content.gemini import GeminiError, GeminiMedia, gemini_openai_base_url, is_gemini_key, pcm_to_wav


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def media(handler, key="AIzaTEST") -> GeminiMedia:
    return GeminiMedia(key, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def output(*content) -> dict:
    return {"id": "v1_x", "status": "completed",
            "steps": [{"type": "user_input", "content": [{"type": "text", "text": "q"}]},
                      {"type": "model_output", "content": list(content)}]}


@pytest.fixture
def no_sleep(monkeypatch):
    slept = []

    async def fake(s):
        slept.append(s)
    monkeypatch.setattr(g, "_sleep", fake)
    return slept


def test_helpers():
    assert is_gemini_key("AIzaSyABC") and not is_gemini_key("sk-abc") and not is_gemini_key("")
    assert gemini_openai_base_url() == "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert GeminiMedia.DEFAULTS["video_model"] == "gemini-omni-1.2-flash"


async def test_image():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=output({"type": "image", "mime_type": "image/png", "data": b64(b"\x89PNGdata")}))

    png = await media(handler).image("кот в шляпе", aspect="9:16")
    assert png == b"\x89PNGdata"
    req = seen[0]
    assert req.url.path == "/v1beta/interactions" and req.headers["x-goog-api-key"] == "AIzaTEST"
    body = json.loads(req.content)
    assert body["model"] == "gemini-nano-banana-2.1"
    assert body["response_format"]["aspect_ratio"] == "9:16" and body["input"][0]["text"] == "кот в шляпе"


async def test_image_empty_response_raises():
    m = media(lambda req: httpx.Response(200, json=output({"type": "text", "text": "не могу"})))
    with pytest.raises(GeminiError, match="не могу"):
        await m.image("x")


def test_pcm_to_wav_header():
    pcm = b"\x01\x00" * 100
    wav = pcm_to_wav(pcm)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    channels, rate, byte_rate, _, bits = struct.unpack("<HIIHH", wav[22:36])
    assert (channels, rate, byte_rate, bits) == (1, 24000, 48000, 16)
    assert wav[36:40] == b"data" and struct.unpack("<I", wav[40:44])[0] == len(pcm) and wav[44:] == pcm


async def test_speech_wraps_raw_pcm():
    pcm = b"\x02\x00" * 240
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=output({"type": "audio", "mime_type": "audio/l16;rate=24000", "data": b64(pcm)}))

    wav = await media(handler).speech("Привет")
    assert wav[:4] == b"RIFF" and struct.unpack("<I", wav[24:28])[0] == 24000
    assert struct.unpack("<I", wav[40:44])[0] == len(pcm)
    assert seen[0]["generation_config"]["speech_config"] == [{"voice": "Kore"}]
    assert seen[0]["model"] == "gemini-3.8-flash-tts"


async def test_speech_passes_through_ready_wav():
    ready = pcm_to_wav(b"\x00\x00" * 10)
    m = media(lambda req: httpx.Response(200, json=output({"type": "audio", "mime_type": "audio/wav", "data": b64(ready)})))
    assert await m.speech("hi") == ready


async def test_video_sync_with_first_frame():
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=output({"type": "video", "mime_type": "video/mp4", "data": b64(b"MP4BYTES")}))

    mp4 = await media(handler).video("волны", image=b"\x89PNG....", duration=99, negative_prompt="текст")
    assert mp4 == b"MP4BYTES"
    body = json.loads(seen[0].content)
    assert body["model"] == "gemini-omni-1.2-flash"
    assert body["response_format"] == {"type": "video", "aspect_ratio": "9:16", "duration": "15s", "resolution": "720p"}
    assert body["input"][0]["type"] == "image" and body["input"][0]["mime_type"] == "image/png"
    assert "текст" in body["input"][1]["text"]


async def test_video_polls_until_done_and_downloads_uri(no_sleep):
    calls = []

    def handler(req):
        calls.append((req.method, str(req.url)))
        assert req.headers["x-goog-api-key"] == "AIzaTEST"
        if req.method == "POST":
            return httpx.Response(200, json={"id": "v1_op", "status": "in_progress", "steps": []})
        if req.url.path.endswith("/interactions/v1_op"):
            n = sum(1 for m, u in calls if m == "GET" and u.endswith("/interactions/v1_op"))
            if n < 2:
                return httpx.Response(200, json={"id": "v1_op", "status": "in_progress", "steps": []})
            return httpx.Response(200, json=output({"type": "video", "mime_type": "video/mp4",
                                                    "uri": "https://generativelanguage.googleapis.com/v1beta/files/abc:download?alt=media"}))
        assert req.url.path.endswith("/files/abc:download")
        return httpx.Response(200, content=b"MP4FILE")

    assert await media(handler).video("x", poll_interval=3) == b"MP4FILE"
    assert no_sleep == [3, 3]
    assert [m for m, _ in calls] == ["POST", "GET", "GET", "GET"]


async def test_video_filtered_raises_russian_error():
    m = media(lambda req: httpx.Response(200, json=output({"type": "text", "text": "запрос нарушает правила безопасности"})))
    with pytest.raises(GeminiError, match="Omni отклонил запрос: запрос нарушает"):
        await m.video("x")


async def test_video_failed_status():
    m = media(lambda req: httpx.Response(200, json={"status": "failed", "error": {"message": "internal"}, "steps": []}))
    with pytest.raises(GeminiError, match="internal"):
        await m.video("x")


async def test_video_timeout(no_sleep):
    m = media(lambda req: httpx.Response(200, json={"id": "v1_op", "status": "in_progress", "steps": []}))
    with pytest.raises(GeminiError, match="не завершилась"):
        await m.video("x", poll_interval=5, timeout=10)
    assert no_sleep == [5, 5]


async def test_http_error_message():
    m = media(lambda req: httpx.Response(429, json={"error": {"code": 429, "message": "Quota exceeded", "status": "RESOURCE_EXHAUSTED"}}))
    with pytest.raises(GeminiError, match="Gemini 429: Quota exceeded"):
        await m.image("x")


async def test_http_error_plain_text():
    m = media(lambda req: httpx.Response(503, text="upstream down"))
    with pytest.raises(GeminiError, match="Gemini 503: upstream down"):
        await m.speech("x")


async def test_disabled_without_key_makes_no_calls():
    def boom(req):
        raise AssertionError("HTTP не должен вызываться")

    m = media(boom, key="")
    assert not (m.images_enabled or m.video_enabled or m.tts_enabled)
    assert await m.image("x") is None and await m.speech("x") is None and await m.video("x") is None


async def test_tts_rate_limit_waits_then_falls_back(monkeypatch):
    """Минутный лимит → пауза и повтор; дневной лимит → lite-модель."""
    from leadhunter.content import gemini as g

    calls: list[str] = []
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(g, "_sleep", fake_sleep)

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        calls.append(body["model"])
        if len(calls) == 1:
            return httpx.Response(429, json={"error": {"message": "Rate limit exceeded (limit: 3 requests per minute). Please retry in 7s"}})
        if len(calls) == 2:
            return httpx.Response(429, json={"error": {"message": "Rate limit exceeded (limit: 10 requests per day). Please retry in 14h45m"}})
        wav = b"RIFF" + b"\x00" * 40
        return httpx.Response(200, json={"steps": [{"type": "model_output", "content": [{"type": "audio", "mime_type": "audio/wav",
                                                                                          "data": base64.b64encode(wav).decode()}]}]})

    media = g.GeminiMedia("AIzaTest", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    out = await media.speech("Привет")
    assert out and out[:4] == b"RIFF"
    assert calls == [g.DEFAULTS["tts_model"], g.DEFAULTS["tts_model"], g.TTS_FALLBACK] and sleeps == [9.0]
