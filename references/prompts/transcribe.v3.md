# TRANSCRIBE v3 — transcription policy (hash-bound)

Этот файл — версионированный policy-контракт этапа `TRANSCRIBE`, а не свободный prompt.
Dedicated-модель `gemini-3.5-transcribe` **не получает этот текст**: её поведение задаётся
`transcription_config` в запросе. Файл едет в образ, хэшируется и входит в provenance,
чтобы смена политики не могла пройти незаметно.

Отличие от v2: `custom_vocabulary` исключён из запроса. Живой прогон 30.08.2026 —
провайдер отвечает HTTP 400 «custom_vocabulary is incompatible with timestamps»
(в документации ограничение не описано), а word timestamps — несущие для всего
пайплайна. Билдер словаря (`app/analysis/vocabulary.py`) сохранён и возвращается
в контракт новой версией, когда провайдер снимет ограничение.

## Transcription config

- `type = verbatim`; `smart` запрещён (несовместим с diarization и word timestamps).
- `diarization_mode = speaker` — включён всегда.
- `timestamp_granularities = ["word"]`.
- Языковой hint — `ru-RU`.
- `custom_vocabulary` НЕ передаётся (несовместим с timestamps у провайдера).
- При включённой diarization / word timestamps провайдер ограничивает аудио 30 минутами —
  chunk-стратегии `whole` и `30m_overlap60s` с dedicated-моделью несовместимы.

## Speaker labels

- Метка провайдера нормализуется в `SPK_N`; отсутствующая или повреждённая — `UNKNOWN`.
- `SPK_N` — локальный акустический кластер этого чанка: не место, не профиль, не роль.
  `SPK_1` соседнего чанка — не обязательно тот же человек. Глобальную склейку делает
  только `STITCH` по дублированной речи в overlap.
- Восстанавливать метку по содержанию слов запрещено.
- Diarization держит до 8 голосов, attribution для 3+ — experimental: за столом 12–15
  человек, поэтому метка — только supporting evidence для следующих шагов.

## Word timestamps

- Таймстампы провайдера сохраняются как есть (в пределах duration чанка).
- Пересечения слов разных голосов разрешены: одновременная речь — данные, а не дефект.
- Порядок сегментов — по `local_start_ms` неубывающе.

## Review-коды

`review_notes` чанка: `NO_TRANSCRIPT_TEXT`, `WORD_TIMESTAMPS_MISSING`, `NO_DIARIZATION`
(провайдер не вернул ни одной метки), `SPEAKER_LIMIT_REACHED` (кластеров ≥ 8 — вероятна
склейка голосов).

## Приватность / provenance

- `store=false`; raw provider response не хранится, только SHA-256.
- Роли, игровые выводы и данные host sheet на этом этапе не существуют.
- Output привязан к exact media chunk, model version, prompt policy version и
  schema version.
