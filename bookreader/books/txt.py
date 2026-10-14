"""TXT — простой текст. Главы угадываются по заголовкам."""

from __future__ import annotations

import re
from pathlib import Path

from .assets import Assets
from .model import Block, Book, KIND_P, KIND_TITLE, Section, clean_text

CHAPTER_WORDS = (
    "глава", "часть", "книга", "пролог", "эпилог", "интерлюдия", "послесловие",
    "введение", "глава I", "chapter", "part", "book", "prologue", "epilogue",
    "act", "introduction", "chapter one",
)

PATTERNS = (
    re.compile(r"^\s*(глава|часть|книга)\s+([0-9]+|[IVXLC]+|[а-яё]+)\b", re.I),
    re.compile(r"^\s*(chapter|part|book|prologue|epilogue|act)\s+"
               r"([0-9]+|[IVXLC]+|[a-z]+)\b", re.I),
    re.compile(r"^\s*((?:глава|часть|chapter|part)\s+)?([0-9]{1,3})\s*[.)]?\s*$", re.I),
)


def _is_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > 80:
        return False
    low = stripped.lower().rstrip(".")
    if any(low.startswith(w) for w in CHAPTER_WORDS):
        return len(stripped.split()) <= 6
    if any(p.match(stripped) for p in PATTERNS):
        return True
    # строка капсом без знаков конца — часто заголовок
    letters = [c for c in stripped if c.isalpha()]
    if len(letters) >= 3 and all(c.isupper() for c in letters) and len(stripped) <= 60:
        return not stripped.endswith((".", "!", "?"))
    return False


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1251", "koi8-r", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def load(path: Path, assets: Assets) -> Book:
    path = Path(path)
    stat = path.stat()
    text = _decode(path.read_bytes())
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    sections: list[Section] = [Section(title="")]
    current = sections[0]
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            joined = clean_text(" ".join(buffer))
            if joined:
                current.blocks.append(Block(joined, KIND_P))
            buffer.clear()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        if _is_heading(stripped):
            flush()
            if current.blocks or current.title:
                sections.append(Section(title=clean_text(stripped)))
            else:
                current.title = clean_text(stripped)
            continue
        buffer.append(stripped)
    flush()

    sections = [s for s in sections if s.blocks or s.title]
    return Book(
        path=str(path),
        title=path.stem.replace("_", " ").replace("-", " ").strip(),
        author="",
        lang=None,
        sections=sections,
        images={},
        meta={"format": "txt", "size": stat.st_size, "mtime": int(stat.st_mtime)},
    )