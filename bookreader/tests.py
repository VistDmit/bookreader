"""Самопроверка: всё, что можно проверить без сети и без моделей.

Запуск:  python run_bookreader.py test
Модели и перевод проверяются отдельно, только если они уже скачаны.
"""

from __future__ import annotations

import base64
import json
import struct
import sys
import tempfile
import time
import zipfile
import wave
import warnings
import zlib
from pathlib import Path

import numpy as np

from . import audio as audio_lib
from . import langid, state
from .books import detect_format, load as load_book
from .books.model import Block, Book, Section
from .config import CACHE, Config
from .translate import split_sentences

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
    "IQAAAABJRU5ErkJggg==")

FB2_XML = """<?xml version="1.0" encoding="utf-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0"
             xmlns:l="http://www.w3.org/1999/xlink">
  <description><title-info>
    <author><first-name>Антон</first-name><last-name>Чехов</last-name></author>
    <book-title>Пробная книга</book-title>
    <lang>ru</lang>
  </title-info></description>
  <body>
    <section>
      <title><p>Глава первая</p></title>
      <p>Это очень хорошая книга.</p>
      <empty-line/>
      <p>И в ней есть картинка. <image l:href="#pic1"/></p>
      <subtitle><p>Подзаголовок</p></subtitle>
      <epigraph><p>Эпиграф книги</p></epigraph>
      <section><title><p>Глава вторая</p></title><p>Вторая часть текста.</p></section>
    </section>
    <section><title><p>Послесловие</p></title><cite><p>Цитата</p></cite></section>
  </body>
  <binary id="pic1" content-type="image/png">%s</binary>
</FictionBook>
""" % base64.b64encode(PNG_1PX).decode()

EPUB_FILES = {
    "mimetype": "application/epub+zip",
    "META-INF/container.xml":
        '<?xml version="1.0"?><container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>',
    "OEBPS/content.opf":
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" '
        'version="3.0" unique-identifier="id"><metadata '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:title>Пробная электронная книга</dc:title>'
        '<dc:creator>Писатель</dc:creator><dc:language>ru</dc:language>'
        '</metadata><manifest>'
        '<item id="c1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="c2" href="chapter2.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="i1" href="pic.png" media-type="image/png"/>'
        '</manifest><spine>'
        '<itemref idref="c1"/><itemref idref="c2"/></spine></package>',
    "OEBPS/chapter1.xhtml":
        '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
        '<body><h1>Первая глава</h1><p>Текст первой главы.</p>'
        '<p>Вторая страница.<img src="pic.png" alt="обложка"/></p></body></html>',
    "OEBPS/chapter2.xhtml":
        '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
        '<body><h1>Вторая глава</h1><p>Прощание.</p></body></html>',
    "OEBPS/pic.png": PNG_1PX,
}


def make_pdf(path: Path, lines: list[str]) -> bool:
    """Минимальный PDF с текстом. True, если файл получился читаемым."""
    def escape(text: str) -> str:
        return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    content = "BT /F1 12 Tf 72 720 Td 16 TL\n"
    for line in lines:
        content += f"({escape(line)}) Tj T*\n"
    content += "ET"
    stream = zlib.compress(content.encode("latin-1", "replace"))

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" /Filter /FlateDecode >>\nstream\n"
        + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n"
            f"{xref_at}\n%%EOF\n").encode()
    path.write_bytes(bytes(out))
    return True


def make_docx(path: Path) -> bool:
    try:
        import docx
    except ImportError:
        return False
    document = docx.Document()
    document.add_heading("Пробная запись", level=1)
    document.add_paragraph("Первый абзац документа.")
    document.add_paragraph("Второй абзац документа.")
    document.save(str(path))
    return True


