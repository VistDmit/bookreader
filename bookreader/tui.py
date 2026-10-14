"""Чтение в терминале: список книг, плеер, прогресс.

textual рисует интерфейс, звук играет sounddevice через bookreader.player.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import (Button, Footer, Header, Input, Label, ListItem,
                             ListView, Static)

from . import settings as settings_mod
from . import state
from .audio import write_wav
from .config import CACHE, Config
from .player import Player
from .reader import PlayerBridge, Reader


class SettingsScreen(ModalScreen):
    """Настройки в терминале: тот же список, что и в вебе."""

    BINDINGS = [
        Binding("escape", "close", "Закрыть"),
        Binding("q", "close", "Закрыть"),
    ]

    def __init__(self, config: Config, on_change=None):
        super().__init__()
        self.config = config
        self.on_change = on_change
        self.specs: list = []
        self.groups: list = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Label("Настройки — стрелки меняют значение, t проверить голос, "
                    "пробел переключает, Esc закрыть")
        yield ListView(id="setlist")
        yield Static("", id="sethint")
        yield Footer()

    def on_mount(self) -> None:
        self.rebuild()

    def rebuild(self) -> None:
        """Пересобрать список под текущие значения (варианты зависят от модели)."""
        described = settings_mod.describe(self.config)
        self.groups = described["groups"]
        listing = self.query_one("#setlist", ListView)
        listing.clear()
        self.specs = []
        for group in self.groups:
            header = ListItem(Label(f"— {group['label'].upper()} —"))
            header.disabled = True
            listing.append(header)
            for spec in group["fields"]:
                spec["value"] = self.config[spec["key"]]
                # варианты (например, список голосов) зависят от текущих значений
                spec["choices"] = settings_mod.resolve(
                    settings_mod.field_by_key(spec["key"]), self.config)
                self.specs.append(spec)
                listing.append(self._row(spec))

    def _row_text(self, spec: dict) -> str:
        text = f"  {spec['label']:<26} {self._shown(spec)}"
        if spec["hint"]:
            text += f"   [dim]{spec['hint']}[/dim]"
        if spec["restart"]:
            text += "  [yellow](перезапуск)[/yellow]"
        return text

    def _row(self, spec: dict) -> ListItem:
        item = ListItem(Label(self._row_text(spec)))
        item.spec = spec
        return item

    def _shown(self, spec: dict) -> str:
        value = self.config[spec["key"]]
        if spec["kind"] == "bool":
            return "да" if value else "нет"
        return f"{value}"

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        item = event.item
        spec = getattr(item, "spec", None)
        if spec:
            self.query_one("#sethint", Static).update(
                f"{spec['label']}: {self._shown(spec)}   {spec['hint']}")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self._change(event.item, +1)

    def on_key(self, event) -> None:
        listing = self.query_one("#setlist", ListView)
        index = listing.index
        spec = getattr(listing.children[index], "spec", None) if index is not None \
            else None
        if event.key in ("left", "minus", "minus_sign"):
            self._change(listing.children[index], -1)
        elif event.key in ("right", "plus", "plus_sign"):
            self._change(listing.children[index], +1)
        elif event.key in ("space", "enter"):
            self._change(listing.children[index], +1)
        elif event.key == "t":
            self._test_voice()
        else:
            return
        event.stop()

    def _change(self, item, direction: int) -> None:
        spec = getattr(item, "spec", None)
        if not spec:
            return
        value = self.config[spec["key"]]
        if spec["kind"] == "bool":
            new_value = not value
        elif spec["kind"] == "choice" and spec["choices"]:
            choices = spec["choices"]
            position = choices.index(str(value)) if str(value) in choices else 0
            new_value = choices[(position + direction) % len(choices)]
        elif spec["kind"] in ("int", "float"):
            step = float(spec["step"] or 1)
            if spec["kind"] == "int":
                new_value = int(round(float(value) + direction * step))
            else:
                new_value = round(float(value) + direction * step, 3)
        else:
            new_value = value
        accepted, errors = settings_mod.apply(self.config, {spec["key"]: new_value})
        if errors:
            self.query_one("#sethint", Static).update(errors[0])
            return
        item.query_one(Label).update(self._row_text(spec))
        hint = f"{spec['label']}: {self._shown(spec)}"
        if spec["restart"]:
            hint += "  (нужен перезапуск сервера)"
        self.query_one("#sethint", Static).update(hint)
        if self.on_change:
            self.on_change(accepted)
        if spec["key"] in ("voice_model", "voice_engine"):
            self.rebuild()          # варианты голоса зависят от модели

    def _test_voice(self) -> None:
        self.query_one("#sethint", Static).update("озвучиваю образец...")
        from .voice import Voice

        try:
            voice = Voice(self.config)
            samples = voice.preview(seconds=3.0)
            target = CACHE / "preview.wav"
            write_wav(target, samples, voice.play_rate)
            from .player import Player as PreviewPlayer

            self.app.play_preview(samples, voice.play_rate)
            self.query_one("#sethint", Static).update(
                f"образец {target} ({voice.speaker})")
        except Exception as exc:                       # noqa: BLE001
            self.query_one("#sethint", Static).update(f"не получилось: {exc}")

    def action_close(self) -> None:
        self.dismiss(None)


class BookTui(App):
    """Книжный помощник в терминале."""

    CSS = """
    Screen { layout: vertical; }
    #top { height: 3; }
    #status { height: 1; color: $accent; }
    #list { height: 12; border: round $accent; }
    #body { height: 1fr; border: round $primary; padding: 1 2; }
    #images { height: 8; border: round $secondary; }
    """

    BINDINGS = [
        Binding("space", "toggle", "Читать/пауза"),
        Binding("s", "stop", "Стоп"),
        Binding("n", "next", "Вперёд"),
        Binding("b", "prev", "Назад"),
        Binding("f", "faster", "Быстрее"),
        Binding("g", "slower", "Медленнее"),
        Binding("o", "open", "Открыть"),
        Binding("t", "translate", "Перевод"),
        Binding("m", "settings", "Настройки"),
        Binding("q", "quit", "Выход"),
    ]

    def __init__(self, config: Config | None = None, book_path: str | None = None):
        super().__init__()
        self.config = config or Config()
        self.reader = Reader(self.config)
        self.player = Player(rate=22050, device=self.config["voice_output_device"],
                             on_event=self._on_event)
        self.bridge: PlayerBridge | None = None
        self.current = 0
        self.book_path = book_path
        self._lock = threading.Lock()
        self._preview_player = None

    # --- интерфейс ----------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="top"):
            yield Input(placeholder="путь к книге (fb2, epub, txt, pdf, docx)",
                        id="path")
            yield Button("Открыть", id="open", variant="primary")
        yield Static("", id="status")
        yield ListView(id="list")
        with VerticalScroll(id="body"):
            yield Static("", id="text")
        yield Static("", id="images")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(0.5, self._tick)
        self.set_interval(6.0, self._refresh_recent)
        self._refresh_recent()
        if self.book_path:
            self.query_one("#path", Input).value = self.book_path
            self.action_open()
        else:
            self._show_hint()

    def _show_hint(self) -> None:
        self.query_one("#text", Static).update(
            "Выбери книгу в списке или впиши путь и нажми «Открыть».\n"
            "Пробел — читать, n/b — абзац вперёд-назад, f/g — темп, t — перевод.")

    def _refresh_recent(self) -> None:
        rows = state.recent_books(20)
        if not rows:
            return
        listing = self.query_one("#list", ListView)
        if listing.children:
            return
        for row in rows:
            name = row["title"] or Path(row["path"]).name
            item = ListItem(Label(f"{name}  [{row['format']}, {row['blocks']} абз.]"))
            item.path = row["path"]
            listing.append(item)

    # --- события ------------------------------------------------------------
    def on_list_view_selected(self, event: ListView.Selected) -> None:
        path = getattr(event.item, "path", None)
        if path:
            self.query_one("#path", Input).value = path
            self.action_open()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "open":
            self.action_open()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_open()

    def _on_event(self, event: str, index: int, position: float) -> None:
        if event == "block" and index >= 0:
            self.call_from_thread(self._show_block, index)

    def _tick(self) -> None:
        with self._lock:
            index, position = self.player.current_index, self.player.position
            state_ = self.player.state
        info = self.reader.info()
        if not info:
            return
        self.query_one("#status", Static).update(
            f"{self._status_text()} · {info['voice']}")

    def action_settings(self) -> None:
        """Окно настроек: те же значения, что и в веб-читалке."""
        if self.is_screen_installed(SettingsScreen):
            return
        screen = SettingsScreen(self.config, on_change=self._on_settings)
        self.push_screen(screen, callback=self._settings_closed)

    def _settings_closed(self, screen=None) -> None:
        if screen is not None:
            self.refresh(recompose=True)
        self.query_one("#status", Static).update(self._status_text())

    def _on_settings(self, changed: dict) -> None:
        """Настройки применились: голос и очередь обновляем на ходу."""
        voice_keys = {"voice_engine", "voice_model", "voice_speaker", "piper_model",
                      "sample_rate", "sample_rate_play", "pause_after_paragraph"}
        if changed.keys() & voice_keys:
            try:
                self.reader.set_voice()
            except Exception as exc:                 # noqa: BLE001
                self.query_one("#status", Static).update(f"голос: {exc}")
        if "prefetch" in changed:
            self.reader.prefetch = int(changed["prefetch"])
        if "speed" in changed and self.player.state == "stopped":
            self.player.set_speed(float(changed["speed"]))
        self.query_one("#status", Static).update(
            f"настроек изменено: {', '.join(changed)}")

    def play_preview(self, samples, rate: int) -> None:
        """Проиграть образец голоса прямо в терминале."""
        from .player import Player as PreviewPlayer

        if self._preview_player is None:
            self._preview_player = PreviewPlayer(rate=rate,
                                                 device=self.config[
                                                     "voice_output_device"])
        self._preview_player.clear()
        self._preview_player.push(0, samples, rate)
        self._preview_player.play()

    def _status_text(self) -> str:
        info = self.reader.info()
        if not info:
            return ""
        index, position = self.player.current_index, self.player.position
        return (f"{Path(info['path']).name} · {fmt(index + 1)}/{fmt(info['blocks'])} · "
                f"{int(position)} с · {self.player.state} · язык {info['lang']} · "
                f"{'перевод ' + info['translate'] if info['translate'] != 'off' else 'без перевода'}")

    def _show_block(self, index: int) -> None:
        block = self.reader.block(index)
        images = block.images
        head = f"[b]{block.kind}[/b]  абзац {index + 1}\n\n"
        self.query_one("#text", Static).update(head + block.text)
        if images:
            names = ", ".join(images)
            self.query_one("#images", Static).update(f"картинки: {names}")
        else:
            self.query_one("#images", Static).update("")

    # --- команды ------------------------------------------------------------
    def action_open(self) -> None:
        path = self.query_one("#path", Input).value.strip()
        if not path:
            self._show_hint()
            return
        try:
            self.reader.open(path)
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            self.query_one("#status", Static).update(f"не открылось: {exc}")
            return
        info = self.reader.info()
        self.query_one("#status", Static).update(
            f"{info['title']} · {info['blocks']} абз. · язык {info['lang']} · "
            f"{'перевод ' + info['translate'] if info['translate'] != 'off' else 'без перевода'}")
        self._show_block(0)

    def action_toggle(self) -> None:
        if not self.reader.book:
            self._show_hint()
            return
        if self.player.state == "paused":
            self.player.resume()
            return
        if self.player.state in ("playing", "waiting"):
            self.player.pause()
            return
        start = state.read_position(self.reader.book.key)
        self.current = start
        self.bridge = PlayerBridge(self.reader, self.player, on_block=self._on_block)
        self.bridge.start(start)

    def _on_block(self, index: int) -> None:
        self.current = index
        if self.reader.book:
            state.set_read_position(self.reader.book.key, index)

    def action_stop(self) -> None:
        if self.bridge:
            self.bridge.stop()
        self.player.stop()

    def action_next(self) -> None:
        if not self.reader.book:
            return
        target = min(self.player.current_index + 1, self.reader.book.block_count - 1)
        self.player.skip_to(max(0, target))

    def action_prev(self) -> None:
        target = self.player.current_index - 1
        self.player.skip_to(max(0, target))

    def action_faster(self) -> None:
        self.player.set_speed(self.player.speed + 0.1)
        self.config.update(speed=self.player.speed)
        self.query_one("#status", Static).update(f"темп {self.player.speed:.2f}")

    def action_slower(self) -> None:
        self.player.set_speed(self.player.speed - 0.1)
        self.config.update(speed=self.player.speed)
        self.query_one("#status", Static).update(f"темп {self.player.speed:.2f}")

    def action_translate(self) -> None:
        if not self.reader.book:
            return
        if self.reader.translate_mode == "off":
            self.reader.set_translate(True)
            self.reader.open(self.reader.book.path, want_images=False)
        else:
            self.reader.set_translate(False)
        info = self.reader.info()
        self.query_one("#status", Static).update(
            f"перевод: {info['translate']} ({info.get('error') or 'ок'})")

    def save_voice_sample(self, seconds: float = 6.0) -> Path:
        """Положить кусок озвучки в файл: послушать, не читая книгу."""
        voice = self.reader.voice
        if voice is None:
            raise RuntimeError("книга не открыта, голос не загружен")
        samples = voice.preview(seconds=seconds)
        target = CACHE / "preview.wav"
        write_wav(target, samples, voice.play_rate)
        return target


def fmt(number: int) -> str:
    return f"{number:,}".replace(",", " ")


def run(config: Config | None = None, book_path: str | None = None) -> None:
    app = BookTui(config, book_path)
    app.run()


def preview_to_file(config: Config | None = None, seconds: float = 6.0) -> Path:
    """Сохранить пример голоса в WAV (для проверки без чтения книги)."""
    from .voice import Voice

    voice = Voice(config or Config())
    samples = voice.preview(seconds=seconds)
    CACHE.mkdir(parents=True, exist_ok=True)
    target = CACHE / "preview.wav"
    write_wav(target, samples, voice.play_rate)
    return target


def wait_forever(seconds: float) -> None:
    time.sleep(seconds)