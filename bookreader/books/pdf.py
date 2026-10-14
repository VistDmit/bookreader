"""PDF — текст со страниц через pypdf, картинки вытаскиваем, если умеет.

Вёрстку не восстанавливаем: главы определяются по тексту заголовков,
а если не выходит — режем страницы на абзацы.
"""

from __future__ import annotations

from pathlib import Path

from .assets import Assets
from .model import Block, Book, KIND_P, KIND_TITLE, Section, clean_text
from .txt import _is_heading

PAGE_CHARS = 2600        # на страницу столько символов максимум


def _pages_text(reader, limit: int | None = None) -> list[str]:
    pages = []
    total = len(reader.pages)
    for index, page in enumerate(reader.pages):
        if limit is not None and index >= limit:
            break
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")
        if index and index % 25 == 0:
            pages.append("")     # разделитель страниц
    return pages


def _extract_images(reader, assets: Assets) -> None:
    """pypdf умеет отдавать картинки страницы; молча пропускаем неудачные."""
    for page_no, page in enumerate(reader.pages[:60]):
        try:
            images = list(page.images)
        except Exception:
            continue
        for image in images:
            try:
                data = image.data
            except Exception:
                continue
            if data and len(data) > 600:
                assets.save(f"page{page_no + 1}_{image.name}", data)


def load(path: Path, assets: Assets, want_images: bool = True) -> Book:
    try:
        from pypdf import PdfReader
    except ImportError as exc:                      # pypdf не установлен
        raise RuntimeError("для PDF нужен pypdf: pip install pypdf") from exc

    path = Path(path)
    stat = path.stat()
    reader = PdfReader(str(path))
    meta_title = ""
    try:
        info = reader.metadata or {}
        meta_title = (info.get("/Title") or "").strip()
    except Exception:
        pass

    if want_images:
        try:
            _extract_images(reader, assets)
        except Exception:
            pass

    pages = _pages_text(reader)
    sections: list[Section] = []
    current = Section(title="")
    sections.append(current)
    buffer: list[str] = []

    def flush() -> None:
        joined = clean_text(" ".join(buffer))
        if joined:
            current.blocks.append(Block(joined, KIND_P))
        buffer.clear()

    for page in pages:
        if not page:
            continue
        for line in page.replace("\r", "").split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            if _is_heading(stripped):
                flush()
                current = Section(title=clean_text(stripped))
                sections.append(current)
                continue
            buffer.append(stripped)
            if sum(len(b) for b in buffer) > PAGE_CHARS:
                flush()
        flush()

    sections = [s for s in sections if s.blocks or s.title]
    title = meta_title or path.stem.replace("_", " ").strip()
    return Book(
        path=str(path),
        title=title,
        author="",
        lang=None,
        sections=sections,
        images=dict(assets.map),
        meta={"format": "pdf", "size": stat.st_size, "mtime": int(stat.st_mtime),
              "pages": len(reader.pages)},
    )