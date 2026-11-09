"""Семантический дедуп без внешних моделей: шинглы из лемм + Жаккар.
Ловит перефразированные повторы («нужен сайт для салона, бюджет 30к» ≈ «салону нужен сайт, 30к бюджет»)."""
import time
from collections import deque

from .filters import lemmatize

STOP = {"и", "в", "на", "с", "по", "для", "не", "а", "но", "у", "о", "к", "за", "из", "от", "до", "же", "ли", "бы",
        "то", "это", "я", "мы", "вы", "он", "она", "они", "мой", "наш", "ваш", "кто", "что", "как", "где", "там",
        "тут", "очень", "ещё", "еще", "уже", "или", "если", "the", "a", "to", "of"}


def shingles(text: str, n: int = 2) -> set[str]:
    lem = [w for w in lemmatize(text) if w not in STOP and len(w) > 1]
    if len(lem) < n:
        return set(lem)
    return {" ".join(lem[i:i + n]) for i in range(len(lem) - n + 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class NearDuplicateIndex:
    def __init__(self, ttl_hours: int = 48, max_items: int = 2000, threshold: float = 0.6):
        self.ttl = ttl_hours * 3600
        self.threshold = threshold
        self._items: deque[tuple[float, set[str]]] = deque(maxlen=max_items)

    def is_duplicate(self, text: str) -> bool:
        """Проверяет и запоминает. True — похожий текст уже был."""
        now = time.time()
        while self._items and now - self._items[0][0] > self.ttl:
            self._items.popleft()
        sh = shingles(text)
        if len(sh) >= 3:
            for _, other in self._items:
                if jaccard(sh, other) >= self.threshold:
                    return True
        self._items.append((now, sh))
        return False
