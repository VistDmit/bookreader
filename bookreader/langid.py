"""Определение языка книги: без внешних моделей, по буквам и служебным словам.

Нужно, чтобы понять: книга уже на русском (читаем как есть) или её
надо переводить. Ошибка здесь стоит лишнего перевода всей книги,
поэтому сомневаемся осторожно.
"""

from __future__ import annotations

import re
import unicodedata

# код ISO 639-1 -> коды NLLB/FLORES-200 (для кириллицы и латиницы)
NLLB_CODES = {
    "ru": "rus_Cyrl", "uk": "ukr_Cyrl", "bg": "bul_Cyrl", "be": "bel_Cyrl",
    "sr": "srp_Cyrl", "mk": "mkd_Cyrl", "mn": "mon_Cyrl", "tg": "tgk_Cyrl",
    "kk": "kaz_Cyrl", "ky": "kir_Cyrl", "ky ": "kir_Cyrl", "tt": "tat_Cyrl",
    "ba": "bak_Cyrl", "cv": "chv_Cyrl", "sah": "sah_Cyrl", "ce": "che_Cyrl",
    "ab": "abq_Cyrl", "os": "oss_Cyrl", "udm": "udm_Cyrl", "kbd": "kbd_Cyrl",
    "tyv": "tyv_Cyrl", "kv": "kom_Cyrl", "mhr": "mhr_Cyrl", "lez": "lez_Cyrl",
    "tab": "tab_Cyrl", "ady": "ady_Cyrl", "krc": "krc_Cyrl", "ava": "ava_Cyrl",
    "arg": "arg_Cyrl", "inh": "inh_Cyrl", "mag": "mag_Cyrl",
    "en": "eng_Latn", "de": "deu_Latn", "fr": "fra_Latn", "es": "spa_Latn",
    "it": "ita_Latn", "pt": "por_Latn", "nl": "nld_Latn", "pl": "pol_Latn",
    "cs": "ces_Latn", "sk": "slk_Latn", "sv": "swe_Latn", "da": "dan_Latn",
    "no": "nob_Latn", "fi": "fin_Latn", "tr": "tur_Latn", "hu": "hun_Latn",
    "ro": "ron_Latn", "el": "ell_Grek", "he": "heb_Hebr", "ar": "arb_Arab",
    "fa": "pes_Arab", "hi": "hin_Deva", "id": "ind_Latn", "vi": "vie_Latn",
    "th": "tha_Thai", "ja": "jpn_Jpan", "zh": "zho_Hans", "ko": "kor_Hang",
    "ukr": "ukr_Cyrl", "sq": "sqi_Latn", "et": "est_Latn", "lv": "lvs_Latn",
    "lt": "lit_Latn", "sl": "slv_Latn", "hr": "hrv_Latn", "sr-Latn": "srp_Latn",
}

# самые частые слова: их и считаем
# самые частые слова: их и считаем
STOPWORDS = {
    "ru": "и в не на я с что он как а но то все она так его да ты к у же вы за бы по "
          "только ее мне было вот от меня еще нет о из ему теперь когда даже ну вдруг "
          "ли если уже или ни быть был него до вас нибудь опять уж вам ведь там потом "
          "себя ничего ей может они тут где есть надо ней для мы тебя их чем была сам "
          "чтоб без будто чего раз тоже себе под будет ж тогда кто этот того потому "
          "этого какой совсем ним здесь этом один почти мой тем чтобы нее сейчас были "
          "куда зачем всех никогда можно при наконец два об другой хоть после над больше "
          "тот через эти нас про всего них какая много разве три эту моя впрочем "
          "хорошо этой перед иногда лучше чуть том нельзя такой им более всегда конечно "
          "всю между",
    "uk": "і в на з до що не як за від по для це та або але він вона воно "
          "вони ми ви я ти його її який яка яке які був була були бути "
          "може можна треба також дуже щоб коли де хто",
    "bg": "и в на да се за от с не че като по са то той тя те този тази "
          "може беше бяха го",
    "sr": "и у на да се за од са не као по али или он она оно они ми ви "
          "ја ти њега њен њихов која који које би био била били",
    "en": "the of and to a in that is was it for on with as at by from this these "
          "have he she they we you i not but be are was were his her its their our "
          "your what which who whom will would can could should may might must do does "
          "did no nor so if then than there here when where why how all any some "
          "each other more most much many one two out up down over under about into "
          "after before while very just only also even still yet said says because",
    "de": "der die das und ist von zu den ein eine in mit nicht auf für des dem "
          "sich auch es an werden aus er hat dass sie nach wird bei einer um am sind "
          "noch wie einem über einen so zum haben nur oder aber vor zur bis mehr "
          "durch man sein wurde sei waren schon wenn doch nichts sehr ich du wir ihr",
    "fr": "le la les de des du un une et est en que qui dans pour pas sur au aux ce "
          "avec par son ses ne je tu il elle nous vous ils elles mais ou "
          "comme tout tous cette ces sont avait être fait plus très bien "
          "leur leurs quand à ça dit sans sous entre encore moi toi soi",
    "es": "el la los las de que y a en un una por con no para es se del al lo como "
          "más pero sus le ya o este sí porque esta entre cuando muy sin sobre también "
          "me hasta hay donde quien desde todo nos durante todos uno les ni contra otros "
          "ese eso ante ellos e esto mí antes algunos qué unos yo otro otra él",
    "it": "il lo la i gli le di che è in un una per non con da come più ma se al dei "
          "del della nel alla sul questo quella sono mi si ho ha ci vi loro io tu lei "
          "noi voi era erano avevano essere fatto molto anche ancora quando dove "
          "perché tutto niente sempre senza già",
    "pt": "de que não em um para com uma os no se na por mais as dos como mas ao ele "
          "das à seu sua ou quando muito nos já eu também só pelo pela até isso ela "
          "entre depois sem mesmo aos seus quem nas me esse eles você essa num nem "
          "portanto aquela",
    "nl": "de van het een en in is dat op te zijn met voor niet aan er die ook maar "
          "om door over ze uit als hij was zij werd bij nog naar dan heeft mijn "
          "heb zich hebben deze wilt kunnen ons",
    "sv": "och det att i en av som på för med den till de är inte om ett har han "
          "men var vi kan när sig så till",
    "pl": "w i na nie do że się jest to o a z dla po przez są ale jak tylko "
          "jego jej ich który była był być czy tego przy już może gdy nawet "
          "bardzo także oraz bardzo są",
}

WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def _script_ratios(text: str) -> tuple[int, int, int]:
    cyr = lat = other = 0
    for ch in text:
        if not ch.isalpha():
            continue
        name = unicodedata.name(ch, "")
        if "CYRILLIC" in name:
            cyr += 1
        elif "LATIN" in name:
            lat += 1
        else:
            other += 1
    return cyr, lat, other


def detect(text: str, default: str = "en", sample: int = 6000) -> tuple[str, float]:
    """Вернуть (код языка, уверенность 0..1)."""
    if not text or not text.strip():
        return default, 0.0
    text = text[:sample].lower()
    cyr, lat, other = _script_ratios(text)
    letters = cyr + lat + other
    if letters < 8:
        return default, 0.0

    # иероглифы и прочее: определяем по алфавиту, служебные слова тут бесполезны
    if other > letters * 0.5:
        return _other_script(text), 0.8

    if cyr > lat:
        return _detect_stopwords(text, ("ru", "uk", "bg", "sr"), default="ru")

    return _detect_stopwords(text, ("en", "de", "fr", "es", "it", "pt", "nl", "pl", "sv"),
                             default="en")


def _detect_stopwords(text: str, langs: tuple[str, ...], default: str) -> tuple[str, float]:
    words = WORD_RE.findall(text)
    if not words:
        return default, 0.0
    total = len(words)
    scores = {}
    for lang in langs:
        table = set(STOPWORDS.get(lang, "").split())
        if not table:
            continue
        hits = sum(1 for w in words if w in table)
        scores[lang] = hits / total * 100
    if not scores:
        return default, 0.0
    best = max(scores, key=lambda k: scores[k])
    top = scores[best]
    second = max((v for k, v in scores.items() if k != best), default=0.0)
    if top < 0.5:                      # слишком мало слов-маркеров
        return default, min(0.4, top / 2)
    confidence = min(0.99, top * 1.6) * (1 - min(0.5, second / max(top, 0.01)))
    return best, round(confidence, 3)


def _other_script(text: str) -> str:
    sample = text[:400]
    if any("CJK" in unicodedata.name(ch, "") or "HIRAGANA" in unicodedata.name(ch, "")
           or "KATAKANA" in unicodedata.name(ch, "") for ch in sample if ch.isalpha()):
        return "ja" if any("HIRAGANA" in unicodedata.name(ch, "")
                           or "KATAKANA" in unicodedata.name(ch, "")
                           for ch in sample if ch.isalpha()) else "zh"
    if any("HANGUL" in unicodedata.name(ch, "") for ch in sample if ch.isalpha()):
        return "ko"
    if any("ARABIC" in unicodedata.name(ch, "") for ch in sample if ch.isalpha()):
        return "ar"
    if any("HEBREW" in unicodedata.name(ch, "") for ch in sample if ch.isalpha()):
        return "he"
    if any("DEVANAGARI" in unicodedata.name(ch, "") for ch in sample if ch.isalpha()):
        return "hi"
    if any("GREEK" in unicodedata.name(ch, "") for ch in sample if ch.isalpha()):
        return "el"
    if any("THAI" in unicodedata.name(ch, "") for ch in sample if ch.isalpha()):
        return "th"
    return "en"


def nllb_code(lang: str) -> str | None:
    """Код языка -> код NLLB. None, если язык модель не знает."""
    lang = (lang or "").lower()
    lang = lang.split("-")[0]
    return NLLB_CODES.get(lang)


def needs_translation(lang: str | None, target: str = "ru") -> bool:
    """Нужен ли перевод. Неизвестный язык считаем нужным: лучше лишний раз спросим."""
    if not lang:
        return True
    return lang.split("-")[0].lower() != target.lower()