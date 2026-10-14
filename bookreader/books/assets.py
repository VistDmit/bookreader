"""Картинки книги: распакованные файлы и учёт имён.

Парсер отдаёт байты и имя, Assets решает, как файл будет называться
на диске (имя всегда уникальное, порядок сохранён).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

EXT_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/tiff": ".tif",
    "image/svg+xml": ".svg",
}

MAGIC = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
    (b"II*\x00", ".tif"),
    (b"MM\x00*", ".tif"),
)

UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def guess_ext(data: bytes, content_type: str = "", fallback: str = ".img") -> str:
    for magic, ext in MAGIC:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if b"<svg" in data[:400].lower():
        return ".svg"
    ext = EXT_BY_TYPE.get((content_type or "").split(";")[0].strip().lower())
    return ext or fallback


class Assets:
    """Распакованные файлы книги в отдельной папке."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.map: dict[str, str] = {}     # имя в книге -> файл на диске
        self.by_digest: dict[str, str] = {}  # хэш содержимого -> файл (дедупликация)
        self.count = 0

    def _unique(self, name: str) -> str:
        stem = UNSAFE.sub("_", unicodedata.normalize("NFKD", name).strip())[:60]
        return f"{stem or 'img'}_{self.count:04d}"

    def save(self, name: str, data: bytes, content_type: str = "",
             ext: str | None = None) -> str:
        """Сохранить картинку, вернуть имя файла."""
        if not data:
            return ""
        digest = hashlib.sha1(data).hexdigest()
        if digest in self.by_digest:
            stored = self.by_digest[digest]
            self.map[name] = stored
            return stored
        suffix = ext or guess_ext(data, content_type)
        stored = self._unique(Path(name).stem) + suffix
        (self.folder / stored).write_bytes(data)
        self.by_digest[digest] = stored
        self.map[name] = stored
        self.count += 1
        return stored

    def path(self, stored_name: str) -> Path:
        return self.folder / stored_name

    def __len__(self) -> int:
        return len(self.by_digest)