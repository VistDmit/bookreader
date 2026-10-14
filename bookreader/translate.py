"""Перевод книг: NLLB-200 локально, при неудаче — онлайн-сервисы.

Модель NLLB уже скачана в CTranslate2-формате (630 МБ) и работает
на CPU. Онлайн-переводчик — запасной путь, если офлайн-модель
не знает язык или её не скачали.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

from . import langid, state
from .config import MODELS, Config

DEFAULT_REPO = "mijuanlo/nllb-200-distilled-600M-ct2-int8"
BIG_REPO = "OpenNMT/nllb-200-distilled-1.3B-ct2-int8"
REQUIRED_FILES = ("model.bin", "tokenizer.json")
OPTIONAL_FILES = ("config.json", "tokenizer_config.json", "special_tokens_map.json",
                  "sentencepiece.bpe.model", "shared_vocabulary.json",
                  "generation_config.json")
MODEL_FILES = REQUIRED_FILES + OPTIONAL_FILES


def model_dir(repo: str) -> Path:
    """Папка модели по имени репозитория: 600M и 1.3B лежат рядом."""
    return MODELS / repo.split("/")[-1]


MODEL_DIR = model_dir(DEFAULT_REPO)
MODEL_REPO = DEFAULT_REPO

INTRA_THREADS = 8        # 8 физических ядер, 16 потоков дают меньший выигрыш
INTER_THREADS = 1        # перевод идёт одним потоком, но пачками
BEAM_SIZE = 4            # меньше — быстрее, но заметно грубее перевод
BATCH = 4                # сколько предложений отдаём модели за раз

# предложения режем аккуратно: сокращения и инициалы не разрываем
SPLIT_RE = re.compile(r"(?<=[.!?…])[\s ]+(?=[\"'«(\[]?[A-ZА-ЯЁЇІЄҐA-Z0-9])")
ABBREV = ("т.е.", "т.д.", "т.п.", "и.о.", "др.", "см.", "стр.", "г.", "гг.",
          "в.", "с.", "им.", "фиг.", "руб.", "коп.", "тыс.", "мин.", "ч.")


def split_sentences(text: str, limit: int = 400) -> list[str]:
    """Разбить абзац на куски помельче: NLLB лучше работает с короткими фразами."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    parts = []
    for piece in SPLIT_RE.split(text):
        piece = piece.strip()
        if not piece:
            continue
        if any(piece.lower().endswith(a) for a in ABBREV):
            # склеили обратно с предыдущим: сокращение не конец фразы
            if parts:
                parts[-1] = parts[-1] + " " + piece
                continue
        if len(piece) <= limit:
            parts.append(piece)
            continue
        # слишком длинный кусок режем по запятым, иначе по словам
        chunks = re.split(r"(?<=[,;:—–])\s+", piece)
        buf = ""
        for chunk in chunks:
            if len(buf) + len(chunk) + 1 > limit and buf:
                parts.append(buf.strip())
                buf = chunk
            else:
                buf = f"{buf} {chunk}".strip()
        if buf:
            parts.append(buf.strip())
    return [p for p in parts if p]


class EngineError(RuntimeError):
    pass


