#!/usr/bin/env python3
"""Точка входа книжного помощника.

    python run_bookreader.py web ~/книга.fb2
    python run_bookreader.py read ~/книга.fb2
    python run_bookreader.py doctor

Вся работа — в bookreader/launcher.py: он перезапускает интерпретатор
с -X gil=0, потому что ctranslate2 и lxml при импорте возвращают GIL
обратно и останавливают потоки озвучки, перевода и веба.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bookreader.launcher import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())