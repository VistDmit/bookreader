"""Воспроизведение звука на машине: sounddevice с очередью и управлением.

Используется терминальным режимом и командой read. В браузере звук играет
сам браузер, там этот класс не нужен.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

import numpy as np

try:
    import sounddevice as sd
    HAVE_SOUNDDEVICE = True
except Exception:                       # нет portaudio — работаем без звука
    sd = None
    HAVE_SOUNDDEVICE = False

SILENT = 0.0008                        # тишина вместо резкого нуля


class Player:
    """Очередь кусков озвучки с паузой, остановкой и темпом."""

    def __init__(self, rate: int = 22050, device: str | int | None = None,
                 on_event: Callable[[str, int, float], None] | None = None):
        self.rate = rate
        self.device = device
        self.on_event = on_event
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._queue: list[tuple[int, np.ndarray, int]] = []   # (номер абзаца, звук, частота)
        self._ready: list[tuple[int, np.ndarray, int]] = []
        self._current: tuple[int, np.ndarray, int] | None = None
        self._current_x: np.ndarray | None = None
        self._pos = 0.0                 # позиция внутри абзаца, секунды
        self._speed = 1.0
        self._state = "stopped"         # stopped | playing | paused | waiting
        self._stream = None
        self._thread: threading.Thread | None = None
        self._stop_flag = threading.Event()
        self._started_at = 0.0
        self._seek_to = -1

    # --- состояние ----------------------------------------------------------
    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def current_index(self) -> int:
        with self._lock:
            return self._current[0] if self._current else -1

    @property
    def position(self) -> float:
        with self._lock:
            if self._current is None:
                return 0.0
            _, samples, rate = self._current
            return min(self._pos, samples.size / rate)

    @property
    def speed(self) -> float:
        with self._lock:
            return self._speed

    def available(self) -> int:
        with self._lock:
            return len(self._ready) + len(self._queue)

    # --- очередь ------------------------------------------------------------
    def push(self, index: int, samples: np.ndarray, rate: int) -> None:
        """Добавить озвучку абзаца. Можно вызывать из потока подготовки."""
        if samples is None or samples.size == 0:
            return
        with self._lock:
            self._queue.append((index, np.asarray(samples, dtype=np.float32), rate))
            self._cond.notify_all()

    def clear(self) -> None:
        with self._lock:
            self._queue.clear()
            self._ready.clear()
            self._current = None
            self._current_x = None
            self._pos = 0.0

    # --- управление ---------------------------------------------------------
    def play(self) -> None:
        with self._lock:
            if self._state == "playing":
                return
            self._state = "playing"
            self._stop_flag.clear()
        self._ensure_stream()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        self._emit("play", self.current_index, self.position)

    def pause(self) -> None:
        with self._lock:
            if self._state == "playing":
                self._state = "paused"
        self._emit("pause", self.current_index, self.position)

    def resume(self) -> None:
        if self.state == "paused":
            self.play()

    def stop(self) -> None:
        self._stop_flag.set()
        with self._lock:
            self._state = "stopped"
            self._current = None
            self._current_x = None
            self._pos = 0.0
            self._ready.clear()
            self._cond.notify_all()
        self._emit("stop", self.current_index, 0.0)

    def set_speed(self, speed: float) -> None:
        with self._lock:
            self._speed = max(0.5, min(2.5, float(speed)))
        self._emit("speed", self.current_index, self.position)

    def skip_to(self, index: int) -> None:
        """Перейти к абзацу: если его озвучка уже готова — сразу, иначе как дойдём."""
        with self._lock:
            self._seek_to = index
            self._pos = 0.0
            if self._state == "stopped":
                self._state = "playing"
            self._cond.notify_all()
        self._ensure_stream()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def wait_finish(self, timeout: float | None = None) -> bool:
        started = time.time()
        while self.state in ("playing", "waiting"):
            if timeout and time.time() - started > timeout:
                return False
            time.sleep(0.05)
        return True

    # --- внутреннее ---------------------------------------------------------
    def _emit(self, event: str, index: int, position: float) -> None:
        if self.on_event:
            try:
                self.on_event(event, index, position)
            except Exception:
                pass

    def _ensure_stream(self) -> None:
        if not HAVE_SOUNDDEVICE or self._stream is not None:
            return
        try:
            self._stream = sd.OutputStream(
                samplerate=self.rate, channels=1, dtype="float32",
                device=self.device, blocksize=1024, callback=self._callback)
            self._stream.start()
        except Exception as exc:
            self._stream = None
            self._emit("error", -1, 0.0)
            raise RuntimeError(f"звук не открылся: {exc}") from exc

    def _take(self):
        """Взять следующий кусок: сначала нужный по переходу, иначе по порядку."""
        if not self._ready and self._queue:
            self._ready = sorted(self._queue, key=lambda item: item[0])
            self._queue.clear()
        if not self._ready:
            return None
        if self._seek_to >= 0:
            for position, item in enumerate(self._ready):
                if item[0] >= self._seek_to:
                    self._ready.pop(position)
                    self._seek_to = -1
                    return item
            self._seek_to = -1
            return self._ready.pop(0)
        return self._ready.pop(0)

    def _callback(self, outdata, frames, time_info, status):     # noqa: ARG002
        out = np.full((frames, 1), SILENT, dtype=np.float32)
        with self._lock:
            if self._state != "playing":
                outdata[:] = out
                return
            if self._current is None:
                item = self._take()
                if item is None:
                    self._state = "waiting"
                    self._cond.notify_all()
                    outdata[:] = out
                    return
                self._current = item
                self._current_x = np.arange(item[1].size, dtype=np.float32)
                self._pos = 0.0
                self._emit("block", item[0], 0.0)
            index, samples, rate = self._current
            step = self._speed * self.rate / float(rate or self.rate)
            start = self._pos
            positions = start + np.arange(frames, dtype=np.float32) * step
            if positions[-1] >= samples.size:
                self._current = None
                self._current_x = None
                self._pos = 0.0
                self._cond.notify_all()
                fill = int(max(0, (samples.size - start) / step)) if step > 0 else 0
                if fill:
                    take = min(fill, frames)
                    out[:take, 0] = samples[int(start):int(start) + take]
                outdata[:] = out
                return
            out[:, 0] = np.interp(positions, self._current_x, samples,
                                  left=0.0, right=0.0)
            self._pos = float(positions[-1])
            outdata[:] = out

    def _run(self) -> None:
        """Следит за состоянием: ждёт озвучку, снимает ожидание, чинит поток."""
        last_block = None
        while not self._stop_flag.is_set():
            with self._lock:
                state = self._state
                has_ready = bool(self._ready or self._queue)
                current = self._current
            if state == "paused":
                time.sleep(0.05)
                continue
            if state == "waiting" and has_ready and current is None:
                with self._lock:
                    self._state = "playing"
                continue
            if state == "waiting" and not has_ready:
                time.sleep(0.1)
                continue
            with self._lock:
                if self._state == "playing":
                    block = self._current[0] if self._current else None
                    position = self.position
                else:
                    block, position = None, 0.0
            if block is not None and block != last_block:
                last_block = block
            time.sleep(0.05)
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    # --- удобное ------------------------------------------------------------
    def play_all(self, chunks: list[tuple[int, np.ndarray, int]],
                 on_done: Callable[[], None] | None = None) -> None:
        """Сыграть готовый список и дождаться конца (для консольного режима)."""
        self.clear()
        for index, samples, rate in chunks:
            self.push(index, samples, rate)
        self.play()
        while self.state in ("playing", "waiting"):
            if self.state == "waiting" and self.available() == 0:
                break
            time.sleep(0.05)
        if on_done:
            on_done()


def output_devices() -> list[str]:
    """Список устройств вывода для настроек."""
    if not HAVE_SOUNDDEVICE:
        return []
    try:
        return [str(device) for device in sd.query_devices()
                if device.get("max_output_channels", 0) > 0]
    except Exception:
        return []