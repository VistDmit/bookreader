"""Команды книжного помощника.

Все команды запускаются через run_bookreader.py (или команду bookreader).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

from . import audio as audio_lib
from . import langid, state, voice as voice_mod
from .books import detect_format
from .books import load as load_book
from .config import CACHE, DEFAULTS, MODELS, STATE, Config, ensure_dirs

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
OFF = "\033[0m"


def supports_color() -> bool:
    return sys.stdout.isatty()


def paint(text: str, color: str) -> str:
    return f"{color}{text}{OFF}" if supports_color() else text


def heading(text: str) -> None:
    print()
    print(paint(text, BOLD))


def human_size(num: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if num < 1024 or unit == "ГБ":
            return f"{num:.0f} {unit}" if unit == "Б" else f"{num / 1:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} ГБ"


# --- команды -----------------------------------------------------------------

def cmd_doctor(args, config: Config) -> int:
    """Проверка машины: что стоит, чего не хватает, сколько свободно."""
    heading("Книжный помощник: проверка окружения")
    ok = True

    from .launcher import _is_free_threading

    if _is_free_threading():
        gil = "GIL выключен" if not sys._is_gil_enabled() else "GIL включён"
    else:
        gil = "обычная сборка, GIL включён"
    print(f"  python        {sys.version.split()[0]}   {gil}")
    print(f"  проект        {Path(__file__).resolve().parent}")
    print(f"  состояние     {STATE}")

    for module, why in (("numpy", "звук и передискретизация"),
                        ("torch", "голос silero"),
                        ("ctranslate2", "перевод NLLB"),
                        ("transformers", "токенизатор NLLB"),
                        ("sounddevice", "звук в терминале"),
                        ("starlette", "веб-читалка")):
        try:
            module_info = __import__(module)
            version = getattr(module_info, "__version__", "?")
            print(f"  {module:<13} {version:<12} {DIM}{why}{OFF}")
        except ImportError:
            print(f"  {paint(module, RED):<13} {paint('НЕТ', RED):<12} {DIM}{why}{OFF}")
            ok = False

    heading("Модели")
    for entry in voice_mod.engines_report():
        mark = paint("есть", GREEN) if entry["ready"] else paint("качать", YELLOW)
        print(f"  {entry['engine']:<6} {entry['model']:<22} {mark}"
              f"  {DIM}{entry['note']}{OFF}")
    from .translate import BIG_REPO, DEFAULT_REPO, NllbEngine, model_dir

    chosen = config["nllb_repo"]
    for repo in (DEFAULT_REPO, BIG_REPO):
        folder = model_dir(repo)
        mark = paint(" <- выбрана", GREEN) if repo == chosen else ""
        size = sum(f.stat().st_size for f in folder.glob("*") if f.is_file()) \
            if folder.exists() else 0
        state_line = (f"{paint('есть', GREEN)} {human_size(size)}" if size
                      else paint("качать", YELLOW))
        print(f"  {repo.split('/')[-1]:<38} {state_line}{mark}")
        if not size:
            print(f"    {DIM}python run_bookreader.py models nllb --model {repo}{OFF}")

    heading("Место и память")
    usage = shutil.disk_usage("/")
    print(f"  диск /        свободно {human_size(usage.free)} из {human_size(usage.total)}")
    meminfo = Path("/proc/meminfo").read_text().splitlines()
    available = 0
    for line in meminfo:
        if line.startswith("MemAvailable:"):
            available = int(line.split()[1]) * 1024
    print(f"  память        доступно {human_size(available)}")

    heading("Звук")
    try:
        from .player import output_devices

        devices = output_devices()
        print(f"  устройств вывода: {len(devices)}")
        for device in devices[:5]:
            print(f"    {DIM}{device}{OFF}")
        if not devices:
            ok = False
    except Exception as exc:
        print(f"  {paint('звук недоступен', YELLOW)}: {exc}")

    heading("Книги")
    rows = state.recent_books(5)
    if rows:
        for row in rows:
            print(f"  {row['title'][:38]:<40} {row['format']:<6} {row['blocks']} абз.")
    else:
        print(f"  {DIM}пока ни одной открытой книги{OFF}")

    print()
    print("готово" if ok else "готово, но чего-то не хватает (см. красным)")
    return 0


def cmd_models(args, config: Config) -> int:
    """Скачать модели: голос и перевод."""
    what = args.what or ["silero", "nllb"]
    if "silero" in what:
        model = args.model or config["voice_model"]
        path = voice_mod.download_silero(model)
        print(f"голос: {path.name} {human_size(path.stat().st_size)}")
    if "piper" in what:
        parts = (args.model or config["piper_model"]).split("-")
        path = voice_mod.download_piper(parts[1], parts[2])
        print(f"голос: {path.name} {human_size(path.stat().st_size)}")
    if "nllb" in what:
        from .translate import BIG_REPO, DEFAULT_REPO, download_model, model_size

        repo = args.model or config["nllb_repo"] or DEFAULT_REPO
        started = time.time()
        path = download_model(repo)
        print(f"перевод: {repo}\n           {human_size(model_size(path))} "
              f"за {int(time.time() - started)} с")
        if repo != DEFAULT_REPO:
            print(f"           включить: python run_bookreader.py config "
                  f"--set nllb_repo={repo}")
    return 0


def cmd_voices(args, config: Config) -> int:
    heading("Голоса на этой машине")
    current_voice = config["voice_speaker"]
    for entry in voice_mod.engines_report():
        if not entry["ready"]:
            continue
        mark = ""
        if entry["engine"] == config["voice_engine"]:
            mark = paint(" <- выбран", GREEN)
        print(f"  {entry['engine']:<6} {entry['model']:<22} "
              f"{', '.join(entry['speakers'])} {mark}")
    heading("Переключить голос")
    print(f"  python run_bookreader.py voice use silero v5_5_ru {current_voice}")
    print(f"  python run_bookreader.py voice use piper ru_RU-dmitri-medium")
    return 0


def cmd_voice(args, config: Config) -> int:
    if args.action in ("test", "sample"):
        started = time.time()
        voice = voice_mod.Voice(config)
        seconds = args.seconds
        samples = voice.preview(seconds=seconds)
        target = Path(args.out) if args.out else CACHE / "preview.wav"
        audio_lib.write_wav(target, samples, voice.play_rate)
        length = audio_lib.duration(samples, voice.play_rate)
        print(f"голос: {voice.status()}")
        print(f"образец: {target} ({length:.1f} с звука за {time.time() - started:.1f} с)")
        if args.play and command_exists("ffplay"):
            subprocess.run(["ffplay", "-nodisp", "-autoexit", str(target)], check=False)
        return 0
    if args.action == "use":
        if not args.model:
            print("нужно указать модель: silero v5_5_ru baya", file=sys.stderr)
            return 2
        engine, model = args.model.split()[0], args.model.split()[1]
        updates = {"voice_engine": engine}
        if engine == "silero":
            updates["voice_model"] = model
            if len(args.model.split()) > 2:
                updates["voice_speaker"] = args.model.split()[2]
        else:
            updates["piper_model"] = model
        config.update(**updates)
        print("сохранено:", {k: config[k] for k in updates})
        return 0
    if args.action == "rate":
        config.update(sample_rate_play=args.rate)
        print("частота воспроизведения:", args.rate)
        return 0
    print("куда: test | use | rate", file=sys.stderr)
    return 2


def cmd_info(args, config: Config) -> int:
    """Разбор книги: сколько абзацев, какой язык, есть ли картинки."""
    book = load_book(args.file)
    detected, confidence = langid.detect(book.plain_text()[:20000],
                                        default=book.lang or "en")
    heading(book.title or Path(args.file).name)
    print(f"  файл      {book.path}")
    print(f"  формат    {book.meta.get('format')} ({detect_format(args.file)})")
    print(f"  автор     {book.author or paint('не указан', YELLOW)}")
    print(f"  язык      {book.lang or '-'} -> определён {detected} "
          f"({int(confidence * 100)}%)")
    target = config["target_lang"]
    print(f"  перевод   {'нужен' if langid.needs_translation(detected, target) else 'не нужен'}")
    print(f"  разделов  {len(book.sections)}")
    print(f"  абзацев   {book.block_count}")
    print(f"  знаков    {book.char_count()}")
    print(f"  картинок  {len(book.images)}")
    minutes = sum(len(b.text) for b in book.blocks()) / 900        # ~900 знаков в минуту
    print(f"  примерно  {minutes:.0f} мин чтения вслух")
    heading("Первые абзацы")
    shown = 0
    for block in book.blocks():
        if not block.text:
            continue
        print(f"  [{block.kind}] {block.text[:100]}")
        shown += 1
        if shown >= args.lines:
            break
    return 0


def cmd_translate(args, config: Config) -> int:
    """Проверить перевод на своём тексте."""
    from .translate import EngineError, Translator

    translator = Translator(config)
    print("движок:", translator.status())
    text = " ".join(args.text) if args.text else (
        "The old man sat by the window and watched the rain fall on the roofs.")
    source = args.source
    if source == "auto":
        source, _ = langid.detect(text, default="en")
    started = time.time()
    try:
        result = translator.translate(text, source, args.target, use_cache=False)
    except EngineError as exc:
        print(paint(f"перевод не удался: {exc}", RED))
        return 1
    print(f"  {source} -> {args.target}: {result}")
    print(paint(f"  {time.time() - started:.1f} с", DIM))
    return 0


def cmd_translate_book(args, config: Config) -> int:
    """Перевести книгу целиком, сохраняя результат в кэш."""
    from .reader import Reader

    reader = Reader(config)
    book = reader.open(args.file)
    heading(f"{book.title}: язык {reader.lang}, перевод {reader.translate_mode}")
    if reader.translate_mode == "off":
        print(paint(f"перевод не включён: {reader.info().get('error') or 'книга уже по-русски'}",
                    YELLOW))
        return 1
    limit = args.limit or book.block_count
    reader.prefetch_book(limit)
    started = time.time()
    done = 0
    while True:
        with reader._lock:
            done = sum(1 for b in reader._blocks.values() if b.ready)
        finished = done >= limit
        print(f"\r  {done}/{limit} ({int(done * 100 / max(1, limit))}%)  "
              f"{int(time.time() - started)} с", end="", flush=True)
        if finished or reader._stop.is_set():
            break
        time.sleep(1.0)
    print()
    reader.close()
    return 0


def cmd_read(args, config: Config) -> int:
    """Чтение вслух прямо в терминале."""
    from .player import Player
    from .reader import PlayerBridge, Reader

    reader = Reader(config)
    book = reader.open(args.file)
    start = args.start
    if start < 0:
        start = state.read_position(book.key)
    limit = args.paragraphs or 0
    print(f"{book.title} — {book.author or 'автор не указан'}")
    print(f"язык {reader.lang}, перевод {reader.translate_mode}, "
          f"голос {reader.voice.status()}")
    print(f"с абзаца {start + 1}"
          f"{f' до {start + limit}' if limit else ' до конца'}, темп {args.speed}")
    print("пробел не работает: остановить — Ctrl+C\n")

    player = Player(rate=reader.voice.play_rate,
                    device=config["voice_output_device"],
                    on_event=lambda event, index, position: _console_event(
                        event, index, position, book.block_count))
    player.set_speed(args.speed)

    def mark(index: int) -> None:
        state.set_read_position(book.key, index)

    bridge = PlayerBridge(reader, player, on_block=mark)
    bridge.start(start, limit=limit or None)
    try:
        while bridge._thread and bridge._thread.is_alive():
            bridge._thread.join(timeout=0.5)
        # доигрываем уже подготовленное
        deadline = time.time() + 600
        while (player.available() or player.state in ("playing", "waiting")) \
                and time.time() < deadline:
            if player.state == "waiting" and player.available() == 0:
                break
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\nостановлено")
    finally:
        bridge.stop()
        player.stop()
        reader.close()
    print("готово")
    return 0


def _console_event(event: str, index: int, position: float, total: int) -> None:
    if event == "block" and index >= 0:
        print(f"  [{index + 1}/{total}]  {int(position)} с", flush=True)


def port_is_free(host: str, port: int) -> bool:
    """Порт занят — значит, сервер уже запущен (частая ошибка)."""
    import socket

    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) != 0


def cmd_web(args, config: Config) -> int:
    """Веб-читалка в браузере."""
    from .server import run as run_server

    if not port_is_free(args.host, args.port):
        print(paint(f"порт {args.port} уже занят — сервер, похоже, уже запущен.",
                    YELLOW))
        print(f"открой http://{args.host}:{args.port} или укажи другой порт: "
              "--port 8811")
        return 1

    if args.file:
        config.update(host=args.host, port=args.port)
        from .server import WebApp
        ctx = WebApp(config)
        try:
            ctx.open_book(args.file)
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            print(paint(f"книга не открылась: {exc}", RED))
            return 1
        import uvicorn
        from .server import build_app
        app = build_app(config)
        app_state = ctx
        from . import server as server_mod
        server_mod.app_state["app"] = app_state
        print(f"книга: {ctx.reader.book.title}")
        print(f"читалка: http://{args.host}:{args.port}")
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning",
                    access_log=False)
        return 0
    config.update(host=args.host, port=args.port)
    run_server(config, args.host, args.port)
    return 0


def cmd_tui(args, config: Config) -> int:
    from .tui import run as run_tui

    run_tui(config, args.file)
    return 0


def cmd_recent(args, config: Config) -> int:
    rows = state.recent_books(args.limit)
    if not rows:
        print("истории пока нет")
        return 0
    heading("Недавние книги")
    for row in rows:
        mark = f"прочитано до абзаца {row['read_to'] + 1}" if row["read_to"] else "не начата"
        print(f"  {row['title'][:34]:<36} {row['format']:<6} "
              f"{str(row['blocks']):>6} абз.  {DIM}{mark}{OFF}")
        print(f"    {DIM}{row['path']}{OFF}")
    return 0


def cmd_config(args, config: Config) -> int:
    if not args.set:
        print(json.dumps(config.as_dict(), ensure_ascii=False, indent=2))
        return 0
    updates = {}
    for pair in args.set:
        if "=" not in pair:
            print(f"не понял параметр: {pair}", file=sys.stderr)
            return 2
        key, value = pair.split("=", 1)
        key = key.strip()
        if key not in DEFAULTS:
            print(f"неизвестный параметр: {key}", file=sys.stderr)
            return 2
        default = DEFAULTS[key]
        if isinstance(default, bool):
            updates[key] = value.strip().lower() in ("1", "true", "да", "on")
        elif isinstance(default, int):
            updates[key] = int(value)
        elif isinstance(default, float):
            updates[key] = float(value)
        elif isinstance(default, list):
            updates[key] = [v.strip() for v in value.split(",") if v.strip()]
        else:
            updates[key] = value
    config.update(**updates)
    print("сохранено:", json.dumps(updates, ensure_ascii=False))
    return 0


def cmd_cache(args, config: Config) -> int:
    sizes = state.cache_size()
    folder = CACHE
    used = 0
    if folder.exists():
        for item in folder.rglob("*"):
            if item.is_file():
                used += item.stat().st_size
    heading("Кэш")
    print(f"  переводов   {sizes['translations']}")
    print(f"  озвучек     {sizes['audio']}")
    print(f"  на диске    {human_size(used)}  ({folder})")
    if args.clean:
        removed = state.drop_audio_files(list(folder.rglob("*.wav")))
        print(f"  убрано записей о пропавших файлах: {removed}")
    return 0


def cmd_test(args, config: Config) -> int:
    from .tests import run_tests

    return run_tests(verbose=not args.quiet)


def cmd_verify(args, config: Config) -> int:
    """Проверка читалки в настоящем браузере (сервер должен быть запущен)."""
    from .cdp import check_reader

    book = Path(args.book).expanduser() if args.book else ""
    url = args.url
    if book and "book=" not in url:
        url += ("&" if "?" in url else "?") + "book=" + urllib.parse.quote(str(book))
    shot = Path(args.shot) if args.shot else STATE / "screenshot.png"
    heading("Проверка в браузере")
    print(f"  адрес: {url}")
    try:
        report = check_reader(url, shot, listen=args.seconds)
    except Exception as exc:
        print(paint(f"проверка не прошла: {type(exc).__name__}: {exc}", RED))
        return 1
    failures = 0
    for name, value in report.items():
        if name == "браузер":
            print(f"  {DIM}{value}{OFF}")
            continue
        ok = value not in (False, None, 0)
        if name in ("скриншот", "абзацев на экране", "картинок", "текущий абзац",
                    "позиция в звуке, с") or isinstance(value, str):
            ok = True
        failures += 0 if ok else 1
        mark = paint("ok  ", GREEN) if ok else paint("FAIL", RED)
        print(f"  {mark} {name}: {value}")
    print()
    print("читалка работает" if not failures else f"проблем: {failures}")
    return 0 if not failures else 1


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


# --- разбор аргументов -------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_bookreader.py",
        description="Книжный помощник: читает книги вслух, переводит, показывает картинки")
    ensure_dirs()
    config = Config()
    sub = parser.add_subparsers(dest="command")

    web = sub.add_parser("web", help="веб-читалка в браузере")
    web.add_argument("file", nargs="?", help="сразу открыть книгу")
    web.add_argument("--host", default=config["host"])
    web.add_argument("--port", type=int, default=config["port"])
    web.set_defaults(func=cmd_web)

    tui = sub.add_parser("tui", help="терминальный интерфейс")
    tui.add_argument("file", nargs="?")
    tui.set_defaults(func=cmd_tui)

    read = sub.add_parser("read", help="читать вслух в терминале")
    read.add_argument("file")
    read.add_argument("--start", type=int, default=0, help="с какого абзаца")
    read.add_argument("--paragraphs", type=int, default=0, help="сколько абзацев")
    read.add_argument("--speed", type=float, default=1.0)
    read.set_defaults(func=cmd_read)

    info = sub.add_parser("info", help="разбор книги: абзацы, язык, картинки")
    info.add_argument("file")
    info.add_argument("--lines", type=int, default=5)
    info.set_defaults(func=cmd_info)

    models = sub.add_parser("models", help="скачать модели голоса и перевода")
    models.add_argument("what", nargs="*", choices=["silero", "piper", "nllb"])
    models.add_argument("--model", help="v5_5_ru или ru_RU-irina-medium")
    models.set_defaults(func=cmd_models)

    voices = sub.add_parser("voices", help="какие голоса есть на машине")
    voices.set_defaults(func=cmd_voices)

    voice = sub.add_parser("voice", help="проверить голос / сменить голос")
    voice.add_argument("action", choices=["test", "use", "rate"], default="test",
                       nargs="?")
    voice.add_argument("model", nargs="*", help="silero v5_5_ru baya")
    voice.add_argument("--seconds", type=float, default=6.0)
    voice.add_argument("--out", help="куда сохранить wav")
    voice.add_argument("--play", action="store_true", help="проиграть ffplay")
    voice.add_argument("--rate", type=int, default=22050)
    voice.set_defaults(func=cmd_voice)

    translate = sub.add_parser("translate", help="проверить перевод")
    translate.add_argument("text", nargs="*")
    translate.add_argument("--source", default="auto")
    translate.add_argument("--target", default="ru")
    translate.set_defaults(func=cmd_translate)

    translate_book = sub.add_parser("translate-book", help="перевести книгу целиком")
    translate_book.add_argument("file")
    translate_book.add_argument("--limit", type=int, default=0)
    translate_book.set_defaults(func=cmd_translate_book)

    doctor = sub.add_parser("doctor", help="проверка окружения")
    doctor.set_defaults(func=cmd_doctor)

    recent = sub.add_parser("recent", help="недавние книги и где остановились")
    recent.add_argument("--limit", type=int, default=10)
    recent.set_defaults(func=cmd_recent)

    conf = sub.add_parser("config", help="показать или изменить настройки")
    conf.add_argument("--set", nargs="*", metavar="КЛЮЧ=ЗНАЧЕНИЕ")
    conf.set_defaults(func=cmd_config)

    cache = sub.add_parser("cache", help="что лежит в кэше")
    cache.add_argument("--clean", action="store_true")
    cache.set_defaults(func=cmd_cache)

    verify = sub.add_parser("verify", help="проверить читалку в браузере (CDP)")
    verify.add_argument("book", nargs="?", help="книга, которую открыть")
    verify.add_argument("--url", default=f"http://127.0.0.1:{config['port']}/")
    verify.add_argument("--shot", help="куда сохранить скриншот")
    verify.add_argument("--seconds", type=float, default=8.0,
                        help="сколько слушать до проверки")
    verify.set_defaults(func=cmd_verify)

    test = sub.add_parser("test", help="самопроверка без сети и моделей")
    test.add_argument("-q", "--quiet", action="store_true")
    test.set_defaults(func=cmd_test)

    parser.set_defaults(func=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        print()
        print("быстрый старт:")
        print("  python run_bookreader.py doctor")
        print("  python run_bookreader.py models")
        print("  python run_bookreader.py web ~/Загрузки/arifureta.fb2")
        return 0
    config = Config()
    if getattr(args, "port", None):
        config.update(port=args.port)
    try:
        return args.func(args, config)
    except KeyboardInterrupt:
        print("\nпрервано")
        return 130
    except Exception as exc:
        print(paint(f"ошибка: {type(exc).__name__}: {exc}", RED), file=sys.stderr)
        return 1