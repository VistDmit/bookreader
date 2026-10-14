"""FB2 (FictionBook 2.0) — zip с XML, внутри текст, главы и картинки.

Самая удобная книга для чтения вслух: главы уже размечены,
картинки лежат рядом с текстом.
"""

from __future__ import annotations

import base64
import binascii
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .assets import Assets
from .model import (Block, Book, KIND_EPIGRAPH, KIND_P, KIND_QUOTE,
                    KIND_SUBTITLE, KIND_TITLE, Section, clean_text)

TITLE = "http://www.gribuser.ru/xml/fictionbook/2.0"
XLINK = "http://www.w3.org/1999/xlink"

# тег в книге -> вид абзаца
KIND_BY_TAG = {
    "subtitle": KIND_SUBTITLE,
    "epigraph": KIND_EPIGRAPH,
    "cite": KIND_QUOTE,
}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _decode(data: bytes) -> str:
    """FB2 часто приходит в cp1251 или koi8-r, метка в XML может врать."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    if data[:3] == b"\xef\xbb\xbf":
        return data.decode("utf-8-sig")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for enc in ("cp1251", "koi8-r", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _author_name(node: ET.Element | None) -> str:
    if node is None:
        return ""
    names = []
    for tag in ("first-name", "middle-name", "last-name", "nickname"):
        found = node.find(f"{{{TITLE}}}{tag}")
        if found is not None and (found.text or "").strip():
            names.append(found.text.strip())
    return " ".join(names)


def _text_of(node: ET.Element, hrefs: dict[str, list[str]]) -> tuple[str, list[str]]:
    """Текст элемента и картинки, которые в нём встречаются."""
    parts: list[str] = []
    pics: list[str] = []
    for item in node.iter():
        if _local(item.tag) == "image":
            href = item.get(f"{{{XLINK}}}href") or item.get("href") or ""
            pics.extend(hrefs.get(href.lstrip("#"), []))
        if item.text:
            parts.append(item.text)
        if item is not node and item.tail:
            parts.append(item.tail)
    return clean_text(" ".join(parts)), pics


def _image_refs(node: ET.Element) -> list[str]:
    refs = []
    for item in node.iter():
        if _local(item.tag) != "image":
            continue
        href = item.get(f"{{{XLINK}}}href") or item.get("href") or ""
        key = href.lstrip("#")
        if key:
            refs.append(key)
    return refs


def _read_section(node: ET.Element, hrefs: dict[str, list[str]],
                  level: int, out: list[Section]) -> None:
    title = ""
    for tn in (c for c in node if _local(c.tag) == "title"):
        chunks = []
        for p in tn:
            if _local(p.tag) != "p":
                continue
            text, _ = _text_of(p, hrefs)
            chunks.append(text)
        title = " ".join(c for c in chunks if c).strip()
        if title:
            break

    section = Section(title=title, level=level)
    nested: list[Section] = []
    for child in node:
        tag = _local(child.tag)
        if tag == "title":
            continue
        if tag == "section":
            _read_section(child, hrefs, level + 1, nested)
            continue
        text, pics = _text_of(child, hrefs) if tag in ("p", *KIND_BY_TAG) else ("", [])
        if not text:
            continue
        if tag == "p":
            section.blocks.append(Block(text, KIND_P, pics))
        elif tag in KIND_BY_TAG:
            section.blocks.append(Block(text, KIND_BY_TAG[tag], pics))

    # сначала текст самой главы, потом вложенные: иначе главы идут не по порядке
    if section.blocks or section.title:
        out.append(section)
    out.extend(child for child in nested if child.blocks or child.title)


def _binaries_from_xml(raw_xml: bytes) -> dict[str, tuple[bytes, str]]:
    """Картинки из <binary id=... content-type=...> — base64 внутри XML."""
    found: dict[str, tuple[bytes, str]] = {}
    try:
        root = ET.fromstring(_decode(raw_xml))
    except ET.ParseError:
        return found
    for node in root.iter():
        if _local(node.tag) != "binary":
            continue
        bid = node.get("id") or ""
        payload = re.sub(r"\s+", "", node.text or "")
        if not bid or not payload:
            continue
        try:
            data = base64.b64decode(payload)
        except (binascii.Error, ValueError):
            continue
        found[bid] = (data, node.get("content-type", ""))
    return found


def _read_source(path: Path) -> tuple[bytes, dict[str, tuple[bytes, str]]]:
    """FB2 бывает и zip-архивом, и обычным XML-файлом — читаем оба."""
    with path.open("rb") as handle:
        head = handle.read(2)
    if head == b"PK":
        with zipfile.ZipFile(path) as zf:
            xml_name = None
            for name in zf.namelist():
                if name.lower().endswith(".xml") and "/" not in name:
                    xml_name = name
                    break
            if xml_name is None:
                raise ValueError("в архиве нет .xml с текстом книги")
            raw = zf.read(xml_name)
            binaries: dict[str, tuple[bytes, str]] = {}
            for name in zf.namelist():
                if name.lower().endswith(".xml"):
                    binaries.update(_binaries_from_xml(zf.read(name)))
            return raw, binaries
    raw = path.read_bytes()
    return raw, _binaries_from_xml(raw)


def load(path: Path, assets: Assets) -> Book:
    path = Path(path)
    stat = path.stat()
    raw, binaries = _read_source(path)
    root = ET.fromstring(_decode(raw))

    body = None
    title = ""
    lang = None
    for node in root.iter():
        tag = _local(node.tag)
        if tag == "body" and body is None:
            body = node
        elif tag == "book-title" and not title:
            title = (node.text or "").strip()
        elif tag == "lang" and not lang and (node.text or "").strip():
            lang = node.text.strip()
    author = _author_name(next((n for n in root.iter() if _local(n.tag) == "author"), None))
    if body is None:
        raise ValueError("в FB2 нет <body>")

    # id картинки -> файлы на диске
    hrefs: dict[str, list[str]] = {ref: [] for ref in _image_refs(root)}
    for bid, (data, ctype) in binaries.items():
        stored = assets.save(bid, data, ctype)
        if stored:
            hrefs.setdefault(bid, []).append(stored)

    sections: list[Section] = []
    for node in body:
        tag = _local(node.tag)
        if tag == "section":
            _read_section(node, hrefs, 1, sections)
        elif tag == "title":
            text, pics = _text_of(node, hrefs)
            if text:
                sections.append(Section(title=text, level=1,
                                       blocks=[Block(text, KIND_TITLE, pics)]))
        elif tag == "p":
            text, pics = _text_of(node, hrefs)
            if text:
                sections.append(Section(title="", blocks=[Block(text, KIND_P, pics)]))

    if not title:
        for section in sections:
            if section.blocks:
                title = section.blocks[0].text[:60]
                break
    if lang and "-" in lang:
        lang = lang.split("-")[0]
    title = title or path.stem
    images = {k: v[0] for k, v in hrefs.items() if v}

    # картинки, на которые не сослался текст (обычно обложка из описания),
    # показываем в начале: иначе читатель их вообще не увидит
    used = {name for section in sections for block in section.blocks
            for name in block.images}
    spare = [name for name in images.values() if name not in used]
    if spare:
        if sections and sections[0].blocks:
            sections[0].blocks[0].images.extend(spare[:5])
        else:
            sections.insert(0, Section(title="Обложка",
                                       blocks=[Block("", KIND_P, spare[:5])]))

    return Book(
        path=str(path),
        title=title,
        author=author,
        lang=lang,
        sections=sections,
        images=images,
        meta={"format": "fb2", "size": stat.st_size, "mtime": int(stat.st_mtime)},
    )