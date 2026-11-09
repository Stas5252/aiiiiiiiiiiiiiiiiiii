import itertools
import math
import os
import struct
import wave

import pytest
from PIL import Image

from leadhunter.content.render import Brand
from leadhunter.content.toon import (ACTIONS, BGS, EMOTIONS, STYLES, ToonResult, build_toon, normalize_script,
                                     render_frame)
from leadhunter.content.video import ffmpeg_available, probe_duration

BRAND = Brand(name="Студия Лидов", handle="@leadhunt")
CHARS = {"boss": {"name": "Директор", "color": "#F2C14E", "shape": "round"},
         "dev": {"name": "Разработчик", "color": "#6FB9F0", "shape": "tall"}}


def _scene(**kw):
    sc = {"bg": "office", "duration": 4.0,
          "characters": [{"id": "boss", "pos": "left", "emotion": "angry", "action": "talk"},
                         {"id": "dev", "pos": "right", "emotion": "neutral", "action": "idle"}],
          "line": {"speaker": "boss", "text": "Где заявки?!"}, "caption": "Проблема №1"}
    sc.update(kw)
    return sc


def _script(scenes=None, style="flat"):
    return {"title": "Почему сайт не приносит заявки", "style": style, "characters": CHARS,
            "scenes": scenes or [_scene()]}


def _silent_wav(path, seconds):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<h", 0) * int(16000 * seconds))
    return path.read_bytes()


# ── normalize_script ──

def test_normalize_defaults_and_clamping():
    s = normalize_script({"characters": {"a": {}}, "scenes": [
        {"characters": [{"id": "a"}], "duration": 0.2},
        {"bg": "moon", "characters": [{"id": "a", "pos": "top", "emotion": "??", "action": "fly"}], "duration": 99},
        {"line": {"speaker": "a", "text": "Привет"}, "duration": "abc"}]})
    assert s["style"] == "flat"
    assert s["characters"]["a"]["color"].startswith("#") and s["characters"]["a"]["shape"] == "round"
    d = [sc["duration"] for sc in s["scenes"]]
    assert d == [1.5, 8.0, 4.0]
    c = s["scenes"][1]["characters"][0]
    assert (c["pos"], c["emotion"], c["action"]) == ("center", "neutral", "idle")
    assert s["scenes"][1]["bg"] == "abstract"


def test_normalize_drops_bad_scenes_and_limits():
    raw = {"style": "neon", "characters": {f"c{i}": {"color": "zzz"} for i in range(6)},
           "scenes": [{"characters": [{"id": "ghost"}]}, {"caption": "только заголовок"}, "мусор"] +
                     [{"characters": [{"id": "c0"}]} for _ in range(20)]}
    s = normalize_script(raw)
    assert s["style"] == "flat" and len(s["characters"]) == 4 and len(s["scenes"]) == 12
    assert all(c["color"].startswith("#") and len(c["color"]) == 7 for c in s["characters"].values())


def test_normalize_positions_and_speaker_talks():
    s = normalize_script(_script([_scene(characters=[{"id": "boss", "pos": "left"}, {"id": "dev", "pos": "left"}])]))
    ch = s["scenes"][0]["characters"]
    assert len({c["pos"] for c in ch}) == 2
    assert ch[0]["action"] == "talk"  # говорящий не стоит истуканом


@pytest.mark.parametrize("bad", [{}, {"scenes": []}, {"scenes": [{"characters": []}]}, [], None])
def test_normalize_empty_raises(bad):
    with pytest.raises(ValueError):
        normalize_script(bad)


# ── render_frame ──

def test_render_frame_matrix():
    script = normalize_script(_script())
    for style, bg, emo, act in itertools.product(STYLES, BGS, EMOTIONS[:3], ACTIONS[:4]):
        script["style"] = style
        script["scenes"][0].update(bg=bg)
        script["scenes"][0]["characters"][0].update(emotion=emo, action=act)
        img = render_frame(script, 0, 0.9, BRAND, (270, 480))
        assert img.mode == "RGB" and img.size == (270, 480)


@pytest.mark.parametrize("emo", EMOTIONS)
@pytest.mark.parametrize("act", ACTIONS)
def test_render_frame_every_emotion_action(emo, act):
    script = normalize_script(_script([_scene(characters=[{"id": "boss", "pos": "center", "emotion": emo, "action": act}])]))
    for t in (0.0, 0.2, 1.7):
        assert render_frame(script, 0, t, BRAND, (216, 384)).size == (216, 384)


