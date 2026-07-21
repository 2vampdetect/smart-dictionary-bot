"""
Умный словарь — Telegram-бот.

Отправьте боту слово (на русском или английском) — он вернёт:
  • перевод (EN ↔ RU),
  • пример употребления,
  • синонимы,
  • определение.

Используются бесплатные API без ключей:
  • MyMemory        — перевод (https://mymemory.translated.net)
  • Free Dictionary — определения и примеры (https://dictionaryapi.dev)
  • Datamuse        — синонимы (https://www.datamuse.com/api/)

Запуск:
  export BOT_TOKEN="123456:ABC..."   # токен от @BotFather
  python bot.py
"""

import asyncio
import html
import logging
import os
import re

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("smart-dictionary-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)
MAX_SYNONYMS = 8

dp = Dispatcher()

START_TEXT = (
    "👋 Привет! Я <b>Умный словарь</b>.\n\n"
    "Просто отправь мне слово на русском или английском, и я пришлю:\n"
    "🌐 перевод\n"
    "💬 пример употребления\n"
    "🔁 синонимы\n"
    "📝 определение\n\n"
    "Попробуй, например: <code>serendipity</code> или <code>вдохновение</code>"
)

HELP_TEXT = (
    "ℹ️ Как пользоваться:\n\n"
    "Отправь одно слово или короткую фразу (до 100 символов).\n"
    "• Английское слово → перевод на русский + пример, синонимы, определение.\n"
    "• Русское слово → перевод на английский + пример и синонимы для перевода.\n\n"
    "Команды:\n"
    "/start — приветствие\n"
    "/help — эта справка"
)


async def fetch_json(session: aiohttp.ClientSession, url: str, params: dict | None = None):
    """GET-запрос, возвращает JSON или None при любой ошибке."""
    try:
        async with session.get(url, params=params, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            return await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        log.warning("Request failed %s: %s", url, exc)
        return None


async def translate(session: aiohttp.ClientSession, text: str, src: str, dst: str) -> str | None:
    """Перевод через MyMemory (бесплатно, без ключа)."""
    data = await fetch_json(
        session,
        "https://api.mymemory.translated.net/get",
        {"q": text, "langpair": f"{src}|{dst}"},
    )
    if not data:
        return None
    translated = (data.get("responseData") or {}).get("translatedText")
    if not translated:
        return None
    translated = translated.strip()
    # MyMemory иногда возвращает текст ЗАГЛАВНЫМИ буквами
    if translated.isupper() and len(translated) > 3:
        translated = translated.lower()
    return translated


async def dictionary_entry(session: aiohttp.ClientSession, word_en: str) -> dict:
    """Определение, пример и синонимы английского слова (dictionaryapi.dev)."""
    result = {"definition": None, "example": None, "synonyms": []}
    data = await fetch_json(
        session,
        f"https://api.dictionaryapi.dev/api/v2/entries/en/{word_en}",
    )
    if not isinstance(data, list) or not data:
        return result

    synonyms: list[str] = []
    for entry in data:
        for meaning in entry.get("meanings", []):
            synonyms.extend(meaning.get("synonyms", []))
            for definition in meaning.get("definitions", []):
                if result["definition"] is None and definition.get("definition"):
                    result["definition"] = definition["definition"]
                if result["example"] is None and definition.get("example"):
                    result["example"] = definition["example"]
                synonyms.extend(definition.get("synonyms", []))

    seen: set[str] = set()
    for syn in synonyms:
        key = syn.lower().strip()
        if key and key not in seen:
            seen.add(key)
            result["synonyms"].append(syn.strip())
    return result


async def datamuse_synonyms(session: aiohttp.ClientSession, word_en: str) -> list[str]:
    """Синонимы английского слова через Datamuse."""
    data = await fetch_json(
        session,
        "https://api.datamuse.com/words",
        {"rel_syn": word_en, "max": MAX_SYNONYMS},
    )
    if not isinstance(data, list):
        return []
    return [item["word"] for item in data if isinstance(item, dict) and item.get("word")]


def merge_synonyms(*lists: list[str]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for lst in lists:
        for syn in lst:
            key = syn.lower().strip()
            if key and key not in seen:
                seen.add(key)
                merged.append(syn.strip())
    return merged[:MAX_SYNONYMS]


async def build_answer(word: str) -> str:
    """Собирает ответ бота для присланного слова."""
    is_russian = bool(CYRILLIC_RE.search(word))
    safe_word = html.escape(word)

    async with aiohttp.ClientSession() as session:
        if is_russian:
            translation = await translate(session, word, "ru", "en")
            word_en = translation  # словарь и синонимы — для английского перевода
        else:
            translation = await translate(session, word, "en", "ru")
            word_en = word

        entry = {"definition": None, "example": None, "synonyms": []}
        datamuse: list[str] = []
        if word_en and " " not in word_en:
            entry_task = dictionary_entry(session, word_en.lower())
            datamuse_task = datamuse_synonyms(session, word_en.lower())
            entry, datamuse = await asyncio.gather(entry_task, datamuse_task)

    synonyms = merge_synonyms(entry["synonyms"], datamuse)

    lines: list[str] = [f"📖 <b>{safe_word}</b>"]

    if translation:
        lines.append(f"\n🌐 <b>Перевод:</b> {html.escape(translation)}")
    else:
        lines.append("\n🌐 Перевод не найден 😔")

    if entry["definition"]:
        lines.append(f"\n📝 <b>Определение (EN):</b> {html.escape(entry['definition'])}")

    if entry["example"]:
        lines.append(f"\n💬 <b>Пример:</b> <i>{html.escape(entry['example'])}</i>")

    if synonyms:
        label = "Синонимы (EN)" if is_russian else "Синонимы"
        lines.append(f"\n🔁 <b>{label}:</b> {html.escape(', '.join(synonyms))}")

    if len(lines) == 1:
        return (
            f"😔 Не удалось найти информацию по слову <b>{safe_word}</b>.\n"
            "Проверь написание и попробуй ещё раз."
        )
    return "".join(lines)


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(START_TEXT)


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


@dp.message(F.text)
async def handle_word(message: Message) -> None:
    word = (message.text or "").strip()
    if not word or word.startswith("/"):
        return
    if len(word) > 100:
        await message.answer("✂️ Слишком длинный текст. Отправь одно слово или короткую фразу.")
        return

    await message.bot.send_chat_action(message.chat.id, "typing")
    try:
        answer = await build_answer(word)
    except Exception:  # noqa: BLE001
        log.exception("Failed to build answer for %r", word)
        answer = "⚠️ Что-то пошло не так. Попробуй ещё раз чуть позже."
    await message.answer(answer)


@dp.message()
async def handle_other(message: Message) -> None:
    await message.answer("Я понимаю только текст 🙂 Отправь мне слово.")


async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "Не задан токен бота.\n"
            "Получите токен у @BotFather и запустите:\n"
            "  export BOT_TOKEN=\"123456:ABC...\"\n"
            "  python bot.py"
        )
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    log.info("Бот запущен. Нажмите Ctrl+C для остановки.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Бот остановлен.")
