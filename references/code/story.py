"""App-owned STORY materialization and mandatory program fact-check."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

from app import db
from app.analysis import article, orchestration, scoring
from app.identity import service as identity
from app.league import service as league
from app.publication import service as publication

log = logging.getLogger("mafia.analysis")

HEDGES = ("по мнению", "похоже", "возможно", "судя", "предполож", "вероятно")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _id(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} должен быть positive integer")
    return value


def game_data(
    analysis_run_id: int, *, fact_set_id: int, claim_set_id: int | None
) -> tuple[scoring.GameContext, scoring.Timeline, dict[int, str], dict[str, str]]:
    """Контекст партии для статьи: та же хронология, что у оценок, имена из состава."""
    run = db.one(
        "SELECT r.game_id, r.rule_set_id, e.venue_id FROM analysis_run r "
        "JOIN game g ON g.id=r.game_id JOIN game_evening e ON e.id=g.evening_id WHERE r.id=?",
        (analysis_run_id,),
    )
    if run is None:
        raise ValueError("analysis run не найден")
    ctx = scoring.context_for_run(analysis_run_id, fact_set_id=fact_set_id, claim_set_id=claim_set_id)
    timeline = scoring.build_timeline(ctx)
    names = {
        seat_no: identity.nickname(seat.profile_id, int(run["venue_id"]))
        for seat_no, seat in ctx.seats.items()
    }
    role_names = {code: str(role["name"]) for code, role in league.role_map(int(run["rule_set_id"])).items()}
    return ctx, timeline, names, role_names


def assemble_article(
    *, analysis_run_id: int, fact_set_id: int, claim_set_id: int | None, story: dict[str, Any]
) -> list[dict[str, Any]]:
    ctx, timeline, names, role_names = game_data(
        analysis_run_id, fact_set_id=fact_set_id, claim_set_id=claim_set_id
    )
    return article.assemble(
        ctx=ctx, timeline=timeline, names=names, role_names=role_names, story=story
    )


def materialize_succeeded(
    job, payload: dict[str, Any], bindings: dict[str, Any], *, now_ms: int
) -> int:
    if job["step"] == "STORY":
        existing = db.one("SELECT id FROM article_version WHERE ai_job_id=?", (job["id"],))
        if existing is not None:
            return int(existing["id"])
        fact_set_id = _id(bindings.get("fact_set_id"), "fact_set_id")
        claim_raw = bindings.get("claim_set_id")
        claim_set_id = None if claim_raw is None else _id(claim_raw, "claim_set_id")
        if int(payload.get("schema_version", 1)) >= 2:
            blocks = assemble_article(
                analysis_run_id=int(job["analysis_run_id"]),
                fact_set_id=fact_set_id,
                claim_set_id=claim_set_id,
                story=payload,
            )
        else:
            blocks = payload["blocks"]
        blocks, degraded = publication.salvage_article_blocks(
            game_id=job["game_id"],
            fact_set_id=fact_set_id,
            claim_set_id=claim_set_id,
            blocks=blocks,
        )
        for index, reason in degraded:
            log.warning(
                "STORY block salvaged run_id=%s block_index=%s reason=%s",
                job["analysis_run_id"], index, reason,
            )
        publication.validate_article_blocks(
            game_id=job["game_id"],
            fact_set_id=fact_set_id,
            claim_set_id=claim_set_id,
            blocks=blocks,
            require_typed=True,
        )
        return publication.save_article(
            game_id=job["game_id"],
            fact_set_id=fact_set_id,
            claim_set_id=claim_set_id,
            title=payload["title"],
            lead=payload["lead"],
            blocks=blocks,
            created_by=None,
            ai_job_id=job["id"],
        )
    existing = db.one(
        "SELECT id, status FROM fact_check_report WHERE ai_job_id=? ORDER BY id DESC LIMIT 1",
        (job["id"],),
    )
    if existing is not None and existing["status"] != "blocked":
        return int(existing["id"])
    # Вердикт отчёта неизменяем (аудит), а правило программной части может измениться —
    # прогон 44 партии 28 (03.09.2026) был заблокирован четырьмя замечаниями стиля по
    # старому правилу. Переоценка — это новый отчёт по той же оплаченной джобе: старый
    # остаётся историей, провайдерские нарушения переносятся из того же output.
    article_id = _id(bindings.get("article_version_id"), "article_version_id")
    article = db.one(
        "SELECT * FROM article_version WHERE id=? AND game_id=?", (article_id, job["game_id"])
    )
    if article is None:
        raise ValueError("FACT_CHECK article отсутствует")
    report_id = int(
        db.execute(
            "INSERT INTO fact_check_report(analysis_run_id, game_id, ai_job_id, "
            "article_version_id, fact_set_id, claim_set_id, status, content_sha256, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'provider_draft', ?, ?)",
            (
                job["analysis_run_id"],
                job["game_id"],
                job["id"],
                article_id,
                article["fact_set_id"],
                article["claim_set_id"],
                hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest(),
                now_ms,
            ),
        ).lastrowid
    )
    for violation in payload["violations"]:
        db.execute(
            "INSERT INTO fact_check_violation(report_id, game_id, source, code, block_index, detail) "
            "VALUES (?, ?, 'provider', ?, ?, ?)",
            (
                report_id,
                job["game_id"],
                violation["code"],
                violation["block_index"],
                violation["detail"],
            ),
        )
    return report_id


# Нарушение фактов блокирует публикацию (§6A.4): факт без основания, мнение как факт,
# дословная цитата, скрытая информация, битая структура ссылок. Замечание стиля —
# категоричная формулировка вывода — не блокирует: живой прогон 44 партии 28
# (02.09.2026) умер на четырёх «без слова „вероятно“», когда оценки тринадцати игроков
# и статья из 18 блоков были уже посчитаны. Спор двух моделей о формулировке доезжает
# до ведущего рядом с текстом, и он правит слово, а не перезапускает разбор.
BLOCKING_CODES = frozenset(
    {
        "UNGROUNDED_FACT",
        "OPINION_AS_FACT",
        "VERBATIM_QUOTE",
        "HIDDEN_INFO",
        "STRUCTURE",
        "OPINION_WITHOUT_BASIS",
    }
)
ADVISORY_CODES = frozenset({"HARD_CLAIM", "OTHER_GROUNDING"})


def is_blocking(code: str) -> bool:
    return code in BLOCKING_CODES


def _tokens(text: str) -> set[str]:
    """Грубые токены для сопоставления претензии с абзацем: номера мест и основы слов."""
    return {
        token if token.startswith("№") else token[:6]
        for token in re.findall(r"№\s*\d+|[а-яёa-z]{4,}", str(text).lower())
    }


def locate_block(blocks: list[dict[str, Any]], block_index: int | None, detail: str) -> int | None:
    """Какой модельный абзац имеет в виду претензия проверки.

    Индекс от модели-проверщика ненадёжен: в живом прогоне 48 партии 28 (03.09.2026)
    `block_index=36` указывал на абзац про маньяка, а текст претензии говорил о блоке 38
    («комиссар номинировал мафиози»). Поэтому индекс — лишь подсказка: побеждает
    модельный абзац с наибольшим пересечением слов с претензией; при равенстве —
    названный индекс. Ничего не совпало — индекс, если он модельный, иначе None.
    """
    candidates = [
        index for index, block in enumerate(blocks)
        if block.get("source") == "model" and block.get("type") != "heading"
    ]
    if not candidates:
        return None
    wanted = _tokens(detail)
    scores = {index: len(wanted & _tokens(blocks[index].get("text", ""))) for index in candidates}
    best = max(candidates, key=lambda index: (scores[index], index == block_index))
    if scores[best] == 0:
        return block_index if block_index in candidates else None
    return best


def _latest_report(analysis_run_id: int):
    return db.one(
        "SELECT id, status FROM fact_check_report WHERE analysis_run_id=? "
        "ORDER BY id DESC LIMIT 1",
        (analysis_run_id,),
    )


def _violations(report_id: int, *, blocking: bool) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT v.id, v.block_index, v.code, v.detail, r.resolution "
        "FROM fact_check_violation v "
        "LEFT JOIN fact_check_resolution r ON r.violation_id = v.id "
        "WHERE v.report_id=? ORDER BY v.block_index, v.id",
        (report_id,),
    )
    return [
        {
            "violation_id": int(row["id"]),
            "block_index": row["block_index"],
            "code": row["code"],
            "detail": row["detail"],
            "resolution": row["resolution"],
        }
        for row in rows
        if is_blocking(str(row["code"])) == blocking
    ]


def blocking_violations(analysis_run_id: int) -> list[dict[str, Any]]:
    """Нерешённые блокирующие нарушения последнего отчёта проверки статьи прогона.

    Пусто, если последний отчёт прошёл, его ещё нет или по каждой претензии ведущий
    уже решил («оставить» или «удалить абзац»). Непустой список означает: AI-статью
    публиковать нельзя, пока ведущий не решит по каждой претензии.
    """
    report = _latest_report(analysis_run_id)
    if report is None or report["status"] != "blocked":
        return []
    return [item for item in _violations(int(report["id"]), blocking=True) if item["resolution"] is None]


def flagged_blocks(analysis_run_id: int, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Нерешённые претензии вместе с текстом абзаца, о котором они говорят.

    Приёмка владельца 03.09.2026: «не смог найти в тексте абзац 37, надо непроверенный
    факт выводить отдельным окном с кнопками Удалить или Оставить».
    """
    result = []
    for item in blocking_violations(analysis_run_id):
        index = locate_block(blocks, item["block_index"], item["detail"])
        result.append(
            {
                **item,
                "located_index": index,
                "text": blocks[index].get("text") if index is not None else None,
            }
        )
    return result


