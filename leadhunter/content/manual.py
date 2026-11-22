"""Материалы без ИИ: владелец сам пишет слайды, сцены или реплики через «|» — в боте и в кабинете одинаково."""
from .toon import MAX_CHARS, MAX_SCENE_CHARS

MANUAL_HINT = ("Без ИИ можно: /publish текст, /carousel Заголовок | слайд 1 | слайд 2, /reel фраза 1 | фраза 2, "
               "/toon Название | Директор: реплика | Разработчик: реплика")
_COLORS = ("#F2C14E", "#6FB9F0", "#8EEE98", "#F08A6F", "#C9A0F5", "#8ED1A0")


def _toon(parts: list[str]) -> dict:
    """«Название | Имя: реплика | Имя: реплика». В кадре до трёх героев — говорящий есть всегда."""
    lines: list[tuple[str, str]] = []
    chars: dict[str, dict] = {}
    for p in parts[1:]:
        who, sep, text = p.partition(":")
        who, text = (who.strip() or "Герой", text.strip()) if sep and text.strip() else ("Герой", p.strip())
        if who not in chars and len(chars) >= MAX_CHARS:
            who = next(iter(chars))  # героев больше, чем помещается в мультик, — реплику говорит первый
        chars.setdefault(who, {"name": who, "color": _COLORS[len(chars) % len(_COLORS)], "shape": "round"})
        lines.append((who, text))
    order = list(chars)
    scenes = []
    for n, (who, text) in enumerate(lines):
        # рядом с говорящим — те, кто говорил только что, а если таких нет — кто заговорит следом
        around = [w for w, _ in reversed(lines[:n])] + [w for w, _ in lines[n + 1:]]
        cast = [who]
        for w in around:
            if w not in cast and len(cast) < MAX_SCENE_CHARS:
                cast.append(w)
        cast.sort(key=order.index)
        spots = ("left", "right") if len(cast) == 2 else ("left", "center", "right") if len(cast) == 3 else ("center",)
        scenes.append({"bg": "office", "duration": 3.5,
                       "characters": [{"id": c, "pos": spots[i], "emotion": "happy" if c == who else "neutral",
                                       "action": "talk" if c == who else "idle"} for i, c in enumerate(cast)],
                       "line": {"speaker": who, "text": text}})
    return {"title": parts[0], "style": "flat", "characters": chars, "scenes": scenes, "caption": parts[0], "hashtags": []}


def manual_data(kind: str, topic: str) -> dict | None:
    """Материал из текста с разделителем «|» (первый кусок — заголовок) или None, если формат не подходит."""
    parts = [p.strip() for p in str(topic or "").split("|") if p.strip()]
    if len(parts) < 2:
        return None
    if kind == "carousel":
        return {"title": parts[0], "slides": [{"title": parts[0], "text": ""}] + [{"title": "", "text": p} for p in parts[1:]],
                "caption": parts[0], "hashtags": []}
    if kind == "reel":
        return {"title": parts[0], "scenes": [{"text": p, "voice": "", "duration": 3} for p in parts],
                "caption": parts[0], "hashtags": []}
    if kind == "toon":
        return _toon(parts)
    return None
