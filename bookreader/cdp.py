"""Проверка страницы в настоящем браузере через CDP.

Свой отладочный порт и свой профиль: занятый порт перехватывает чужой
браузер, и ты меришь старую страницу вместо своей.
Браузер берём из~/.cache/ms-playwright, системный slimjet не умеет
фейковый микрофон и часть флагов.
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

JS_SET_VOICE = (
    "(() => { const rows = [...document.querySelectorAll('#settingsBody .field')];"
    " const row = rows.find(n => n.querySelector('label').textContent === 'Голос');"
    " const select = row.querySelector('select'); select.value = %s;"
    " select.dispatchEvent(new Event('change')); return true; })()")

JS_PICK_VOICE = "(() => {\n  const rows = [...document.querySelectorAll('#settingsBody .field')];\n  const row = rows.find(n => n.querySelector('label').textContent === 'Голос');\n  const select = row.querySelector('select');\n  select.value = 'kseniya';\n  select.dispatchEvent(new Event('change'));\n  return true;\n})()"

CHROME_GLOBS = ("chromium-*/chrome-linux64/chrome", "chromium-*/chrome-linux/chrome")


def find_chrome() -> str | None:
    """Chromium для проверок: сначала системный, потом из кэша playwright."""
    root = Path.home() / ".cache/ms-playwright"
    for pattern in CHROME_GLOBS:
        found = sorted(root.glob(pattern))
        if found:
            return str(found[-1])
    for candidate in ("/usr/bin/chromium", "/usr/bin/chromium-browser",
                      "/usr/bin/google-chrome"):
        if Path(candidate).exists():
            return candidate
    return shutil.which("chromium") or shutil.which("google-chrome")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Browser:
    """Минимальный клиент CDP: открыть страницу, кликнуть, выполнить JS."""

    def __init__(self, headless: bool = True, timeout: float = 30.0):
        self.chrome = find_chrome()
        self.headless = headless
        self.timeout = timeout
        self.port = free_port()
        self.profile = Path(tempfile.mkdtemp(prefix="bookreader-cdp-"))
        self.process: subprocess.Popen | None = None
        self.socket = None
        self.ws_url = ""
        self._id = 0

    # --- запуск -------------------------------------------------------------
    def start(self) -> None:
        if not self.chrome:
            raise RuntimeError("не найден браузер для проверки")
        args = [
            self.chrome,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile}",
            "--no-first-run", "--no-default-browser-check",
            "--disable-gpu", "--no-sandbox",
            "--autoplay-policy=no-user-gesture-required",
            "--window-size=1400,900",
            "about:blank",
        ]
        if self.headless:
            args.insert(1, "--headless=new")
        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
        deadline = time.time() + self.timeout
        info_url = f"http://127.0.0.1:{self.port}/json/version"
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(info_url, timeout=2) as response:
                    self.ws_url = json.loads(response.read().decode())[
                        "webSocketDebuggerUrl"]
                    break
            except Exception:
                time.sleep(0.3)
        else:
            raise RuntimeError("браузер не поднялся")

    def stop(self) -> None:
        if self.socket is not None:
            try:
                asyncio.run(self.socket.close())
            except Exception:
                pass
            self.socket = None
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        shutil.rmtree(self.profile, ignore_errors=True)

    # --- протокол -----------------------------------------------------------
    async def _connect(self, ws_url: str):
        import websockets

        if self.socket is None or getattr(self, "_ws", None) != ws_url:
            self.socket = await websockets.connect(ws_url, max_size=64 * 1024 * 1024)
            self._ws = ws_url
            self.ws_url = ws_url          # дальше говорим именно с этой вкладкой
        return self.socket

    async def _send(self, method: str, params: dict | None = None) -> dict:
        socket = await self._connect(self.ws_url)
        self._id += 1
        await socket.send(json.dumps({"id": self._id, "method": method,
                                      "params": params or {}}))
        while True:
            raw = await asyncio.wait_for(socket.recv(), timeout=self.timeout)
            data = json.loads(raw)
            if data.get("id") == self._id:
                if "error" in data:
                    raise RuntimeError(f"{method}: {data['error']}")
                return data.get("result", {})

    async def visit(self, url: str) -> None:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/json/new?{url}", method="PUT")
        with urllib.request.urlopen(request, timeout=10) as response:
            target = json.loads(response.read().decode())
        await self._connect(target["webSocketDebuggerUrl"])
        await self._send("Page.enable")
        await self._send("Runtime.enable")
        await self._send("Network.enable")
        await self._send("Network.setCacheDisabled", {"cacheDisabled": True})
        origin = url.split("/")[0] + "//" + url.split("/")[2]
        await self._send("Browser.grantPermissions", {
            "permissions": ["audioCapture"], "origin": origin})

    async def js(self, expression: str, await_promise: bool = False):
        result = await self._send("Runtime.evaluate", {
            "expression": expression, "awaitPromise": await_promise,
            "returnByValue": True})
        if result.get("exceptionDetails"):
            raise RuntimeError(result["exceptionDetails"].get("text", "ошибка JS"))
        return result.get("result", {}).get("value")

    async def click(self, selector: str) -> bool:
        box = await self.js(f"""(() => {{
          const node = document.querySelector({json.dumps(selector)});
          if (!node) return null;
          node.scrollIntoView();
          const r = node.getBoundingClientRect();
          return {{x: r.x + r.width / 2, y: r.y + r.height / 2}};
        }})()""")
        if not box:
            return False
        for kind in ("mousePressed", "mouseReleased"):
            await self._send("Input.dispatchMouseEvent", {
                "type": kind, "x": box["x"], "y": box["y"],
                "button": "left", "clickCount": 1})
        return True

    async def wait_for(self, expression: str, timeout: float = 30.0,
                       interval: float = 0.5) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if await self.js(expression):
                    return True
            except RuntimeError:
                pass
            await asyncio.sleep(interval)
        return False

    async def screenshot(self, path: Path) -> Path:
        result = await self._send("Page.captureScreenshot", {"format": "png"})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(result["data"]))
        return path


async def _check(url: str, shot: Path | None, listen: float) -> dict:
    browser = Browser(headless=True)
    report: dict = {"браузер": Path(browser.chrome).name if browser.chrome else "нет"}
    try:
        browser.start()
        await browser.visit(url)
        report["страница открылась"] = await browser.wait_for(
            "document.querySelectorAll('#text p').length > 3", timeout=60)
        report["абзацев на экране"] = await browser.js(
            "document.querySelectorAll('#text p').length")
        report["название книги"] = await browser.js(
            "document.getElementById('title').textContent")
        report["картинок"] = await browser.js(
            "document.querySelectorAll('#text img, aside img').length")
        report["статус до чтения"] = await browser.js(
            "document.getElementById('status').textContent")

        # окно настроек: открывается, поля есть, значение сохраняется
        report["окно настроек открылось"] = await browser.click("#settingsBtn")
        report["полей в настройках"] = await browser.js(
            "document.querySelectorAll('#settingsBody .field').length")
        report["есть группы Голос и Перевод"] = await browser.js(
            "[...document.querySelectorAll('#settingsBody fieldset legend')]"
            ".map(n => n.textContent).join(',')")
        await browser.wait_for(
            "document.getElementById('settings').classList.contains('open')"
            " && settingsSchema && settingsSchema.groups.length > 0", timeout=20)
        original = await browser.js(
            "settingsSchema && settingsSchema.values.voice_speaker")
        await browser.js(JS_PICK_VOICE)
        # проверяем состояние страницы, а не fetch: значение уже пришло ответом
        report["выбор голоса сохранился"] = bool(await browser.wait_for(
            "settingsSchema && settingsSchema.values.voice_speaker === 'kseniya'",
            timeout=20))
        if original:                       # вернуть как было, проверка не должна менять настройки
            await browser.js(JS_SET_VOICE % json.dumps(original))
            await browser.wait_for(
                "settingsSchema && settingsSchema.values.voice_speaker === "
                + json.dumps(original), timeout=20)
        report["лишние поля скрыты"] = await browser.js(
            "document.querySelectorAll('#settingsBody .field.hidden').length")
        await browser.js("document.getElementById('closeSettings').click()")

        report["кнопка play"] = await browser.click("#play")
        report["звук пошёл"] = await browser.wait_for(
            "!!document.getElementById('audio').src && "
            "!document.getElementById('audio').paused", timeout=60)
        await asyncio.sleep(listen)
        report["текущий абзац"] = await browser.js(
            "Number((document.querySelector('#text p.now') || {dataset:{}}).dataset"
            ".index)")
        report["позиция в звуке, с"] = await browser.js(
            "Math.round(document.getElementById('audio').currentTime * 10) / 10")
        report["счётчик"] = await browser.js(
            "document.getElementById('counter').textContent")
        report["статус при чтении"] = await browser.js(
            "document.getElementById('status').textContent")
        if shot:
            report["скриншот"] = str(await browser.screenshot(shot))
    finally:
        browser.stop()
    return report


def check_reader(url: str, shot: Path | None = None, listen: float = 8.0) -> dict:
    """Открыть читалку, нажать «читать», дождаться звука и снять скриншот."""
    return asyncio.run(_check(url, shot, listen))