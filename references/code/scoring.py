"""Оценка навыков по формуле: цифру считает код, модель поставляет наблюдения.

Первая живая публикация (партия 28, 03.09.2026) показала, что оценка «цифрой от модели»
не работает как продукт: три игрока из тринадцати получили ~5.3 и потеряли рейтинг, десять
остались без оценки вовсе, и один факт мог сдвинуть навык на всю шкалу. Здесь оценка
раскладывается на две части, и обе объяснимы ведущему по одному экрану:

    score = base + delta

**База** — детерминированная функция того, что известно без модели: сколько партии игрок
отыграл (доля фаз, в начале которых он был жив), победила ли его сторона, и как он
реализовал механику роли по листу (проверки комиссара и дона, спасения доктора, выстрелы
мафии и маньяка, активация камикадзе, превращение оборотня, ход жулика). Игрок без единого
наблюдения получает базу — осторожную середину, а не пустоту и не приговор.

**Сдвиг** — сумма сигналов. Сигнал — это наблюдение с основанием: `sign ∈ {−1, +1}`,
`strength ∈ {1, 2, 3}` (эпизод / заметный поступок / поступок, решивший фазу). Часть
сигналов детерминирована (попадания ночных ролей по листу, точность публичных указаний
из подтверждённых заявлений против ролей целей), часть даёт JUDGE — но только с basis
на факт или заявление того же игрока, иначе сигнал отбрасывается.

    raw    = Σ sign_i × strength_i
    n      = число сигналов
    delta  = clamp(raw × 0.5, −3.0, +3.0) × n / (n + 2)
    score  = clamp(base + delta, 1.0, 10.0)

Множитель `n / (n + 2)` — сжатие: один сигнал двигает не больше чем на 0.5, три — на 60 %
возможного, десять согласных — почти на весь коридор ±3. Так один хороший или плохой факт
не завышает и не занижает оценку, а шкала роудмапа §11 (9–10 «решил партию») достигается
только накоплением. Всё считается в тысячных на `Fraction`, округление half-up — один раз,
на последнем шаге навыка (§7.2 мастер-плана).

Роли и ночные действия берутся только из листа (§6A.2); модель ролей не видит и сигналы
для навыка «исполнение роли» не даёт — он целиком детерминирован.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from app import db
from app.analysis import rule_simulator
from app.analysis.events import PLAYED_NIGHT_STATES
from app.progression.rating import round_half_up

FORMULA_VERSION = "scoring-v1"
SKILLS = ("analysis", "intuition", "influence", "role", "discipline")
DAY_SKILLS = ("analysis", "intuition", "influence")
# Модель размечает только публичное поведение; механику роли оценивает лист.
MODEL_SKILLS = ("analysis", "intuition", "influence", "discipline")

MIN_MILLI = 1000
MAX_MILLI = 10000

# ---- константы формулы (тысячные). Одна таблица, чтобы менять было где ----
DAY_BASE = 5000  # середина шкалы для дневных навыков
DAY_PARTICIPATION = 1000  # дожил до конца → +1.0
DAY_WIN = 1500  # сторона победила → +1.5 × доля отыгранной партии
DISCIPLINE_BASE = 8000  # как в эталонных разборах: 8 минус нарушения

ROLE_BASE_STANDARD = 3000  # мирный и роли без ночной механики
ROLE_STANDARD_PARTICIPATION = 2000
ROLE_STANDARD_WIN = 1500  # × доля отыгранной партии
ROLE_COMMISSAR_FLOOR = 3000
ROLE_COMMISSAR_SPAN = 7000  # 3.0 + 7.0 × точность проверок
ROLE_CHECK_RED_WEIGHT = Fraction(1, 2)  # проверка красного — половина попадания
ROLE_DOCTOR_FLOOR = 3000
ROLE_DOCTOR_HEAL = 300  # попытка лечения, не больше трёх
ROLE_DOCTOR_SAVE_CIVILIAN = 3000
ROLE_DOCTOR_SAVE_ACTIVE = 5000  # спасённая активная роль ценнее нескольких лечений
ROLE_BLACK_FLOOR = 3000
ROLE_BLACK_PARTICIPATION = 3500
ROLE_BLACK_WIN = 2000
ROLE_BLACK_KILL_CIVILIAN = 500
ROLE_BLACK_KILL_ACTIVE = 1000
ROLE_BLACK_KILL_CAP = 1500
ROLE_BOSS_FIND_ACTIVE = 1000
ROLE_MANIAC_FLOOR = 4000
ROLE_MANIAC_PARTICIPATION = 2500
ROLE_MANIAC_WIN = 3000
ROLE_MANIAC_KILL_CIVILIAN = 500
ROLE_MANIAC_KILL_ACTIVE = 1000
ROLE_MANIAC_KILL_BLACK = 1500
ROLE_MANIAC_KILL_CAP = 3000
ROLE_KAMIKAZE_ACTIVATED = 3000
ROLE_CHEAT_USED = 2000

SIGNAL_UNIT = 500  # одна единица raw = 0.5
DELTA_CAP = 3000
MODEL_SIGNALS_PER_SKILL = 6  # больше — модель накручивает, а не наблюдает

CLAIM_POINTING_TYPES = ("accusation", "nomination", "vote_intent")


@dataclass(frozen=True)
class Signal:
    skill: str
    sign: int
    strength: int
    source: str  # sheet | facts | claims | model
    code: str
    basis: tuple[tuple[str, int], ...]  # (kind, id): fact | claim | sheet | outcome
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "skill": self.skill,
            "sign": self.sign,
            "strength": self.strength,
            "source": self.source,
            "code": self.code,
            "basis": [{"kind": kind, "id": ref} for kind, ref in self.basis],
            "note": self.note,
        }


@dataclass(frozen=True)
class Role:
    code: str
    name: str
    side: str
    parity_group: str
    win_group: str
    blocks_red_victory: bool
    elimination_effect: str
    night_order: int | None

    @property
    def is_black(self) -> bool:
        return self.blocks_red_victory or self.parity_group == "black"

    @property
    def is_active(self) -> bool:
        """Роль с ночной механикой или особым эффектом — ценная цель и ценная карта."""
        return self.night_order is not None or self.elimination_effect != "standard"


@dataclass(frozen=True)
class Seat:
    seat_no: int
    profile_id: int
    role_code: str
    final_role: str


@dataclass
class GameContext:
    seats: dict[int, Seat]
    roles: dict[str, Role]
    night_actions: list[dict[str, Any]]
    events: list[dict[str, Any]]
    claims: list[dict[str, Any]]
    winner_side: str | None
    winners: frozenset[int]
    outcome_id: int | None

    def role_of(self, seat_no: int, *, final: bool = False) -> Role | None:
        seat = self.seats.get(seat_no)
        if seat is None:
            return None
        return self.roles.get(seat.final_role if final else seat.role_code)


@dataclass
class Timeline:
    phases: list[tuple[str, int]]
    alive_at_start: dict[tuple[str, int], frozenset[int]]
    gone: dict[int, tuple[str, int]]  # seat → фаза, в которой выбыл
    night_deaths: dict[int, frozenset[int]]
    shots: list[dict[str, Any]]  # night_no, role_code, actor_seats, target, killed
    heals: list[dict[str, Any]]  # night_no, actor_seats, target, saved
    checks: list[dict[str, Any]]  # night_no, role_code, actor_seats, target, hit

    def participation(self, seat_no: int) -> Fraction:
        """Доля партии, которую игрок отыграл: 0 — выбыл в первую же фазу, 1 — дожил."""
        if len(self.phases) <= 1:
            return Fraction(1)
        alive = sum(seat_no in self.alive_at_start[phase] for phase in self.phases)
        return Fraction(max(alive - 1, 0), len(self.phases) - 1)

    def survived(self, seat_no: int) -> bool:
        return seat_no not in self.gone


@dataclass
class SkillScore:
    skill: str
    base_milli: int
    score_milli: int
    signals: list[Signal] = field(default_factory=list)

    @property
    def basis(self) -> list[dict[str, Any]]:
        seen: list[tuple[str, int]] = []
        for signal in self.signals:
            for ref in signal.basis:
                if ref not in seen:
                    seen.append(ref)
        return [{"kind": kind, "id": ref} for kind, ref in seen]


# --------------------------------------------------------------------------- контекст


def load_context(
    *, game_id: int, setup_revision_id: int, rule_set_id: int, fact_set_id: int,
    claim_set_id: int | None,
) -> GameContext:
    """Всё, что нужно формуле, из подтверждённых источников одной партии."""
    roles = {
        str(row["code"]): Role(
            code=str(row["code"]),
            name=str(row["name"]),
            side=str(row["side"]),
            parity_group=str(row["parity_group"]),
            win_group=str(row["win_group"]),
            blocks_red_victory=bool(row["blocks_red_victory"]),
            elimination_effect=str(row["elimination_effect"]),
            night_order=row["night_order"],
        )
        for row in db.query(
            "SELECT code, name, side, parity_group, win_group, blocks_red_victory, "
            "elimination_effect, night_order FROM role_revision WHERE rule_set_id=?",
            (rule_set_id,),
        )
    }
    outcome = db.one(
        "SELECT id, winner_side FROM game_outcome_revision WHERE game_id=? AND fact_set_id=? "
        "ORDER BY id DESC LIMIT 1",
        (game_id, fact_set_id),
    ) or db.one(
        "SELECT id, winner_side FROM game_outcome_revision WHERE game_id=? ORDER BY id DESC LIMIT 1",
        (game_id,),
    )
    outcome_players = (
        {
            int(row["seat_no"]): row
            for row in db.query(
                "SELECT seat_no, final_role, is_winner FROM game_outcome_player "
                "WHERE outcome_revision_id=?",
                (outcome["id"],),
            )
        }
        if outcome is not None
        else {}
    )
    seats = {}
    for row in db.query(
        "SELECT seat_no, profile_id, role_code FROM game_setup_seat "
        "WHERE setup_revision_id=? AND game_id=? ORDER BY seat_no",
        (setup_revision_id, game_id),
    ):
        seat_no = int(row["seat_no"])
        played = outcome_players.get(seat_no)
        seats[seat_no] = Seat(
            seat_no=seat_no,
            profile_id=int(row["profile_id"]),
            role_code=str(row["role_code"]),
            final_role=str(played["final_role"]) if played is not None else str(row["role_code"]),
        )
    winners = frozenset(
        seat_no for seat_no, row in outcome_players.items() if int(row["is_winner"]) == 1
    )
    night_actions = [
        dict(row)
        for row in db.query(
            "SELECT id, night_no, role_code, state, target_seat FROM night_action "
            "WHERE setup_revision_id=? ORDER BY night_no, role_code",
            (setup_revision_id,),
        )
    ]
    events = [
        dict(row)
        for row in db.query(
            "SELECT id, phase, phase_no, type, seat_no, target_seat, rule_action FROM game_event "
            "WHERE fact_set_id=? ORDER BY sort, id",
            (fact_set_id,),
        )
    ]
    claims: list[dict[str, Any]] = []
    if claim_set_id is not None:
        for row in db.query(
            "SELECT c.id, c.claim_type, c.speaker_seat, c.personal_eval_ok, "
            "(SELECT json_group_array(t.seat_no) FROM claim_target t WHERE t.claim_id=c.id) targets "
            "FROM claim c WHERE c.claim_set_id=? ORDER BY c.id",
            (claim_set_id,),
        ):
            claims.append(
                {
                    "id": int(row["id"]),
                    "claim_type": str(row["claim_type"]),
                    "speaker_seat": row["speaker_seat"],
                    "personal_eval_ok": bool(row["personal_eval_ok"]),
                    "targets": [int(x) for x in json.loads(row["targets"] or "[]")],
                }
            )
    return GameContext(
        seats=seats,
        roles=roles,
        night_actions=night_actions,
        events=events,
        claims=claims,
        winner_side=str(outcome["winner_side"]) if outcome is not None else None,
        winners=winners,
        outcome_id=int(outcome["id"]) if outcome is not None else None,
    )


def context_for_run(analysis_run_id: int, *, fact_set_id: int, claim_set_id: int | None) -> GameContext:
    run = db.one(
        "SELECT game_id, rule_set_id, input_setup_revision_id FROM analysis_run WHERE id=?",
        (analysis_run_id,),
    )
    if run is None:
        raise ValueError("analysis run не найден")
    return load_context(
        game_id=int(run["game_id"]),
        setup_revision_id=int(run["input_setup_revision_id"]),
        rule_set_id=int(run["rule_set_id"]),
        fact_set_id=fact_set_id,
        claim_set_id=claim_set_id,
    )


# --------------------------------------------------------------------------- хронология


def _role_state(ctx: GameContext) -> dict[int, dict[str, Any]]:
    state: dict[int, dict[str, Any]] = {}
    for seat_no, seat in ctx.seats.items():
        role = ctx.roles.get(seat.role_code)
        if role is None:
            continue
        state[seat_no] = {
            "alive": True,
            "role_code": role.code,
            "side": role.side,
            "parity_group": role.parity_group,
            "win_group": role.win_group,
            "blocks_red_victory": int(role.blocks_red_victory),
            "elimination_effect": role.elimination_effect,
        }
    return state


def build_timeline(ctx: GameContext) -> Timeline:
    """Фазы партии и кто в них жив — по листу (ночи) и подтверждённым фактам (дни).

    Порядок фаз: день 1 (знакомство), ночь 1, день 2, …, ночь k, день k+1. Ночные смерти
    выводятся тем же кодом, что в симуляторе (`_apply_night_actions`): цель мафии и маньяка
    умирает, если доктор не лечил именно её; камикадзе уводит маньяка; оборотень чернеет.
    Дневные выбытия — события `expelled` с местом.
    """
    played_nights = sorted(
        {
            int(action["night_no"])
            for action in ctx.night_actions
            if action["state"] in PLAYED_NIGHT_STATES
        }
    )
    last_night = played_nights[-1] if played_nights else 0
    last_day = max(
        [last_night + 1]
        + [int(event["phase_no"]) for event in ctx.events if event["phase"] == "day"]
    )
    phases: list[tuple[str, int]] = []
    for no in range(1, last_day + 1):
        phases.append(("day", no))
        if no <= last_night:
            phases.append(("night", no))

    role_state = _role_state(ctx)
    alive_at_start: dict[tuple[str, int], frozenset[int]] = {}
    gone: dict[int, tuple[str, int]] = {}
    night_deaths: dict[int, frozenset[int]] = {}
    shots: list[dict[str, Any]] = []
    heals: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    expelled_by_day: dict[int, list[int]] = {}
    for event in ctx.events:
        if event["type"] == "expelled" and event["seat_no"] is not None and event["phase"] == "day":
            expelled_by_day.setdefault(int(event["phase_no"]), []).append(int(event["seat_no"]))

    for phase in phases:
        alive = frozenset(seat for seat, state in role_state.items() if state["alive"])
        alive_at_start[phase] = alive
        kind, no = phase
        if kind == "day":
            for seat_no in expelled_by_day.get(no, []):
                state = role_state.get(seat_no)
                if state is not None and state["alive"]:
                    state["alive"] = False
                    gone[seat_no] = phase
            continue
        actors_by_role: dict[str, list[int]] = {}
        for seat_no, state in role_state.items():
            if state["alive"]:
                actors_by_role.setdefault(state["role_code"], []).append(seat_no)
        performed = [
            action
            for action in ctx.night_actions
            if int(action["night_no"]) == no
            and action["state"] == "performed"
            and action["target_seat"] is not None
        ]
        healed = {
            int(action["target_seat"])
            for action in performed
            if action["role_code"] in rule_simulator.NIGHT_HEAL_ROLES
        }
        shot_targets = {
            int(action["target_seat"])
            for action in performed
            if action["role_code"] in rule_simulator.NIGHT_KILL_ROLES
        }
        deaths = rule_simulator._apply_night_actions(
            role_state, performed, span_id=0, phase_no=no, conflicts=[], transitions=[]
        )
        night_deaths[no] = frozenset(deaths)
        for seat_no in deaths:
            gone[seat_no] = phase
        for action in performed:
            target = int(action["target_seat"])
            role_code = str(action["role_code"])
            record = {
                "night_no": no,
                "role_code": role_code,
                "sheet_id": int(action["id"]),
                "target": target,
                "target_role": ctx.role_of(target),
            }
            if role_code in rule_simulator.NIGHT_HEAL_ROLES:
                actors = actors_by_role.get(role_code, [])
                saved = target in shot_targets and target not in deaths and target in alive
                heals.append({**record, "actor_seats": actors, "saved": saved})
            elif role_code in rule_simulator.NIGHT_KILL_ROLES:
                # Мафия стреляет командой: решение делят все живые чёрные карты паритета.
                actors = (
                    [s for s, st in role_state.items() if st["parity_group"] == "black" and s in alive]
                    if role_code == "mafia"
                    else actors_by_role.get(role_code, [])
                )
                shots.append(
                    {
                        **record,
                        "actor_seats": actors,
                        "killed": target in deaths,
                        "healed": target in healed,
                    }
                )
            elif role_code in ("commissar", "boss"):
                target_role = ctx.role_of(target)
                hit = (
                    target_role is not None
                    and (
                        target_role.is_black
                        if role_code == "commissar"
                        else (not target_role.is_black and target_role.is_active)
                    )
                )
                checks.append(
                    {**record, "actor_seats": actors_by_role.get(role_code, []), "hit": bool(hit)}
                )
    return Timeline(
        phases=phases,
        alive_at_start=alive_at_start,
        gone=gone,
        night_deaths=night_deaths,
        shots=shots,
        heals=heals,
        checks=checks,
    )


# --------------------------------------------------------------------------- базы


def _won(ctx: GameContext, seat_no: int) -> bool:
    return seat_no in ctx.winners


def day_base(ctx: GameContext, timeline: Timeline, seat_no: int) -> Fraction:
    """Середина плюс участие плюс вклад в победу — оба пропорциональны отыгранной доле.

    Игрок, выбывший в первую ночь при победе своей стороны, к победе почти не причастен:
    эталонные разборы ставят таким 5/5/5, дожившим победителям — 7–8.
    """
    participation = timeline.participation(seat_no)
    base = Fraction(DAY_BASE) + DAY_PARTICIPATION * participation
    if _won(ctx, seat_no):
        base += DAY_WIN * participation
    return base


def _standard_role_base(ctx: GameContext, timeline: Timeline, seat_no: int) -> Fraction:
    participation = timeline.participation(seat_no)
    base = Fraction(ROLE_BASE_STANDARD) + ROLE_STANDARD_PARTICIPATION * participation
    if _won(ctx, seat_no):
        base += ROLE_STANDARD_WIN * participation
    return base


def _black_role_base(ctx: GameContext, timeline: Timeline, seat_no: int) -> Fraction:
    base = Fraction(ROLE_BLACK_FLOOR) + ROLE_BLACK_PARTICIPATION * timeline.participation(seat_no)
    if _won(ctx, seat_no):
        base += ROLE_BLACK_WIN
    kills = 0
    for shot in timeline.shots:
        if shot["role_code"] != "mafia" or seat_no not in shot["actor_seats"] or not shot["killed"]:
            continue
        target_role = shot["target_role"]
        kills += ROLE_BLACK_KILL_ACTIVE if target_role and target_role.is_active else ROLE_BLACK_KILL_CIVILIAN
    base += min(kills, ROLE_BLACK_KILL_CAP)
    for check in timeline.checks:
        if check["role_code"] == "boss" and seat_no in check["actor_seats"] and check["hit"]:
            base += ROLE_BOSS_FIND_ACTIVE
    return base


def _maniac_role_base(ctx: GameContext, timeline: Timeline, seat_no: int) -> Fraction:
    base = Fraction(ROLE_MANIAC_FLOOR) + ROLE_MANIAC_PARTICIPATION * timeline.participation(seat_no)
    if _won(ctx, seat_no):
        base += ROLE_MANIAC_WIN
    kills = 0
    for shot in timeline.shots:
        if shot["role_code"] != "maniac" or seat_no not in shot["actor_seats"] or not shot["killed"]:
            continue
        target_role = shot["target_role"]
        if target_role is not None and target_role.is_black:
            kills += ROLE_MANIAC_KILL_BLACK
        elif target_role is not None and target_role.is_active:
            kills += ROLE_MANIAC_KILL_ACTIVE
        else:
            kills += ROLE_MANIAC_KILL_CIVILIAN
    return base + min(kills, ROLE_MANIAC_KILL_CAP)


def _commissar_role_base(timeline: Timeline, seat_no: int) -> Fraction:
    """Точность проверок относительно ночей ПАРТИИ, а не ночей, которые комиссар прожил.

    Делить на прожитые ночи значило ставить 10.0 комиссару, погибшему в первую ночь после
    одного попадания (партия 29, 04.09.2026: 6.9 у формулы против 3.0 у владельца). Роль
    комиссара — довести проверки до стола; погибший в первую ночь довёл одну из четырёх.
    """
    nights_total = sum(1 for phase in timeline.phases if phase[0] == "night")
    own = [c for c in timeline.checks if c["role_code"] == "commissar" and seat_no in c["actor_seats"]]
    hits = sum(c["hit"] for c in own)
    misses = len(own) - hits
    effectiveness = Fraction(hits + misses * ROLE_CHECK_RED_WEIGHT, max(nights_total, 1))
    return Fraction(ROLE_COMMISSAR_FLOOR) + ROLE_COMMISSAR_SPAN * min(effectiveness, Fraction(1))


def _doctor_role_base(timeline: Timeline, seat_no: int) -> Fraction:
    own = [h for h in timeline.heals if seat_no in h["actor_seats"]]
    base = Fraction(ROLE_DOCTOR_FLOOR) + ROLE_DOCTOR_HEAL * min(len(own), 3)
    for heal in own:
        if not heal["saved"]:
            continue
        target_role = heal["target_role"]
        base += ROLE_DOCTOR_SAVE_ACTIVE if target_role and target_role.is_active else ROLE_DOCTOR_SAVE_CIVILIAN
    return base


def role_base(ctx: GameContext, timeline: Timeline, seat_no: int) -> Fraction:
    """База навыка «исполнение роли» — механика роли по листу и исходу партии."""
    seat = ctx.seats[seat_no]
    start = ctx.roles.get(seat.role_code)
    final = ctx.roles.get(seat.final_role)
    if start is None:
        return Fraction(ROLE_BASE_STANDARD)
    code = start.code
    if code == "commissar":
        return _commissar_role_base(timeline, seat_no)
    if code == "doctor":
        return _doctor_role_base(timeline, seat_no)
    if code == "maniac":
        return _maniac_role_base(ctx, timeline, seat_no)
    if start.parity_group == "black" and start.win_group == "black":
        return _black_role_base(ctx, timeline, seat_no)
    if start.elimination_effect == "werewolf_turn_black":
        # Превращённый оборотень доигрывает чёрным — и оценивается как чёрный.
        if final is not None and final.parity_group == "black":
            return _black_role_base(ctx, timeline, seat_no)
        return _standard_role_base(ctx, timeline, seat_no)
    if start.elimination_effect == "cheat_take_role":
        base = _standard_role_base(ctx, timeline, seat_no)
        if final is not None and final.code != start.code:
            base += ROLE_CHEAT_USED
        return base
    if start.elimination_effect == "kamikaze_pair":
        base = _standard_role_base(ctx, timeline, seat_no)
        gone = timeline.gone.get(seat_no)
        if gone is not None and gone[0] == "night":
            maniacs = {s for s, st in ctx.seats.items() if st.role_code == "maniac"}
            if maniacs & timeline.night_deaths.get(gone[1], frozenset()):
                base += ROLE_KAMIKAZE_ACTIVATED
        return base
    return _standard_role_base(ctx, timeline, seat_no)


def bases(ctx: GameContext, timeline: Timeline, seat_no: int) -> dict[str, Fraction]:
    day = day_base(ctx, timeline, seat_no)
    return {
        "analysis": day,
        "intuition": day,
        "influence": day,
        "role": role_base(ctx, timeline, seat_no),
        "discipline": Fraction(DISCIPLINE_BASE),
    }


# --------------------------------------------------------------------------- сигналы


def _outcome_basis(ctx: GameContext) -> tuple[tuple[str, int], ...]:
    return (("outcome", ctx.outcome_id),) if ctx.outcome_id is not None else ()


def deterministic_signals(ctx: GameContext, timeline: Timeline, seat_no: int) -> list[Signal]:
    """Наблюдения, которые не требуют модели: лист против ролей, заявления против ролей."""
    signals: list[Signal] = []
    own_role = ctx.role_of(seat_no)
    # Ночь: попадания по листу.
    for check in timeline.checks:
        if seat_no not in check["actor_seats"] or not check["hit"]:
            continue
        basis = (("sheet", check["sheet_id"]),)
        signals.append(
            Signal("intuition", +1, 3, "sheet", "check_hit", basis,
                   f"проверка в ночь {check['night_no']} попала в цель"))
        # Кого проверять — решение по столу: попадание говорит и об аналитике.
        signals.append(
            Signal("analysis", +1, 2, "sheet", "check_hit", basis,
                   f"цель проверки в ночь {check['night_no']} выбрана верно"))
    for heal in timeline.heals:
        if seat_no not in heal["actor_seats"]:
            continue
        if heal["saved"]:
            basis = (("sheet", heal["sheet_id"]),)
            signals.append(
                Signal("intuition", +1, 3, "sheet", "heal_saved", basis,
                       f"лечение в ночь {heal['night_no']} остановило выстрел"))
            target_role = heal["target_role"]
            if target_role is not None and target_role.is_active:
                # Спасённая активная роль продолжает работать — это дороже, чем
                # несколько безопасных лечений (вывод владельца по разбору 16.08.2026).
                signals.append(
                    Signal("intuition", +1, 3, "sheet", "saved_active_role", basis,
                           f"спасённая в ночь {heal['night_no']} карта осталась в игре"))
                signals.append(
                    Signal("analysis", +1, 2, "sheet", "saved_active_role", basis,
                           f"цель лечения в ночь {heal['night_no']} выбрана верно"))
        else:
            signals.append(
                Signal(
                    "intuition", -1, 1, "sheet", "heal_missed", (("sheet", heal["sheet_id"]),),
                    f"лечение в ночь {heal['night_no']} не совпало с выстрелом",
                )
            )
    for shot in timeline.shots:
        if seat_no not in shot["actor_seats"] or not shot["killed"]:
            continue
        target_role = shot["target_role"]
        if target_role is not None and shot["role_code"] == "maniac" and target_role.is_black:
            strength, note = 3, "снял чёрную карту"
        elif target_role is not None and target_role.is_active:
            strength, note = 2, "снял активную карту"
        else:
            strength, note = 1, "достиг цели"
        signals.append(
            Signal("intuition", +1, strength, "sheet", "shot_killed", (("sheet", shot["sheet_id"]),),
                   f"выстрел в ночь {shot['night_no']} {note}"))
    # День: публичные указания подтверждённого голоса против ролей целей.
    # Только настоящие дневные изгнания по хронологии: утреннее перечисление погибших
    # ночью тоже приходит как `expelled`, но выбывшим ночью «указание» не засчитывается.
    expelled_by_day: dict[int, set[int]] = {}
    for seat, (kind, no) in timeline.gone.items():
        if kind == "day":
            expelled_by_day.setdefault(int(no), set()).add(int(seat))
    expelled_all = set().union(*expelled_by_day.values()) if expelled_by_day else set()
    if own_role is not None:
        for claim in ctx.claims:
            if not claim["personal_eval_ok"] or claim["speaker_seat"] != seat_no:
                continue
            if claim["claim_type"] not in CLAIM_POINTING_TYPES or not claim["targets"]:
                continue
            basis = (("claim", claim["id"]),)
            for target in claim["targets"]:
                if target == seat_no:
                    continue
                target_role = ctx.role_of(target)
                if target_role is None:
                    continue
                if own_role.win_group == "solo":
                    # Маньяк играет против всех: точность его указаний оценивает модель.
                    continue
                if own_role.is_black:
                    # Чёрный уводит стол на красных: попадание в изгнание — влияние.
                    if not target_role.is_black and target in expelled_all:
                        signals.append(
                            Signal("influence", +1, 2, "claims", "pointed_at_expelled", basis,
                                   "названный игрок покинул стол"))
                    continue
                if target_role.is_black:
                    signals.append(
                        Signal("analysis", +1, 2 if target in expelled_all else 1, "claims",
                               "pointed_at_black", basis, "указал на чёрную карту"))
                    if target in expelled_all:
                        signals.append(
                            Signal("influence", +1, 2, "claims", "pointed_at_expelled", basis,
                                   "названная чёрная карта покинула стол"))
                else:
                    signals.append(
                        Signal("analysis", -1, 2 if target in expelled_all else 1, "claims",
                               "pointed_at_red", basis, "указал на красного"))
    # День: кандидат, который отбился; красный, которого стол вывел; чёрный, переживший
    # день с изгнанием (маскировка — его дневная работа).
    days_with_expulsion = {
        int(event["phase_no"]): int(event["id"])
        for event in ctx.events
        if event["type"] == "expelled" and event["seat_no"] is not None
    }
    for event in ctx.events:
        if event["type"] != "defense_start" or event["seat_no"] != seat_no:
            continue
        if seat_no not in expelled_by_day.get(int(event["phase_no"]), set()):
            signals.append(
                Signal("influence", +1, 1, "facts", "defended_and_stayed",
                       (("fact", int(event["id"])),), "остался за столом после оправдательной речи"))
    for event in ctx.events:
        if event["type"] != "expelled" or event["seat_no"] != seat_no:
            continue
        basis = (("fact", int(event["id"])),)
        if event.get("rule_action") == "foul_removed":
            # Удаление за фолы — единственный детерминированный сигнал дисциплины:
            # три нарушения регламента, стол потерял карту не по игре.
            signals.append(Signal("discipline", -1, 3, "facts", "foul_removed", basis,
                                  "удалён из партии за фолы"))
            continue
        if own_role is None or own_role.is_black:
            continue
        signals.append(Signal("influence", -1, 2, "facts", "expelled_as_red", basis,
                              "стол вывел красную карту — не удержался"))
        signals.append(Signal("intuition", -1, 1, "facts", "expelled_as_red", basis,
                              "не почувствовал угрозу голосования"))
        signals.append(Signal("analysis", -1, 1, "facts", "expelled_as_red", basis,
                              "не смог объяснить столу свою позицию"))
        signals.append(Signal("role", -1, 2, "facts", "expelled_as_red", basis,
                              "красная карта ушла со стола по решению города"))
    if own_role is not None and own_role.is_black:
        for phase in timeline.phases:
            kind, no = phase
            if kind != "day" or no not in days_with_expulsion or seat_no not in timeline.alive_at_start[phase]:
                continue
            if seat_no in expelled_by_day.get(no, set()):
                continue
            basis = (("fact", days_with_expulsion[no]),)
            signals.append(Signal("influence", +1, 1, "facts", "survived_vote_as_black", basis,
                                  f"день {no}: стол вывел не его"))
            signals.append(Signal("analysis", +1, 1, "facts", "survived_vote_as_black", basis,
                                  f"день {no}: остался вне подозрений"))
    return signals


def validate_model_signals(
    items: Iterable[Mapping[str, Any]],
    *,
    valid_facts: set[int],
    valid_claims: set[int],
) -> tuple[list[Signal], list[tuple[int, str]]]:
    """Сигналы модели: право на сигнал даёт основание, а не источник.

    Отбрасываются (не роняя судейство): неизвестный навык, навык роли (его считает лист),
    пустое или чужое основание, повтор одного основания в одном навыке, всё сверх
    `MODEL_SIGNALS_PER_SKILL` по убыванию силы.
    """
    accepted: list[Signal] = []
    dropped: list[tuple[int, str]] = []
    seen: set[tuple[str, tuple[tuple[str, int], ...]]] = set()
    per_skill: dict[str, list[Signal]] = {}
    for index, item in enumerate(items):
        skill = item.get("skill")
        if skill not in MODEL_SKILLS:
            dropped.append((index, "skill_not_model_scored"))
            continue
        sign = item.get("sign")
        strength = item.get("strength")
        if sign not in (-1, 1) or isinstance(strength, bool) or strength not in (1, 2, 3):
            dropped.append((index, "bad_sign_or_strength"))
            continue
        basis_raw = item.get("basis")
        if not isinstance(basis_raw, list) or not basis_raw:
            dropped.append((index, "no_basis"))
            continue
        refs: list[tuple[str, int]] = []
        problem = None
        for ref in basis_raw:
            kind = ref.get("kind") if isinstance(ref, Mapping) else None
            ref_id = ref.get("id") if isinstance(ref, Mapping) else None
            if isinstance(ref_id, bool) or not isinstance(ref_id, int):
                problem = "bad_basis_ref"
                break
            if kind == "fact" and ref_id in valid_facts:
                refs.append(("fact", ref_id))
            elif kind == "claim" and ref_id in valid_claims:
                refs.append(("claim", ref_id))
            else:
                problem = "basis_outside_evidence"
                break
        if problem is not None:
            dropped.append((index, problem))
            continue
        key = (str(skill), tuple(sorted(set(refs))))
        if key in seen:
            dropped.append((index, "duplicate_basis"))
            continue
        seen.add(key)
        signal = Signal(
            str(skill), int(sign), int(strength), "model", "model_observation",
            tuple(sorted(set(refs))), str(item.get("note") or "").strip()[:160],
        )
        per_skill.setdefault(str(skill), []).append(signal)
    for skill, signals in per_skill.items():
        ranked = sorted(signals, key=lambda s: -s.strength)
        accepted.extend(ranked[:MODEL_SIGNALS_PER_SKILL])
        for _ in ranked[MODEL_SIGNALS_PER_SKILL:]:
            dropped.append((-1, f"{skill}_over_limit"))
    return accepted, dropped


# --------------------------------------------------------------------------- формула


def clamp(value: Fraction, low: int, high: int) -> Fraction:
    return max(Fraction(low), min(Fraction(high), value))


def delta_milli(signals: Iterable[Signal]) -> Fraction:
    signals = list(signals)
    if not signals:
        return Fraction(0)
    raw = sum(s.sign * s.strength for s in signals)
    n = len(signals)
    return clamp(Fraction(raw * SIGNAL_UNIT), -DELTA_CAP, DELTA_CAP) * Fraction(n, n + 2)


def score_skill(skill: str, base: Fraction, signals: list[Signal]) -> SkillScore:
    own = [s for s in signals if s.skill == skill]
    total = clamp(base + delta_milli(own), MIN_MILLI, MAX_MILLI)
    return SkillScore(
        skill=skill,
        base_milli=round_half_up(clamp(base, MIN_MILLI, MAX_MILLI)),
        score_milli=round_half_up(total),
        signals=own,
    )


def score_player(
    ctx: GameContext,
    timeline: Timeline,
    seat_no: int,
    model_signals: Iterable[Signal] = (),
) -> dict[str, SkillScore]:
    signals = deterministic_signals(ctx, timeline, seat_no) + list(model_signals)
    return {
        skill: score_skill(skill, base, signals)
        for skill, base in bases(ctx, timeline, seat_no).items()
    }


def rationale(score: SkillScore) -> str:
    """Одна строка ведущему: из чего сложилась цифра. Без имён и без цитат."""
    parts = [s.note for s in score.signals if s.note]
    text = "; ".join(dict.fromkeys(parts))
    return text[:240]


def opinion_payload(
    ctx: GameContext,
    scores: dict[str, SkillScore],
    *,
    comment: str,
) -> dict[str, Any]:
    """Payload `judge_opinion` схемы 3: та же форма `skills`, что видели v1/v2, плюс формула."""
    outcome_basis = [{"kind": kind, "id": ref} for kind, ref in _outcome_basis(ctx)]
    skills: dict[str, Any] = {}
    for skill in SKILLS:
        score = scores[skill]
        skills[skill] = {
            "status": "SCORED",
            "score_milli": score.score_milli,
            "base_milli": score.base_milli,
            "basis": score.basis or outcome_basis,
            "rationale": rationale(score),
            "signals": [s.as_dict() for s in score.signals],
        }
    return {"formula": FORMULA_VERSION, "skills": skills, "comment": comment.strip()[:400]}


# --------------------------------------------------------------------------- номинации


@dataclass(frozen=True)
class Candidate:
    code: str
    seat_no: int
    profile_id: int
    headline: str
    weight: int


def nomination_candidates(ctx: GameContext, timeline: Timeline) -> list[Candidate]:
    """Кого есть за что отметить — по метрикам, а не по впечатлению модели.

    Модель получает этот список и только формулирует название и причину. Роли в
    заголовке допустимы: разбор публикуется после партии (решение владельца 03.09.2026).
    """
    candidates: list[Candidate] = []

    def role_name(seat_no: int) -> str:
        role = ctx.role_of(seat_no, final=True)
        return role.name if role is not None else "игрок"

    def add(code: str, seat_no: int, headline: str, weight: int) -> None:
        seat = ctx.seats.get(seat_no)
        if seat is None:
            return
        candidates.append(Candidate(code, seat_no, seat.profile_id, headline, weight))

    # Проверки комиссара: больше всего чёрных карт.
    hits_by_seat: dict[int, int] = {}
    checks_by_seat: dict[int, int] = {}
    for check in timeline.checks:
        if check["role_code"] != "commissar":
            continue
        for seat_no in check["actor_seats"]:
            checks_by_seat[seat_no] = checks_by_seat.get(seat_no, 0) + 1
            if check["hit"]:
                hits_by_seat[seat_no] = hits_by_seat.get(seat_no, 0) + 1
    if hits_by_seat:
        seat_no = max(hits_by_seat, key=lambda s: (hits_by_seat[s], -s))
        add(
            "best_checks", seat_no,
            f"{role_name(seat_no)}: {hits_by_seat[seat_no]} чёрных карт из {checks_by_seat[seat_no]} проверок",
            30 + 10 * hits_by_seat[seat_no],
        )
    # Спасение доктора: остановленный выстрел, приоритет — активная роль.
    best_save: tuple[int, int, str] | None = None
    for heal in timeline.heals:
        if not heal["saved"]:
            continue
        target_role = heal["target_role"]
        weight = 45 if target_role and target_role.is_active else 30
        for seat_no in heal["actor_seats"]:
            if best_save is None or weight > best_save[0]:
                headline = f"{role_name(seat_no)}: лечение в ночь {heal['night_no']} остановило выстрел"
                best_save = (weight, seat_no, headline)
    if best_save is not None:
        add("key_heal", best_save[1], best_save[2], best_save[0])
    # Маньяк: результативность.
    for seat_no, seat in ctx.seats.items():
        if seat.role_code != "maniac":
            continue
        kills = [
            s for s in timeline.shots
            if s["role_code"] == "maniac" and seat_no in s["actor_seats"] and s["killed"]
        ]
        black = sum(1 for s in kills if s["target_role"] is not None and s["target_role"].is_black)
        if kills and (len(kills) >= 2 or black or _won(ctx, seat_no)):
            add(
                "maniac_run", seat_no,
                f"{role_name(seat_no)}: {len(kills)} результативных выстрелов, чёрных карт — {black}"
                + (", победа в одиночку" if _won(ctx, seat_no) else ""),
                35 + 5 * len(kills) + (15 if _won(ctx, seat_no) else 0),
            )
    # Последняя чёрная карта: чёрный паритета, отыгравший больше всех.
    blacks = [
        seat_no for seat_no, seat in ctx.seats.items()
        if (ctx.role_of(seat_no, final=True) or ctx.role_of(seat_no)) is not None
        and ctx.role_of(seat_no, final=True).parity_group == "black"
    ]
    if blacks:
        seat_no = max(blacks, key=lambda s: (timeline.participation(s), -s))
        if timeline.participation(seat_no) >= Fraction(1, 2):
            fate = "дошёл до финала" if timeline.survived(seat_no) else "продержался дольше всех чёрных карт"
            add(
                "last_black", seat_no,
                f"{role_name(seat_no)}: {fate}" + (", победа" if _won(ctx, seat_no) else ""),
                25 + (15 if _won(ctx, seat_no) else 0) + (10 if timeline.survived(seat_no) else 0),
            )
    # Чтение стола: точность подтверждённых указаний красного игрока.
    accuracy: dict[int, int] = {}
    for seat_no in ctx.seats:
        role = ctx.role_of(seat_no)
        if role is None or role.is_black:
            continue
        for signal in deterministic_signals(ctx, timeline, seat_no):
            if signal.code == "pointed_at_black":
                accuracy[seat_no] = accuracy.get(seat_no, 0) + 1
            elif signal.code == "pointed_at_red":
                accuracy[seat_no] = accuracy.get(seat_no, 0) - 1
    if accuracy:
        seat_no = max(accuracy, key=lambda s: (accuracy[s], -s))
        if accuracy[seat_no] >= 2:
            add(
                "table_reader", seat_no,
                f"{role_name(seat_no)}: чаще других указывал на чёрные карты",
                20 + 5 * accuracy[seat_no],
            )
    # Активированный камикадзе.
    for seat_no, seat in ctx.seats.items():
        role = ctx.roles.get(seat.role_code)
        gone = timeline.gone.get(seat_no)
        if role is None or role.elimination_effect != "kamikaze_pair" or gone is None or gone[0] != "night":
            continue
        maniacs = {s for s, st in ctx.seats.items() if st.role_code == "maniac"}
        if maniacs & timeline.night_deaths.get(gone[1], frozenset()):
            add("kamikaze_hit", seat_no, f"{role_name(seat_no)}: забрал маньяка с собой", 40)
    # Запасной кандидат: дожил до конца на стороне победителя.
    if len(candidates) < 3:
        survivors = [s for s in ctx.seats if timeline.survived(s) and _won(ctx, s)]
        for seat_no in sorted(survivors, key=lambda s: (-timeline.participation(s), s))[:3 - len(candidates)]:
            if not any(c.seat_no == seat_no for c in candidates):
                add("survivor_win", seat_no, f"{role_name(seat_no)}: дошёл до победы", 10)
    ranked = sorted(candidates, key=lambda c: (-c.weight, c.seat_no))
    unique: list[Candidate] = []
    for candidate in ranked:
        if any(u.seat_no == candidate.seat_no for u in unique):
            continue
        unique.append(candidate)
    return unique[:6]
