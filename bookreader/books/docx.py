"""DOCX — абзацы с их стилями (Heading -> заголовок) и встроенные картинки."""

from __future__ import annotations

from pathlib import Path

from .assets import Assets
from .model import Block, Book, KIND_P, KIND_TITLE, Section, clean_text

STYLE_TITLE = ("heading", "заголовок", "title", "chapter")
# документ с картинками читают целиком, ограничиваем только разбор XML
MAX_CHARS = 4_000_000


def _image_parts(document) -> list[tuple[str, bytes]]:
    parts = []
    for rel_id, rel in document.part.rels.items():
        if "image" not in rel.reltype:
            continue
        try:
            parts.append((rel.target_part.partname, rel.target_part.blob))
        except Exception:
            continue
    return parts


def load(path: Path, assets: Assets) -> Book:
    try:
        import docx  # python-docx
    except ImportError as exc:
        raise RuntimeError("для DOCX нужен python-docx: pip install python-docx") from exc

    path = Path(path)
    stat = path.stat()
    document = docx.Document(str(path))

    core = document.core_properties
    title = (core.title or "").strip() or path.stem.replace("_", " ").strip()
    author = (core.author or "").strip()

    for partname, blob in _image_parts(document):
        assets.save(partname.rsplit("/", 1)[-1], blob)

    sections: list[Section] = []
    current = Section(title="")
    sections.append(current)
    chars = 0

    for paragraph in document.paragraphs:
        text = clean_text(paragraph.text)
        style = (paragraph.style.name or "").lower() if paragraph.style else ""
        if not text:
            continue
        if any(mark in style for mark in STYLE_TITLE):
            if current.blocks or current.title:
                current = Section(title=text)
                sections.append(current)
            else:
                current.title = text
                current.blocks.append(Block(text, KIND_TITLE))
            continue
        current.blocks.append(Block(text, KIND_P))
        chars += len(text)
        if chars > MAX_CHARS:
            break

    # картинки ставим в начало текста: в DOCX их положение внутри абзаца
    # восстановить дорого, а сами картинки читателю нужны
    if assets.map:
        head = Section(title="Иллюстрации", blocks=[
            Block("", KIND_P, list(assets.map.values())[:40])])
        sections.insert(0, head)

    sections = [s for s in sections if s.blocks or s.title]
    return Book(
        path=str(path),
        title=title,
        author=author,
        lang=None,
        sections=sections,
        images=dict(assets.map),
        meta={"format": "docx", "size": stat.st_size, "mtime": int(stat.st_mtime)},
    )