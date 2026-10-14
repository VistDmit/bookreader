"""Запуск с отключённым GIL.

В сборке CPython 3.14t GIL по умолчанию выключен, но импорт модуля без
пометки «безопасно без GIL» (ctranslate2, lxml, часть модулей torch)
ВКЛЮЧАЕТ его обратно. Тогда озвучка, перевод и веб-сервер начинают стоять
друг другу и предупреждение уходит в лог.

Поэтому любой вход — и run_bookreader.py, и команда bookreader из
консоли — сначала один раз перезапускает интерпретатор с -X gil=0.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

GUARD = "BOOKREADER_NO_REEXEC"


def _is_free_threading() -> bool:
    """Сборка без GIL? У обычной сборки нет sys._is_gil_enabled."""
    return hasattr(sys, "_is_gil_enabled")


def _needs_free_threading() -> bool:
    if not _is_free_threading():
        return False
    return bool(sys._is_gil_enabled()) or "gil" not in sys._xoptions


def _reexec() -> None:
    """Перезапустить себя с -X gil=0 (один раз, метка в переменной окружения)."""
    if os.environ.get(GUARD):
        return
    entry = Path(__file__).resolve().parent.parent / "run_bookreader.py"
    script = str(entry) if entry.exists() else str(Path(sys.argv[0]).resolve())
    env = dict(os.environ, **{GUARD: "1"})
    os.execve(sys.executable, [sys.executable, "-X", "gil=0", script,
                               *sys.argv[1:]], env)


def main(argv: list[str] | None = None) -> int:
    if _needs_free_threading():
        _reexec()

    if _is_free_threading():
        # 8 физических ядер: 16 потоков дают меньший выигрыш
        os.environ.setdefault("OMP_NUM_THREADS", "8")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

    from .cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())