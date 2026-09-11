"""Рейтинг и ранги — чистая арифметика в fixed-point (§7.3, §7.4 контракта).

B = (prior·k + Σ score) / (k + n); Рейтинг = round_half_up(B × 100).
Всё в целых: оценки хранятся в тысячных (`score_milli`, 1000..10000).
Float не используется нигде — только Fraction/целочисленное деление.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

# Параметры формулы v1 (замораживаются в таблице formula при первой публикации сезона)
PRIOR_MILLI = 5500  # 5.5
K = 3
CALIBRATION_GAMES = 3  # «Калибровка» — меньше 3 игр в сезоне
ROOKIE_GAMES = 3  # «Новичок» — меньше 3 игр за всё время

# имена и порядок — как в разборах автора (статьи 16.08.2026, D10)
SKILLS = (
    ("analysis", "Аналитика"),
    ("intuition", "Интуиция"),
    ("influence", "Влияние"),
    ("role", "Исполнение роли"),
    ("discipline", "Дисциплина"),
)


def round_half_up(value: Fraction) -> int:
    """Округление половины вверх (в контракте — единая политика последнего шага)."""
    floor = value.numerator // value.denominator
    remainder = value - floor
    return floor + 1 if remainder >= Fraction(1, 2) else floor


def smoothed_milli(score_sum_milli: int, games: int) -> Fraction:
    """B в тысячных как точная дробь."""
    return Fraction(PRIOR_MILLI * K + score_sum_milli, K + games)


def rating(score_sum_milli: int, games: int) -> int:
    """«Рейтинг» — целое: 782 означает сглаженную оценку 7.82."""
    return round_half_up(smoothed_milli(score_sum_milli, games) / 10)


def average_milli(score_sum_milli: int, games: int) -> int | None:
    """Фактическая средняя оценка в тысячных (без сглаживания)."""
    if games <= 0:
        return None
    return round_half_up(Fraction(score_sum_milli, games))


def score_from_skills(skills_milli: dict[str, int]) -> int:
    """Итоговая оценка партии — среднее пяти навыков, round-half-up на последнем шаге."""
    values = [skills_milli[code] for code, _ in SKILLS]
    return round_half_up(Fraction(sum(values), len(values)))


def format_milli(value: int | None, digits: int = 1) -> str:
    """1000-е → человеческая строка: 7820 → «7.8»."""
    if value is None:
        return "—"
    scale = 10**digits
    scaled = round_half_up(Fraction(value * scale, 1000))
    whole, frac = divmod(scaled, scale)
    return f"{whole}.{str(frac).rjust(digits, '0')}" if digits else str(whole)


@dataclass(frozen=True)
class Rank:
    code: str
    name: str
    min_xp: int


RANKS: tuple[Rank, ...] = (
    Rank("recruit", "Новобранец", 0),
    Rank("soldier", "Солдат", 500),
    Rank("capo", "Капо", 1500),
    Rank("consigliere", "Консильери", 3000),
    Rank("don", "Дон", 6000),
    Rank("godfather", "Крёстный отец", 10000),
)

# XP (§7.4)
XP_PARTICIPATION = 100  # за каждую партию
XP_WIN = 50
XP_NOMINATION = 30  # только FULL
XP_FIRST_ROLE = 20
XP_STREAK = {3: 50, 5: 100, 10: 250}


def rank_for(xp: int) -> Rank:
    current = RANKS[0]
    for rank in RANKS:
        if xp >= rank.min_xp:
            current = rank
    return current


def next_rank(xp: int) -> Rank | None:
    for rank in RANKS:
        if xp < rank.min_xp:
            return rank
    return None


def rank_progress(xp: int) -> int:
    """Прогресс до следующего ранга в процентах (0..100)."""
    nxt = next_rank(xp)
    if nxt is None:
        return 100
    cur = rank_for(xp)
    span = nxt.min_xp - cur.min_xp
    if span <= 0:
        return 100
    return max(0, min(100, (xp - cur.min_xp) * 100 // span))