class NllbEngine:
    """Локальный перевод NLLB-200 через CTranslate2 (int8, CPU)."""

    name = "nllb"

    def __init__(self, repo: str = DEFAULT_REPO, threads: int = INTRA_THREADS):
        self.repo = repo
        self.model_dir = model_dir(repo)
        self.threads = threads
        self._translator = None
        self._tokenizer = None
        self._lock = threading.Lock()

    # --- готовность ---------------------------------------------------------
    def is_ready(self) -> bool:
        return (self.model_dir / "model.bin").exists() and \
               (self.model_dir / "tokenizer.json").exists()

    def status(self) -> str:
        if not self.is_ready():
            return f"модель не скачана ({self.repo})"
        return f"{self.repo} на месте: {self.model_dir}"

    def load(self) -> None:
        if self._translator is not None:
            return
        if not self.is_ready():
            raise EngineError(self.status())
        import ctranslate2
        from transformers import AutoTokenizer

        self._tokenizer = AutoTokenizer.from_pretrained(str(self.model_dir))
        self._translator = ctranslate2.Translator(
            str(self.model_dir),
            device="cpu",
            compute_type="int8",
            inter_threads=INTER_THREADS,
            intra_threads=self.threads,
        )

    # --- перевод ------------------------------------------------------------
    def _encode(self, text: str, src_code: str) -> list[str]:
        """<s> код_языка токены… </s> — так модель понимает, с какого языка."""
        self._tokenizer.src_lang = src_code
        ids = list(self._tokenizer(text)["input_ids"])
        if ids and ids[-1] == self._tokenizer.eos_token_id:
            ids.pop()
        ids = [self._tokenizer.bos_token_id] + ids + [self._tokenizer.eos_token_id]
        return self._tokenizer.convert_ids_to_tokens(ids)

    def _decode(self, tokens: list[str], tgt_code: str) -> str:
        clean = []
        for token in tokens:
            if token in ("</s>", "<pad>", "<s>", tgt_code):
                continue
            clean.append(token)
        return self._tokenizer.convert_tokens_to_string(clean).strip()

    def translate(self, texts: list[str], source: str, target: str) -> list[str]:
        """CTranslate2 4.x: токены строками, целевой язык — через target_prefix."""
        if not texts:
            return []
        self.load()
        src_code = langid.nllb_code(source)
        tgt_code = langid.nllb_code(target)
        if not src_code:
            raise EngineError(f"язык {source} модель не знает")
        if not tgt_code:
            raise EngineError(f"язык {target} модель не знает")

        sources = [self._encode(text, src_code) for text in texts]
        with self._lock:
            results = self._translator.translate_batch(
                sources,
                target_prefix=[[tgt_code]] * len(sources),
                beam_size=BEAM_SIZE,
                max_decoding_length=384,
                max_input_length=512,
                replace_unknowns=True,
            )
            return [self._decode(r.hypotheses[0], tgt_code) for r in results]


