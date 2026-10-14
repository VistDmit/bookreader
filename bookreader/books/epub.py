"""EPUB — zip с XHTML-файлами, оглавлением и картинками."""

from __future__ import annotations

import posixpath
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path

from .assets import Assets
from .model import Block, Book, KIND_P, KIND_TITLE, Section, clean_text

HEADING_TAGS = {"h1": KIND_TITLE, "h2": KIND_TITLE, "h3": KIND_TITLE}
SKIP_TAGS = {"script", "style", "head", "title"}
BLOCK_TAGS = {"p", "div", "li", "blockquote", "td"}


class _Text(HTMLParser):
    """Достаёт абзацы и картинки из XHTML, разметку игнорирует."""

    def __init__(self, base_dir: str, hrefs: dict[str, str]):
        super().__init__(convert_charrefs=True)
        self.base_dir = base_dir
        self.hrefs = hrefs
        self.blocks: list[tuple[str, str, list[str]]] = []  # (текст, вид, картинки)
        self.images: list[str] = []
        self._buf: list[str] = []
        self._kind = KIND_P
        self._pics: list[str] = []
        self._skip = 0

    def _flush(self) -> None:
        text = clean_text("".join(self._buf))
        if text or self._pics:
            self.blocks.append((text, self._kind, list(self._pics)))
        self._buf = []
        self._pics = []
        self._kind = KIND_P

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in SKIP_TAGS:
            self._skip += 1
            return
        if tag == "img":
            src = (attrs.get("src") or "").split("#")[0]
            key = posixpath.normpath(posixpath.join(self.base_dir, src)) if src else ""
            stored = self.hrefs.get(key)
            if stored:
                self._pics.append(stored)
                self.images.append(stored)
            return
        if tag in HEADING_TAGS:
            self._flush()
            self._kind = KIND_TITLE

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if tag in HEADING_TAGS or tag in BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        if not self._skip:
            self._buf.append(data)

    def close(self):
        super().close()
        self._flush()


def _read(zf: zipfile.ZipFile, name: str) -> str:
    raw = zf.read(name)
    head = raw[:400].lower()
    match = re.search(rb'encoding=["\']([\w-]+)', head)
    enc = match.group(1).decode() if match else "utf-8"
    try:
        return raw.decode(enc, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


def _opf_path(zf: zipfile.ZipFile) -> str:
    try:
        container = _read(zf, "META-INF/container.xml")
    except KeyError:
        for name in zf.namelist():
            if name.lower().endswith(".opf"):
                return name
        raise ValueError("нет META-INF/container.xml") from None
    match = re.search(r'full-path=["\']([^"\']+)', container)
    return match.group(1) if match else ""


def load(path: Path, assets: Assets) -> Book:
    path = Path(path)
    stat = path.stat()
    with zipfile.ZipFile(path) as zf:
        opf_name = _opf_path(zf)
        if not opf_name:
            raise ValueError("в EPUB не найден .opf")
        opf = _read(zf, opf_name)
        root_dir = posixpath.dirname(opf_name)

        title = _first(re.findall(r"<dc:title[^>]*>(.*?)</dc:title>", opf, re.S)) or path.stem
        author = _first(re.findall(r"<dc:creator[^>]*>(.*?)</dc:creator>", opf, re.S))
        lang = _first(re.findall(r"<dc:language[^>]*>(.*?)</dc:language>", opf, re.S))
        if lang and "-" in lang:
            lang = lang.split("-")[0]

        # картинки из манифеста
        hrefs: dict[str, str] = {}
        for item_id, item_href, item_type in re.findall(
                r'<item[^>]+id=["\']([^"\']+)["\'][^>]+href=["\']([^"\']+)["\']'
                r'(?:[^>]+media-type=["\']([^"\']+)["\'])?', opf):
            key = posixpath.normpath(posixpath.join(root_dir, item_href))
            if key in zf.namelist() and item_type.startswith("image"):
                stored = assets.save(item_id, zf.read(key), item_type)
                if stored:
                    hrefs[key] = stored

        # порядок глав — spine, иначе как лежат в архиве
        order: list[str] = []
        for idref in re.findall(r'<itemref[^>]+idref=["\']([^"\']+)', opf):
            match = re.search(
                r'<item[^>]+id=["\']%s["\'][^>]+href=["\']([^"\']+)' % re.escape(idref), opf)
            if match:
                key = posixpath.normpath(posixpath.join(root_dir, match.group(1)))
                if key in zf.namelist():
                    order.append(key)
        if not order:
            order = [n for n in zf.namelist()
                     if n.lower().endswith((".xhtml", ".html", ".htm"))]

    sections: list[Section] = []
    with zipfile.ZipFile(path) as zf:
        for name in order:
            base = posixpath.dirname(name)
            parser = _Text(base, hrefs)
            try:
                parser.feed(_read(zf, name))
            except Exception:                     # битый XHTML — пропускаем
                continue
            parser.close()
            blocks = [Block(t, k, i) for t, k, i in parser.blocks
                      if t or i]
            if not blocks:
                continue
            heading = next((b.text for b in blocks if b.kind == KIND_TITLE), "")
            sections.append(Section(title=heading, level=1, blocks=blocks))

    for bid, stored in hrefs.items():
        assets.map.setdefault(bid, stored)

    return Book(
        path=str(path),
        title=title,
        author=author,
        lang=lang,
        sections=sections,
        images={k: v for k, v in hrefs.items()},
        meta={"format": "epub", "size": stat.st_size, "mtime": int(stat.st_mtime)},
    )


def _first(values: list[str]) -> str:
    for value in values:
        text = re.sub(r"<[^>]+>", "", value).strip()
        if text:
            return text
    return ""