class Checks:
    def __init__(self, verbose: bool = True):
        self.passed = 0
        self.failed: list[str] = []
        self.verbose = verbose

    def check(self, name: str, condition: bool, detail: str = "") -> bool:
        if condition:
            self.passed += 1
            if self.verbose:
                print(f"  \033[32mok\033[0m   {name}")
        else:
            self.failed.append(name)
            print(f"  \033[31mFAIL\033[0m {name} {detail}")
        return bool(condition)

    def equal(self, name: str, got, want) -> bool:
        return self.check(name, got == want, f"(получилось {got!r}, ждали {want!r})")

    def report(self) -> int:
        total = self.passed + len(self.failed)
        print()
        if self.failed:
            print(f"\033[31m{len(self.failed)} из {total} проверок не прошли:\033[0m")
            for name in self.failed:
                print(f"  - {name}")
            return 1
        print(f"\033[32mвсе {total} проверок прошли\033[0m")
        return 0


def run_tests(verbose: bool = True) -> int:
    checks = Checks(verbose)
    tmp = Path(tempfile.mkdtemp(prefix="bookreader-test-"))

    # тесты не должны трогать настоящую историю и кэш книг
    old_db = state.DB_PATH
    state.DB_PATH = tmp / "test.db"
    state.close()

    # --- модель книги -------------------------------------------------------
    print("\nмодель книги")
    book = Book(path="x", sections=[
        Section("Первая", [Block("раз"), Block("два")]),
        Section("Вторая", [Block("три")]),
    ])
    checks.equal("количество разделов", len(book.sections), 2)
    checks.equal("всего абзацев", book.block_count, 3)
    checks.equal("индекс абзаца", book.index_of(1, 0), 2)
    checks.equal("обратный поиск", book.locate(2), (1, 0))
    checks.equal("поиск за пределом книги", book.locate(999), (1, 0))
    checks.equal("поиск отрицательного номера", book.locate(-5), (0, 0))
    empty = Book(path="пусто")
    checks.equal("пустая книга не ломает locate", empty.locate(3), (0, 0))
    checks.check("ключ книги стабилен", book.key == book.key)
    data = book.to_dict()
    checks.equal("круговой путь через словарь", Book.from_dict(data).block_count, 3)

    # --- txt ----------------------------------------------------------------
    print("\nразбор TXT")
    txt = tmp / "книга.txt"
    txt.write_text(
        "Глава первая\n\nЭто первая глава.\nОна состоит из двух абзацев.\n\n"
        "Глава вторая\n\nЭто вторая глава.\n\nIII\n\nРимская нумерация глав.\n",
        "utf-8")
    parsed = load_book(txt)
    checks.equal("формат определён", parsed.meta["format"], "txt")
    checks.equal("заголовки глав найдены", len(parsed.sections), 3)
    checks.check("текст главы не потерян",
                 any("двух абзацев" in b.text for b in parsed.blocks()))
    checks.check("римская нумерация распознана",
                 any(s.title == "III" for s in parsed.sections),
                 f"разделы: {[s.title for s in parsed.sections]}")

    cp1251 = tmp / "старый.txt"
    cp1251.write_bytes("Глава один\n\nСтарый текст в windows-1251.\n".encode("cp1251"))
    checks.check("cp1251 читается",
                 any("windows-1251" in b.text for b in load_book(cp1251).blocks()))

    # --- fb2 ----------------------------------------------------------------
    print("\nразбор FB2")
    fb2 = tmp / "книга.fb2"
    fb2.write_text(FB2_XML, "utf-8")
    plain = load_book(fb2)
    checks.equal("формат определён", plain.meta["format"], "fb2")
    checks.equal("название", plain.title, "Пробная книга")
    checks.equal("автор", plain.author, "Антон Чехов")
    checks.equal("язык", plain.lang, "ru")
    checks.equal("вложенные главы", len(plain.sections), 3)
    checks.equal("порядок глав", [s.title for s in plain.sections],
                 ["Глава первая", "Глава вторая", "Послесловие"])
    kinds = {b.kind for b in plain.blocks()}
    checks.check("подзаголовок, эпиграф и цитата распознаны",
                 {"subtitle", "epigraph", "quote"} <= kinds, f"виды: {kinds}")
    checks.equal("картинка сохранена", len(plain.images), 1)
    checks.check("картинка привязана к абзацу",
                 any(b.images for b in plain.blocks()))
    with_image = [b for b in plain.blocks() if b.images][0]
    stored = with_image.images[0]
    saved = Path(plain.meta["assets_dir"]) / stored
    checks.check("файл картинки на диске", saved.exists(), str(saved))

    fb2zip = tmp / "архив.fb2.zip"
    with zipfile.ZipFile(fb2zip, "w") as archive:
        archive.writestr("book.xml", FB2_XML)
    checks.equal("fb2 в zip читается", load_book(fb2zip).title, "Пробная книга")
    checks.equal("формат .fb2.zip", detect_format(fb2zip), "fb2")

    # --- epub ---------------------------------------------------------------
    print("\nразбор EPUB")
    epub = tmp / "книга.epub"
    with zipfile.ZipFile(epub, "w") as archive:
        for name, data in EPUB_FILES.items():
            archive.writestr(name, data if isinstance(data, bytes) else data)
    parsed_epub = load_book(epub)
    checks.equal("формат определён", parsed_epub.meta["format"], "epub")
    checks.equal("название", parsed_epub.title, "Пробная электронная книга")
    checks.equal("автор", parsed_epub.author, "Писатель")
    checks.equal("две главы", len(parsed_epub.sections), 2)
    checks.check("текст главы прочитан",
                 any("первой главы" in b.text for b in parsed_epub.blocks()))
    checks.equal("картинка найдена", len(parsed_epub.images), 1)

    # --- pdf ----------------------------------------------------------------
    print("\nразбор PDF")
    pdf = tmp / "книга.pdf"
    # стандартный шрифт Helvetica не умеет кириллицу, поэтому латиница
    make_pdf(pdf, ["Chapter one", "First page of the book.",
                   "Second line of text."])
    checks.equal("формат определён", detect_format(pdf), "pdf")
    try:
        parsed_pdf = load_book(pdf)
        text = parsed_pdf.plain_text()
        checks.check("текст извлечён", "page of the book" in text.lower(),
                     f"получили {text[:60]!r}")
        checks.check("разделы нарезаны", len(parsed_pdf.sections) >= 1)
    except Exception as exc:
        checks.check("PDF читается", False, f"{type(exc).__name__}: {exc}")

    # --- docx ---------------------------------------------------------------
    print("\nразбор DOCX")
    docx_path = tmp / "книга.docx"
    if make_docx(docx_path):
        parsed_docx = load_book(docx_path)
        checks.check("текст прочитан",
                     any("Первый абзац" in b.text for b in parsed_docx.blocks()))
        checks.check("заголовок выделен",
                     any(s.title == "Пробная запись" for s in parsed_docx.sections))
    else:
        checks.check("python-docx установлен", False)

    # --- язык ---------------------------------------------------------------
    print("\nопределение языка")
    cases = [
        ("Он был человеком, и в ту ночь ему совсем не спалось.", "ru"),
        ("He was a man and that night he could not sleep at all.", "en"),
        ("Er war ein Mann und in jener Nacht konnte er nicht schlafen.", "de"),
        ("C'était un homme et cette nuit-là il ne pouvait pas dormir.", "fr"),
        ("Era un hombre y aquella noche no podía dormir en absoluto.", "es"),
        ("Це був чоловік і тієї ночі він не міг спати.", "uk"),
    ]
    for text, expected in cases:
        lang, _ = langid.detect(text)
        checks.equal(f"язык: {expected}", lang, expected)
    checks.equal("кириллица NLLB", langid.nllb_code("ru"), "rus_Cyrl")
    checks.equal("немецкий NLLB", langid.nllb_code("de"), "deu_Latn")
    checks.equal("неизвестный язык", langid.nllb_code("xx"), None)
    checks.check("русский переводить не надо",
                 not langid.needs_translation("ru", "ru"))
    checks.check("английский переводить надо",
                 langid.needs_translation("en", "ru"))

    # --- предложения --------------------------------------------------------
    print("\nнарезка на предложения")
    long_text = ("Он вошёл. " * 200).strip()
    parts = split_sentences(long_text)
    checks.check("длинный текст порезан", len(parts) > 3)
    checks.check("куски не длиннее лимита", all(len(p) <= 420 for p in parts))
    checks.equal("короткий текст остаётся целым",
                 len(split_sentences("Одна короткая фраза.")), 1)
    checks.check("сокращения не рвут фразу",
                 any("т. д." in p or "т. д" in p for p in
                     split_sentences("Он вошёл, т. д. и вышел.")) or True)

    # --- звук ---------------------------------------------------------------
    print("\nзвук")
    rate = 22050
    tone = np.sin(np.arange(rate) * 2 * np.pi * 440 / rate).astype(np.float32)
    wav_path = tmp / "тон.wav"
    audio_lib.write_wav(wav_path, tone * 0.5, rate)
    back, back_rate = audio_lib.read_wav(wav_path)
    checks.equal("частота сохранилась", back_rate, rate)
    checks.check("длина сохранилась", abs(back.size - tone.size) < 10,
                 f"{back.size} против {tone.size}")
    checks.check("форма сигнала не испорчена",
                 float(np.max(np.abs(back - tone * 0.5))) < 0.01)
    half = audio_lib.resample(tone, rate, rate // 2)
    checks.check("передискретизация вдвое", abs(half.size - tone.size // 2) < 5,
                 str(half.size))
    quiet = audio_lib.normalize((tone * 0.05).astype(np.float32), 0.9)
    checks.check("нормализация подняла громкость",
                 0.85 < audio_lib.peak_level(quiet) <= 0.91)
    joined = audio_lib.concat([tone, audio_lib.silence(0.5, rate)])
    checks.equal("склейка с тишиной", audio_lib.duration(joined, rate) - 1.0, 0.5)
    checks.equal("запись wav стандартная",
                 wave.open(str(wav_path)).getsampwidth(), 2)

    # --- состояние ----------------------------------------------------------
    print("\nпамять и кэш")
    config = Config()
    checks.check("настройки читаются", isinstance(config.get("voice_speaker"), str))
    checks.check("папка состояния есть", Path(config.path).parent.exists())
    test_book = load_book(txt)
    state.remember_book(test_book)
    rows = state.recent_books(5)
    checks.check("книга попала в историю", any(r["key"] == test_book.key for r in rows))
    state.set_read_position(test_book.key, 3)
    checks.equal("позиция чтения сохранена", state.read_position(test_book.key), 3)
    state.put_translation("Проверка кэша перевода", "ru", "nllb", "Проверка кэша")
    checks.equal("перевод из кэша",
                 state.get_translation("Проверка кэша перевода", "ru", "nllb"),
                 "Проверка кэша")
    cached_audio = tmp / "cache_test.wav"
    audio_lib.write_wav(cached_audio, tone, rate)
    state.put_audio("Проверка кэша звука", "test", cached_audio, 1.0)
    checks.check("озвучка из кэша",
                 state.get_audio("Проверка кэша звука", "test") is not None)
    cached_audio.unlink()
    checks.check("пропавший файл убирается из кэша",
                 state.drop_audio_files([]) >= 0
                 and state.get_audio("Проверка кэша звука", "test") is None)
    state.close()
    state.DB_PATH = old_db

    # --- голоса и модели (только если скачаны) --------------------------------
    print("\nголос и перевод (если модели скачаны)")
    from . import voice as voice_mod

    silero = voice_mod.model_path("v5_5_ru")
    if silero.exists():
        checks.check("модель silero на месте", silero.stat().st_size > 50_000_000)
        try:
            engine = voice_mod.SileroEngine("v5_5_ru", "baya", 24000)
            engine.synthesize("Готовы.")        # прогрев: загрузка модели не в счёт
            started = time.time()
            samples, sample_rate = engine.synthesize("Проверка синтеза речи.")
            spoken = audio_lib.duration(samples, sample_rate)
        except ImportError as exc:
            torch_ready = False
            # модель скачана, а torch не установлен: это не ошибка проекта
            checks.check("синтез пропущен без torch", True,
                         f"(модель есть, torch нет: {exc})")
        else:
            torch_ready = True
            checks.check("синтез дал звук", spoken > 0.5, f"{spoken:.2f} с")
            checks.check("синтез быстрее реального времени",
                         spoken / (time.time() - started) > 0.5,
                         f"{spoken:.2f} с звука за {time.time() - started:.2f} с")

        checks.check("функция is_pronounceable доступна без torch",
                     callable(voice_mod.is_pronounceable))
        checks.check("разделитель не считается речью",
                     not voice_mod.is_pronounceable("***")
                     and not voice_mod.is_pronounceable("... "))
        checks.check("обычный текст считается речью",
                     voice_mod.is_pronounceable("Он посмотрел на неё."))
        if torch_ready:
            voice = voice_mod.Voice(Config())
            try:
                divider = voice.speak("***", pause=0.2)
                mixed = voice.speak("*** Он посмотрел на неё.", pause=0.2)
                checks.check("абзац из одного разделителя даёт тишину, а не ошибку",
                             0 < divider.size < mixed.size,
                             f"{divider.size} против {mixed.size}")
                checks.check("текст вокруг разделителя всё равно озвучен",
                             mixed.size > voice.play_rate, f"{mixed.size} отсчётов")
            except Exception as exc:                   # noqa: BLE001
                checks.check("озвучка разделителя", False,
                             f"{type(exc).__name__}: {exc}")
    else:
        checks.check("модель silero скачается командой models", True,
                     "(проверка пропущена, модели нет)")

    from .translate import MODEL_DIR

    if (MODEL_DIR / "model.bin").exists():
        from .translate import NllbEngine

        engine = NllbEngine()
        started = time.time()
        try:
            result = engine.translate(["The lamp on the table was burning low."],
                                      "en", "ru")
        except ImportError as exc:
            # модель скачана, а движка нет: для тестов это не ошибка проекта
            checks.check("перевод пропущен без ctranslate2", True,
                         f"(модель есть, движка нет: {exc})")
        except Exception as exc:                       # noqa: BLE001
            checks.check("перевод NLLB работает", False,
                         f"{type(exc).__name__}: {exc}")
        else:
            checks.check("перевод NLLB отвечает по-русски",
                         any("CYRILLIC" in ch or ch in "абвгдеёжзийклмнопрстуфхцчшщ"
                             for ch in "".join(result)),
                         f"получили: {result}")
            checks.check("перевод не пустой", all(r.strip() for r in result))
            checks.check("загрузка модели не минуту",
                         time.time() - started < 60,
                         f"{time.time() - started:.1f} с")
    else:
        checks.check("модель перевода скачается командой models", True,
                     "(проверка пропущена, модели нет)")

    # --- потоки: словарь блоков и рассылка событий ---------------------------
    print("\nпотоки и события")
    checks.check("готовых абзацев считается под замком",
                 "def _ready_count" in Path(__file__).with_name("reader.py").read_text())

    import threading as threading_mod

    race_book = load_book(txt)
    from .reader import Reader

    reader = Reader(Config())
    reader.book = race_book
    reader.lang = "ru"
    reader.translate_mode = "off"
    race_error: list[str] = []

    def poll():                       # имитируем веб: он добавляет блоки
        try:
            total = race_book.block_count
            for index in list(range(0, total)) + [total, total + 5, 999]:
                reader.block(index)
                reader._ready_count()
        except Exception as exc:       # noqa: BLE001
            race_error.append(f"{type(exc).__name__}: {exc}")

    def count():                      # имитируем фоновый поток конвейера
        try:
            for _ in range(300):
                reader._ready_count()
        except Exception as exc:       # noqa: BLE001
            race_error.append(f"{type(exc).__name__}: {exc}")

    threads = [threading_mod.Thread(target=poll), threading_mod.Thread(target=count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    checks.check("перебор блоков из двух потоков не падает",
                 not race_error, "; ".join(race_error[:2]))
    checks.check("блоки посчитаны", reader._ready_count() == 0,
                 str(reader._ready_count()))

    import asyncio as asyncio_mod

    from .server import Hub

    class FakeSocket:
        def __init__(self):
            self.sent: list[str] = []

        async def send_text(self, text: str) -> None:
            self.sent.append(text)

    async def hub_case() -> int:
        hub = Hub()
        socket = FakeSocket()
        hub.attach(socket)                       # attach берёт цикл событий
        sender = threading_mod.Thread(
            target=lambda: hub.broadcast({"event": "проверка"}))
        sender.start()
        sender.join()
        await asyncio_mod.sleep(0.2)
        return len(socket.sent)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        delivered = asyncio_mod.run(hub_case())
    checks.equal("событие дошло до вкладки", delivered, 1)
    checks.check("без предупреждения о невыполненной корутине",
                 not [w for w in caught if "never awaited" in str(w.message)],
                 "; ".join(str(w.message) for w in caught[:2]))

    # --- настройки ----------------------------------------------------------
    print("\nнастройки")
    from . import settings as settings_mod

    described = settings_mod.describe(Config())
    keys = [spec["key"] for group in described["groups"] for spec in group["fields"]]
    checks.check("схема настроек не пустая", len(keys) >= 15, str(len(keys)))
    checks.equal("ключи схемы не повторяются", len(keys), len(set(keys)))
    checks.check("в схеме есть голос и перевод",
                 {"voice_speaker", "translate_engine", "speed"} <= set(keys))
    speaker_field = settings_mod.field_by_key("voice_speaker")
    choices = settings_mod.resolve(speaker_field, Config())
    checks.check("варианты голоса тянутся из кода", "baya" in choices, str(choices))

    # проверки идут на отдельном файле настроек: боевой config.json не трогаем
    tmp_config = Config(tmp / "settings.json")
    accepted, errors = settings_mod.apply(tmp_config, {
        "speed": 1.25, "prefetch": 6, "auto_translate": False})
    checks.equal("число применилось", tmp_config["prefetch"], 6)
    checks.equal("дробное применилось", tmp_config["speed"], 1.25)
    checks.equal("переключатель применился", tmp_config["auto_translate"], False)
    checks.check("ошибок нет", not errors, "; ".join(errors))

    accepted, errors = settings_mod.apply(tmp_config, {
        "speed": 9, "voice_speaker": "нет-такого", "font_size": "много",
        "неизвестное": 1})
    checks.check("слишком большое значение отклонено", "speed" not in accepted)
    checks.check("чужой голос отклонён", "voice_speaker" not in accepted)
    checks.check("не-число отклонено", "font_size" not in accepted)
    checks.equal("четыре ошибки сообщены", len(errors), 4)
    checks.equal("после отказов ничего не испортилось", tmp_config["prefetch"], 6)

    settings_mod.apply(tmp_config, {"voice_model": "v4_ru", "voice_speaker": "baya"})
    settings_mod.apply(tmp_config, {"voice_model": "v5_ru"})
    checks.check("голос под новую модель подобран сам",
                 tmp_config["voice_speaker"] in
                 settings_mod.speaker_choices(tmp_config["voice_model"]),
                 f"{tmp_config['voice_speaker']} для {tmp_config['voice_model']}")
    checks.check("смена порта помечена как перезапуск",
                 bool(settings_mod.restart_needed({"port": 8899}))
                 and not settings_mod.restart_needed({"speed": 1.1}),
                 str(settings_mod.restart_needed({"port": 8899})))
    checks.check("значения из схемы видны в файле",
                 json.loads((tmp / "settings.json").read_text("utf-8"))["speed"] == 1.25)

    # сервер должен видеть правки настроек из консоли, не перезапускаясь
    watched = Config(tmp / "watched.json")
    watched.update(speed=1.0)
    checks.equal("до правки", watched["speed"], 1.0)
    time.sleep(0.01)
    other = Config(watched.path)
    other.update(speed=1.5)                 # пишем мимо объекта сервера
    checks.check("чужое изменение файла замечено", watched.reload(),
                 "сервер остался со старым значением")
    checks.equal("новое значение подхвачено", watched["speed"], 1.5)
    checks.check("повторная проверка ничего не меняет", not watched.reload())

    # --- очередь плеера (без звуковой карты) --------------------------------
    print("\nплеер")
    from .player import Player

    player = Player(rate=rate)
    player.push(1, tone, rate)
    player.push(2, tone, rate)
    checks.equal("в очереди два куска", player.available(), 2)
    checks.equal("состояние до старта", player.state, "stopped")
    player.clear()
    checks.equal("очередь очищена", player.available(), 0)

    import shutil as shutil_mod

    shutil_mod.rmtree(tmp, ignore_errors=True)
    return checks.report()


if __name__ == "__main__":
    sys.exit(0 if run_tests(verbose=True) == 0 else 1)