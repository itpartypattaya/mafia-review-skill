#!/usr/bin/env python3
"""Автономный счётчик оценок и кандидатов номинаций (формула клуба `scoring-v1`).

Зачем: установленный скилл лежит вне репозитория сервиса, и `references/code/scoring.py`
там неисполним — он импортирует `app`. Читать 1000 строк кода и считать формулу в уме
дорого и ненадёжно, поэтому арифметику делает этот скрипт: только stdlib, fixed-point
в тысячных, round-half-up один раз в конце — как в сервисе.

Запуск:
    python scripts/score_review.py game.json            # таблица и расчёт по местам
    python scripts/score_review.py game.json --json     # машинный вывод

Формат `game.json` — см. `references/scoring.md` §4a. Коротко:

    {
      "nights": 3,                       // число заполненных колонок ночей на листе
      "days": 4,                         // число дневных фаз (день 1 идёт первым)
      "winner_side": "red",              // red | black | maniac | none
      "seats": [{"seat_no": 1, "name": "Артём", "role": "civilian",
                 "final_role": null}],
      "night_actions": [{"night": 1, "role": "boss", "target": 2}],
      "expelled": [{"day": 2, "seat": 4, "rule_action": null}],
      "defended": [{"day": 2, "seat": 4}],
      "pointing": [{"day": 1, "seat": 1, "target": 7}],
      "model_signals": [{"seat": 2, "skill": "intuition", "sign": 1,
                         "strength": 2, "note": "вскрылся вовремя"}]
    }

Ограничения (честно): скрипт повторяет обычный свод клуба — мафия и маньяк убивают,
доктор лечит, комиссар и босс проверяют, камикадзе уводит маньяка, оборотень под мафией
чернеет. Ночные конфликты, агитатор и переходы жулика он не разбирает: если партия
сложнее, роли после перехода задаются полем `final_role`, а спорную ночь подтверждает
ведущий через questions.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from fractions import Fraction
from pathlib import Path

# --- свод ролей (копия `fixtures/rules/rule_set_v1.json`, проверяется тестом сервиса) ---
ROLES: dict[str, dict] = {
    # code: night_order, parity_group, win_group, blocks_red_victory, elimination_effect
    "civilian": dict(night_order=None, parity="red", win="red", blocks=False, effect="standard"),
    "commissar": dict(night_order=5, parity="red", win="red", blocks=False, effect="standard"),
    "doctor": dict(night_order=6, parity="red", win="red", blocks=False, effect="standard"),
    "witness": dict(night_order=None, parity="red", win="red", blocks=False, effect="standard"),
    "boss": dict(night_order=1, parity="black", win="black", blocks=True, effect="standard"),
    "mafia": dict(night_order=2, parity="black", win="black", blocks=True, effect="standard"),
    "maniac": dict(night_order=3, parity="excluded", win="solo", blocks=True, effect="standard"),
    "agitator": dict(night_order=4, parity="excluded", win="dynamic", blocks=True, effect="standard"),
    "cheat": dict(night_order=None, parity="excluded", win="dynamic", blocks=True,
                  effect="cheat_take_role"),
    "kamikaze": dict(night_order=None, parity="red", win="red", blocks=False,
                     effect="kamikaze_pair"),
    "werewolf": dict(night_order=None, parity="red", win="dynamic", blocks=False,
                     effect="werewolf_turn_black"),
}
RUS_ROLE = {
    "мирный": "civilian", "мирная": "civilian", "комиссар": "commissar", "доктор": "doctor",
    "свидетель": "witness", "наследник": "witness", "босс": "boss", "дон": "boss",
    "мафия": "mafia", "маньяк": "maniac", "нагнетатель": "agitator", "агитатор": "agitator",
    "жулик": "cheat", "камикадзе": "kamikaze", "оборотень": "werewolf",
}
SKILLS = ("analysis", "intuition", "influence", "role", "discipline")
SKILL_RU = {"analysis": "аналитика", "intuition": "интуиция", "influence": "влияние",
            "role": "роль", "discipline": "дисциплина"}

MIN_MILLI, MAX_MILLI = 1000, 10000
DAY_BASE, DAY_PARTICIPATION, DAY_WIN = 5000, 1000, 1500
DISCIPLINE_BASE = 8000
ROLE_BASE_STANDARD, ROLE_STANDARD_PARTICIPATION, ROLE_STANDARD_WIN = 3000, 2000, 1500
ROLE_COMMISSAR_FLOOR, ROLE_COMMISSAR_SPAN, ROLE_CHECK_RED_WEIGHT = 3000, 7000, Fraction(1, 2)
ROLE_DOCTOR_FLOOR, ROLE_DOCTOR_HEAL = 3000, 300
ROLE_DOCTOR_SAVE_CIVILIAN, ROLE_DOCTOR_SAVE_ACTIVE = 3000, 5000
ROLE_BLACK_FLOOR, ROLE_BLACK_PARTICIPATION, ROLE_BLACK_WIN = 3000, 3500, 2000
ROLE_BLACK_KILL_CIVILIAN, ROLE_BLACK_KILL_ACTIVE, ROLE_BLACK_KILL_CAP = 500, 1000, 1500
ROLE_BOSS_FIND_ACTIVE = 1000
ROLE_MANIAC_FLOOR, ROLE_MANIAC_PARTICIPATION, ROLE_MANIAC_WIN = 4000, 2500, 3000
ROLE_MANIAC_KILL_CIVILIAN, ROLE_MANIAC_KILL_ACTIVE = 500, 1000
ROLE_MANIAC_KILL_BLACK, ROLE_MANIAC_KILL_CAP = 1500, 3000
ROLE_KAMIKAZE_ACTIVATED, ROLE_CHEAT_USED = 3000, 2000
SIGNAL_UNIT, DELTA_CAP, MODEL_SIGNALS_PER_SKILL = 500, 3000, 6

KILL_ROLES = ("mafia", "maniac")
HEAL_ROLES = ("doctor",)


class InputError(Exception):
    """Ошибка входного JSON — сообщение показывается ведущему как есть."""


def round_half_up(value: Fraction) -> int:
    """Round-half-up до целых тысячных — та же функция, что в `app/progression/rating.py`."""
    floor = value.numerator // value.denominator
    rest = value - floor
    return floor + 1 if rest >= Fraction(1, 2) else floor


def clamp(value: Fraction, low: int, high: int) -> Fraction:
    return max(Fraction(low), min(Fraction(high), value))


def role_code(raw: str | None) -> str | None:
    if raw is None:
        return None
    key = str(raw).strip().lower().replace("ё", "е")
    if key in ROLES:
        return key
    return RUS_ROLE.get(key) or RUS_ROLE.get(key.replace("е", "ё"))


def is_black(code: str | None) -> bool:
    role = ROLES.get(code or "")
    return bool(role) and (role["blocks"] or role["parity"] == "black")


def is_active(code: str | None) -> bool:
    role = ROLES.get(code or "")
    return bool(role) and (role["night_order"] is not None or role["effect"] != "standard")


# --------------------------------------------------------------------- разбор входа


class Game:
    def __init__(self, raw: dict) -> None:
        self.nights = int(raw.get("nights") or 0)
        self.days = int(raw.get("days") or 0)
        self.winner_side = str(raw.get("winner_side") or "none").strip().lower()
        if self.winner_side not in ("red", "black", "maniac", "solo", "none"):
            raise InputError(f"winner_side: {self.winner_side!r} — ожидалось red/black/maniac/none")
        self.seats: dict[int, dict] = {}
        for item in raw.get("seats") or []:
            seat_no = int(item["seat_no"])
            code = role_code(item.get("role"))
            if code is None:
                raise InputError(f"место {seat_no}: роль {item.get('role')!r} не из свода")
            final = role_code(item.get("final_role")) if item.get("final_role") else None
            self.seats[seat_no] = {
                "seat_no": seat_no,
                "name": str(item.get("name") or f"№{seat_no}"),
                "role": code,
                "final_role": final or code,
            }
        if not self.seats:
            raise InputError("в составе нет ни одного места")
        self.night_actions = []
        for item in raw.get("night_actions") or []:
            code = role_code(item.get("role"))
            if code is None:
                raise InputError(f"ночь {item.get('night')}: роль {item.get('role')!r} не из свода")
            target = item.get("target")
            self.night_actions.append({
                "night": int(item["night"]),
                "role": code,
                "target": int(target) if target not in (None, "", "-") else None,
            })
        if not self.nights:
            self.nights = max((a["night"] for a in self.night_actions), default=0)
        self.expelled = [
            {"day": int(i["day"]), "seat": int(i["seat"]), "rule_action": i.get("rule_action")}
            for i in raw.get("expelled") or []
        ]
        self.defended = [{"day": int(i["day"]), "seat": int(i["seat"])}
                         for i in raw.get("defended") or []]
        self.pointing = [{"day": int(i.get("day") or 0), "seat": int(i["seat"]),
                          "target": int(i["target"])} for i in raw.get("pointing") or []]
        self.model_signals = list(raw.get("model_signals") or [])
        if not self.days:
            self.days = max([self.nights + 1] + [e["day"] for e in self.expelled])
        unknown = {e["seat"] for e in self.expelled} | {p["seat"] for p in self.pointing}
        unknown |= {a["target"] for a in self.night_actions if a["target"]}
        unknown -= set(self.seats)
        if unknown:
            raise InputError(f"мест нет в составе: {sorted(unknown)}")

    def role_of(self, seat_no: int, *, final: bool = False) -> str | None:
        seat = self.seats.get(seat_no)
        if seat is None:
            return None
        return seat["final_role"] if final else seat["role"]

    @property
    def winners(self) -> set[int]:
        """Победители по стороне — те же правила, что у импорта `review.md`."""
        if self.winner_side == "none":
            return set()
        out = set()
        for seat_no, seat in self.seats.items():
            code = seat["final_role"]
            role = ROLES[code]
            if self.winner_side == "red" and not is_black(code):
                out.add(seat_no)
            elif self.winner_side == "black" and role["parity"] == "black":
                out.add(seat_no)
            elif self.winner_side in ("maniac", "solo") and role["win"] == "solo":
                out.add(seat_no)
        return out


# ------------------------------------------------------------------------ хронология


class Timeline:
    """Фазы партии и кто в них жив: день 1, ночь 1, день 2, … — как `build_timeline`."""

    def __init__(self, game: Game) -> None:
        self.game = game
        self.phases: list[tuple[str, int]] = []
        for no in range(1, game.days + 1):
            self.phases.append(("day", no))
            if no <= game.nights:
                self.phases.append(("night", no))
        self.alive_at_start: dict[tuple[str, int], frozenset[int]] = {}
        self.gone: dict[int, tuple[str, int]] = {}
        self.night_deaths: dict[int, frozenset[int]] = {}
        self.shots: list[dict] = []
        self.heals: list[dict] = []
        self.checks: list[dict] = []
        self._run()

    def _run(self) -> None:
        game = self.game
        state = {no: {"alive": True, "role": seat["role"]} for no, seat in game.seats.items()}
        expelled_by_day: dict[int, list[int]] = {}
        for item in game.expelled:
            expelled_by_day.setdefault(item["day"], []).append(item["seat"])
        for phase in self.phases:
            alive = frozenset(no for no, st in state.items() if st["alive"])
            self.alive_at_start[phase] = alive
            kind, no = phase
            if kind == "day":
                for seat_no in expelled_by_day.get(no, []):
                    if state[seat_no]["alive"]:
                        state[seat_no]["alive"] = False
                        self.gone[seat_no] = phase
                continue
            self._night(state, alive, no, phase)

    def _night(self, state: dict, alive: frozenset[int], no: int, phase: tuple[str, int]) -> None:
        by_role: dict[str, list[int]] = {}
        for seat_no, st in state.items():
            if st["alive"]:
                by_role.setdefault(st["role"], []).append(seat_no)
        performed = [a for a in self.game.night_actions if a["night"] == no and a["target"]]
        healed = {a["target"] for a in performed if a["role"] in HEAL_ROLES}
        shot_targets = {a["target"] for a in performed if a["role"] in KILL_ROLES}
        deaths: set[int] = set()
        for action in performed:
            target, code = action["target"], action["role"]
            if code not in KILL_ROLES:
                continue
            target_role = state[target]["role"]
            if code == "mafia" and ROLES[target_role]["effect"] == "werewolf_turn_black":
                state[target]["role"] = "mafia"  # оборотень под мафией чернеет, а не гибнет
                continue
            if code == "maniac" and ROLES[target_role]["effect"] == "kamikaze_pair":
                for seat_no in (target, *by_role.get("maniac", [])):
                    if state[seat_no]["alive"]:
                        state[seat_no]["alive"] = False
                        deaths.add(seat_no)
                continue
            if target in healed or not state[target]["alive"]:
                continue
            state[target]["alive"] = False
            deaths.add(target)
        for seat_no in deaths:
            self.gone[seat_no] = phase
        self.night_deaths[no] = frozenset(deaths)
        for action in performed:
            target, code = action["target"], action["role"]
            record = {"night": no, "role": code, "target": target,
                      "target_role": self.game.role_of(target)}
            if code in HEAL_ROLES:
                saved = target in shot_targets and target not in deaths and target in alive
                self.heals.append({**record, "actors": by_role.get(code, []), "saved": saved})
            elif code in KILL_ROLES:
                actors = ([s for s, st in state.items()
                           if is_black(st["role"]) and ROLES[st["role"]]["parity"] == "black"
                           and s in alive]
                          if code == "mafia" else by_role.get(code, []))
                self.shots.append({**record, "actors": actors, "killed": target in deaths})
            elif code in ("commissar", "boss"):
                target_role = self.game.role_of(target)
                hit = (is_black(target_role) if code == "commissar"
                       else (not is_black(target_role) and is_active(target_role)))
                self.checks.append({**record, "actors": by_role.get(code, []), "hit": bool(hit)})

    def participation(self, seat_no: int) -> Fraction:
        if len(self.phases) <= 1:
            return Fraction(1)
        alive = sum(seat_no in self.alive_at_start[p] for p in self.phases)
        return Fraction(max(alive - 1, 0), len(self.phases) - 1)


# ------------------------------------------------------------------------------ базы


def day_base(game: Game, tl: Timeline, seat_no: int) -> Fraction:
    part = tl.participation(seat_no)
    base = Fraction(DAY_BASE) + DAY_PARTICIPATION * part
    if seat_no in game.winners:
        base += DAY_WIN * part
    return base


def _standard_role_base(game: Game, tl: Timeline, seat_no: int) -> Fraction:
    part = tl.participation(seat_no)
    base = Fraction(ROLE_BASE_STANDARD) + ROLE_STANDARD_PARTICIPATION * part
    if seat_no in game.winners:
        base += ROLE_STANDARD_WIN * part
    return base


def _black_role_base(game: Game, tl: Timeline, seat_no: int) -> Fraction:
    base = Fraction(ROLE_BLACK_FLOOR) + ROLE_BLACK_PARTICIPATION * tl.participation(seat_no)
    if seat_no in game.winners:
        base += ROLE_BLACK_WIN
    kills = 0
    for shot in tl.shots:
        if shot["role"] != "mafia" or seat_no not in shot["actors"] or not shot["killed"]:
            continue
        kills += (ROLE_BLACK_KILL_ACTIVE if is_active(shot["target_role"])
                  else ROLE_BLACK_KILL_CIVILIAN)
    base += min(kills, ROLE_BLACK_KILL_CAP)
    for check in tl.checks:
        if check["role"] == "boss" and seat_no in check["actors"] and check["hit"]:
            base += ROLE_BOSS_FIND_ACTIVE
    return base


def _maniac_role_base(game: Game, tl: Timeline, seat_no: int) -> Fraction:
    base = Fraction(ROLE_MANIAC_FLOOR) + ROLE_MANIAC_PARTICIPATION * tl.participation(seat_no)
    if seat_no in game.winners:
        base += ROLE_MANIAC_WIN
    kills = 0
    for shot in tl.shots:
        if shot["role"] != "maniac" or seat_no not in shot["actors"] or not shot["killed"]:
            continue
        if is_black(shot["target_role"]):
            kills += ROLE_MANIAC_KILL_BLACK
        elif is_active(shot["target_role"]):
            kills += ROLE_MANIAC_KILL_ACTIVE
        else:
            kills += ROLE_MANIAC_KILL_CIVILIAN
    return base + min(kills, ROLE_MANIAC_KILL_CAP)


def _commissar_role_base(game: Game, tl: Timeline, seat_no: int) -> Fraction:
    """Точность относительно ночей ПАРТИИ, а не прожитых ночей."""
    own = [c for c in tl.checks if c["role"] == "commissar" and seat_no in c["actors"]]
    hits = sum(1 for c in own if c["hit"])
    misses = len(own) - hits
    effectiveness = Fraction(hits) + misses * ROLE_CHECK_RED_WEIGHT
    effectiveness = effectiveness / max(game.nights, 1)
    return Fraction(ROLE_COMMISSAR_FLOOR) + ROLE_COMMISSAR_SPAN * min(effectiveness, Fraction(1))


def _doctor_role_base(tl: Timeline, seat_no: int) -> Fraction:
    own = [h for h in tl.heals if seat_no in h["actors"]]
    base = Fraction(ROLE_DOCTOR_FLOOR) + ROLE_DOCTOR_HEAL * min(len(own), 3)
    for heal in own:
        if heal["saved"]:
            base += (ROLE_DOCTOR_SAVE_ACTIVE if is_active(heal["target_role"])
                     else ROLE_DOCTOR_SAVE_CIVILIAN)
    return base


def role_base(game: Game, tl: Timeline, seat_no: int) -> Fraction:
    code = game.role_of(seat_no)
    final = game.role_of(seat_no, final=True)
    role = ROLES[code]
    if code == "commissar":
        return _commissar_role_base(game, tl, seat_no)
    if code == "doctor":
        return _doctor_role_base(tl, seat_no)
    if code == "maniac":
        return _maniac_role_base(game, tl, seat_no)
    if role["parity"] == "black" and role["win"] == "black":
        return _black_role_base(game, tl, seat_no)
    if role["effect"] == "werewolf_turn_black":
        if final and ROLES[final]["parity"] == "black":
            return _black_role_base(game, tl, seat_no)
        return _standard_role_base(game, tl, seat_no)
    if role["effect"] == "cheat_take_role":
        base = _standard_role_base(game, tl, seat_no)
        return base + ROLE_CHEAT_USED if final and final != code else base
    if role["effect"] == "kamikaze_pair":
        base = _standard_role_base(game, tl, seat_no)
        gone = tl.gone.get(seat_no)
        if gone and gone[0] == "night":
            maniacs = {s for s, seat in game.seats.items() if seat["role"] == "maniac"}
            if maniacs & tl.night_deaths.get(gone[1], frozenset()):
                base += ROLE_KAMIKAZE_ACTIVATED
        return base
    return _standard_role_base(game, tl, seat_no)


def bases(game: Game, tl: Timeline, seat_no: int) -> dict[str, Fraction]:
    day = day_base(game, tl, seat_no)
    return {"analysis": day, "intuition": day, "influence": day,
            "role": role_base(game, tl, seat_no), "discipline": Fraction(DISCIPLINE_BASE)}


# --------------------------------------------------------------------------- сигналы


def deterministic_signals(game: Game, tl: Timeline, seat_no: int) -> list[dict]:
    out: list[dict] = []

    def add(skill: str, sign: int, strength: int, code: str, note: str) -> None:
        out.append({"skill": skill, "sign": sign, "strength": strength, "code": code,
                    "source": "sheet", "note": note})

    own = game.role_of(seat_no)
    for check in tl.checks:
        if seat_no in check["actors"] and check["hit"]:
            add("intuition", +1, 3, "check_hit", f"проверка в ночь {check['night']} попала")
            add("analysis", +1, 2, "check_hit", f"цель проверки в ночь {check['night']} верна")
    for heal in tl.heals:
        if seat_no not in heal["actors"]:
            continue
        if heal["saved"]:
            add("intuition", +1, 3, "heal_saved",
                f"лечение в ночь {heal['night']} остановило выстрел")
            if is_active(heal["target_role"]):
                add("intuition", +1, 3, "saved_active_role",
                    f"спасённая в ночь {heal['night']} карта осталась в игре")
                add("analysis", +1, 2, "saved_active_role",
                    f"цель лечения в ночь {heal['night']} выбрана верно")
        else:
            add("intuition", -1, 1, "heal_missed",
                f"лечение в ночь {heal['night']} не совпало с выстрелом")
    for shot in tl.shots:
        if seat_no not in shot["actors"] or not shot["killed"]:
            continue
        if shot["role"] == "maniac" and is_black(shot["target_role"]):
            strength, note = 3, "снял чёрную карту"
        elif is_active(shot["target_role"]):
            strength, note = 2, "снял активную карту"
        else:
            strength, note = 1, "достиг цели"
        add("intuition", +1, strength, "shot_killed", f"выстрел в ночь {shot['night']} {note}")

    expelled_by_day: dict[int, set[int]] = {}
    for seat, (kind, no) in tl.gone.items():
        if kind == "day":
            expelled_by_day.setdefault(no, set()).add(seat)
    expelled_all = set().union(*expelled_by_day.values()) if expelled_by_day else set()

    if own is not None and ROLES[own]["win"] != "solo":
        for point in game.pointing:
            if point["seat"] != seat_no or point["target"] == seat_no:
                continue
            target_role = game.role_of(point["target"])
            if target_role is None:
                continue
            if is_black(own):
                if not is_black(target_role) and point["target"] in expelled_all:
                    out.append({"skill": "influence", "sign": 1, "strength": 2,
                                "code": "pointed_at_expelled", "source": "claims",
                                "note": "названный игрок покинул стол"})
                continue
            hit_day = point["target"] in expelled_all
            if is_black(target_role):
                out.append({"skill": "analysis", "sign": 1, "strength": 2 if hit_day else 1,
                            "code": "pointed_at_black", "source": "claims",
                            "note": "указал на чёрную карту"})
                if hit_day:
                    out.append({"skill": "influence", "sign": 1, "strength": 2,
                                "code": "pointed_at_expelled", "source": "claims",
                                "note": "названная чёрная карта покинула стол"})
            else:
                out.append({"skill": "analysis", "sign": -1, "strength": 2 if hit_day else 1,
                            "code": "pointed_at_red", "source": "claims",
                            "note": "указал на красного"})

    for item in game.defended:
        if item["seat"] == seat_no and seat_no not in expelled_by_day.get(item["day"], set()):
            add("influence", +1, 1, "defended_and_stayed", "остался за столом после оправдания")
    for item in game.expelled:
        if item["seat"] != seat_no:
            continue
        if item.get("rule_action") == "foul_removed":
            add("discipline", -1, 3, "foul_removed", "удалён из партии за фолы")
            continue
        if own is None or is_black(own):
            continue
        add("influence", -1, 2, "expelled_as_red", "стол вывел красную карту")
        add("intuition", -1, 1, "expelled_as_red", "не почувствовал угрозу голосования")
        add("analysis", -1, 1, "expelled_as_red", "не смог объяснить столу свою позицию")
        add("role", -1, 2, "expelled_as_red", "красная карта ушла по решению города")
    if own is not None and is_black(own):
        days_with_expulsion = {item["day"] for item in game.expelled}
        for phase in tl.phases:
            kind, no = phase
            if (kind != "day" or no not in days_with_expulsion
                    or seat_no not in tl.alive_at_start[phase]
                    or seat_no in expelled_by_day.get(no, set())):
                continue
            add("influence", +1, 1, "survived_vote_as_black", f"день {no}: стол вывел не его")
            add("analysis", +1, 1, "survived_vote_as_black", f"день {no}: остался вне подозрений")
    return out


def model_signals_for(game: Game, seat_no: int) -> list[dict]:
    out = []
    per_skill: dict[str, int] = {}
    for item in game.model_signals:
        if int(item["seat"]) != seat_no:
            continue
        skill = str(item["skill"])
        if skill not in SKILLS:
            raise InputError(f"место {seat_no}: навык {skill!r} не из пяти")
        if skill == "role":
            raise InputError(f"место {seat_no}: навык «роль» считается только по листу")
        sign, strength = int(item.get("sign", 1)), int(item.get("strength", 1))
        if sign not in (-1, 1) or strength not in (1, 2, 3):
            raise InputError(f"место {seat_no}: sign ±1 и strength 1–3, пришло {sign}/{strength}")
        per_skill[skill] = per_skill.get(skill, 0) + 1
        if per_skill[skill] > MODEL_SIGNALS_PER_SKILL:
            raise InputError(
                f"место {seat_no}, навык {SKILL_RU[skill]}: больше {MODEL_SIGNALS_PER_SKILL}"
                " сигналов — это накрутка, а не наблюдение")
        if not str(item.get("note") or "").strip():
            raise InputError(f"место {seat_no}: сигнал без основания не существует")
        out.append({"skill": skill, "sign": sign, "strength": strength, "code": "model",
                    "source": "model", "note": str(item["note"])})
    return out


def delta_milli(signals: list[dict]) -> Fraction:
    if not signals:
        return Fraction(0)
    raw = sum(s["sign"] * s["strength"] for s in signals)
    n = len(signals)
    return clamp(Fraction(raw * SIGNAL_UNIT), -DELTA_CAP, DELTA_CAP) * Fraction(n, n + 2)


def score_seat(game: Game, tl: Timeline, seat_no: int) -> dict:
    signals = deterministic_signals(game, tl, seat_no) + model_signals_for(game, seat_no)
    result = {}
    for skill, base in bases(game, tl, seat_no).items():
        own = [s for s in signals if s["skill"] == skill]
        total = clamp(base + delta_milli(own), MIN_MILLI, MAX_MILLI)
        result[skill] = {
            "base_milli": round_half_up(clamp(base, MIN_MILLI, MAX_MILLI)),
            "score_milli": round_half_up(total),
            "signals": own,
        }
    return result


# ------------------------------------------------------------------------ номинации


def nomination_candidates(game: Game, tl: Timeline) -> list[dict]:
    out: list[dict] = []

    def add(code: str, seat_no: int, headline: str, weight: int) -> None:
        out.append({"code": code, "seat_no": seat_no, "name": game.seats[seat_no]["name"],
                    "headline": headline, "weight": weight})

    checks_by_seat: dict[int, int] = {}
    for check in tl.checks:
        if check["role"] == "commissar" and check["hit"]:
            for actor in check["actors"]:
                checks_by_seat[actor] = checks_by_seat.get(actor, 0) + 1
    for seat_no, hits in checks_by_seat.items():
        add("best_checks", seat_no, f"{hits} чёрных карт по проверкам", 30 + 10 * hits)
    for heal in tl.heals:
        if not heal["saved"]:
            continue
        for actor in heal["actors"]:
            active = is_active(heal["target_role"])
            add("key_heal", actor, f"лечение в ночь {heal['night']} остановило выстрел",
                45 if active else 30)
    kills_by_seat: dict[int, int] = {}
    black_kill: set[int] = set()
    for shot in tl.shots:
        if shot["role"] != "maniac" or not shot["killed"]:
            continue
        for actor in shot["actors"]:
            kills_by_seat[actor] = kills_by_seat.get(actor, 0) + 1
            if is_black(shot["target_role"]):
                black_kill.add(actor)
    for seat_no, kills in kills_by_seat.items():
        won = seat_no in game.winners
        if kills >= 2 or seat_no in black_kill or won:
            add("maniac_run", seat_no, f"{kills} результативных выстрелов",
                35 + 5 * kills + (15 if won else 0))
    half = Fraction(1, 2)
    for seat_no, seat in game.seats.items():
        if not is_black(seat["role"]) or ROLES[seat["role"]]["win"] == "solo":
            continue
        if tl.participation(seat_no) >= half:
            weight = 25 + (15 if seat_no in game.winners else 0) + (10 if tl.gone.get(seat_no) is None else 0)
            add("last_black", seat_no, "чёрная карта с наибольшим участием", weight)
    balance: dict[int, int] = {}
    for point in game.pointing:
        if is_black(game.role_of(point["seat"])):
            continue
        balance[point["seat"]] = balance.get(point["seat"], 0) + (
            1 if is_black(game.role_of(point["target"])) else -1)
    for seat_no, value in balance.items():
        if value >= 2:
            add("table_reader", seat_no, f"чаще других указывал на чёрных (баланс {value})",
                20 + 5 * value)
    for seat_no, seat in game.seats.items():
        if seat["role"] != "kamikaze":
            continue
        gone = tl.gone.get(seat_no)
        if gone and gone[0] == "night":
            maniacs = {s for s, other in game.seats.items() if other["role"] == "maniac"}
            if maniacs & tl.night_deaths.get(gone[1], frozenset()):
                add("kamikaze_hit", seat_no, "забрал маньяка", 40)
    best: dict[int, dict] = {}
    for item in sorted(out, key=lambda i: -i["weight"]):
        best.setdefault(item["seat_no"], item)
    ranked = sorted(best.values(), key=lambda i: (-i["weight"], i["seat_no"]))
    if len(ranked) < 3:
        for seat_no in sorted(game.winners):
            if tl.gone.get(seat_no) is None and seat_no not in best:
                ranked.append({"code": "survivor_win", "seat_no": seat_no,
                               "name": game.seats[seat_no]["name"],
                               "headline": "дожил до победы", "weight": 10})
                break
    return ranked[:3]


# ----------------------------------------------------------------------------- вывод


def milli(value: int) -> str:
    return f"{value / 1000:.2f}".rstrip("0").rstrip(".")


def render(game: Game, tl: Timeline, scores: dict[int, dict], candidates: list[dict]) -> str:
    lines = [f"Фазы: {', '.join(f'{k} {n}' for k, n in tl.phases)}",
             f"Победители: {sorted(game.winners) or 'никто'}", ""]
    for seat_no in sorted(game.seats):
        seat = game.seats[seat_no]
        part = tl.participation(seat_no)
        gone = tl.gone.get(seat_no)
        where = f"выбыл: {gone[0]} {gone[1]}" if gone else "дожил"
        lines.append(f"№{seat_no} {seat['name']}, {seat['role']}, {where}, "
                     f"participation {part.numerator}/{part.denominator}")
        for skill in SKILLS:
            item = scores[seat_no][skill]
            notes = "; ".join(f"{s['code']} {s['sign'] * s['strength']:+d}" for s in item["signals"])
            lines.append(f"  {SKILL_RU[skill]:<10} base {milli(item['base_milli']):>5}"
                         f" → {milli(item['score_milli']):>5}   {notes}")
        lines.append("")
    lines.append("Строки для «## Оценки игроков» (проверь комментарии сам):")
    lines.append("| № | Игрок | Аналитика | Интуиция | Влияние | Роль | Дисциплина | Комментарий |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for seat_no in sorted(game.seats):
        cells = " | ".join(milli(scores[seat_no][skill]["score_milli"]) for skill in SKILLS)
        lines.append(f"| {seat_no} | {game.seats[seat_no]['name']} | {cells} |  |")
    lines.append("")
    lines.append("Кандидаты номинаций (выбери до трёх, название придумай сам):")
    for item in candidates:
        lines.append(f"  {item['weight']:>3}  {item['code']:<14} №{item['seat_no']} "
                     f"{item['name']} — {item['headline']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Оценки и кандидаты номинаций по формуле клуба")
    parser.add_argument("game", type=Path, help="JSON с составом, ночами и исходом")
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    args = parser.parse_args(argv)
    try:
        raw = json.loads(args.game.read_text(encoding="utf-8"))
        game = Game(raw)
        tl = Timeline(game)
        scores = {seat_no: score_seat(game, tl, seat_no) for seat_no in sorted(game.seats)}
        candidates = nomination_candidates(game, tl)
    except (InputError, KeyError, ValueError) as error:
        print(f"Ошибка входных данных: {error}", file=sys.stderr)
        return 2
    if args.json:
        payload = {
            "formula_version": "scoring-v1",
            "phases": [f"{k} {n}" for k, n in tl.phases],
            "winners": sorted(game.winners),
            "scores": {str(seat): {skill: data[skill]["score_milli"] for skill in SKILLS}
                       for seat, data in scores.items()},
            "detail": {str(seat): data for seat, data in scores.items()},
            "nomination_candidates": candidates,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render(game, tl, scores, candidates))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
