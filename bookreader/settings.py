"""Единая схема настроек: из неё собираются и веб-форма, и терминальный экран.

Иначе правки разъезжаются: одно и то же придётся описывать дважды, и рано
или поздно веб и терминал начнут предлагать разное.

Правило простое: значение меняется сразу в state/config.json, а то, что уже
готовится для чтения, применяется при следующем открытии книги.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import DEFAULTS, Config


@dataclass
class Field:
    key: str                       # ключ в config.json
    label: str                     # как называется по-русски
    kind: str = "text"             # bool | int | float | text | choice
    choices: list = field(default_factory=list)
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    hint: str = ""                 # когда настройка в силе / что требуется
    restart: bool = False          # нужно перезапустить сервер
    choices_from: str = ""         # список вариантов берём из кода (см. сервер)

    @property
    def unit(self) -> str:
        if self.kind == "int":
            return " шт."
        if self.kind == "float":
            return ""
        return ""


@dataclass
class Group:
    key: str
    label: str
    fields: list[Field] = field(default_factory=list)


FIELDS: list[Group] = [
    Group("voice", "Голос", [
        Field("voice_engine", "Движок", "choice",
              ["silero", "piper"],
              hint="silero — живее, piper — легче и без torch"),
        Field("voice_model", "Модель silero", "choice",
              ["v5_5_ru", "v5_4_ru", "v5_3_ru", "v5_2_ru", "v5_ru", "v4_ru"],
              hint="v5 — автоударения и омографы, качество выше"),
        Field("voice_speaker", "Голос", "choice", choices_from="speakers",
              hint="список зависит от выбранной модели"),
        Field("piper_model", "Модель piper", "choice",
              ["ru_RU-irina-medium", "ru_RU-dmitri-medium",
               "ru_RU-denis-medium", "ru_RU-ruslan-medium"],
              hint="работает, только если движок piper"),
        Field("sample_rate", "Частота синтеза", "choice",
              ["8000", "24000", "48000"],
              hint="48000 — самое чистое, но озвучка получается больше"),
        Field("sample_rate_play", "Частота воспроизведения", "choice",
              ["16000", "22050", "24000", "44100"],
              hint="ниже — меньше файлы и быстрее готовятся"),
        Field("voice_output_device", "Звуковая карта", "text",
              hint="пусто = устройство по умолчанию, применяется в терминале"),
    ]),
    Group("reading", "Чтение", [
        Field("speed", "Темп по умолчанию", "float",
              minimum=0.6, maximum=2.0, step=0.05,
              hint="в вебе и терминале стартует с этого значения"),
        Field("pause_after_paragraph", "Пауза между абзацами", "float",
              minimum=0.0, maximum=2.0, step=0.05, hint="секунды"),
        Field("prefetch", "Готовить вперёд", "int",
              minimum=1, maximum=12, hint="сколько абзацев озвучивать заранее"),
        Field("target_lang", "Язык перевода", "choice",
              ["ru", "uk", "en", "de", "fr", "es"],
              hint="на какой язык переводить книги"),
    ]),
    Group("translate", "Перевод", [
        Field("auto_translate", "Переводить книги с другого языка", "bool",
              hint="включается только если язык книги отличается"),
        Field("translate_engine", "Чем переводить", "choice",
              ["nllb", "online", "off"],
              hint="off — читать как есть"),
        Field("nllb_repo", "Модель перевода", "choice",
              ["mijuanlo/nllb-200-distilled-600M-ct2-int8",
               "OpenNMT/nllb-200-distilled-1.3B-ct2-int8"],
              hint="600M — быстро и легко, 1.3B — перевод лучше"),
    ]),
    Group("interface", "Интерфейс", [
        Field("font_size", "Размер шрифта", "int", minimum=14, maximum=34,
              hint="в веб-читалке"),
        Field("theme", "Тема", "choice", ["dark", "light"]),
        Field("port", "Порт веб-читалки", "int", minimum=1024, maximum=65535,
              restart=True, hint="нужен перезапуск сервера"),
    ]),
]


def field_by_key(key: str) -> Field:
    """Описание поля по ключу — нужно, чтобы пересчитать список вариантов."""
    for group in FIELDS:
        for spec in group.fields:
            if spec.key == key:
                return spec
    return Field(key, key, "text")


def speaker_choices(model: str) -> list[str]:
    """Голоса для выбранной модели silero."""
    from .voice import SILERO_MODELS

    return list(SILERO_MODELS.get(model, []))


def resolve(field_spec: Field, config: Config) -> list[str]:
    """Список вариантов для поля, если он вычисляется из кода."""
    if field_spec.choices_from == "speakers":
        return speaker_choices(str(config["voice_model"]))
    return [str(choice) for choice in field_spec.choices]


def coerce(field_spec: Field, value, config: Config | None = None) -> object:
    """Привести значение к типу поля или объяснить, почему не вышло."""
    if field_spec.kind == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "да", "on", "yes")
        return bool(value)
    if field_spec.kind in ("int", "float"):
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"«{value}» — не число") from None
        if field_spec.minimum is not None and number < field_spec.minimum:
            raise ValueError(f"минимум {field_spec.minimum:g}")
        if field_spec.maximum is not None and number > field_spec.maximum:
            raise ValueError(f"максимум {field_spec.maximum:g}")
        return int(round(number)) if field_spec.kind == "int" else round(number, 3)
    if field_spec.kind == "choice":
        text = str(value).strip()
        allowed = resolve(field_spec, config or Config())
        if allowed and text not in allowed:
            raise ValueError(f"варианты: {', '.join(allowed)}")
        return text
    text = str(value).strip()
    if len(text) > 200:
        raise ValueError("слишком длинное значение")
    return text


def describe(config: Config | None = None) -> dict:
    """Схема + текущие значения: это и есть ответ /api/settings."""
    config = config or Config()
    groups = []
    for group in FIELDS:
        items = []
        for spec in group.fields:
            items.append({
                "key": spec.key,
                "label": spec.label,
                "kind": spec.kind,
                "value": config[spec.key],
                "choices": resolve(spec, config),
                "min": spec.minimum,
                "max": spec.maximum,
                "step": spec.step,
                "hint": spec.hint,
                "restart": spec.restart,
            })
        groups.append({"key": group.key, "label": group.label, "fields": items})
    return {"groups": groups, "values": config.as_dict()}


def apply(config: Config, values: dict) -> tuple[dict, list[str]]:
    """Применить порцию настроек. Возвращает (принятые, отклонённые)."""
    known = {spec.key: spec for group in FIELDS for spec in group.fields}
    accepted: dict = {}
    errors: list[str] = []
    for key, value in values.items():
        spec = known.get(key) or (Field(key, key, _kind_of(key))
                                  if key in DEFAULTS else None)
        if spec is None:
            errors.append(f"неизвестный параметр: {key}")
            continue
        try:
            accepted[key] = coerce(spec, value, config)
        except ValueError as exc:
            errors.append(f"{spec.label}: {exc}")
    if accepted:
        config.update(**accepted)
        fix_dangling(config, accepted)
    return accepted, errors


def fix_dangling(config: Config, accepted: dict) -> None:
    """После смены модели голоса старый голос мог ей не принадлежать.

    Молча оставлять несовместимую пару нельзя: синтез упадёт с
    непонятной ошибкой в самый неподходящий момент.
    """
    if not ({"voice_model", "voice_engine"} & accepted.keys()):
        return
    if str(config["voice_engine"]) != "silero":
        return
    speakers = speaker_choices(str(config["voice_model"]))
    if speakers and str(config["voice_speaker"]) not in speakers:
        config.update(voice_speaker=speakers[0])
        accepted["voice_speaker"] = speakers[0]


def _kind_of(key: str) -> str:
    default = DEFAULTS.get(key)
    if isinstance(default, bool):
        return "bool"
    if isinstance(default, int):
        return "int"
    if isinstance(default, float):
        return "float"
    return "text"


def restart_needed(values: dict) -> list[str]:
    """Какие настройки вступят в силу только после перезапуска сервера."""
    known = {spec.key: spec for group in FIELDS for spec in group.fields}
    return [known[key].label for key in values if key in known and known[key].restart]