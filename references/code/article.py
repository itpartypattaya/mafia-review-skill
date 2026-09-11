"""Статья-реконструкция по структуре роудмапа §16: разделы из данных плюс аналитика модели.

Первая живая публикация (партия 28, 03.09.2026) показала, что «короткий клубный разбор»
из плоских абзацев модели не читается как разбор: придуманное название, неверный лид,
«вероятно» в каждом втором абзаце, имена из речи вместо состава. Эталонные разборы
владельца устроены иначе — это реконструкция с фиксированными разделами, и половина из
них не текст, а данные.

Здесь собираются программные разделы — **состав стола** и **хронология по дням и ночам**
— из листа (роли, ночные ходы), подтверждённых фактов (кандидаты, изгнания) и той же
хронологии, по которой считаются оценки (`scoring.build_timeline`). Модель (STORY v2)
пишет только аналитику: название, сводку, абзац к каждой фазе, «что решило игру», ходы и
итог. Оценки и номинации в текст статьи не входят — они живут в данных публикации и
рендерятся страницей таблицей, иначе правка оценки ведущим расходилась бы с текстом.
Текстовый раздел оценок жил один день (04.09.2026): на живой странице он встал рядом с
той же таблицей и показывал другие цифры — сырые оценки прогона против опубликованных.

Программные блоки помечены `source: program`: проверка фактов их не оценивает (они не
могут переврать лист), а редактура имён при публикации работает по тексту, как и для
блоков модели.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.analysis import scoring

SECTION_ROSTER = "Состав стола"
SECTION_NIGHTS = "Ночные события"
SECTION_TIMELINE = "Хронология"
SECTION_DECISIVE = "Что решило игру"
SECTION_MOVES = "Лучшие и худшие ходы"
SECTION_BEST_MOVES = "Лучшие ходы"
SECTION_MISTAKES = "Ошибки"
SECTION_CONCLUSION = "Итог"
SECTION_WHY_LOST = {
    "red": "Почему проиграли чёрные",
    "black": "Почему проиграл город",
    "neutral": "Почему проиграл город",
    "none": "Почему никто не выиграл",
}

# Порядок ночи по своду правил §4.3; роли без ночного хода в ночь не попадают.
_NIGHT_VERBS = {
    "boss": "проверяет",
    "commissar": "проверяет",
    "mafia": "стреляет в",
    "maniac": "стреляет в",
    "doctor": "лечит",
    "agitator": "нагнетает",
}


def _heading(text: str) -> dict[str, Any]:
    return {"type": "heading", "sentence_type": "editorial", "text": text, "source": "program"}


def _item(text: str, fact_ids: list[int] | None = None) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "item", "sentence_type": "editorial", "text": text, "source": "program"}
    if fact_ids:
        block["sentence_type"] = "verified_fact"
        block["fact_ids"] = sorted(set(fact_ids))
        block["claim_id"] = None
        block["judge_opinion_id"] = None
    return block


def _paragraph(text: str) -> dict[str, Any]:
    return {"type": "paragraph", "sentence_type": "editorial", "text": text, "source": "program"}


def nights_blocks(
    ctx: scoring.GameContext,
    timeline: scoring.Timeline,
    names: Mapping[int, str],
    role_names: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Раздел «Ночные события»: все ночи по листу одним списком, до хронологии.

    Так устроены эталонные разборы владельца (13 из 24 имеют этот раздел): читатель
    сначала видит скелет ночей целиком, потом — дни с речью. Абзац на ночь собирается из
    тех же строк, что и ночь в хронологии (решение владельца 04.09.2026, партия 29).
    """
    nights = [no for kind, no in timeline.phases if kind == "night"]
    if not nights:
        return []
    blocks = [_heading(SECTION_NIGHTS)]
    for no in nights:
        lines = [block["text"] for block in _night_blocks(ctx, timeline, no, names, role_names)]
        blocks.append(_paragraph(f"Ночь {no}. " + " ".join(lines)))
    return blocks


def label(seat_no: int, names: Mapping[int, str]) -> str:
    """«№6 Рафаэль» — как в эталонных разборах; имя только из состава."""
    name = names.get(seat_no)
    return f"№{seat_no} {name}" if name else f"№{seat_no}"


def _role_name(ctx: scoring.GameContext, role_code: str, role_names: Mapping[str, str]) -> str:
    return role_names.get(role_code) or (ctx.roles[role_code].name if role_code in ctx.roles else role_code)


