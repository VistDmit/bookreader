"""Работа со звуком: WAV в памяти, тишина, склейка, темп.

Своё намеренно, а не numpy-библиотеки: нужен только 16-битный моно-WAV,
а лишние зависимости на этой машине дороже.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

SAMPLE_WIDTH = 2          # байт на отсчёт (PCM 16 бит)


def to_int16(samples: np.ndarray) -> np.ndarray:
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2")


def from_int16(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


def silence(seconds: float, rate: int) -> np.ndarray:
    return np.zeros(max(0, int(seconds * rate)), dtype=np.float32)


def read_wav(path: str | Path) -> tuple[np.ndarray, int]:
    """Прочитать WAV. Поддерживаются только PCM 8/16/32 бит без сжатия."""
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    if width == SAMPLE_WIDTH:
        data = from_int16(frames)
    elif width == 1:
        data = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 4:
        data = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"не понимаю WAV шириной {width} байт")
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data, rate


def write_wav(path: str | Path, samples: np.ndarray, rate: int) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with wave.open(str(tmp), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(SAMPLE_WIDTH)
        handle.setframerate(rate)
        handle.writeframes(to_int16(samples).tobytes())
    tmp.replace(path)


def resample(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Линейная передискретизация: медленнее, зато без scipy и без артефактов."""
    if src_rate == dst_rate or samples.size == 0:
        return samples
    length = int(round(samples.size * dst_rate / src_rate))
    if length <= 0:
        return np.zeros(0, dtype=np.float32)
    positions = np.linspace(0, samples.size - 1, length, dtype=np.float64)
    return np.interp(positions, np.arange(samples.size), samples).astype(np.float32)


def fade(samples: np.ndarray, rate: int, length: float = 0.012) -> np.ndarray:
    """Мягкие края, чтобы не было щелчков между абзацами."""
    count = int(length * rate)
    if count <= 1 or samples.size < count * 2:
        return samples
    ramp = np.linspace(0.0, 1.0, count, dtype=np.float32)
    out = samples.copy()
    out[:count] *= ramp
    out[-count:] *= ramp[::-1]
    return out


def normalize(samples: np.ndarray, peak: float = 0.89) -> np.ndarray:
    """Поднять тихий кусок до нормальной громкости."""
    top = float(np.max(np.abs(samples))) if samples.size else 0.0
    if top < 1e-4:
        return samples
    return (samples * (peak / top)).astype(np.float32)


def concat(parts: list[np.ndarray]) -> np.ndarray:
    parts = [p for p in parts if p is not None and p.size]
    if not parts:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(parts).astype(np.float32)


def duration(samples: np.ndarray, rate: int) -> float:
    return samples.size / float(rate or 1)


def peak_level(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    return float(np.max(np.abs(samples)))