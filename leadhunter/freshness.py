"""Шанс забрать заказ: биржи и вакансии живут часами — через 2 часа и 20 откликов писать уже поздно.

Для источников с подразумеваемым намерением (биржи) считаем «свежесть»: возраст и число конкурентов.
Старые/перегретые заказы отсекаются, свежие и пустые — получают бонус к баллу."""
from dataclasses import dataclass
from datetime import datetime, timezone

from .models import Lead

# Источники-биржи: заказ там «протухает» быстро. Остальные (чаты, карты, аудит) живут по общему max_age_hours.
BOARD_SOURCES = {"kwork", "weblancer", "freelance.ru", "hh.ru"}


@dataclass
class Freshness:
    board_max_age_hours: float = 3.0  # старше — почти наверняка уже разобрали
    max_competitors: int = 15  # больше откликов — не пробиться
    hot_age_minutes: float = 30.0  # моложе — бонус к баллу
    hot_competitors: int = 3  # меньше откликов — бонус к баллу

    @classmethod
    def from_settings(cls, raw: dict[str, str | None]) -> "Freshness":
        f = cls()
        try:
            if raw.get("board_max_age_hours"):
                f.board_max_age_hours = float(raw["board_max_age_hours"])
            if raw.get("max_competitors"):
                f.max_competitors = int(raw["max_competitors"])
        except ValueError:
            pass
        return f


def is_board(lead: Lead) -> bool:
    return lead.source in BOARD_SOURCES or lead.source.startswith("rss:") or lead.source == "rss"


def age_minutes(lead: Lead, now: datetime | None = None) -> float:
    found = lead.found_at if lead.found_at.tzinfo else lead.found_at.replace(tzinfo=timezone.utc)
    return max(0.0, ((now or datetime.now(timezone.utc)) - found).total_seconds() / 60)


def check(lead: Lead, f: Freshness, now: datetime | None = None) -> tuple[bool, int, str]:
    """(пропускать?, поправка к баллу, пометка для карточки)."""
    if not is_board(lead):
        return True, 0, ""
    age = age_minutes(lead, now)
    comp = lead.competitors
    if age > f.board_max_age_hours * 60:
        return False, 0, f"старше {f.board_max_age_hours:g} ч"
    if comp is not None and comp > f.max_competitors:
        return False, 0, f"{comp} откликов"
    bonus = 0
    tags = []
    if age <= f.hot_age_minutes:
        bonus += 1
        tags.append(f"{int(age)} мин назад" if age >= 1 else "только что")
    else:
        tags.append(f"{int(age)} мин назад" if age < 120 else f"{age / 60:.1f} ч назад")
    if comp is not None:
        if comp <= f.hot_competitors:
            bonus += 1
        tags.append("откликов нет" if comp == 0 else f"{comp} откл.")
    return True, bonus, " · ".join(tags)