def _join(parts: list[str]) -> str:
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " и " + parts[-1]


def roster_blocks(
    ctx: scoring.GameContext, names: Mapping[int, str], role_names: Mapping[str, str]
) -> list[dict[str, Any]]:
    """Состав по группам, как в эталоне: чёрные карты, активные красные, мирные."""
    black: list[str] = []
    active: list[str] = []
    civil: list[str] = []
    for seat_no in sorted(ctx.seats):
        seat = ctx.seats[seat_no]
        role = ctx.roles.get(seat.final_role) or ctx.roles.get(seat.role_code)
        text = f"{label(seat_no, names)} — {_role_name(ctx, seat.final_role, role_names).lower()}"
        if seat.final_role != seat.role_code:
            text += f" (стартовая карта — {_role_name(ctx, seat.role_code, role_names).lower()})"
        if role is not None and role.is_black:
            black.append(text)
        elif role is not None and role.is_active:
            active.append(text)
        else:
            civil.append(text)
    blocks = [_heading(SECTION_ROSTER)]
    if black:
        blocks.append(_item("Чёрные карты: " + "; ".join(black) + "."))
    if active:
        blocks.append(_item("Активные красные роли: " + "; ".join(active) + "."))
    if civil:
        blocks.append(_item("Мирные жители: " + "; ".join(civil) + "."))
    return blocks