def advisory_notes(analysis_run_id: int, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Замечания стиля последнего отчёта с текстом абзаца — публикации не мешают."""
    report = _latest_report(analysis_run_id)
    if report is None or report["status"] not in {"passed", "blocked"}:
        return []
    result = []
    for item in _violations(int(report["id"]), blocking=False):
        index = locate_block(blocks, item["block_index"], item["detail"])
        result.append({**item, "text": blocks[index].get("text") if index is not None else None})
    return result


def resolve_violation(
    violation_id: int,
    *,
    game_id: int,
    resolution: str,
    actor_user_id: int,
    article_version_id: int | None = None,
    now_ms: int | None = None,
) -> None:
    """Решение ведущего по претензии: `kept` — абзац остаётся, `dropped` — удалён редакцией."""
    if resolution not in {"kept", "dropped"}:
        raise ValueError("неизвестное решение по претензии")
    if (resolution == "dropped") != (article_version_id is not None):
        raise ValueError("удаление абзаца требует редакцию статьи, «оставить» — нет")
    row = db.one(
        "SELECT v.id FROM fact_check_violation v "
        "LEFT JOIN fact_check_resolution r ON r.violation_id = v.id "
        "WHERE v.id=? AND v.game_id=? AND r.violation_id IS NULL",
        (violation_id, game_id),
    )
    if row is None:
        raise ValueError("претензия не найдена или уже решена")
    db.execute(
        "INSERT INTO fact_check_resolution(violation_id, game_id, resolution, "
        "article_version_id, resolved_by, resolved_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            violation_id, game_id, resolution, article_version_id, actor_user_id,
            now_ms if now_ms is not None else db.now_ms(),
        ),
    )


def decide_flag(
    *,
    analysis_run_id: int,
    game_id: int,
    violation_id: int,
    action: str,
    actor_user_id: int,
) -> int | None:
    """Кнопки «Удалить абзац» / «Оставить» под претензией. Возвращает id новой редакции."""
    output = db.one(
        "SELECT output_article_version_id FROM analysis_run_output WHERE analysis_run_id=?",
        (analysis_run_id,),
    )
    if output is None or output["output_article_version_id"] is None:
        raise ValueError("у прогона нет утверждённой статьи")
    root_id = int(output["output_article_version_id"])
    current = publication.latest_edition(root_id) or publication.article(root_id)
    blocks = publication.article_blocks(current)
    flag = next(
        (item for item in flagged_blocks(analysis_run_id, blocks) if item["violation_id"] == violation_id),
        None,
    )
    if flag is None:
        raise ValueError("претензия не найдена или уже решена")
    if action == "keep":
        resolve_violation(
            violation_id, game_id=game_id, resolution="kept", actor_user_id=actor_user_id
        )
        return None
    if action != "drop":
        raise ValueError("неизвестное действие по претензии")
    if flag["located_index"] is None:
        raise ValueError("абзац претензии не найден в статье — удалять нечего")
    edition_id = publication.drop_block(
        int(current["id"]), int(flag["located_index"]), created_by=actor_user_id
    )
    resolve_violation(
        violation_id, game_id=game_id, resolution="dropped",
        actor_user_id=actor_user_id, article_version_id=edition_id,
    )
    return edition_id


def run_program(report_id: int, *, now_ms: int | None = None) -> bool:
    report = db.one(
        "SELECT * FROM fact_check_report WHERE id=? AND status='provider_draft'", (report_id,)
    )
    if report is None:
        raise ValueError("provider_draft fact check report не найден")
    article = db.one("SELECT * FROM article_version WHERE id=?", (report["article_version_id"],))
    blocks = json.loads(article["blocks_json"])
    violations: list[tuple[str, int | None, str]] = []
    try:
        publication.validate_article_blocks(
            game_id=report["game_id"],
            fact_set_id=report["fact_set_id"],
            claim_set_id=report["claim_set_id"],
            blocks=blocks,
            require_typed=True,
        )
    except ValueError as exc:
        violations.append(("STRUCTURE", None, str(exc)[:500]))
    for index, block in enumerate(blocks):
        sentence_type = block.get("sentence_type")
        if sentence_type == "inferred_claim" and not any(
            hedge in str(block.get("text", "")).casefold() for hedge in HEDGES
        ):
            violations.append(
                ("HARD_CLAIM", index, "inferred_claim не имеет мягкой формулировки")
            )
        if sentence_type == "opinion":
            opinion = db.one(
                "SELECT payload_json FROM judge_opinion WHERE id=?",
                (block.get("judge_opinion_id"),),
            )
            if opinion is not None and not any(
                skill.get("basis")
                for skill in json.loads(opinion["payload_json"])["skills"].values()
            ):
                violations.append(
                    ("OPINION_WITHOUT_BASIS", index, "Judge opinion не имеет basis")
                )
    with db.write() as conn:
        for code, block_index, detail in violations:
            conn.execute(
                "INSERT INTO fact_check_violation(report_id, game_id, source, code, "
                "block_index, detail) VALUES (?, ?, 'program', ?, ?, ?)",
                (report_id, report["game_id"], code, block_index, detail),
            )
        rows = conn.execute(
            "SELECT code, source FROM fact_check_violation WHERE report_id=?", (report_id,)
        ).fetchall()
        codes = [str(row[0]) for row in rows]
        total = sum(1 for code in codes if is_blocking(code))
        advisory = len(codes) - total
        # Битая структура ссылок — детерминированный отказ нашего же валидатора: это
        # дефект кода или порча данных, и прогон честно падает. Нарушение фактов от
        # модели-проверщика — спор двух моделей о тексте: отчёт `blocked`, но шаг
        # завершается, разбор доезжает до формы, а публикация AI-текста как есть
        # отклоняется, пока ведущий не поправит абзацы. Живой прогон 48 партии 28
        # (03.09.2026): один UNGROUNDED_FACT с неверным индексом блока и ошибочной
        # посылкой (игрок «уже выбыл», хотя по фактам говорил в тот день) убил прогон
        # с оценками тринадцати игроков, статьёй и двумя обложками.
        program_blocking = any(
            is_blocking(str(row[0])) and str(row[1]) == "program" for row in rows
        )
        now_ms = now_ms if now_ms is not None else db.now_ms()
        conn.execute(
            "UPDATE fact_check_report SET status=?, completed_at=? WHERE id=?",
            ("blocked" if total else "passed", now_ms, report_id),
        )
    if advisory:
        log.info(
            "fact check advisory run_id=%s report_id=%s notes=%s",
            report["analysis_run_id"], report_id, advisory,
        )
    if program_blocking:
        orchestration.fail_step(
            report["analysis_run_id"],
            "FACT_CHECK",
            reason_code="blocking_violations",
            now_ms=now_ms,
        )
        return False
    if total:
        log.warning(
            "fact check blocked run_id=%s report_id=%s violations=%s",
            report["analysis_run_id"], report_id, total,
        )
    orchestration.complete_program_step(
        report["analysis_run_id"],
        "FACT_CHECK",
        output_bindings={
            "fact_check_report_id": report_id,
            "article_version_id": report["article_version_id"],
            "violations": total,
            "advisory_notes": advisory,
        },
        now_ms=now_ms,
    )
    return True
