"""Настройки книжного помощника.

Всё живёт рядом с кодом проекта, в bookreader/state/.
Никаких секретов, только технические параметры.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

BASE = Path(__file__).resolve().parent
STATE = BASE / "state"
MODELS = STATE / "models"
CACHE = STATE / "cache"
BOOKS = STATE / "books"
DB_PATH = STATE / "reader.db"
CONFIG_PATH = STATE / "config.json"
WEB = BASE / "web"

# --- значения по умолчанию ---------------------------------------------------

DEFAULTS: dict = {
    # озвучка
    "voice_engine": "silero",        # silero | piper | none
    "voice_model": "v5_5_ru",       # silero: v5_5_ru | v5_4_ru | v5_3_ru | v5_ru
    "voice_speaker": "baya",        # aidar | baya | kseniya | xenia | eugene
    "sample_rate": 48000,           # 8000 | 24000 | 48000
    "piper_model": "ru_RU-irina-medium",
    "sample_rate_play": 22050,      # частота для воспроизведения (экономим память)
    "speed": 1.0,                   # темп чтения
    "pause_after_title": 0.7,       # пауза после заголовка, сек
    "pause_after_paragraph": 0.35,  # пауза между абзацами, сек

    # перевод
    "target_lang": "ru",
    "translate_engine": "nllb",     # nllb | online | off
    "auto_translate": True,         # переводить, если язык книги != target_lang
    # модель NLLB в CTranslate2-формате:
    #   600M — 630 МБ, быстро; 1.3B (OpenNMT/nllb-200-distilled-1.3B-ct2-int8)
    #   — 1,4 ГБ, перевод заметно лучше
    "nllb_repo": "mijuanlo/nllb-200-distilled-600M-ct2-int8",
    "online_endpoints": [
        "https://libretranslate.de",
        "https://translate.argosopentech.com",
        "https://lt.vern.cc",
    ],

    # чтение
    "prefetch": 3,                  # на сколько абзацев вперёд готовить озвучку
    "voice_output_device": None,    # None = устройство по умолчанию
    "host": "127.0.0.1",
    "port": 8810,
    "font_size": 20,
    "theme": "dark",                # dark | light
}

_ENV_PREFIX = "BOOKREADER_"


def _from_env(data: dict) -> dict:
    """BOOKREADER_PORT=8899 python run_bookreader.py web"""
    for key in list(data):
        env = os.environ.get(_ENV_PREFIX + key.upper())
        if env is None:
            continue
        default = DEFAULTS.get(key)
        if isinstance(default, bool):
            data[key] = env.strip().lower() in ("1", "true", "yes", "on")
        elif isinstance(default, int):
            data[key] = int(env)
        elif isinstance(default, float):
            data[key] = float(env)
        else:
            data[key] = env
    return data


class Config:
    def __init__(self, path: Path = CONFIG_PATH):
        self.path = Path(path)
        self.data: dict = dict(DEFAULTS)
        self._mtime = 0.0
        self._read()
        self.data = _from_env(self.data)

    def _read(self) -> None:
        if not self.path.exists():
            return
        try:
            stored = json.loads(self.path.read_text("utf-8"))
            if isinstance(stored, dict):
                self.data.update(stored)
            self._mtime = self.path.stat().st_mtime
        except (OSError, ValueError):
            pass

    def reload(self) -> bool:
        """Перечитать файл, если его правили со стороны.

        Сервер живёт долго: если настройки поменяли из терминала
        (run_bookreader.py config --set ...) или из другой вкладки,
        он обязан их увидеть, а не держать старые в памяти.
        """
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return False
        if mtime <= self._mtime:
            return False
        self.data = dict(DEFAULTS)
        self._read()
        self.data = _from_env(self.data)
        return True

    def __getitem__(self, key: str):
        return self.data[key]

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        if key not in DEFAULTS:
            raise KeyError(f"неизвестный параметр: {key}")
        self.data[key] = value
        self.save()

    def update(self, **kwargs) -> None:
        for key, value in kwargs.items():
            if key in DEFAULTS:
                self.data[key] = value
        self.save()

    def save(self) -> None:
        STATE.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), "utf-8")
        try:
            self._mtime = self.path.stat().st_mtime
        except OSError:
            self._mtime = 0.0

    def as_dict(self) -> dict:
        return dict(self.data)


def ensure_dirs() -> None:
    for folder in (STATE, MODELS, CACHE, BOOKS):
        folder.mkdir(parents=True, exist_ok=True)