"""Озвучка: Silero v5 (torch) как основной голос, Piper как запасной.

Модель Silero весит 145 МБ и звучит заметно живее piper-а, но требует torch.
Piper уже стоит на машине и включается, если torch нет.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np

from . import audio, state
from .config import MODELS, CACHE, Config

TORCH_THREADS = 8           # физических ядер 8; 16 потоков дают меньше
SILERO_URL = "https://models.silero.ai/models/tts/ru/{model}.pt"
PIPER_URL = ("https://huggingface.co/rhasspy/piper-voices/resolve/main/"
             "ru/ru_RU/{voice}/{quality}/{name}.onnx")

# модель -> доступные голоса (из models.yml silero-models)
SILERO_MODELS = {
    "v5_5_ru": ["aidar", "baya", "kseniya", "xenia", "eugene"],
    "v5_4_ru": ["aidar", "baya", "kseniya", "xenia"],
    "v5_3_ru": ["aidar", "baya", "kseniya", "xenia", "eugene"],
    "v5_2_ru": ["aidar", "baya", "kseniya", "xenia", "eugene"],
    "v5_ru": ["aidar", "baya", "kseniya", "xenia", "eugene"],
    "v4_ru": ["aidar", "baya", "kseniya", "xenia"],
}
PIPER_VOICES = ("irina", "dmitri", "denis", "ruslan")
PIPER_QUALITY = ("medium", "x_low", "low")

# длинный текст режем: так модель звучит ровнее, а ошибка не съедает полкниги
CHUNK_CHARS = 600


class VoiceError(RuntimeError):
    pass


def _download(url: str, target: Path, label: str) -> Path:
    """Скачать файл, если его нет. Сеть на машине нестабильна — с понятной ошибкой."""
    if target.exists() and target.stat().st_size > 0:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=60) as response, tmp.open("wb") as out:
            shutil.copyfileobj(response, out, length=1 << 20)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise VoiceError(f"не скачался {label}: {exc}") from exc
    tmp.replace(target)
    return target


def model_path(model: str) -> Path:
    return MODELS / f"{model}.pt"


def download_silero(model: str = "v5_5_ru") -> Path:
    if model not in SILERO_MODELS:
        raise VoiceError(f"неизвестная модель silero: {model}")
    return _download(SILERO_URL.format(model=model), model_path(model), model)


def piper_paths(voice: str, quality: str) -> tuple[Path, Path]:
    name = f"ru_RU-{voice}-{quality}"
    base = MODELS / "piper"
    return base / f"{name}.onnx", base / f"{name}.onnx.json"


def download_piper(voice: str = "irina", quality: str = "medium") -> Path:
    model, config = piper_paths(voice, quality)
    if not model.exists():
        _download(PIPER_URL.format(voice=voice, quality=quality, name=f"ru_RU-{voice}-{quality}"),
                  model, model.name)
    if not config.exists():
        _download(PIPER_URL.format(voice=voice, quality=quality,
                                   name=f"ru_RU-{voice}-{quality}") + ".json",
                  config, config.name)
    return model


class SileroEngine:
    """Silero TTS v5: torch.package, пять голосов, SSML-паузы."""

    name = "silero"
    supports_speed = True

    def __init__(self, model: str = "v5_5_ru", speaker: str = "baya",
                 rate: int = 48000, threads: int = TORCH_THREADS):
        self.model = model
        self.speaker = speaker
        self.rate = rate
        self.threads = threads
        self._model = None
        self._lock = threading.Lock()

    @property
    def model_file(self) -> Path:
        return model_path(self.model)

    def ready(self) -> bool:
        return self.model_file.exists()

    def status(self) -> str:
        return (f"silero {self.model} голос {self.speaker}, {self.rate} Гц"
                if self.ready() else "модель silero не скачана")

    def load(self):
        if self._model is not None:
            return self._model
        if not self.ready():
            raise VoiceError("модель silero не скачана: python run_bookreader.py models")
        import torch

        torch.set_num_threads(self.threads)
        started = time.time()
        importer = torch.package.PackageImporter(str(self.model_file))
        model = importer.load_pickle("tts_models", "model")
        model.to(torch.device("cpu"))
        self._torch = torch
        self._model = model
        self._load_seconds = time.time() - started
        return model

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        model = self.load()
        kwargs = dict(speaker=self.speaker, sample_rate=self.rate)
        if self.model.startswith("v5"):
            kwargs.update(put_accent=True, put_yo=True,
                          put_stress_homo=True, put_yo_homo=True)
        else:
            kwargs.update(put_accent=True, put_yo=True)
        with self._lock:
            data = model.apply_tts(text=text, **kwargs)
        return np.asarray(data, dtype=np.float32), self.rate

    def synthesize_ssml(self, ssml: str) -> tuple[np.ndarray, int]:
        """SSML умеет паузы <break> — используем для пауз между абзацами."""
        model = self.load()
        with self._lock:
            data = model.apply_tts(ssml_text=ssml, speaker=self.speaker,
                                   sample_rate=self.rate)
        return np.asarray(data, dtype=np.float32), self.rate


class PiperEngine:
    """Piper: быстрый и лёгкий, но голос ровнее не выйдет."""

    name = "piper"
    supports_speed = False

    def __init__(self, voice: str = "irina", quality: str = "medium"):
        self.voice = voice
        self.quality = quality
        self.binary = Path.home() / ".local/piper/piper"
        self._rate = 22050

    def ready(self) -> bool:
        return piper_paths(self.voice, self.quality)[0].exists() and self.binary.exists()

    def status(self) -> str:
        return (f"piper ru_RU-{self.voice}-{self.quality}" if self.ready()
                else "модель piper не скачана")

    def load(self):
        return self.ready()

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        if not self.ready():
            raise VoiceError("модель piper не скачана")
        with self.binary.open("rb"):        # битый бинарник должен ругаться сразу
            pass
        proc = subprocess.run(
            [str(self.binary), "-m", str(piper_paths(self.voice, self.quality)[0]),
             "-q", "--output_raw"],
            input=text.encode("utf-8"), stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=300)
        if proc.returncode != 0 or not proc.stdout:
            raise VoiceError("piper не отработал")
        samples = np.frombuffer(proc.stdout, dtype="<i2").astype(np.float32) / 32768.0
        return samples, self._rate


PRONOUNCEABLE = re.compile(r"[\w\d]", re.UNICODE)


def is_pronounceable(text: str) -> bool:
    """Есть ли что читать.

    Модель падает с пустым ValueError на разделителях вроде "***",
    точках и прочих кусках без букв и цифр.
    """
    return bool(PRONOUNCEABLE.search(text or ""))


def split_for_speech(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    """Резать текст по точкам, а не по символам: голос не спотыкается."""
    from .translate import split_sentences

    sentences = split_sentences(text, limit=limit)
    return sentences or ([text.strip()] if text.strip() else [])


class Voice:
    """Голос читателя: кэш на диске, склейка кусков и паузы."""

    def __init__(self, config: Config | None = None):
        self.config = config or Config()
        self.engine_name = self.config["voice_engine"]
        self.speech_rate = int(self.config["sample_rate"])
        self.play_rate = int(self.config["sample_rate_play"])
        self.speakers = SILERO_MODELS.get(self.config["voice_model"], [])
        if self.engine_name == "silero":
            self.engine = SileroEngine(
                self.config["voice_model"], self.config["voice_speaker"],
                self.speech_rate)
        elif self.engine_name == "piper":
            self.engine = PiperEngine(self.config["piper_model"].split("-")[1]
                                      if "-" in self.config["piper_model"] else "irina")
        else:
            raise VoiceError(f"неизвестный движок озвучки: {self.engine_name}")
        self._lock = threading.Lock()

    # --- состояние ----------------------------------------------------------
    @property
    def speaker(self) -> str:
        return getattr(self.engine, "speaker", self.config["voice_speaker"])

    def status(self) -> str:
        return self.engine.status()

    def ready(self) -> bool:
        return self.engine.ready()

    def download(self) -> str:
        if self.engine_name == "silero":
            path = download_silero(self.config["voice_model"])
            return f"скачана модель {path.name} ({path.stat().st_size // 1048576} МБ)"
        voice = self.config["piper_model"]
        parts = voice.split("-")
        path = download_piper(parts[1] if len(parts) > 2 else "irina",
                              parts[2] if len(parts) > 2 else "medium")
        return f"скачана модель {path.name}"

    # --- синтез -------------------------------------------------------------
    def speak_with_file(self, text: str, pause: float = 0.35,
                        use_cache: bool = True) -> tuple[np.ndarray, Path | None]:
        """Абзац -> звук для воспроизведения и путь к файлу (если записали)."""
        text = (text or "").strip()
        if not text:
            return audio.silence(pause, self.play_rate), None

        key = f"{self.speaker}|{self.engine_name}|{self.play_rate}"
        if use_cache:
            cached = state.get_audio(text, key)
            if cached:
                samples, rate = audio.read_wav(cached)
                if abs(rate - self.play_rate) < 1:
                    return samples, Path(cached)

        samples, rate = self._synthesize(text)
        samples = audio.fade(audio.normalize(samples), rate)
        if rate != self.play_rate:
            samples = audio.resample(samples, rate, self.play_rate)
            rate = self.play_rate
        samples = audio.concat([samples, audio.silence(pause, rate)])

        path = None
        if use_cache:
            folder = CACHE / "audio"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{state.text_hash(text + key)}.wav"
            try:
                audio.write_wav(path, samples, rate)
                state.put_audio(text, key, path, audio.duration(samples, rate))
            except OSError:
                path = None
        return samples, path

    def speak(self, text: str, pause: float = 0.35,
              use_cache: bool = True) -> np.ndarray:
        return self.speak_with_file(text, pause, use_cache)[0]

    def _synthesize(self, text: str) -> tuple[np.ndarray, int]:
        chunks = [chunk for chunk in split_for_speech(text)
                  if is_pronounceable(chunk)]
        if not chunks:
            return np.zeros(0, dtype=np.float32), self.play_rate
        parts = []
        rate = self.speech_rate
        for chunk in chunks:
            try:
                samples, chunk_rate = self.engine.synthesize(chunk)
            except (ValueError, RuntimeError):
                # кусок не прочитан (пустая строка, странный символ) —
                # читаем остальное, а не роняем весь абзац
                continue
            rate = chunk_rate
            parts.append(samples)
            parts.append(audio.silence(0.12, chunk_rate))
        return audio.concat(parts), rate

    def preview(self, text: str = "Проверка голоса. Так звучит чтение вслух.",
                seconds: float = 2.0) -> np.ndarray:
        """Короткий кусок для проверки голоса."""
        samples, rate = self.engine.synthesize(text)
        samples = audio.normalize(samples, 0.8)
        if rate != self.play_rate:
            samples = audio.resample(samples, rate, self.play_rate)
        limit = int(seconds * self.play_rate)
        return samples[:limit] if samples.size > limit else samples


def engines_report() -> list[dict]:
    """Что на машине: какие голоса готовы, какие надо качать."""
    report = []
    for name, speakers in SILERO_MODELS.items():
        path = model_path(name)
        report.append({
            "engine": "silero", "model": name, "ready": path.exists(),
            "size_mb": path.stat().st_size // 1048576 if path.exists() else 0,
            "speakers": speakers,
            "note": "лучшее качество, нужен torch" if name.startswith("v5") else "",
        })
    for voice in PIPER_VOICES:
        path, _ = piper_paths(voice, "medium")
        report.append({
            "engine": "piper", "model": f"ru_RU-{voice}-medium", "ready": path.exists(),
            "size_mb": path.stat().st_size // 1048576 if path.exists() else 0,
            "speakers": ["0"], "note": "лёгкий запасной голос",
        })
    return report


def save_preview(path: Path, samples: np.ndarray, rate: int) -> Path:
    audio.write_wav(path, samples, rate)
    return path