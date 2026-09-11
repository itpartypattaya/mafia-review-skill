#!/usr/bin/env python3
"""Автономная проверка комплекта разбора до загрузки в сервис.

Повторяет отказы парсера `app/publication/review_import.py` без зависимости от него:
установленный скилл лежит вне репозитория сервиса и вызвать парсер не может, а узнавать
об ошибке формата уже в студии партии — дорого (владелец возвращает файл на переделку).
Паритет с парсером держит тест сервиса `tests/unit/test_review_import_parser.py`.

Запуск:
    python scripts/validate_review_bundle.py review-2026-09-06-1/
    python scripts/validate_review_bundle.py --review review.md --facts facts.md

Коды выхода: 0 — можно грузить, 1 — есть ошибки, 2 — файлы не найдены.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

MAX_REVIEW_BYTES = 1 * 1024 * 1024
MAX_COVER_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 4 * 1024 * 1024

ROLE_BY_PHRASE = {
    "мирный": "civilian", "мирная": "civilian", "мирный житель": "civilian",
    "комиссар": "commissar", "доктор": "doctor", "свидетель": "witness",
    "наследник": "witness", "босс": "boss", "дон": "boss", "мафия": "mafia",
    "маньяк": "maniac", "нагнетатель": "agitator", "агитатор": "agitator",
    "жулик": "cheat", "камикадзе": "kamikaze", "оборотень": "werewolf",
}
BLACK_ROLES = {"boss", "mafia"}
SOLO_ROLES = {"maniac", "agitator"}
ROLE_TRANSITIONS = {"witness": {"commissar", "doctor"}, "werewolf": {"mafia"},
                    "cheat": set(ROLE_BY_PHRASE.values())}
WINNER_WORDS = {"победа": True, "победил": True, "победили": True, "выиграл": True,
                "✓": True, "да": True, "поражение": False, "проиграл": False,
                "—": False, "-": False, "нет": False, "": None}
# «Нейросеточные» связки: их ловит ревью статей, а не парсер, — поэтому это предупреждение.
STOCK_PHRASES = ("решающим фактором стал", "способствовал", "в лице", "разбор отмечает",
                 "стоит отметить", "нельзя не отметить", "подводя итог")


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, text: str) -> None:
        self.errors.append(text)

    def warn(self, text: str) -> None:
        self.warnings.append(text)


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_separator(line: str) -> bool:
    return bool(re.fullmatch(r"\|?[\s:|-]+\|?", line.strip())) and "-" in line


def _sections(text: str) -> dict[str, list[str]]:
    """Разделы второго уровня; эмодзи и лишние пробелы в заголовке игнорируются."""
    out: dict[str, list[str]] = {}
    current = ""
    for line in text.splitlines():
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match and not line.startswith("###"):
            title = re.sub(r"[^\w\s«».,()-]", "", match.group(1), flags=re.UNICODE).strip().lower()
            current = title
            out.setdefault(current, [])
            continue
        if current:
            out[current].append(line)
    return out


def _find(sections: dict[str, list[str]], *names: str) -> list[str] | None:
    for title, lines in sections.items():
        if any(name in title for name in names):
            return lines
    return None


def _role(raw: str) -> str | None:
    key = raw.strip().lower().replace("ё", "е")
    for phrase, code in ROLE_BY_PHRASE.items():
        if phrase.replace("ё", "е") == key:
            return code
    return None


def _winner_side(lines: list[str]) -> str | None:
    for line in lines:
        match = re.match(r"^\s*победил[аи]?\s*[:—-]\s*(.+)$", line, flags=re.IGNORECASE)
        if not match:
            continue
        low = match.group(1).lower().replace("ё", "е")
        if "никто" in low or "ничья" in low:
            return "none"
        if "красн" in low or "город" in low or "мирн" in low:
            return "red"
        if "черн" in low or "мафи" in low:
            return "black"
        if "маньяк" in low or "нагнетател" in low or "оборот" in low:
            return "neutral"
        return "?"
    return None


def check_review(path: Path, report: Report) -> dict[int, str]:
    text = path.read_text(encoding="utf-8")
    if len(text.encode("utf-8")) > MAX_REVIEW_BYTES:
        report.error(f"{path.name}: больше 1 МиБ — сервис не примет")
    titles = [line for line in text.splitlines() if line.startswith("# ")]
    if len(titles) != 1:
        report.error(f"{path.name}: нужен ровно один заголовок «# », найдено {len(titles)}")
    body = text.split(titles[0], 1)[1] if titles else text
    lead = next((line.strip() for line in body.splitlines()
                 if line.strip() and not line.startswith("#")), "")
    if not lead:
        report.error(f"{path.name}: нет лида — первый абзац после заголовка")
    if lead.startswith(("|", "-", "*")):
        report.error(f"{path.name}: лид — абзац прозой, не таблица и не список")

    sections = _sections(text)
    outcome = _find(sections, "исход")
    seats: dict[int, str] = {}
    finals: dict[int, str] = {}
    winners: dict[int, bool | None] = {}
    if outcome is None:
        report.error(f"{path.name}: нет раздела «## Исход»")
        return seats
    side = _winner_side(outcome)
    if side is None:
        report.error(f"{path.name}: в «## Исход» нет строки «Победили: …»")
    elif side == "?":
        report.error(f"{path.name}: сторону в строке «Победили: …» не понять")

    header: list[str] | None = None
    for line in outcome:
        if not line.strip().startswith("|"):
            continue
        cells = _cells(line)
        if header is None:
            header = [c.lower() for c in cells]
            continue
        if _is_separator(line):
            continue
        row = dict(zip(header, cells, strict=False))
        raw_no = cells[0]
        if not raw_no.isdigit():
            report.error(f"{path.name}: место «{raw_no}» — не число")
            continue
        seat_no = int(raw_no)
        if seat_no in seats:
            report.error(f"{path.name}: место {seat_no} указано дважды")
            continue
        role = _role(cells[2] if len(cells) > 2 else "")
        if role is None:
            report.error(f"{path.name}: место {seat_no}: роль «{cells[2]}» не из свода")
            continue
        seats[seat_no] = role
        final_raw = next((row.get(key, "") for key in row if "финальн" in key), "")
        if final_raw:
            final = _role(final_raw)
            if final is None:
                report.error(f"{path.name}: место {seat_no}: финальная роль «{final_raw}»"
                             " не из свода")
            elif final == role:
                report.error(f"{path.name}: место {seat_no}: финальная роль совпадает"
                             " со стартовой — колонку оставляют пустой")
            elif final not in ROLE_TRANSITIONS.get(role, set()):
                report.error(f"{path.name}: место {seat_no}: переход {role} → {final}"
                             " не предусмотрен сводом")
            else:
                finals[seat_no] = final
        result_raw = next((row.get(key, "") for key in row if "итог" in key), "")
        key = result_raw.strip().lower().rstrip(".")
        winners[seat_no] = next((value for word, value in WINNER_WORDS.items()
                                 if key.startswith(word) and word), None) if key else None
    if not seats:
        report.error(f"{path.name}: в «## Исход» нет таблицы состава")
        return seats
    missing = [no for no in range(1, max(seats) + 1) if no not in seats]
    if missing:
        report.error(f"{path.name}: в составе нет мест {missing} — считай по листу,"
                     " пропуск одного места отменяет импорт целиком")

    if side in ("red", "black"):
        for seat_no, role in seats.items():
            final = finals.get(seat_no, role)
            expected = (final in BLACK_ROLES) if side == "black" else (
                final not in BLACK_ROLES and final not in SOLO_ROLES)
            if winners.get(seat_no) is not None and winners[seat_no] != expected:
                report.error(f"{path.name}: место {seat_no}: колонка «Итог» спорит"
                             f" со стороной-победителем ({side})")
    elif side == "neutral":
        marked = [no for no, value in winners.items() if value]
        wrong = [no for no in marked if finals.get(no, seats[no]) not in SOLO_ROLES]
        if wrong:
            report.error(f"{path.name}: победа одиночки — победителем может быть только он,"
                         f" а отмечены места {wrong}")
        if not marked:
            report.error(f"{path.name}: победа одиночки задаётся колонкой «Итог» — она пуста")
    elif side == "none" and any(winners.values()):
        report.error(f"{path.name}: ничья — победителей быть не может")

    scores = _find(sections, "оценки игроков")
    if scores is None:
        report.warn(f"{path.name}: нет раздела «## Оценки игроков» — сервис возьмёт партию"
                    " без оценок, рейтинг не изменится")
    else:
        scored: set[int] = set()
        seen_header = False
        for line in scores:
            if not line.strip().startswith("|") or _is_separator(line):
                continue
            cells = _cells(line)
            if not seen_header:
                seen_header = True
                if len(cells) < 8:
                    report.error(f"{path.name}: в таблице оценок должно быть 8 колонок")
                continue
            if not cells[0].isdigit():
                continue
            seat_no = int(cells[0])
            scored.add(seat_no)
            if seat_no not in seats:
                report.error(f"{path.name}: оценки: места {seat_no} нет в составе")
            for value in cells[2:7]:
                try:
                    number = float(value.replace(",", "."))
                except ValueError:
                    report.error(f"{path.name}: место {seat_no}: «{value}» — не число")
                    continue
                if not 1.0 <= number <= 10.0:
                    report.error(f"{path.name}: место {seat_no}: {number} вне шкалы 1.0–10.0")
        absent = sorted(set(seats) - scored)
        if absent:
            report.error(f"{path.name}: нет оценок у мест {absent} — оценка нужна каждому"
                         " игроку состава, включая ушедшего в первую ночь")

    nominations = _find(sections, "номинации")
    if nominations is not None:
        items = [line for line in nominations if line.strip().startswith("-")]
        if len(items) > 3:
            report.error(f"{path.name}: номинаций {len(items)} — не больше 3")
        for item in items:
            match = re.match(r"^\s*-\s*(?:\S+\s+)?\*\*(.+?)\*\*\s*[—-]\s*№(\d+)\s+([^.:]+)[.:]\s*(.+)$",
                             item)
            if not match:
                report.error(f"{path.name}: номинация не по шаблону"
                             " «- эмодзи **Название** — №N Имя. Причина.»: {}".format(item.strip()))
                continue
            if int(match.group(2)) not in seats:
                report.error(f"{path.name}: номинация на место {match.group(2)},"
                             " которого нет в составе")

    text_sections = [title for title in _sections(text)
                     if not any(word in title for word in
                                ("исход", "оценки игроков", "номинации", "обложка"))]
    if not text_sections:
        report.error(f"{path.name}: ни одного раздела текста статьи")
    low = text.lower()
    for phrase in STOCK_PHRASES:
        if phrase in low:
            report.warn(f"{path.name}: «нейросеточная» связка «{phrase}» — перепиши")
    if re.search(r"[«\"][^«»\"]{40,}[»\"]", text):
        report.warn(f"{path.name}: похоже на дословную цитату игрока — только пересказ")
    return seats


def check_facts(path: Path, seats: dict[int, str], report: Report) -> None:
    text = path.read_text(encoding="utf-8")
    if len(text.encode("utf-8")) > MAX_ATTACHMENT_BYTES:
        report.error(f"{path.name}: больше 4 МиБ — сервис не примет вложение")
    sections = _sections(text)
    roster = _find(sections, "состав a1", "состав a1 для подтверждения")
    if roster is None:
        report.error(f"{path.name}: нет заголовка «## Состав A1 для подтверждения»")
    else:
        rows: dict[int, str] = {}
        for line in roster:
            if not line.strip().startswith("|") or _is_separator(line):
                continue
            cells = _cells(line)
            if not cells[0].isdigit():
                continue
            role = _role(cells[2] if len(cells) > 2 else "")
            if role is None:
                report.error(f"{path.name}: место {cells[0]}: роль «{cells[2]}» не из свода")
                continue
            rows[int(cells[0])] = role
        if not rows:
            report.error(f"{path.name}: таблица состава пуста")
        for seat_no, role in seats.items():
            if seat_no in rows and rows[seat_no] != role:
                report.error(f"{path.name}: место {seat_no}: роль в facts.md ({rows[seat_no]})"
                             f" спорит с review.md ({role})")
        extra = sorted(set(rows) - set(seats)) if seats else []
        if extra:
            report.error(f"{path.name}: места {extra} есть в facts.md, но нет в review.md")
    nights = _find(sections, "ночные действия по листу")
    if nights is None:
        report.error(f"{path.name}: нет заголовка «## Ночные действия по листу»")
        return
    pattern = re.compile(r"^(проверка|выстрел|лечение)\s+№\d+$|^не применимо:.+$|^$")
    seen = 0
    for line in nights:
        if not line.strip().startswith("|") or _is_separator(line):
            continue
        cells = _cells(line)
        if not cells[0].isdigit():
            continue
        seen += 1
        for cell in cells[1:]:
            if not pattern.match(cell.strip().lower()):
                report.error(f"{path.name}: ночь {cells[0]}: клетка «{cell}» не по формату"
                             " «проверка/выстрел/лечение №N» или «не применимо: …»")
    if not seen:
        report.error(f"{path.name}: в таблице ночных действий нет ни одной ночи")


def check_cover(path: Path, report: Report) -> None:
    data = path.read_bytes()
    if len(data) > MAX_COVER_BYTES:
        report.error(f"{path.name}: больше 10 МиБ")
    if data.startswith(b"\x89PNG\r\n\x1a\n") or data[:3] == b"\xff\xd8\xff":
        return
    report.error(f"{path.name}: не PNG и не JPEG — сервис принимает только их")


def check_transcript(path: Path, report: Report) -> None:
    text = path.read_text(encoding="utf-8")
    if len(text.encode("utf-8")) > MAX_ATTACHMENT_BYTES:
        report.error(f"{path.name}: больше 4 МиБ")
    if not re.search(r"^##\s", text, flags=re.MULTILINE):
        report.warn(f"{path.name}: нет фазовых заголовков «## День 1», «## Ночь 1» — расшифровка"
                    " хранится по фазам")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка комплекта разбора до загрузки")
    parser.add_argument("folder", nargs="?", type=Path, help="папка review-<дата>-<номер>/")
    parser.add_argument("--review", type=Path)
    parser.add_argument("--facts", type=Path)
    parser.add_argument("--transcript", type=Path)
    parser.add_argument("--cover", type=Path)
    args = parser.parse_args(argv)

    folder = args.folder
    review = args.review or (folder / "review.md" if folder else None)
    facts = args.facts or (folder / "facts.md" if folder else None)
    transcript = args.transcript or (folder / "transcript.md" if folder else None)
    cover = args.cover
    if cover is None and folder is not None:
        cover = next((folder / name for name in ("banner.png", "banner.jpg", "cover.png")
                      if (folder / name).exists()), None)
    if review is None or not review.exists():
        print("Не найден review.md — укажи папку разбора или --review", file=sys.stderr)
        return 2

    report = Report()
    seats = check_review(review, report)
    if facts and facts.exists():
        check_facts(facts, seats, report)
    else:
        report.warn("facts.md нет — сервис не сможет подтвердить роли и ночные ходы по файлу")
    if transcript and transcript.exists():
        check_transcript(transcript, report)
    else:
        report.warn("transcript.md нет — расшифровка не попадёт в партию")
    if cover and cover.exists():
        check_cover(cover, report)
    else:
        report.warn("баннера нет — обложку придётся добавлять отдельно")

    for text in report.warnings:
        print(f"оговорка: {text}")
    for text in report.errors:
        print(f"ОШИБКА: {text}")
    if report.errors:
        print(f"\nНе годится к загрузке: ошибок {len(report.errors)}.")
        return 1
    print(f"\nКомплект по контракту: ошибок нет, оговорок {len(report.warnings)}.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
