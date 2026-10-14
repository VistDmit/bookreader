"""Память проекта: где остановились, что уже переведено и озвучено.

Один sqlite-файл в bookreader/state/reader.db.
Кэш перевода и кэш звука хранятся здесь же ключами, сами файлы — рядом.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from pathlib import Path

from .config import DB_PATH, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    key       TEXT PRIMARY KEY,
    path      TEXT,
    title     TEXT,
    author    TEXT,
    lang      TEXT,
    format    TEXT,
    blocks    INTEGER,
    opened_at REAL,
    read_to   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS translations (
    hash    TEXT NOT NULL,
    target  TEXT NOT NULL,
    engine  TEXT NOT NULL,
    text    TEXT,
    made_at REAL,
    PRIMARY KEY (hash, target, engine)
);

CREATE TABLE IF NOT EXISTS audio (
    hash     TEXT PRIMARY KEY,
    file     TEXT,
    seconds  REAL,
    speaker  TEXT,
    made_at  REAL
);
"""

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def connect() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            ensure_dirs()
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.executescript(SCHEMA)
            _conn.commit()
        return _conn


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def text_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]


# --- книги -------------------------------------------------------------------

def remember_book(book) -> None:
    conn = connect()
    with _lock:
        conn.execute(
            """INSERT INTO books (key, path, title, author, lang, format, blocks, opened_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET opened_at=excluded.opened_at""",
            (book.key, book.path, book.title, book.author, book.lang,
             book.meta.get("format", ""), book.block_count, time.time()),
        )
        conn.commit()


def recent_books(limit: int = 20) -> list[sqlite3.Row]:
    conn = connect()
    with _lock:
        return list(conn.execute(
            "SELECT * FROM books ORDER BY opened_at DESC LIMIT ?", (limit,)))


def set_read_position(key: str, block: int) -> None:
    conn = connect()
    with _lock:
        conn.execute("UPDATE books SET read_to=? WHERE key=?", (block, key))
        conn.commit()


def read_position(key: str) -> int:
    conn = connect()
    with _lock:
        row = conn.execute("SELECT read_to FROM books WHERE key=?", (key,)).fetchone()
    return int(row["read_to"]) if row else 0


# --- кэш перевода ------------------------------------------------------------

def get_translation(text: str, target: str, engine: str) -> str | None:
    conn = connect()
    with _lock:
        row = conn.execute(
            "SELECT text FROM translations WHERE hash=? AND target=? AND engine=?",
            (text_hash(text), target, engine)).fetchone()
    return row["text"] if row else None


def put_translation(text: str, target: str, engine: str, result: str) -> None:
    conn = connect()
    with _lock:
        conn.execute(
            "INSERT OR REPLACE INTO translations (hash, target, engine, text, made_at)"
            " VALUES (?,?,?,?,?)",
            (text_hash(text), target, engine, result, time.time()))
        conn.commit()


def cache_size() -> dict:
    conn = connect()
    with _lock:
        translations = conn.execute(
            "SELECT COUNT(*) AS n FROM translations").fetchone()["n"]
        audio = conn.execute("SELECT COUNT(*) AS n FROM audio").fetchone()["n"]
    return {"translations": translations, "audio": audio}


# --- кэш звука ---------------------------------------------------------------

def get_audio(text: str, speaker: str) -> str | None:
    """Файл озвучки для текста (хэш считается с голосом)."""
    conn = connect()
    key = text_hash(text + "|" + speaker)
    with _lock:
        row = conn.execute(
            "SELECT file FROM audio WHERE hash=?", (key,)).fetchone()
    if not row:
        return None
    path = Path(row["file"])
    return str(path) if path.exists() else None


def put_audio(text: str, speaker: str, path: Path, seconds: float) -> None:
    conn = connect()
    with _lock:
        conn.execute(
            "INSERT OR REPLACE INTO audio (hash, file, seconds, speaker, made_at)"
            " VALUES (?,?,?,?,?)",
            (text_hash(text + "|" + speaker), str(path), seconds, speaker, time.time()))
        conn.commit()


def drop_audio_files(paths: list[Path]) -> int:
    """Убрать пропавшие файлы озвучки из кэша (кто-то почистил диск)."""
    conn = connect()
    removed = 0
    with _lock:
        for row in conn.execute("SELECT hash, file FROM audio").fetchall():
            if not Path(row["file"]).exists():
                conn.execute("DELETE FROM audio WHERE hash=?", (row["hash"],))
                removed += 1
        conn.commit()
    return removed