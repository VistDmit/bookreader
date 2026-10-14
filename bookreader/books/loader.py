"""Загрузка книги любого поддерживаемого формата.

Формат определяем по расширению, а если расширение неизвестно —
по содержимому файла (zip с .opf = epub, zip с .xml = fb2).
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

from . import docx as docx_parser
from . import epub as epub_parser
from . import fb2 as fb2_parser
from . import pdf as pdf_parser
from . import txt as txt_parser
from .assets import Assets
from .model import Block, Book

SUPPORTED = {".fb2", ".fb2.zip", ".epub", ".txt", ".text", ".pdf", ".docx"}
ZIP_LIKE = {".zip", ".fb2", ".fb2.zip", ".epub"}
MAX_BLOCK_CHARS = 1200      # длиннее — режем на части для показа и озвучки


def book_key(path: Path) -> str:
    """Ключ книги: путь + размер + дата изменения. Меняешь файл — меняется ключ."""
    path = Path(path)
    try:
        stat = path.stat()
        raw = f"{path.resolve()}|{stat.st_size}|{int(stat.st_mtime)}"
    except OSError:
        raw = str(path)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def assets_dir(key: str) -> Path:
    from ..config import BOOKS
    return BOOKS / key / "images"


SUFFIXES_BY_FORMAT = {"fb2": (".fb2",), "txt": (".txt", ".text"),
                      "docx": (".docx",), "pdf": (".pdf",)}


def zip_members(path: Path) -> list[str]:
    try:
        with zipfile.ZipFile(path) as zf:
            return [name for name in zf.namelist() if not name.endswith("/")]
    except (zipfile.BadZipFile, OSError):
        return []


def format_in_zip(path: Path) -> tuple[str, str | None] | None:
    """Что лежит внутри .zip: (формат, имя файла). None — разбираться не с чем."""
    members = zip_members(path)
    if not members:
        return None
    if any(name.lower().endswith(".opf") for name in members):
        return "epub", None                      # epub читается как есть
    if any(name.lower().endswith(".xml") and "/" not in name for name in members):
        return "fb2", None                      # fb2 в zip читается как есть
    for fmt, suffixes in SUFFIXES_BY_FORMAT.items():
        found = [name for name in members if name.lower().endswith(suffixes)]
        if found:
            return fmt, max(found, key=len)
    return None


def detect_format(path: Path) -> str:
    path = Path(path)
    name = path.name.lower()
    if name.endswith(".fb2.zip"):
        return "fb2"
    suffix = path.suffix.lower()
    if suffix == ".epub":
        return "epub"
    if suffix == ".fb2":
        return "fb2"
    if suffix in (".txt", ".text"):
        return "txt"
    if suffix == ".pdf":
        return "pdf"
    if suffix == ".docx":
        return "docx"
    if suffix == ".zip":
        found = format_in_zip(path)
        return found[0] if found else "txt"
    # неизвестное расширение: пробуем понять по содержимому
    try:
        head = path.read_bytes()[:4096]
    except OSError:
        return "txt"
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
                if any(n.lower().endswith(".opf") for n in names):
                    return "epub"
                if any(n.lower().endswith(".xml") for n in names):
                    return "fb2"
                return "txt"
        except zipfile.BadZipFile:
            return "txt"
    if head.startswith(b"%PDF"):
        return "pdf"
    if head[:4] == b"\xd0\xcf\x11\xe0":
        return "doc"          # старый формат Word, текст не вытащим
    return "txt"


def _split_long_blocks(book: Book, limit: int = MAX_BLOCK_CHARS) -> Book:
    """Слишком длинные абзацы режем на куски.

    В epub и pdf одна глава иногда приходит одним куском в десятки тысяч
    знаков: такой абзац невозможно ни показать, ни слушать по частям.
    """
    from ..translate import split_sentences

    for section in book.sections:
        chunks: list[Block] = []
        for block in section.blocks:
            if len(block.text) <= limit:
                chunks.append(block)
                continue
            pieces = split_sentences(block.text, limit=limit)
            for index, piece in enumerate(pieces):
                chunks.append(Block(piece, block.kind,
                                    list(block.images) if index == 0 else []))
        section.blocks = chunks
    return book


def _unwrap_zip(path: Path, key: str) -> Path:
    """Книга внутри .zip: достаём наружу, парсеруем обычный файл."""
    from ..config import CACHE

    found = format_in_zip(path)
    if not found or found[1] is None:
        return path
    member = found[1]
    target_dir = CACHE / "zip"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{key}{Path(member).suffix}"
    if target.exists() and target.stat().st_size > 0:
        return target
    with zipfile.ZipFile(path) as zf:
        target.write_bytes(zf.read(member))
    return target


def load(path: str | Path, want_images: bool = True) -> Book:
    """Прочитать книгу и вернуть Book. Картинки лягут в state/books/<ключ>/images."""
    path = Path(path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"файла нет: {path}")
    fmt = detect_format(path)
    if fmt == "doc":
        raise RuntimeError("старый .doc не поддерживается, пересохраните в .docx")

    key = book_key(path)
    folder = assets_dir(key)
    assets = Assets(folder)
    if not want_images:
        assets.save = lambda *a, **k: ""      # noqa: E731  картинки не нужны

    loader = {
        "fb2": fb2_parser.load,
        "epub": epub_parser.load,
        "txt": txt_parser.load,
        "pdf": pdf_parser.load,
        "docx": docx_parser.load,
    }[fmt]

    source = path
    if path.suffix.lower() == ".zip":
        source = _unwrap_zip(path, key)

    book = loader(source, assets)
    if source != path:
        book.path = str(path)              # показываем исходный файл, а не распаковку
        book.meta["source_file"] = str(source)
    book = _split_long_blocks(book)
    book.meta.setdefault("format", fmt)
    book.meta["key"] = key
    book.meta["assets_dir"] = str(folder)
    if not book.images and assets.map:
        book.images = dict(assets.map)
    return book