def _night_blocks(
    ctx: scoring.GameContext,
    timeline: scoring.Timeline,
    night_no: int,
    names: Mapping[int, str],
    role_names: Mapping[str, str],
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    alive = timeline.alive_at_start.get(("night", night_no), frozenset())
    actions = [
        action
        for action in ctx.night_actions
        if int(action["night_no"]) == night_no
        and action["state"] == "performed"
        and action["target_seat"] is not None
    ]
    order = {code: (role.night_order or 99) for code, role in ctx.roles.items()}
    actions.sort(key=lambda a: (order.get(str(a["role_code"]), 99), str(a["role_code"])))
    deaths = timeline.night_deaths.get(night_no, frozenset())
    saved = {h["target"] for h in timeline.heals if h["night_no"] == night_no and h["saved"]}
    for action in actions:
        role_code = str(action["role_code"])
        target = int(action["target_seat"])
        actors = [
            s for s in sorted(alive)
            if ctx.seats[s].role_code == role_code
            or (role_code == "mafia" and ctx.roles.get(ctx.seats[s].role_code) is not None
                and ctx.roles[ctx.seats[s].role_code].parity_group == "black")
        ]
        who = _role_name(ctx, role_code, role_names)
        if role_code != "mafia" and len(actors) == 1:
            who = f"{who} {label(actors[0], names)}"
        verb = _NIGHT_VERBS.get(role_code, "выбирает")
        text = f"{who} {verb} {label(target, names)}"
        target_role = ctx.role_of(target)
        if role_code in ("commissar", "boss") and target_role is not None:
            if role_code == "commissar":
                text += " — чёрная карта" if target_role.is_black else " — красная карта"
            else:
                text += f" — {_role_name(ctx, target_role.code, role_names).lower()}"
        elif role_code == "doctor" and target in saved:
            text += " и останавливает выстрел"
        blocks.append(_item(text + "."))
    if deaths:
        gone = [
            f"{label(s, names)} ({_role_name(ctx, ctx.seats[s].role_code, role_names).lower()})"
            for s in sorted(deaths)
        ]
        blocks.append(_item(f"Утром стол покида{'ет' if len(gone) == 1 else 'ют'} {_join(gone)}."))
    elif actions:
        blocks.append(_item("Ночь проходит без потерь."))
    else:
        blocks.append(_item("Записи о ходах этой ночи в листе нет."))
    return blocks


def _day_blocks(
    ctx: scoring.GameContext,
    timeline: scoring.Timeline,
    day_no: int,
    names: Mapping[int, str],
    role_names: Mapping[str, str],
    *,
    is_last: bool,
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    candidates: list[tuple[int, int]] = []
    expelled: list[tuple[int, int]] = []
    # Ведущий утром перечисляет погибших ночью, и модель размечает это как `expelled`;
    # симулятор помечает повтор info-конфликтом, но факт остаётся. Изгнать мёртвого нельзя:
    # партия 29 (04.09.2026) писала «Стол выводит … №11 Сархан», погибшего ночью раньше.
    alive = timeline.alive_at_start.get(("day", day_no))
    for event in ctx.events:
        if event["phase"] != "day" or int(event["phase_no"]) != day_no or event["seat_no"] is None:
            continue
        if alive is not None and int(event["seat_no"]) not in alive:
            continue
        if event["type"] == "defense_start":
            candidates.append((int(event["seat_no"]), int(event["id"])))
        elif event["type"] == "expelled":
            expelled.append((int(event["seat_no"]), int(event["id"])))
    if candidates:
        seats = sorted({s for s, _ in candidates})
        blocks.append(
            _item(
                f"На оправдание выходят {_join([label(s, names) for s in seats])}.",
                [fid for _, fid in candidates],
            )
        )
    fouled = [
        (int(e["seat_no"]), int(e["id"]))
        for e in ctx.events
        if e["phase"] == "day" and int(e["phase_no"]) == day_no and e["type"] == "expelled"
        and e["seat_no"] is not None and e.get("rule_action") == "foul_removed"
    ]
    fouled_seats = {s for s, _ in fouled}
    if fouled:
        blocks.append(
            _item(
                f"За фолы удал{'ён' if len(fouled) == 1 else 'ены'} "
                f"{_join([label(s, names) for s in sorted(fouled_seats)])}.",
                [fid for _, fid in fouled],
            )
        )
    expelled = [(s, fid) for s, fid in expelled if s not in fouled_seats]
    if expelled:
        gone = [
            f"{label(s, names)} ({_role_name(ctx, ctx.seats[s].role_code, role_names).lower()})"
            for s in sorted({s for s, _ in expelled})
            if s in ctx.seats
        ]
        blocks.append(
            _item(f"Стол выводит {_join(gone)}.", [fid for _, fid in expelled])
        )
    elif not fouled and (day_no > 1 or candidates):
        blocks.append(_item("Стол уходит в ночь без потери карты."))
    else:
        blocks.append(_item("Знакомство и первый круг речей."))
    if is_last and ctx.winner_side:
        outcome = {
            "red": "победа красных", "black": "победа чёрных", "neutral": "победа одиночки",
        }.get(ctx.winner_side)
        if outcome:
            blocks.append(_item(f"Партия окончена: {outcome}."))
    return blocks


def timeline_blocks(
    ctx: scoring.GameContext,
    timeline: scoring.Timeline,
    names: Mapping[int, str],
    role_names: Mapping[str, str],
    phase_narratives: Mapping[tuple[str, int], Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Хронология по фазам: заголовок «День N», пункты фактов и абзац модели.

    STORY даёт к каждой фазе абзац о решениях людей — он идёт после программных
    пунктов той же фазы, чтобы факты стояли раньше рассказа о них.

    Подписи фаз («День 6 — Триумф маньяка») отменены 04.09.2026: модель пересказывала
    в них тот же абзац, а эталонные разборы владельца фазы не подписывают вовсе.
    """
    phase_narratives = phase_narratives or {}
    blocks = [_heading(SECTION_TIMELINE)]
    for index, (kind, no) in enumerate(timeline.phases):
        blocks.append(_heading(f"{'День' if kind == 'day' else 'Ночь'} {no}"))
        if kind == "night":
            blocks.extend(_night_blocks(ctx, timeline, no, names, role_names))
        else:
            blocks.extend(
                _day_blocks(
                    ctx, timeline, no, names, role_names,
                    is_last=index == len(timeline.phases) - 1,
                )
            )
        narrative = phase_narratives.get((kind, no))
        if narrative:
            blocks.append(_model_block(narrative, "paragraph"))
    return blocks


def _model_block(item: Mapping[str, Any], block_type: str) -> dict[str, Any]:
    block = {
        "type": block_type,
        "sentence_type": item.get("sentence_type", "editorial"),
        "text": str(item.get("text", "")).strip(),
        "fact_ids": list(item.get("fact_ids") or []),
        "claim_id": item.get("claim_id"),
        "judge_opinion_id": item.get("judge_opinion_id"),
        "source": "model",
    }
    return block


def phase_narratives_from(
    items: Any, timeline: scoring.Timeline
) -> dict[tuple[str, int], Mapping[str, Any]]:
    """Абзацы фаз от модели (STORY v3); фаза вне хронологии отбрасывается."""
    result: dict[tuple[str, int], Mapping[str, Any]] = {}
    known = set(timeline.phases)
    for item in items or []:
        if not isinstance(item, Mapping):
            continue
        key = (str(item.get("phase")), item.get("phase_no"))
        narrative = item.get("narrative")
        if key in known and isinstance(key[1], int) and isinstance(narrative, Mapping):
            if str(narrative.get("text", "")).strip():
                result[key] = narrative
    return result


def assemble(
    *,
    ctx: scoring.GameContext,
    timeline: scoring.Timeline,
    names: Mapping[int, str],
    role_names: Mapping[str, str],
    story: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Полная статья: программные разделы плюс разделы модели в порядке роудмапа §16."""
    blocks: list[dict[str, Any]] = []
    blocks.extend(roster_blocks(ctx, names, role_names))
    blocks.extend(nights_blocks(ctx, timeline, names, role_names))
    blocks.extend(
        timeline_blocks(
            ctx, timeline, names, role_names,
            phase_narratives_from(story.get("phases"), timeline),
        )
    )

    def section(title: str, key: str, block_type: str) -> None:
        items = [_model_block(item, block_type) for item in story.get(key) or []]
        if items:
            blocks.append(_heading(title))
            blocks.extend(items)

    section(SECTION_DECISIVE, "decisive", "item")
    section(SECTION_WHY_LOST.get(str(ctx.winner_side), SECTION_WHY_LOST["none"]), "why_lost", "paragraph")
    section(SECTION_MOVES, "moves", "item")
    section(SECTION_BEST_MOVES, "best_moves", "item")
    section(SECTION_MISTAKES, "mistakes", "item")
    section(SECTION_CONCLUSION, "conclusion", "paragraph")
    return [block for block in blocks if block["text"]]


def story_payload(
    ctx: scoring.GameContext,
    timeline: scoring.Timeline,
    names: Mapping[int, str],
    role_names: Mapping[str, str],
) -> dict[str, Any]:
    """Данные партии для STORY: состав с ролями, ночи по листу, дни по фактам.

    Роли и ночные ходы — из листа (§6A.2), подтверждённого на A1; статья пишется после
    партии, когда роли вскрыты, поэтому модель их видит и может объяснять ход игры.
    """
    roster = [
        {
            "seat_no": seat_no,
            "name": names.get(seat_no, ""),
            "role_code": seat.role_code,
            "role_name": _role_name(ctx, seat.role_code, role_names),
            "final_role_code": seat.final_role,
            "final_role_name": _role_name(ctx, seat.final_role, role_names),
            "is_winner": seat_no in ctx.winners,
            "left_in": (
                {"phase": timeline.gone[seat_no][0], "phase_no": timeline.gone[seat_no][1]}
                if seat_no in timeline.gone
                else None
            ),
        }
        for seat_no, seat in sorted(ctx.seats.items())
    ]
    nights = []
    for kind, no in timeline.phases:
        if kind != "night":
            continue
        nights.append(
            {
                "night_no": no,
                "actions": [
                    {
                        "role_code": str(a["role_code"]),
                        "target_seat": int(a["target_seat"]),
                    }
                    for a in ctx.night_actions
                    if int(a["night_no"]) == no and a["state"] == "performed" and a["target_seat"] is not None
                ],
                "deaths": sorted(timeline.night_deaths.get(no, frozenset())),
                "saved": sorted(
                    {h["target"] for h in timeline.heals if h["night_no"] == no and h["saved"]}
                ),
            }
        )
    days = []
    for kind, no in timeline.phases:
        if kind != "day":
            continue
        days.append(
            {
                "day_no": no,
                "candidates": sorted(
                    {int(e["seat_no"]) for e in ctx.events
                     if e["phase"] == "day" and int(e["phase_no"]) == no
                     and e["type"] == "defense_start" and e["seat_no"] is not None}
                ),
                "expelled": sorted(
                    {int(e["seat_no"]) for e in ctx.events
                     if e["phase"] == "day" and int(e["phase_no"]) == no
                     and e["type"] == "expelled" and e["seat_no"] is not None}
                ),
            }
        )
    return {
        "roster": roster,
        "winner_side": ctx.winner_side,
        "phases": [{"phase": kind, "phase_no": no} for kind, no in timeline.phases],
        "nights": nights,
        "days": days,
    }
