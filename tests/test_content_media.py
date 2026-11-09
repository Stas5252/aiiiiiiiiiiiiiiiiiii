import io
import subprocess

import pytest
from PIL import Image

from leadhunter.content.render import (Brand, find_font, load_font, render_carousel, render_cover, render_scene,
                                       render_slide)
from leadhunter.content.video import build_video, ffmpeg_available, probe_duration

BRAND = Brand(name="Студия Лидов", handle="@leadhunt")
LONG = "Длинный текст про автоматизацию бизнеса и ИИ-ассистентов для салонов красоты. " * 20


def _open(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


def test_render_slide_png():
    img = _open(render_slide("Заголовок слайда", "Текст слайда на русском", 2, 7, BRAND))
    assert img.format == "PNG" and img.size == (1080, 1350) and img.mode == "RGB"


def test_carousel_seven_slides():
    slides = [{"title": f"Слайд номер {i} с длинным заголовком про воронки и лиды", "text": LONG[:300]} for i in range(7)]
    out = render_carousel(slides, BRAND)
    assert len(out) == 7
    assert all(_open(b).size == (1080, 1350) for b in out)


def test_cover_and_scene_sizes():
    assert _open(render_cover("Как получать заявки", "5 шагов", BRAND)).size == (1080, 1080)
    assert _open(render_scene("Крупный текст по центру", BRAND)).size == (1080, 1920)
    assert _open(render_scene("Текст", BRAND, size=(540, 960), subtitle="подпись")).size == (540, 960)


def test_very_long_text_and_unbroken_word():
    img = _open(render_slide(LONG, "я" * 3000, 3, 7, BRAND))
    assert img.size == (1080, 1350)
    _open(render_scene(LONG * 3, BRAND))


def test_text_actually_drawn():
    img = _open(render_slide("Привет", "мир", 2, 3, BRAND))
    assert len(img.getcolors(maxcolors=1 << 20)) > 5


def test_font_fallback(monkeypatch):
    from leadhunter.content import render
    monkeypatch.setattr(render, "find_font", lambda bold=False: None)
    render.load_font.cache_clear()
    try:
        assert render.load_font(40) is not None
        assert _open(render.render_slide("Привет", "мир", 1, 1, BRAND)).size == (1080, 1350)
    finally:
        render.load_font.cache_clear()


def test_find_font_returns_path_or_none():
    assert find_font() is None or find_font().endswith(".ttf")
    assert load_font(20, True) is not None


need_ffmpeg = pytest.mark.skipif(not ffmpeg_available(), reason="нет ffmpeg")


def _silence(fmt: str, seconds: float = 1.0) -> bytes:
    codec = ["-c:a", "libmp3lame"] if fmt == "mp3" else []
    r = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", str(seconds),
                        *codec, "-f", fmt, "-"], capture_output=True)
    if r.returncode != 0 or not r.stdout:
        pytest.skip(f"ffmpeg не умеет {fmt}")
    return r.stdout


@need_ffmpeg
async def test_build_video_no_audio(tmp_path):
    out = tmp_path / "v.mp4"
    res = await build_video([{"text": "Первая сцена"}, {"text": "Вторая сцена", "duration": 3.0}], BRAND, str(out),
                            size=(540, 960))
    assert res == str(out) and out.stat().st_size > 0
    assert await probe_duration(res) == pytest.approx(6.0, abs=0.5)


@need_ffmpeg
async def test_build_video_with_audio(tmp_path):
    try:
        audio = _silence("mp3")
    except pytest.skip.Exception:
        audio = _silence("wav")
    out = tmp_path / "a.mp4"
    await build_video([{"text": "Озвучка", "audio": audio}], BRAND, str(out), size=(540, 960))
    assert 1.2 <= await probe_duration(str(out)) <= 1.7
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_type",
                            "-of", "csv=p=0", str(out)], capture_output=True, text=True)
    assert "audio" in probe.stdout


@need_ffmpeg
async def test_build_video_mixed_audio_and_music(tmp_path):
    audio = _silence("wav")
    music = tmp_path / "m.wav"
    music.write_bytes(_silence("wav", 0.5))
    out = tmp_path / "m.mp4"
    await build_video([{"text": "Без звука", "duration": 1.0}, {"text": "Со звуком", "audio": audio}], BRAND,
                      str(out), music_path=str(music), size=(540, 960))
    assert await probe_duration(str(out)) == pytest.approx(2.3, abs=0.5)


async def test_probe_duration_bad_path():
    assert await probe_duration("/nonexistent/file.mp4") == 0.0
