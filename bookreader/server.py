"""Веб-читалка: локальный сервер, книга, озвучка, картинки.

Звук в браузере играет сам: сервер только готовит WAV-файлы и отдаёт их
по одному абзацу. Так текст, подсветка и звук не расходятся.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import (FileResponse, JSONResponse, PlainTextResponse,
                                 Response)
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import settings as settings_mod
from . import state
from .config import BOOKS, WEB, Config
from .reader import Reader

UPLOAD_DIR = BOOKS / "_upload"


class Hub:
    """Рассылка событий в браузер по websocket."""

    def __init__(self):
        self.clients: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    def attach(self, websocket: WebSocket) -> None:
        self.clients.add(websocket)
        self.loop = asyncio.get_running_loop()

    def detach(self, websocket: WebSocket) -> None:
        self.clients.discard(websocket)

    def broadcast(self, event: dict) -> None:
        if not self.clients or self.loop is None:
            return
        message = json.dumps(event, ensure_ascii=False)
        for websocket in list(self.clients):
            try:
                # именно run_coroutine_threadsafe: если отдать циклу
                # websocket.send_text(message) через call_soon_threadsafe,
                # корутина создаётся в чужом потоке и её никто не ждёт —
                # сообщение не уходит, а Python ругается в лог
                asyncio.run_coroutine_threadsafe(
                    websocket.send_text(message), self.loop)
            except Exception:
                self.clients.discard(websocket)


class WebApp:
    """Состояние веба: открытая книга, читатель и события."""

    def __init__(self, config: Config | None = None):
        self.config = config or Config()
        self.reader = Reader(self.config)
        self.hub = Hub()
        self.opened_at = 0.0
        self.report_job = None

    # --- вспомогательное ----------------------------------------------------
    def notify(self, event: str, **fields) -> None:
        payload = {"event": event, "time": time.time(), **fields}
        self.hub.broadcast(payload)

    def book_dict(self, with_blocks: bool = True, limit: int = 800) -> dict:
        book = self.reader.book
        if not book:
            return {}
        data = book.to_dict()
        data["info"] = self.reader.info()
        data["ready"] = {str(i): True for i in self.reader.ready_map()}
        if with_blocks:
            # первые абзацы сразу, остальные браузер доберёт по мере прокрутки
            data["blocks_text"] = self.reader.all_blocks(limit)
        return data

    # --- команды ------------------------------------------------------------
    def open_book(self, path: str, want_images: bool = True) -> dict:
        book = self.reader.open(path, want_images=want_images)
        self.opened_at = time.time()
        # первые абзацы готовим сразу: иначе читатель видит книгу
        # на чужом языке, пока перевод ещё не поспел
        self.reader.prepare(0)
        self.reader.prepare_ahead(0, 40)
        self.notify("opened", info=self.reader.info())
        return self.book_dict()

    def start_reading(self, index: int, prefetch: int | None = None) -> dict:
        if not self.reader.book:
            raise ValueError("книга не открыта")
        index = max(0, min(index, self.reader.book.block_count - 1))
        self.reader.prepare(index)
        self.reader.prepare_ahead(index, prefetch)
        self.notify("reading", index=index)
        return {"index": index, "prefetch": prefetch or self.reader.prefetch}

    def on_settings_changed(self, changed: dict) -> None:
        """Что нужно пересобрать, когда настройки поменяли прямо сейчас."""
        voice_keys = {"voice_engine", "voice_model", "voice_speaker", "piper_model",
                      "sample_rate", "sample_rate_play", "pause_after_paragraph"}
        if changed.keys() & voice_keys:
            try:
                self.reader.set_voice()
            except Exception as exc:                 # noqa: BLE001
                self.reader._last_error = str(exc)
        if "prefetch" in changed:
            self.reader.prefetch = int(changed["prefetch"])
        translate_keys = {"auto_translate", "translate_engine", "nllb_repo"}
        if changed.keys() & translate_keys and self.reader.book:
            # книга переоткрывается: язык и перевод считаются заново
            try:
                self.reader.set_translate(bool(changed.get("auto_translate",
                                                           self.config["auto_translate"])),
                                           changed.get("translate_engine"))
                self.reader.open(self.reader.book.path, want_images=False)
                self.open_book(self.reader.book.path, want_images=False)
            except (ValueError, RuntimeError, FileNotFoundError) as exc:
                self.notify("error", message=f"книгу не переоткрыло: {exc}")

    def translate_all(self, limit: int | None = None) -> dict:
        try:
            self.reader.translate_whole_book(limit)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        self.notify("translate_started")
        return {"ok": True}


app_state: dict = {}


def _ctx() -> WebApp:
    return app_state["app"]


# --- маршруты ---------------------------------------------------------------

async def index(request):
    return FileResponse(WEB / "index.html",
                        headers={"Cache-Control": "no-store"})


async def api_info(request):
    ctx = _ctx()
    ctx.config.reload()
    return JSONResponse(ctx.reader.info())


async def api_open(request):
    data = await request.json()
    path = (data or {}).get("path", "").strip()
    if not path:
        return JSONResponse({"error": "не указан путь"}, status_code=400)
    try:
        return JSONResponse(_ctx().open_book(path))
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def api_upload(request):
    form = await request.form()
    upload = form.get("file")
    if upload is None:
        return JSONResponse({"error": "файл не пришёл"}, status_code=400)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    name = Path(getattr(upload, "filename", "book.bin")).name
    target = UPLOAD_DIR / name
    data = await upload.read() if hasattr(upload, "read") else b""
    target.write_bytes(data)
    try:
        return JSONResponse(_ctx().open_book(str(target)))
    except (ValueError, RuntimeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def api_book(request):
    ctx = _ctx()
    if not ctx.reader.book:
        return JSONResponse({})
    return JSONResponse(ctx.book_dict())


async def api_control(request):
    ctx = _ctx()
    data = await request.json()
    action = (data or {}).get("action", "")
    try:
        if action == "start":
            return JSONResponse(ctx.start_reading(int(data.get("index", 0)),
                                                  data.get("prefetch")))
        if action == "goto":
            index = int(data.get("index", 0))
            ctx.start_reading(index, data.get("prefetch"))
            if ctx.reader.book:
                state.set_read_position(ctx.reader.book.key, index)
            return JSONResponse({"index": index})
        if action == "stop":
            ctx.notify("stopped")
            return JSONResponse({"ok": True})
        if action == "voice":
            ctx.reader.set_voice(data.get("model"), data.get("speaker"))
            ctx.notify("voice", status=ctx.reader.voice.status())
            return JSONResponse({"voice": ctx.reader.voice.status()})
        if action == "translate":
            enabled = bool(data.get("enabled", True))
            ctx.reader.set_translate(enabled, data.get("engine"))
            ctx.notify("translate", mode=ctx.reader.translate_mode)
            return JSONResponse({"mode": ctx.reader.translate_mode})
        if action == "translate_all":
            return JSONResponse(ctx.translate_all(data.get("limit")))
        if action == "prefetch":
            ctx.reader.prefetch = int(data.get("value", ctx.reader.prefetch))
            return JSONResponse({"prefetch": ctx.reader.prefetch})
        if action == "progress":
            ctx.notify("progress", **ctx.reader.info())
            return JSONResponse({"ok": True})
    except (KeyError, ValueError, TypeError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"error": f"неизвестное действие: {action}"}, status_code=400)


async def api_block(request):
    """Один абзац: текст (переведённый, если готов) и состояние озвучки."""
    ctx = _ctx()
    try:
        index = int(request.path_params["index"])
    except (KeyError, ValueError):
        return JSONResponse({"error": "плохой индекс"}, status_code=400)
    if not ctx.reader.book:
        return JSONResponse({"error": "книга не открыта"}, status_code=404)
    if not 0 <= index < ctx.reader.book.block_count:
        return JSONResponse({"error": "абзаца нет"}, status_code=404)
    block = ctx.reader.block(index)
    if not block.ready:
        ctx.reader.prepare(index)
        ctx.reader.prepare_ahead(index)
    return JSONResponse(block.to_dict())


async def api_blocks(request):
    """Диапазон абзацев: браузер так добирает книгу при прокрутке."""
    ctx = _ctx()
    if not ctx.reader.book:
        return JSONResponse({"error": "книга не открыта"}, status_code=404)
    try:
        first = max(0, int(request.query_params.get("from", 0)))
        count = min(200, max(1, int(request.query_params.get("count", 100))))
    except ValueError:
        return JSONResponse({"error": "плохие границы"}, status_code=400)
    total = ctx.reader.book.block_count
    result = []
    for index in range(first, min(total, first + count)):
        block = ctx.reader.block(index)
        if not block.ready:
            ctx.reader.prepare(index)
        result.append(block.to_dict())
    return JSONResponse({"from": first, "total": total, "blocks": result})


async def api_settings(request):
    """Все настройки разом: схема плюс текущие значения."""
    config = _ctx().config
    config.reload()                 # настройки могли поменять из терминала
    return JSONResponse(settings_mod.describe(config))


async def api_settings_put(request):
    """Сохранить настройки. Значения летят в state/config.json."""
    ctx = _ctx()
    ctx.config.reload()
    data = await request.json()
    values = (data or {}).get("values", {})
    if not isinstance(values, dict) or not values:
        return JSONResponse({"error": "нечего сохранять"}, status_code=400)
    accepted, errors = settings_mod.apply(ctx.config, values)
    if accepted:
        ctx.on_settings_changed(accepted)
    ctx.hub.broadcast({"event": "settings", "values": ctx.config.as_dict(),
                       "accepted": accepted})
    if errors and not accepted:
        return JSONResponse({"error": "; ".join(errors)}, status_code=400)
    return JSONResponse({"values": ctx.config.as_dict(), "accepted": accepted,
                         "errors": errors,
                         "restart": settings_mod.restart_needed(accepted)})


async def api_preview(request):
    """Кусок озвучки текущими настройками: послушать, не открывая книгу."""
    ctx = _ctx()
    from .audio import write_wav
    from .voice import Voice, VoiceError

    try:
        voice = Voice(ctx.config)
        samples = voice.preview(seconds=4.0)
    except VoiceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    except Exception as exc:                   # noqa: BLE001
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"},
                            status_code=500)
    target = BOOKS / "_preview.wav"
    write_wav(target, samples, voice.play_rate)
    return FileResponse(str(target), media_type="audio/wav",
                        headers={"Cache-Control": "no-store"})


async def api_translate_test(request):
    """Проверить перевод на короткой фразе, не открывая книгу."""
    ctx = _ctx()
    from .translate import EngineError, Translator
    from . import langid as langid_mod

    text = (await request.json() or {}).get("text", "").strip()
    if not text:
        text = "The old man sat by the window and watched the rain."
    try:
        translator = Translator(ctx.config)
        source, _ = langid_mod.detect(text, default="en")
        started = time.time()
        result = translator.translate(text, source, ctx.config["target_lang"],
                                      use_cache=False)
    except EngineError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    except Exception as exc:                   # noqa: BLE001
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"},
                            status_code=500)
    return JSONResponse({"source": source, "text": result,
                         "engine": translator.engine,
                         "seconds": round(time.time() - started, 1)})


async def api_audio(request):
    """WAV абзаца. Если озвучка ещё готовится — 202, браузер спросит ещё раз."""
    ctx = _ctx()
    try:
        index = int(request.path_params["index"])
    except (KeyError, ValueError):
        return JSONResponse({"error": "плохой индекс"}, status_code=400)
    if not ctx.reader._in_book(index):
        return JSONResponse({"error": "абзаца нет"}, status_code=404)
    block = ctx.reader.block(index)
    if block.path and Path(block.path).exists():
        return FileResponse(str(block.path), media_type="audio/wav",
                            headers={"Cache-Control": "public, max-age=3600"})
    if not block.ready:
        ctx.reader.prepare(index)
        # 202 -> «ещё готовится», браузер спросит снова.
        # Но если поток подготовки умер, 202 будет крутиться вечно:
        # в этом случае честно отдаём 503 с причиной.
        if ctx.reader.worker_alive():
            return Response(status_code=202, headers={"Retry-After": "1"})
        return JSONResponse(
            {"error": ctx.reader.worker_error
             or "поток озвучки остановился, перезапусти сервер"}, status_code=503)
    return JSONResponse({"error": block.error or "озвучка не готовится"},
                        status_code=503)


async def api_recent(request):
    rows = state.recent_books(20)
    return JSONResponse([{
        "key": row["key"], "path": row["path"], "title": row["title"],
        "author": row["author"], "lang": row["lang"], "format": row["format"],
        "blocks": row["blocks"], "opened_at": row["opened_at"],
        "read_to": row["read_to"],
    } for row in rows])


async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    ctx = _ctx()
    ctx.hub.attach(websocket)
    try:
        await websocket.send_json({"event": "hello",
                                   "info": ctx.reader.info()})
        while True:
            message = await websocket.receive_text()
            try:
                data = json.loads(message)
            except ValueError:
                continue
            if data.get("event") == "ping":
                await websocket.send_json({"event": "pong",
                                           "time": time.time(),
                                           "info": ctx.reader.info()})
            elif data.get("event") == "position":
                if ctx.reader.book:
                    state.set_read_position(ctx.reader.book.key,
                                           int(data.get("index", 0)))
            elif data.get("event") == "progress":
                await websocket.send_json({"event": "progress",
                                           "info": ctx.reader.info()})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        ctx.hub.detach(websocket)


async def ticker():
    """Раз в 2 секунды сообщаем браузеру, как идёт подготовка."""
    ctx = _ctx()
    last = ""
    while True:
        await asyncio.sleep(2.0)
        if not ctx.reader.book:
            continue
        info = ctx.reader.info()
        ready = info.get("ready_blocks", 0)
        current = f"{ready}:{info.get('translate')}:{info.get('progress')}"
        if current != last:
            last = current
            ctx.hub.broadcast({"event": "progress", "info": info})


def build_app(config: Config | None = None) -> Starlette:
    ctx = WebApp(config)
    app_state["app"] = ctx

    @asynccontextmanager
    async def lifespan(application):                 # noqa: ARG001
        task = asyncio.create_task(ticker())
        yield
        task.cancel()

    routes = [
        Route("/", index),
        Route("/api/info", api_info),
        Route("/api/open", api_open, methods=["POST"]),
        Route("/api/upload", api_upload, methods=["POST"]),
        Route("/api/book", api_book),
        Route("/api/control", api_control, methods=["POST"]),
        Route("/api/block/{index:int}", api_block),
        Route("/api/blocks", api_blocks),
        Route("/api/audio/{index:int}", api_audio),
        Route("/api/recent", api_recent),
        Route("/api/settings", api_settings),
        Route("/api/settings", api_settings_put, methods=["PUT"]),
        Route("/api/settings/preview", api_preview, methods=["POST"]),
        Route("/api/settings/translate", api_translate_test, methods=["POST"]),
        WebSocketRoute("/ws", ws_endpoint),
        Mount("/images", app=StaticFiles(directory=str(BOOKS), check_dir=False)),
        Mount("/static", app=StaticFiles(directory=str(WEB), check_dir=False)),
    ]
    return Starlette(routes=routes, lifespan=lifespan)


def run(config: Config | None = None, host: str | None = None,
        port: int | None = None) -> None:
    import uvicorn

    config = config or Config()
    host = host or config["host"]
    port = int(port or config["port"])
    app = build_app(config)
    print(f"читалка: http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning",
                access_log=False)