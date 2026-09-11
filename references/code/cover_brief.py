"""Бриф обложки из событий партии — сцена с людьми, но без имён, оценок и лиц игроков.

Решение владельца 03.09.2026: обложек два варианта, и промпт каждого строится по событиям
партии из разбора (перелом, что решило игру). Первая живая обложка (партия 28) была
одинаковой для любой партии: «латунь, тёмное дерево, стол» — бриф не знал о партии ничего.

Правка 04.09.2026: обложки выходили пустыми — стол без людей или силуэты в дыму, потому
что бриф v2 запрещал людей вовсе, а второй вариант («символ перелома») был натюрмортом.
Эталон — иллюстрации ручных разборов владельца в telegra.ph: живописная нуар-сцена, полный
стол персонажей с живыми лицами, лист ведущего с крестиками, лампы, стаканы, тень в шляпе
за спинами. Поэтому бриф теперь описывает **сцену с героями партии**: сколько людей за
столом, кто в центре и что за момент рисуем.

Мотивы берутся из той же хронологии, что у оценок и статьи (`scoring.build_timeline`),
и формулируются образами, а не терминами партии: «выстрел, остановленный в последний
момент», «двое покинули стол в один день», «победа одиночки». Герой партии — тоже образ
(«одиночка, дошедший до конца»), без имени, места и ника: бриф не получает ни состава,
ни листа, только роли и хронологию.
"""

from __future__ import annotations

from typing import Any

from app.analysis import scoring

BRIEF_VERSION = 3

# Два варианта — два взгляда на одну партию: весь стол и герой её перелома. Оба — с людьми:
# вариант «символ без людей» давал пустые обложки (замечание владельца 04.09.2026).
VARIANTS = {
    1: {
        "code": "table_scene",
        "visual_direction": "общий план стола в закрытом клубе: игроки в вечерней одежде спорят, "
        "перед ведущим лист с пометками, стаканы, латунные лампы, дым в луче света; "
        "лица живые и разные, эмоция спора читается",
    },
    2: {
        "code": "hero_moment",
        "visual_direction": "герой партии крупным планом в решающий момент: он в фокусе и в свете, "
        "остальной стол — размытым вторым планом в тени; тот же клубный интерьер",
    },
}

_MOOD = {
    "red": "рассвет над столом, город выстоял",
    "black": "тень накрывает стол, ночь взяла своё",
    "neutral": "одинокая фигура над пустым столом",
}

# Кто в центре обложки. Образ, а не карточка игрока: ни имени, ни места, ни ника.
_HERO_BLACK = "двое в тени, что весь вечер вели стол за собой"
_HERO_NEUTRAL = "одиночка с лицом своего человека — тот, кому стол верил до последнего дня"
_HERO_TABLE = "стол, дожавший чёрных голосованием: руки, поднятые над картами"


def hero(ctx: scoring.GameContext, timeline: scoring.Timeline) -> str:
    """Герой партии — по исходу и по тому, чей ход её решил."""
    if ctx.winner_side == "neutral":
        return _HERO_NEUTRAL
    if ctx.winner_side == "black":
        return _HERO_BLACK
    hits = sum(1 for c in timeline.checks if c["role_code"] == "commissar" and c["hit"])
    if hits >= 2:
        return "сыщик с бумагами в руках — тот, чья находка развернула стол"
    if any(h["saved"] for h in timeline.heals):
        return "врач, успевший к выстрелу: рука, закрывшая соседа"
    return _HERO_TABLE


def motifs(ctx: scoring.GameContext, timeline: scoring.Timeline) -> list[str]:
    """Образы ключевых событий партии — по хронологии, без имён и ролей."""
    result: list[str] = []
    days = sum(1 for kind, _ in timeline.phases if kind == "day")
    nights = sum(1 for kind, _ in timeline.phases if kind == "night")
    saves = [h for h in timeline.heals if h["saved"]]
    if saves:
        result.append("выстрел, остановленный в последний момент")
    double = [no for no, seats in _expelled_by_day(ctx).items() if len(seats) >= 2]
    if double:
        result.append("двое покинули стол в один день")
    first_night = timeline.night_deaths.get(1, frozenset())
    if len(first_night) >= 2:
        result.append("первая ночь уносит сразу двоих")
    hits = sum(1 for c in timeline.checks if c["role_code"] == "commissar" and c["hit"])
    if hits >= 3:
        result.append("цепочка находок, звено за звеном")
    alive_at_end = [s for s in ctx.seats if timeline.survived(s)]
    if ctx.winner_side == "neutral":
        result.append("один против всех — и один остался")
    elif len(alive_at_end) <= 2:
        result.append("финал один на один")
    if nights >= 4:
        result.append(f"долгая партия: {nights} ночей")
    elif days <= 2:
        result.append("всё решилось быстро")
    if not result:
        result.append("тихая партия без резких поворотов")
    return result[:4]


def _expelled_by_day(ctx: scoring.GameContext) -> dict[int, set[int]]:
    result: dict[int, set[int]] = {}
    for event in ctx.events:
        if event["type"] == "expelled" and event["seat_no"] is not None and event["phase"] == "day":
            result.setdefault(int(event["phase_no"]), set()).add(int(event["seat_no"]))
    return result


def brief(ctx: scoring.GameContext, timeline: scoring.Timeline, *, game_ref: str, variant: int) -> dict[str, Any]:
    spec = VARIANTS.get(variant, VARIANTS[1])
    alive_at_end = [s for s in ctx.seats if timeline.survived(s)]
    return {
        "schema_version": BRIEF_VERSION,
        "game_ref": game_ref,
        "variant": spec["code"],
        "visual_direction": spec["visual_direction"],
        "mood": _MOOD.get(ctx.winner_side or "", "напряжение за столом"),
        "hero": hero(ctx, timeline),
        "people_at_table": len(ctx.seats),
        "people_at_the_end": len(alive_at_end),
        "motifs": motifs(ctx, timeline),
        "forbidden": ["text", "logos", "nicknames", "roles", "scores", "portraits of real people"],
    }
