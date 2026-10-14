"""Конвейер чтения: книга -> перевод -> озвучка -> готовый абзац.

Один фоновый поток готовит абзацы впереди текущего, чтобы читатель
не ждал ни перевода, ни синтеза. Очередь короткая: переводить всю книгу
заранее бессмысленно — на это уйдут часы.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import audio as audio_lib
from . import langid, state
from .books import load as load_book
from .books.model import Book
from .config import Config
from .translate import EngineError, Translator
from .voice import Voice, VoiceError

SAMPLE_CHARS = 20000        # по столько символов смотрим, чтобы понять язык


@dataclass
class Block:
    """Абзац, доведённый до состояния «можно читать»."""

    index: int
    section: int
    kind: str
    original: str
    images: list[str] = field(default_factory=list)
    text: str = ""
    audio: np.ndarray | None = None
    path: Path | None = None
    ready: bool = False
    translated: bool = False
    error: str = ""

    @property
    def seconds(self) -> float:
        if self.audio is None or not self.audio.size:
            return 0.0
        return audio_lib.duration(self.audio, 22050)

    def to_dict(self) -> dict:
        return {
            "i": self.index, "k": self.kind, "t": self.text or self.original,
            "o": self.original, "img": self.images, "ready": self.ready,
            "tr": self.translated, "err": self.error,
            "audio": bool(self.audio is not None and self.audio.size),
        }


class Reader:
    """Держит открытую книгу и готовит абзацы к чтению."""

    def __init__(self, config: Config | None = None):
        self.config = config or Config()
        self.book: Book | None = None
        self.voice: Voice | None = None
        self.translator: Translator | None = None
        self.lang = "ru"
        self.lang_confidence = 0.0
        self.translate_mode = "off"          # off | nllb | online
        self.prefetch = int(self.config["prefetch"])
        self.pause = float(self.config["pause_after_paragraph"])
        self._blocks: dict[int, Block] = {}
        self._order: list[int] = []
        self._queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._pending = set()
        self._last_error = ""
        self.worker_error = ""             # необработанная ошибка в потоке
        self.progress = (0, 0)               # готово, всего (для перевода книги)
        self.progress_label = ""

    # --- открытие книги -----------------------------------------------------
    def open(self, path: str | Path, want_images: bool = True) -> Book:
        self.close()
        book = load_book(path, want_images=want_images)
        state.remember_book(book)

        with self._lock:
            self.book = book
            self._blocks.clear()
            self._order = []
            self._pending = set()
            self._queue = queue.Queue()

        sample = book.plain_text()[:SAMPLE_CHARS]
        detected, confidence = langid.detect(sample, default=book.lang or "en")
        if book.lang and confidence < 0.3:
            detected = book.lang
        self.lang = detected or book.lang or "en"
        self.lang_confidence = confidence

        target = self.config["target_lang"]
        self.translate_mode = "off"
        if self.config["auto_translate"] and langid.needs_translation(self.lang, target):
            if self.config["translate_engine"] == "nllb":
                self.translator = Translator(self.config)
                self.translate_mode = "nllb" if self.translator.ready() else "off"
                if self.translate_mode == "off":
                    self._last_error = self.translator.status()
            elif self.config["translate_engine"] == "online":
                self.translator = Translator(self.config)
                self.translate_mode = "online"

        self.voice = Voice(self.config)
        self._stop.clear()
        self._worker = threading.Thread(target=self._loop, daemon=True,
                                        name="reader")
        self._worker.start()
        return book

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            for index in list(self._pending):
                try:
                    self._queue.put_nowait(None)
                except queue.Full:
                    pass
            self._pending.clear()
        worker = self._worker
        if worker and worker.is_alive():
            worker.join(timeout=2.0)
        self._worker = None

    # --- параметры ----------------------------------------------------------
    def set_translate(self, enabled: bool, engine: str | None = None) -> None:
        self.config.update(auto_translate=enabled)
        if engine:
            self.config.update(translate_engine=engine)
        if self.book:
            self.open(self.book.path, want_images=False)

    def set_voice(self, model: str | None = None, speaker: str | None = None) -> None:
        if model:
            self.config.update(voice_model=model)
        if speaker:
            self.config.update(voice_speaker=speaker)
        self.voice = Voice(self.config)

    # --- абзацы -------------------------------------------------------------
    def block(self, index: int) -> Block:
        """Готовый или заказанный абзац. Подготовка идёт в фоне."""
        with self._lock:
            if index in self._blocks:
                return self._blocks[index]
            if not self._in_book(index):
                return None
            block = self._make_block(index)
            self._blocks[index] = block
            return block

    def _in_book(self, index: int) -> bool:
        book = self.book
        return bool(book) and 0 <= index < book.block_count

    def _make_block(self, index: int) -> Block:
        book = self.book
        section_no, block_no = book.locate(index)
        source = book.sections[section_no].blocks[block_no]
        return Block(index=index, section=section_no, kind=source.kind,
                     original=source.text, images=list(source.images),
                     text=source.text)

    def text_of(self, index: int) -> str:
        with self._lock:
            block = self._blocks.get(index)
        if block is not None:
            return block.text or block.original
        return self._make_block(index).original

    def prepare(self, index: int, block: Block | None = None) -> Block | None:
        """Заказать абзац. Повторный вызов для готового абзаца ничего не делает."""
        with self._lock:
            if index < 0 or index >= (self.book.block_count if self.book else 0):
                return None
            block = block or self.block(index)
            if block.ready:
                return block
            if index in self._pending:
                return block
            self._pending.add(index)
        self._queue.put(index)
        return block

    def prepare_ahead(self, index: int, count: int | None = None) -> None:
        count = count if count is not None else self.prefetch
        if not self.book:
            return
        total = self.book.block_count
        for step in range(1, count + 1):
            self.prepare(min(total - 1, index + step))

    def prefetch_book(self, limit: int | None = None) -> None:
        total = self.book.block_count if self.book else 0
        for index in range(0, min(total, limit or total)):
            self.prepare(index)

    # --- перевод всей книги --------------------------------------------------
    def translate_whole_book(self, limit: int | None = None) -> None:
        """Фоном перевести книгу целиком: для длинных текстов удобно один раз."""
        if self.translate_mode == "off":
            raise EngineError("перевод выключен или модель не скачана")
        self.progress_label = "перевод книги"
        self.prefetch_book(limit)

    @property
    def progress_text(self) -> str:
        done, total = self.progress
        if not total:
            return self.progress_label or ""
        percent = int(done * 100 / total)
        return f"{self.progress_label}: {done}/{total} ({percent}%)"

    # --- фоновый поток ------------------------------------------------------
    def worker_alive(self) -> bool:
        """Жив ли поток подготовки. Если нет — озвучка больше не появится."""
        return bool(self._worker and self._worker.is_alive())

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                index = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if index is None:
                self._queue.task_done()
                break
            try:
                self._handle(index)
            except Exception as exc:        # конвейер не должен умирать
                self.worker_error = f"{type(exc).__name__}: {exc}"
                self._last_error = self.worker_error
                time.sleep(0.2)

    def _handle(self, index: int) -> None:
        """Один абзац из очереди: снять, собрать, отметить готовым."""
        with self._lock:
            self._pending.discard(index)
            block = self._blocks.get(index)
        if block is None:
            self._queue.task_done()
            return
        try:
            self._build(block)
        except (VoiceError, EngineError) as exc:
            block.error = str(exc)
            self._last_error = str(exc)
        except Exception as exc:            # один плохой абзац — не беда
            block.error = f"{type(exc).__name__}: {exc}"
            self._last_error = block.error
        finally:
            block.ready = True
            self.progress = (self._ready_count(),
                             self.book.block_count if self.book else 0)
            self._queue.task_done()

    def _ready_count(self) -> int:
        """Сколько абзацев готово.

        Считать под замком: веб в это время добавляет новые блоки в
        self._blocks, и перебор словаря без замка убивает поток
        (RuntimeError: dictionary changed size during iteration).
        """
        with self._lock:
            return sum(1 for block in self._blocks.values() if block.ready)

    def _build(self, block: Block) -> None:
        """Перевести (если нужно) и озвучить абзац."""
        text = block.original
        if self.translate_mode != "off" and self.translator is not None:
            try:
                translated = self.translator.translate(
                    text, self.lang, self.config["target_lang"])
                if translated:
                    text = translated
                    block.translated = True
            except EngineError as exc:
                block.error = f"перевод: {exc}"
        block.text = text
        if self.voice is None:
            block.ready = True
            return
        pause = self._make_pause(block.kind)
        samples, path = self.voice.speak_with_file(text, pause)
        block.audio = samples
        block.path = path

    def _make_pause(self, kind: str) -> float:
        base = self.pause
        if kind == "title":
            return base * 3.4
        if kind == "subtitle":
            return base * 2.3
        if kind in ("quote", "epigraph"):
            return base * 1.8
        return base

    # --- отчёт --------------------------------------------------------------
    def info(self) -> dict:
        if not self.book:
            return {}
        target = self.config["target_lang"]
        return {
            **self.book.summary(),
            "lang": self.lang,
            "lang_confidence": self.lang_confidence,
            "translate": self.translate_mode,
            "target_lang": target,
            "voice": self.voice.status() if self.voice else "нет",
            "voice_speaker": self.config["voice_speaker"],
            "speakers": self.voice.speakers if self.voice else [],
            "ready_blocks": self._ready_count(),
            "progress": self.progress_text,
            "error": self._last_error,
        }

    def all_blocks(self, limit: int | None = None) -> list[dict]:
        """Все абзацы книги для показа в браузере (текст без озвучки)."""
        if not self.book:
            return []
        result = []
        total = self.book.block_count if limit is None else min(limit, self.book.block_count)
        for index in range(total):
            block = self._blocks.get(index)
            if block is None:
                block = self.block(index)
            result.append(block.to_dict())
        return result

    def ready_map(self) -> list[int]:
        """Номера готовых абзацев."""
        with self._lock:
            return [i for i, b in self._blocks.items() if b.ready]


class PlayerBridge:
    """Связка конвейера с плеером: сам готовит озвучку и сразу играет."""

    def __init__(self, reader: Reader, player, on_block=None):
        self.reader = reader
        self.player = player
        self.on_block = on_block
        self.limit: int | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self, index: int, prefetch: int | None = None,
              limit: int | None = None) -> None:
        self.stop()
        self._stop.clear()
        self.limit = limit
        self.player.clear()
        self._thread = threading.Thread(
            target=self._run, args=(index, prefetch), daemon=True, name="bridge")
        self._thread.start()

    def _run(self, index: int, prefetch: int | None) -> None:
        count = prefetch if prefetch is not None else self.reader.prefetch
        current = index
        total = self.reader.book.block_count
        stop_at = total if self.limit is None else min(total, index + self.limit)
        while not self._stop.is_set() and current < stop_at:
            block = self.reader.prepare(current)
            if block is None:
                break
            deadline = time.time() + 600
            while not block.ready and not self._stop.is_set() and time.time() < deadline:
                time.sleep(0.1)
            if self._stop.is_set():
                break
            if block.error and block.audio is None:
                break
            if block.audio is not None and block.audio.size:
                self.player.push(block.index, block.audio, self.reader.voice.play_rate)
                self.player.play()
                if self.on_block:
                    self.on_block(block.index)
            else:
                state.set_read_position(self.reader.book.key, block.index)
            current += 1
            self.reader.prepare_ahead(current, count)

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None