def test_render_frame_changes_over_time_and_accepts_raw_script():
    a = render_frame(_script(), 0, 0.5, BRAND, (270, 480))
    b = render_frame(_script(), 0, 0.8, BRAND, (270, 480))
    assert a.tobytes() != b.tobytes()
    assert len(a.getcolors(1 << 20)) > 20


def test_fade_in_darkens_start():
    first = render_frame(_script(), 0, 0.0, BRAND, (270, 480))
    later = render_frame(_script(), 0, 1.0, BRAND, (270, 480))
    assert sum(first.convert("L").getdata()) != sum(later.convert("L").getdata())
    assert first.getpixel((135, 240)) == tuple(int(BRAND.primary[i:i + 2], 16) for i in (1, 3, 5))


def test_long_cyrillic_line_wraps_without_crash():
    long = "Очень длинная реплика про автоматизацию бизнеса и заявки с сайта. " * 8 + "я" * 120
    script = _script([_scene(line={"speaker": "boss", "text": long}, caption="Очень длинный заголовок " * 6)])
    for style in STYLES:
        script["style"] = style
        assert render_frame(script, 0, 1.0, BRAND, (1080, 1920)).size == (1080, 1920)


def test_scene_without_characters_or_speaker():
    only_line = _script([{"bg": "none", "line": {"speaker": None, "text": "Просто текст"}}])
    assert render_frame(only_line, 0, 1.0, BRAND, (270, 480)).size == (270, 480)
    ghost = _script([_scene(line={"speaker": "dev_not_in_scene", "text": "Эй"})])
    assert render_frame(ghost, 0, 1.0, BRAND, (270, 480)).size == (270, 480)


# ── build_toon ──

needs_ffmpeg = pytest.mark.skipif(not ffmpeg_available(), reason="нужен ffmpeg")


@needs_ffmpeg
async def test_build_toon_two_scenes(tmp_path):
    script = _script([_scene(duration=1.5), _scene(bg="street", duration=1.5, caption="",
                                                    characters=[{"id": "dev", "pos": "center", "action": "walk",
                                                                 "emotion": "happy"}],
                                                    line={"speaker": "dev", "text": "Покажу, как надо"})], style="chalk")
    out = str(tmp_path / "toon.mp4")
    res = await build_toon(script, BRAND, out, size=(270, 480), fps=12)
    assert isinstance(res, ToonResult) and res.scenes == 2 and res.path == out
    assert os.path.getsize(out) > 1000
    assert abs(res.duration - 3.0) < 0.5
    assert abs(await probe_duration(out) - 3.0) < 0.5

    os.makedirs("/tmp/verify", exist_ok=True)
    frame = render_frame(_script([_scene(duration=1.5)]), 0, 0.7, BRAND, (1080, 1920))
    assert frame.size == (1080, 1920)
    frame.save("/tmp/verify/toon_frame.png")


@needs_ffmpeg
async def test_build_toon_with_voiceover_sets_scene_length(tmp_path):
    wav = _silent_wav(tmp_path / "v.wav", 1.0)
    script = _script([_scene(duration=6.0, audio=wav)])
    res = await build_toon(script, BRAND, str(tmp_path / "a.mp4"), size=(270, 480), fps=12)
    assert 1.3 <= res.duration <= 1.7


@needs_ffmpeg
async def test_build_toon_with_music_and_mixed_audio(tmp_path):
    music = tmp_path / "m.wav"
    _silent_wav(music, 0.5)
    wav = _silent_wav(tmp_path / "v.wav", 1.0)
    script = _script([_scene(audio=wav), _scene(duration=1.5)])
    res = await build_toon(script, BRAND, str(tmp_path / "m.mp4"), size=(270, 480), fps=12, music_path=str(music))
    assert math.isclose(res.duration, 1.3 + 1.5, abs_tol=0.5)


@needs_ffmpeg
async def test_build_toon_rejects_empty_script(tmp_path):
    with pytest.raises(ValueError):
        await build_toon({"scenes": []}, BRAND, str(tmp_path / "x.mp4"))


@needs_ffmpeg
async def test_build_toon_ffmpeg_failure_raises(tmp_path):
    with pytest.raises(RuntimeError):
        await build_toon(_script([_scene(duration=1.5)]), BRAND, str(tmp_path / "nodir" / "x.mp4"),
                         size=(270, 480), fps=12)