class OnlineEngine:
    """Запасной переводчик: публичные инстансы LibreTranslate."""

    name = "online"

    def __init__(self, endpoints: list[str], timeout: float = 12.0):
        self.endpoints = list(endpoints)
        self.timeout = timeout
        self.dead_until: dict[str, float] = {}

    def _post(self, endpoint: str, text: str, source: str, target: str) -> str:
        body = urllib.parse.urlencode({
            "q": text, "source": source or "auto", "target": target,
            "format": "text",
        }).encode()
        req = urllib.request.Request(
            endpoint.rstrip("/") + "/translate", data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "User-Agent": "bookreader/1.0"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            import json
            data = json.loads(resp.read().decode("utf-8"))
        return (data.get("translatedText") or "").strip()

    def translate(self, texts: list[str], source: str, target: str) -> list[str]:
        now = time.time()
        live = [e for e in self.endpoints if self.dead_until.get(e, 0) < now]
        if not live:
            raise EngineError("все онлайн-переводчики не отвечают")
        last_error = None
        for endpoint in live:
            try:
                return [self._post(endpoint, t, source, target) for t in texts]
            except (urllib.error.URLError, OSError, ValueError) as exc:
                last_error = exc
                self.dead_until[endpoint] = time.time() + 300
                continue
        raise EngineError(f"онлайн-перевод не удался: {last_error}")


class MyMemoryEngine:
    """Запасной онлайн-сервис: api.mymemory.translated.net.

    Словарный, качество среднее, зато отвечает без ключа.
    Лимит ~5000 слов в сутки на один IP — поэтому только как запасной путь.
    """

    name = "mymemory"
    MAX_QUERY = 450                # сервис не принимает длинный запрос

    def __init__(self, timeout: float = 15.0, email: str = ""):
        self.timeout = timeout
        self.email = email
        self.quota_left: float | None = None

    def _one(self, text: str, source: str, target: str) -> str:
        import json as json_lib

        params = urllib.parse.urlencode({
            "q": text[:self.MAX_QUERY],
            "langpair": f"{source}|{target}",
            **({"de": self.email} if self.email else {}),
        })
        url = f"https://api.mymemory.translated.net/get?{params}"
        request = urllib.request.Request(
            url, headers={"User-Agent": "bookreader/1.0"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            data = json_lib.loads(response.read().decode("utf-8"))
        if int(data.get("responseStatus", 200)) >= 400 or data.get("quotaFinished"):
            self.quota_left = 0.0
            raise EngineError("лимит MyMemory исчерпан")
        left = data.get("quotaFinished")
        if isinstance(left, (int, float)):
            self.quota_left = float(left)
        return (data.get("responseData", {}).get("translatedText") or "").strip()


    def translate(self, texts: list[str], source: str, target: str) -> list[str]:
        if self.quota_left == 0.0:
            raise EngineError("лимит MyMemory на сегодня исчерпан")
        return [self._one(text, source, target) for text in texts]


class Translator:
    """Перевод абзацев с кэшем в sqlite и выбором движка."""

    def __init__(self, config: Config | None = None):
        self.config = config or Config()
        engine = self.config["translate_engine"]
        self.nllb = NllbEngine(self.config["nllb_repo"])
        self.online = OnlineEngine(self.config["online_endpoints"])
        self.mymemory = MyMemoryEngine()
        self.engine = engine
        self.last_error = ""

    # --- готовность ---------------------------------------------------------
    def ready(self) -> bool:
        if self.engine == "nllb":
            return self.nllb.is_ready()
        if self.engine == "online":
            return bool(self.config["online_endpoints"])
        return True

    def status(self) -> str:
        if self.engine == "off":
            return "перевод выключен"
        if self.engine == "nllb":
            return self.nllb.status()
        return "онлайн: " + ", ".join(self.config["online_endpoints"])

    # --- перевод ------------------------------------------------------------
    def translate(self, text: str, source: str, target: str,
                  use_cache: bool = True) -> str:
        """Перевести один абзац. Пустой текст и уже нужный язык — без перевода."""
        text = (text or "").strip()
        if not text or source.split("-")[0] == target:
            return text
        if use_cache:
            # перевод мог прийти с запасного движка — ищем по всем
            for name in (self.engine, "nllb", "online", "mymemory"):
                cached = state.get_translation(text, target, name)
                if cached:
                    return cached

        chunks = split_sentences(text)
        primary = self.nllb if self.engine == "nllb" else self.online
        try:
            translated = self._run_with(primary, chunks, source, target)
            engine_name = self.engine
        except EngineError as exc:
            self.last_error = str(exc)
            # основной движок не справился — идём по запасным: онлайн,
            # потом словарный сервис, потом (если выбрали онлайн) офлайн
            fallbacks = [self.online, self.mymemory]
            if self.engine == "online":
                fallbacks = [self.mymemory, self.nllb]
            translated = None
            errors = [str(exc)]
            for other in fallbacks:
                try:
                    translated = self._run_with(other, chunks, source, target)
                    engine_name = other.name
                    break
                except EngineError as fallback_exc:
                    errors.append(str(fallback_exc))
            if translated is None:
                raise EngineError("перевод не удался: " + "; ".join(errors))
        result = " ".join(part for part in translated if part).strip()
        if use_cache and result:
            state.put_translation(text, target, engine_name, result)
        return result

    def _run_with(self, engine, chunks: list[str], source: str,
                  target: str) -> list[str]:
        out: list[str] = []
        for start in range(0, len(chunks), BATCH):
            out.extend(engine.translate(chunks[start:start + BATCH], source, target))
        return out

    def translate_many(self, texts: list[str], source: str, target: str) -> list[str]:
        return [self.translate(t, source, target) for t in texts]


def detect_language(text: str, default: str = "en") -> tuple[str, float]:
    return langid.detect(text, default=default)


def download_model(repo: str = DEFAULT_REPO) -> Path:
    """Скачать NLLB в CTranslate2-формате (если её ещё нет).

    Обязательны только model.bin и tokenizer.json: у части конверсий
    нет sentencepiece.bpe.model, а он нам не нужен — токенизатор берётся
    из tokenizer.json.
    """
    folder = model_dir(repo)
    folder.mkdir(parents=True, exist_ok=True)
    if (folder / "model.bin").exists() and (folder / "tokenizer.json").exists():
        return folder
    from huggingface_hub import hf_hub_download

    skipped: list[str] = []
    for name in REQUIRED_FILES + OPTIONAL_FILES:
        if (folder / name).exists():
            continue
        for attempt in range(2):          # при большой загрузке hf иногда рвёт связь
            try:
                hf_hub_download(repo, name, local_dir=str(folder))
                skipped = [item for item in skipped if item != name]
                break
            except Exception as exc:
                if name in REQUIRED_FILES:
                    raise EngineError(
                        f"не скачался {name}: {exc}. Сеть на машине нестабильна, "
                        "попробуй позже или оставь перевод выключенным") from exc
                if attempt:
                    skipped.append(name)
                    print(f"  {name}: нет в этой конверсии, пропускаем")
    return folder


def model_size(folder: Path) -> int:
    return sum(f.stat().st_size for f in folder.glob("*") if f.is_file())