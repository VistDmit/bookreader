"""Модель книги: разделы, абзацы, картинки.

Любой формат (fb2, epub, txt, pdf, docx) приводится к одному виду,
дальше с книгой работает только этот код.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

# вид абзаца влияет на паузу при чтении и на оформление в браузере
KIND_P = "p"
KIND_TITLE = "title"
KIND_SUBTITLE = "subtitle"
KIND_EPIGRAPH = "epigraph"
KIND_QUOTE = "quote"

# пауза после абзаца, секунды
PAUSE_AFTER = {
    KIND_P: 0.35,
    KIND_QUOTE: 0.6,
    KIND_EPIGRAPH: 0.6,
    KIND_SUBTITLE: 0.8,
    KIND_TITLE: 1.2,
}

WHITESPACE = re.compile(r"[ \t  -﻿]+")
NEWLINES = re.compile(r"\s*\n\s*")


def clean_text(text: str) -> str:
    """Мягкая чистка текста без потери слов."""
    if not text:
        return ""
    text = text.replace("\x00", " ")
    text = NEWLINES.sub(" ", text)
    text = WHITESPACE.sub(" ", text)
    return text.strip()


@dataclass(slots=True)
class Block:
    """Абзац или заголовок. images — файлы, которые показать рядом."""

    text: str
    kind: str = KIND_P
    images: list[str] = field(default_factory=list)

    def pause_after(self, base: float = 0.35) -> float:
        """Пауза после абзаца: своя для заголовка, иначе общая."""
        if self.kind == KIND_TITLE:
            return base * 3.4
        if self.kind == KIND_SUBTITLE:
            return base * 2.3
        if self.kind in (KIND_QUOTE, KIND_EPIGRAPH):
            return base * 1.8
        return base


@dataclass(slots=True)
class Section:
    title: str
    blocks: list[Block] = field(default_factory=list)
    level: int = 1


@dataclass
class Book:
    path: str
    title: str = ""
    author: str = ""
    lang: str | None = None
    sections: list[Section] = field(default_factory=list)
    images: dict[str, str] = field(default_factory=dict)  # имя в книге -> файл
    meta: dict = field(default_factory=dict)

    # --- содержимое ---------------------------------------------------------
    def blocks(self):
        """Плоский список блоков по всей книге."""
        for section in self.sections:
            yield from section.blocks

    @property
    def block_count(self) -> int:
        return sum(len(s.blocks) for s in self.sections)

    @property
    def image_list(self) -> list[str]:
        return list(self.images.values())

    def char_count(self) -> int:
        return sum(len(b.text) for b in self.blocks())

    def plain_text(self) -> str:
        return "\n".join(b.text for b in self.blocks())

    def index_of(self, section: int, block: int) -> int:
        """Индекс блока в плоском списке."""
        pos = 0
        for i, sec in enumerate(self.sections):
            if i == section:
                return pos + block
            pos += len(sec.blocks)
        return pos

    def locate(self, flat: int) -> tuple[int, int]:
        """Глобальный номер абзаца -> (раздел, номер в разделе).

        Номер за пределами книги прижимается к краю: промах на один
        абзац не должен превращаться в IndexError у вызывающего.
        """
        if not self.sections:
            return 0, 0
        flat = max(0, min(flat, max(0, self.block_count - 1)))
        pos = 0
        for i, section in enumerate(self.sections):
            size = len(section.blocks)
            if size and pos + size > flat:
                return i, min(flat - pos, size - 1)
            pos += size
        last = len(self.sections) - 1
        return last, max(0, len(self.sections[last].blocks) - 1)

    # --- идентификация ------------------------------------------------------
    @property
    def key(self) -> str:
        """Ключ книги в базе и в кэше: путь + размер + дата."""
        raw = f"{self.path}|{self.meta.get('size', 0)}|{self.meta.get('mtime', 0)}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def summary(self) -> dict:
        return {
            "key": self.key,
            "path": self.path,
            "title": self.title,
            "author": self.author,
            "lang": self.lang,
            "blocks": self.block_count,
            "chars": self.char_count(),
            "images": len(self.images),
            "sections": len(self.sections),
            "format": self.meta.get("format", ""),
        }

    def to_dict(self) -> dict:
        """Для веба: разделы с блоками."""
        return {
            "key": self.key,
            "path": self.path,
            "title": self.title,
            "author": self.author,
            "lang": self.lang,
            "meta": self.meta,
            "images": self.images,
            "blocks": self.block_count,
            "sections": [
                {
                    "title": s.title,
                    "level": s.level,
                    "blocks": [
                        {"t": b.text, "k": b.kind, "i": b.images}
                        for b in s.blocks
                    ],
                }
                for s in self.sections
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Book":
        return cls(
            path=data["path"],
            title=data.get("title", ""),
            author=data.get("author", ""),
            lang=data.get("lang"),
            sections=[
                Section(
                    title=sec.get("title", ""),
                    level=sec.get("level", 1),
                    blocks=[Block(b["t"], b.get("k", KIND_P), list(b.get("i", [])))
                            for b in sec.get("blocks", [])],
                )
                for sec in data.get("sections", [])
            ],
            images=data.get("images", {}),
            meta=data.get("meta", {}),
        )


def guess_title(path) -> str:
    return path.stem.replace("_", " ").replace("-", " ").strip()