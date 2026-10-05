"""
Telegram-бот для поиска и прослушивания музыки (источники — Audius и SoundCloud).

Как работает:
  1. Отправь боту название трека или исполнителя (текстом).
  2. Бот ищет одновременно на Audius (mp3 320 kbps, помечены 💎) и SoundCloud
     (до AAC 160 kbps) и показывает варианты кнопками, постранично.
  3. Жмёшь на нужный — бот скачивает трек в максимальном доступном качестве
     (без перекодирования, с обложкой), показывая полоску прогресса, и
     присылает его; под треком подписано реальное качество файла.

/mood — подборка популярных треков Audius под настроение (грустное, чилл...).

SoundCloud качается библиотекой yt-dlp прямо в процессе, Audius — через его
открытое API. Только для личного использования. См. README.md для запуска.
"""

import asyncio
import json
import logging
import os
import random
import re
import shutil
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import yt_dlp
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv
from mutagen import File as AudioFile
from mutagen.id3 import APIC, ID3, TIT2, TPE1, ID3NoHeaderError

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
# Список разрешённых Telegram ID через запятую. Если пусто — отвечает всем.
OWNER_IDS = {
    int(x) for x in os.getenv("OWNER_IDS", "").replace(" ", "").split(",") if x
}

PAGE_SIZE = 8                          # сколько треков на одной «странице»
SEARCH_FETCH = 24                      # сколько искать на SoundCloud
AUDIUS_FETCH = 20                      # сколько брать из поиска Audius до фильтра
SC_BEFORE_COVERS = 5                   # сколько треков SoundCloud ставить выше каверов с Audius
PREFETCH_TOP = 3                       # сколько первых треков качать заранее
MAX_FILE_MB = 49                       # лимит Telegram на отправку ботом (~50 МБ)
HERE = Path(__file__).parent
DOWNLOAD_DIR = HERE / "downloads"
# v2: в старом cache.json лежат треки в mp3 128 — с ним бот слал бы их повторно
# в старом качестве. Старый файл можно удалить.
CACHE_FILE = HERE / "cache_v2.json"
FFMPEG = shutil.which("ffmpeg")        # путь к ffmpeg (нужен для обложки и HLS)

# --- SoundCloud ---
# Ссылка на трек SoundCloud, собранная из числового id (двоеточия %-кодированы).
SC_TRACK_URL = "https://api.soundcloud.com/tracks/soundcloud%3Atracks%3A{}"
# Лучший поток по битрейту (обычно AAC 160 > MP3 128). 30-сек превью Go+
# исключаем; Opus — тоже (Telegram не играет его как музыкальный трек).
SC_FORMAT = (
    "bestaudio[format_id!*=preview][acodec!*=opus]/"
    "bestaudio[format_id!*=preview]"
)
SC_SORT = ["abr"]                      # сортировать форматы по битрейту
# HLS-потоки нарезаны на сотни кусочков: качаем их параллельно, а не по одному.
HLS_PARALLEL = 8
# Обложка 500×500: Telegram показывает её мелко, а «original» бывает до ~600 КБ.
SC_COVER_ID = "t500x500"

# --- Audius (открытое API, mp3 320 kbps) ---
AUDIUS_API = "https://api.audius.co"
AUDIUS_APP = "musicbro"                # имя приложения — API просит его передавать
AUDIUS_KBPS = 320
AUDIUS_PREFIX = "au_"                  # ключи треков Audius: «au_<id>»
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0"}

# --- Поиск по настроению (/mood) ---
# Каждая кнопка = несколько «настроений» Audius (их разметка на английском).
# Все значения проверены: по каждому Audius отдаёт треки.
MOODS = [
    ("sad", "😢 Грустное", ["Melancholy", "Sentimental", "Yearning"]),
    ("calm", "📚 Спокойное", ["Peaceful", "Easygoing", "Tender"]),
    ("chill", "😎 Чилл", ["Cool", "Sophisticated", "Easygoing"]),
    ("love", "🥰 Романтичное", ["Romantic", "Sensual", "Tender"]),
    ("fun", "🎉 Весёлое", ["Upbeat", "Excited", "Rowdy"]),
    ("sport", "🏃 Для спорта", ["Energizing", "Empowering", "Fiery"]),
    ("angry", "😤 Дерзкое", ["Aggressive", "Defiant", "Gritty"]),
    ("deep", "🌙 Задумчивое", ["Brooding", "Serious", "Stirring"]),
]
MOOD_BY_CODE = {code: (label, moods) for code, label, moods in MOODS}
MOOD_FETCH = 50                        # из скольких популярных треков выбирать

# --- Прогресс скачивания ---
PROGRESS_EVERY = 1.5                   # как часто обновлять сообщение (сек; чаще — лимиты Telegram)
PROGRESS_CELLS = 10                    # длина полоски ▰▰▰▱▱

HELP_TEXT = (
    "🎵 <b>Как пользоваться</b>\n\n"
    "1. Пришли мне название трека или исполнителя.\n"
    "2. Я ищу сразу на Audius и SoundCloud — жми «Ещё ▶️», чтобы увидеть больше.\n"
    "3. Нажми на трек — пришлю его в лучшем доступном качестве "
    "(пока качаю — показываю прогресс).\n\n"
    "🎭 /mood — музыка под настроение: грустное, для спорта, чилл и т.д. "
    "Каждое нажатие — новая подборка.\n\n"
    "💎 — трек с Audius, <b>320 kbps</b> (лучшее качество, но каталог "
    "в основном электроника/хип-хоп/инди).\n"
    "Без значка — SoundCloud, до <b>AAC 160 kbps</b>.\n"
    "Под каждым треком подписано его реальное качество.\n\n"
    "🔒 Некоторые официальные релизы защищены (DRM) и не качаются — "
    "просто выбери другой вариант из списка."
)

DOWNLOAD_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("music_bot")

# Кэш: ключ трека -> {"file_id": ..., "quality": ...} (повторная отправка мгновенно).
if CACHE_FILE.exists():
    file_id_cache: dict[str, dict] = json.loads(CACHE_FILE.read_text("utf-8"))
else:
    file_id_cache = {}

# Метаданные из поиска (ключ -> {id, source, title, uploader, duration, ...}).
meta_cache: dict[str, dict] = {}
# Результаты поиска для листания (token -> список ключей по порядку).
pages_cache: dict[str, list[str]] = {}
# Фоновая предзагрузка: ключ -> asyncio.Task, качающий трек заранее.
prefetch_tasks: dict[str, "asyncio.Task"] = {}
# Прогресс скачивания: ключ -> доля 0..1. Пишут потоки загрузки, читает handle_play.
progress: dict[str, float] = {}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def save_cache() -> None:
    CACHE_FILE.write_text(json.dumps(file_id_cache, ensure_ascii=False), "utf-8")


def is_allowed(user_id: int) -> bool:
    return not OWNER_IDS or user_id in OWNER_IDS


def human_duration(seconds) -> str:
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "?"
    return f"{seconds // 60}:{seconds % 60:02d}"


def track_name(meta: dict) -> str:
    """«Исполнитель — Название» для статуса/подписей."""
    title = meta.get("title") or "трек"
    up = meta.get("uploader")
    return f"{up} — {title}" if up else title


def button_label(r: dict) -> str:
    """Подпись кнопки: «💎 исполнитель — название · длительность».

    Обрезаем при необходимости само название, а длительность оставляем целой.
    """
    dur = human_duration(r["duration"])
    badge = "💎 " if r.get("source") == "au" else ""
    name = badge + track_name(r)
    suffix = "" if dur == "?" else f"  ·  {dur}"
    max_name = 64 - len(suffix)
    if len(name) > max_name:
        name = name[: max_name - 1] + "…"
    return name + suffix


def build_keyboard(token: str, page: int) -> InlineKeyboardMarkup:
    """Клавиатура одной страницы результатов + навигация «Назад/Ещё»."""
    ids = pages_cache.get(token, [])
    start = page * PAGE_SIZE
    chunk = ids[start : start + PAGE_SIZE]
    rows = [
        [InlineKeyboardButton(text=button_label(meta_cache[tid]), callback_data=f"play:{tid}")]
        for tid in chunk
        if tid in meta_cache
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"page:{token}:{page - 1}"))
    if start + PAGE_SIZE < len(ids):
        nav.append(InlineKeyboardButton(text="Ещё ▶️", callback_data=f"page:{token}:{page + 1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def mood_keyboard() -> InlineKeyboardMarkup:
    """Кнопки настроений, по две в ряд."""
    buttons = [InlineKeyboardButton(text=label, callback_data=f"mood:{code}")
               for code, label, _ in MOODS]
    return InlineKeyboardMarkup(inline_keyboard=[buttons[i : i + 2] for i in range(0, len(buttons), 2)])


def report_progress(key: str, frac: float) -> None:
    """Запоминает долю скачанного (вызывается из потока загрузки).

    Только растёт: оценка общего размера у yt-dlp скачет, и без этого проценты
    прыгали бы назад-вперёд.
    """
    frac = max(0.0, min(1.0, frac))
    if frac > progress.get(key, 0.0):
        progress[key] = frac


def progress_text(name: str, frac: Optional[float]) -> str:
    """«⬇️ Качаю «…»» + полоска ▰▰▰▱▱ и проценты, когда они уже известны."""
    head = f"⬇️ Качаю «{name}»"
    if frac is None:
        return head + "…"
    filled = round(frac * PROGRESS_CELLS)
    bar = "▰" * filled + "▱" * (PROGRESS_CELLS - filled)
    return f"{head}\n{bar} {round(frac * 100)}%"


async def safe_edit(msg: Message, text: str) -> None:
    """edit_text, не падающий на «message is not modified» и подобном."""
    try:
        await msg.edit_text(text)
    except TelegramBadRequest:
        pass


async def stop_ticker(ticker: Optional[asyncio.Task]) -> None:
    """Останавливает обновление прогресса и дожидается остановки — чтобы оно
    не перезаписало следующий текст статуса («Отправляю…», ошибку)."""
    if ticker and not ticker.done():
        ticker.cancel()
        try:
            await ticker
        except asyncio.CancelledError:
            pass


async def show_progress(status: Message, key: str, name: str) -> None:
    """Пока идёт скачивание — раз в PROGRESS_EVERY сек обновляет статус с процентами."""
    last = None
    frac = None
    while True:
        # Если запись уже убрали (загрузка кончилась) — держим последнее значение.
        frac = progress.get(key, frac)
        text = progress_text(name, frac)
        if text != last:
            await safe_edit(status, text)
            last = text
        await asyncio.sleep(PROGRESS_EVERY)


def clean_error(msg: str) -> str:
    """Убирает ANSI-раскраску из текста ошибки yt-dlp."""
    return ANSI_RE.sub("", msg or "").strip() or "неизвестная ошибка"


def http_get(url: str, timeout: int = 20):
    return urllib.request.urlopen(
        urllib.request.Request(url, headers=HTTP_HEADERS), timeout=timeout
    )


def quality_label(path: Path, source: str) -> str:
    """Реальное качество, прочитанное из самого файла: «AAC 160 kbps · SoundCloud»."""
    try:
        audio = AudioFile(path)
        kbps = round(audio.info.bitrate / 1000)
        codec = {"MP3": "MP3", "MP4": "AAC"}.get(type(audio).__name__, path.suffix[1:].upper())
        return f"{codec} {kbps} kbps · {source}"
    except Exception:
        return source


def _cleanup(key: str) -> None:
    """Удаляет частично скачанные/временные файлы трека."""
    for p in DOWNLOAD_DIR.glob(f"{key}.*"):
        p.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Поиск (блокирующий — вызывается через to_thread)
# ---------------------------------------------------------------------------
def search_soundcloud(query: str, limit: int = SEARCH_FETCH) -> list[dict]:
    """Ищет треки на SoundCloud. Возвращает список метаданных."""
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"scsearch{limit}:{query}", download=False)

    results: list[dict] = []
    for d in info.get("entries") or []:
        if not d or not d.get("id"):
            continue
        key = str(d["id"])
        meta = {
            "id": key,
            "source": "sc",
            "title": d.get("title") or "Без названия",
            "duration": d.get("duration"),
            "uploader": d.get("uploader") or d.get("uploader_id") or "",
        }
        results.append(meta)
        meta_cache[key] = meta
    return results


def _audius_meta(t: dict) -> Optional[dict]:
    """Метаданные трека Audius или None, если его нельзя прислать.

    Отсекаем закрытые/платные треки и те, что в 320 kbps не влезут в лимит Telegram.
    """
    if not t.get("is_streamable", True) or t.get("stream_conditions"):
        return None
    if audius_too_big(t.get("duration")):
        return None
    art = t.get("artwork") or {}
    return {
        "id": AUDIUS_PREFIX + t["id"],
        "source": "au",
        "title": t.get("title") or "Без названия",
        "duration": t.get("duration"),
        "uploader": (t.get("user") or {}).get("name") or "",
        # Несколько размеров обложки: если сервер картинок сбойнёт на одном — берём другой.
        "artwork": [art[s] for s in ("480x480", "1000x1000", "150x150") if art.get(s)],
    }


def _audius_search(params: str) -> list[dict]:
    url = f"{AUDIUS_API}/v1/tracks/search?app_name={AUDIUS_APP}&{params}"
    with http_get(url) as r:
        return json.load(r).get("data") or []


def search_audius(query: str, limit: int = AUDIUS_FETCH) -> list[dict]:
    """Ищет на Audius только подходящие и открытые для прослушивания треки.

    Поиск Audius «размазанный» (на «Nightcall» может выдать что угодно), поэтому
    оставляем только треки, где в исполнителе+названии есть ВСЕ слова запроса.
    """
    data = _audius_search(f"query={quote(query)}")
    words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 1]
    results: list[dict] = []
    for t in data[:limit]:
        meta = _audius_meta(t)
        if not meta:
            continue
        hay = f"{meta['uploader']} {meta['title']}".lower()
        if words and not all(w in hay for w in words):
            continue  # не про то, что искали
        results.append(meta)
        meta_cache[meta["id"]] = meta
    return results


def search_mood(code: str) -> list[dict]:
    """Популярные треки Audius с нужным настроением, каждый раз в новом порядке.

    Несколько настроений передаются повтором параметра (mood=A&mood=B) — через
    запятую Audius возвращает пустой список.
    """
    _, moods = MOOD_BY_CODE[code]
    params = "query=&sort_method=popular&limit={}&{}".format(
        MOOD_FETCH, "&".join(f"mood={quote(m)}" for m in moods)
    )
    results: list[dict] = []
    for t in _audius_search(params):
        meta = _audius_meta(t)
        if meta:
            results.append(meta)
            meta_cache[meta["id"]] = meta
    random.shuffle(results)  # «радио»: при каждом нажатии — другая подборка сверху
    return results


def search_music(query: str) -> list[dict]:
    """Ищет параллельно на Audius и SoundCloud и собирает общий список.

    Порядок: сначала треки Audius, залитые САМИМ исполнителем из запроса
    (320 kbps и это оригинал), потом первые результаты SoundCloud, потом
    остальное с Audius (каверы/ремиксы/флипы), потом остальной SoundCloud.
    """
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_au = ex.submit(search_audius, query)
        f_sc = ex.submit(search_soundcloud, query)
        try:
            au = f_au.result()
        except Exception as e:
            logger.warning("Поиск Audius не удался: %s", e)
            au = []
        try:
            sc = f_sc.result()
        except Exception:
            if not au:
                raise  # оба источника упали — пусть пользователь увидит ошибку
            logger.exception("Поиск SoundCloud не удался")
            sc = []

    q = query.lower()
    own = [r for r in au if len(r["uploader"]) > 1 and r["uploader"].lower() in q]
    others = [r for r in au if r not in own]
    return own + sc[:SC_BEFORE_COVERS] + others + sc[SC_BEFORE_COVERS:]


# ---------------------------------------------------------------------------
# Скачивание (блокирующее — вызывается через to_thread)
# ---------------------------------------------------------------------------
def download_soundcloud(key: str) -> tuple[Path, dict]:
    """Скачивает трек SoundCloud в лучшем потоке (без перекодирования)."""

    def hook(d: dict) -> None:
        # HLS: считаем по кусочкам (стабильно); прямой файл — по байтам.
        if d.get("status") == "finished":
            report_progress(key, 1.0)
        elif d.get("fragment_count"):
            report_progress(key, (d.get("fragment_index") or 0) / d["fragment_count"])
        else:
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                report_progress(key, (d.get("downloaded_bytes") or 0) / total)

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "format": SC_FORMAT,
        "format_sort": SC_SORT,
        "outtmpl": str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
        "concurrent_fragment_downloads": HLS_PARALLEL,
        "progress_hooks": [hook],
    }
    if FFMPEG:
        # ffmpeg есть → вшиваем обложку (Telegram покажет её в плеере).
        # Само встраивание делает mutagen прямо в процессе (быстро).
        opts["ffmpeg_location"] = FFMPEG
        opts["writethumbnail"] = True
        opts["postprocessors"] = [{"key": "EmbedThumbnail"}]

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            # Сначала только инфо, чтобы оставить лёгкую обложку, потом качаем
            # по этой же инфо — лишних запросов к SoundCloud нет.
            info = ydl.extract_info(SC_TRACK_URL.format(key), download=False)
            small = [t for t in info.get("thumbnails") or [] if t.get("id") == SC_COVER_ID]
            if small:
                info["thumbnails"] = small
            ydl.process_ie_result(info, download=True)
    except yt_dlp.utils.DownloadError as e:
        _cleanup(key)
        raise RuntimeError(clean_error(str(e)))

    # Только аудиофайлы: рядом может остаться .jpg обложки, если вшить не вышло.
    audio_ext = (".mp3", ".m4a", ".opus", ".ogg", ".aac")
    matches = [p for p in DOWNLOAD_DIR.glob(f"{key}.*") if p.suffix in audio_ext]
    if not matches:
        raise RuntimeError("не удалось скачать трек")
    path = matches[0]
    return path, {**meta_cache.get(key, {}), "quality": quality_label(path, "SoundCloud")}


def audius_too_big(duration) -> bool:
    """Трек Audius в 320 kbps не влезет в лимит Telegram (≈21 минута и дольше)."""
    return (duration or 0) * AUDIUS_KBPS * 1000 / 8 > MAX_FILE_MB * 1024 * 1024


def _fetch_cover(urls: list[str]) -> Optional[bytes]:
    """Первая удачно скачанная обложка из списка размеров (или None)."""
    for url in urls:
        try:
            with http_get(url) as r:
                return r.read()
        except Exception as e:
            logger.info("Обложка %s не скачалась: %s", url, e)
    return None


def download_audius(key: str) -> tuple[Path, dict]:
    """Скачивает трек Audius (mp3 320 kbps как есть) и вшивает теги и обложку."""
    meta = meta_cache.get(key, {})
    if audius_too_big(meta.get("duration")):
        raise RuntimeError(
            f"трек слишком длинный: в {AUDIUS_KBPS} kbps он больше лимита "
            f"Telegram ({MAX_FILE_MB} МБ)"
        )

    path = DOWNLOAD_DIR / f"{key}.mp3"
    url = f"{AUDIUS_API}/v1/tracks/{key[len(AUDIUS_PREFIX):]}/stream?app_name={AUDIUS_APP}"
    try:
        with http_get(url, timeout=120) as r:
            if "audio" not in (r.headers.get("Content-Type") or ""):
                raise RuntimeError("Audius не отдал аудио (трек недоступен)")
            # Размер для процентов: из заголовка, иначе по длительности
            # (320 kbps = 40 000 байт в секунду).
            total = int(r.headers.get("Content-Length") or 0) or \
                (meta.get("duration") or 0) * AUDIUS_KBPS * 1000 // 8
            done = 0
            with open(path, "wb") as f:
                while chunk := r.read(256 * 1024):
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        report_progress(key, done / total)
            report_progress(key, 1.0)
    except urllib.error.HTTPError as e:
        _cleanup(key)
        raise RuntimeError(f"Audius: HTTP {e.code} (not found)" if e.code == 404
                           else f"Audius: HTTP {e.code}")
    except Exception:
        _cleanup(key)
        raise

    # Теги и обложка — прямо в mp3 (без перекодирования звука). Название и
    # исполнитель пишем всегда; обложка — если хоть один размер скачался.
    try:
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.add(TIT2(encoding=3, text=meta.get("title") or ""))
        tags.add(TPE1(encoding=3, text=meta.get("uploader") or ""))
        cover = _fetch_cover(meta.get("artwork") or [])
        if cover:
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
        tags.save(path)
    except Exception as e:
        logger.warning("Не удалось записать теги Audius: %s", e)  # трек всё равно отправим

    return path, {**meta, "quality": quality_label(path, "Audius")}


def download_audio(key: str) -> tuple[Path, dict]:
    """Скачивает трек из нужного источника в максимальном доступном качестве."""
    _cleanup(key)
    progress.pop(key, None)  # новая попытка — проценты с нуля
    if key.startswith(AUDIUS_PREFIX):
        return download_audius(key)
    return download_soundcloud(key)


# ---------------------------------------------------------------------------
# Предзагрузка: качаем первые треки страницы, пока пользователь выбирает
# ---------------------------------------------------------------------------
def schedule_prefetch(keys: list[str]) -> None:
    """Ставит фоновое скачивание первых PREFETCH_TOP треков списка."""
    for key in keys[:PREFETCH_TOP]:
        if key in file_id_cache or key in prefetch_tasks:
            continue  # уже в Telegram или уже качается
        task = asyncio.create_task(asyncio.to_thread(download_audio, key))
        task.add_done_callback(lambda t, k=key: _prefetch_done(t, k))
        prefetch_tasks[key] = task


def _prefetch_done(task: "asyncio.Task", key: str) -> None:
    # Забираем исключение, чтобы не было варнинга, если трек так и не выберут,
    # и убираем запись о прогрессе — она больше не нужна.
    if not task.cancelled():
        task.exception()
    progress.pop(key, None)


def sweep_downloads(max_age_sec: int = 2 * 3600) -> None:
    """Удаляет старые невостребованные файлы (например, предзагруженные)."""
    now = time.time()
    for p in DOWNLOAD_DIR.iterdir():
        if p.is_file() and now - p.stat().st_mtime > max_age_sec:
            p.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Хэндлеры
# ---------------------------------------------------------------------------
@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        return
    await message.answer(
        "🎵 Привет! Я ищу музыку на Audius и SoundCloud и присылаю её "
        "в лучшем доступном качестве.\n\n"
        "Просто пришли мне название трека или исполнителя — "
        "я найду варианты, а ты выберешь, что послушать.\n\n"
        "🎭 Не знаешь, что включить? /mood — музыка под настроение.\n"
        "Подсказка по командам — /help\n"
        f"Твой Telegram ID: {message.from_user.id}"
    )


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        return
    await message.answer(HELP_TEXT, parse_mode="HTML")


# Команды регистрируем ДО handle_search: иначе «/mood» ушёл бы в поиск как текст.
@dp.message(Command("mood"))
async def cmd_mood(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        return
    await message.answer(
        "Какое настроение? Подберу популярные треки с Audius (💎 320 kbps).\n"
        "Нажми ещё раз — будет другая подборка.",
        reply_markup=mood_keyboard(),
    )


async def show_results(status: Message, results: list[dict], head: str) -> None:
    """Показывает список треков (постранично) и запускает предзагрузку первых."""
    token = str(status.message_id)
    pages_cache[token] = [r["id"] for r in results]
    await status.edit_text(f"{head}. Выбери трек:", reply_markup=build_keyboard(token, 0))
    # Пока пользователь выбирает — заранее качаем первые треки.
    sweep_downloads()
    schedule_prefetch(pages_cache[token])


@dp.callback_query(F.data.startswith("mood:"))
async def handle_mood(callback: CallbackQuery) -> None:
    if not is_allowed(callback.from_user.id):
        await callback.answer()
        return
    code = callback.data.split(":", 1)[1]
    if code not in MOOD_BY_CODE:
        await callback.answer("Такого настроения нет — открой /mood заново", show_alert=True)
        return
    await callback.answer()

    label, _ = MOOD_BY_CODE[code]
    # Новое сообщение, а кнопки настроений остаются — можно сразу выбрать другое.
    status = await callback.message.answer(f"🔎 Подбираю: {label}…")
    try:
        results = await asyncio.to_thread(search_mood, code)
    except Exception as e:
        logger.exception("Ошибка подбора по настроению")
        await safe_edit(status, f"❌ Не получилось подобрать: {e}")
        return
    if not results:
        await safe_edit(status, "Ничего не нашлось 😔 Попробуй другое настроение.")
        return
    await show_results(status, results, f"{label}: {len(results)} треков, 💎 320 kbps")


@dp.message(F.text)
async def handle_search(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        return

    query = message.text.strip()
    status = await message.answer(f"🔎 Ищу: «{query}»…")

    try:
        results = await asyncio.to_thread(search_music, query)
    except Exception as e:
        logger.exception("Ошибка поиска")
        await status.edit_text(f"❌ Ошибка поиска: {e}")
        return

    if not results:
        await status.edit_text("Ничего не нашёл 😔 Попробуй другой запрос.")
        return

    hq = sum(1 for r in results if r.get("source") == "au")
    head = f"🎵 Нашёл вариантов: {len(results)}"
    if hq:
        head += f" (💎 320 kbps: {hq})"
    await show_results(status, results, head)


@dp.callback_query(F.data.startswith("page:"))
async def handle_page(callback: CallbackQuery) -> None:
    if not is_allowed(callback.from_user.id):
        await callback.answer()
        return
    _, token, page = callback.data.split(":")
    if token not in pages_cache:
        await callback.answer("Список устарел — поищи заново", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=build_keyboard(token, int(page)))
    # Предзагружаем первые треки открытой страницы.
    start = int(page) * PAGE_SIZE
    schedule_prefetch(pages_cache[token][start : start + PAGE_SIZE])


@dp.callback_query(F.data.startswith("play:"))
async def handle_play(callback: CallbackQuery) -> None:
    if not is_allowed(callback.from_user.id):
        await callback.answer()
        return

    key = callback.data.split(":", 1)[1]
    await callback.answer()

    # Если уже отправляли этот трек — шлём мгновенно по сохранённому file_id.
    cached = file_id_cache.get(key)
    if cached:
        await callback.message.answer_audio(
            cached["file_id"], caption=f"🎧 {cached['quality']}" if cached.get("quality") else None
        )
        return

    meta = meta_cache.get(key, {})
    name = track_name(meta)
    task = prefetch_tasks.pop(key, None)
    ready = task is not None and task.done() and task.exception() is None

    # «отправляет файл…» в шапке чата — видно и когда трек уже предзагружен.
    await callback.message.bot.send_chat_action(callback.message.chat.id, "upload_document")

    # Если трек не готов — статус с полоской прогресса, обновляемый в фоне.
    status: Optional[Message] = None
    ticker: Optional[asyncio.Task] = None
    if not ready:
        status = await callback.message.answer(progress_text(name, progress.get(key)))
        ticker = asyncio.create_task(show_progress(status, key, name))

    async def say(text: str) -> None:
        await (safe_edit(status, text) if status else callback.message.answer(text))

    try:
        try:
            if task is not None:
                path, meta = await task          # готово или дожидаемся фона
            else:
                path, meta = await asyncio.to_thread(download_audio, key)
            if not path.exists():                # файл могли только что удалить
                path, meta = await asyncio.to_thread(download_audio, key)
        except Exception as e:
            logger.exception("Ошибка скачивания")
            await stop_ticker(ticker)
            low = str(e).lower()
            # DRM/официальные релизы старая библиотека отдаёт как 404 «Not Found»,
            # а Go+ треки, у которых есть только 30-сек превью, — как «format not available».
            if any(w in low for w in ("drm", "404", "not found", "forbidden",
                                      "requested format", "недоступен")):
                await say("🔒 Этот трек защищён или недоступен (часто так с официальными "
                          "релизами). Выбери другой вариант из списка.")
            else:
                await say(f"❌ Не удалось скачать: {e}")
            return
        await stop_ticker(ticker)

        size_mb = path.stat().st_size / 1024 / 1024
        if size_mb > MAX_FILE_MB:
            await say(f"⚠️ Файл слишком большой ({size_mb:.0f} МБ). "
                      f"Telegram-боты могут отправлять до {MAX_FILE_MB} МБ.")
            return

        # Дальше время зависит от скорости интернета (заливка в Telegram).
        if status:
            await safe_edit(status, f"📤 Отправляю «{name}» в Telegram…")
        duration = meta.get("duration")
        quality = meta.get("quality")
        sent = await callback.message.answer_audio(
            FSInputFile(path),
            title=meta.get("title"),
            performer=meta.get("uploader") or None,
            duration=int(duration) if duration else None,
            caption=f"🎧 {quality}" if quality else None,
        )
        if sent.audio:
            file_id_cache[key] = {"file_id": sent.audio.file_id, "quality": quality}
            save_cache()
        if status:
            try:
                await status.delete()
            except TelegramBadRequest:
                pass
    finally:
        await stop_ticker(ticker)
        _cleanup(key)             # чистим скачанный файл (аудио + временные)
        progress.pop(key, None)


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN. Скопируй .env.example в .env и впиши токен.")
    await bot.set_my_commands([
        BotCommand(command="start", description="Запустить / приветствие"),
        BotCommand(command="mood", description="Музыка под настроение"),
        BotCommand(command="help", description="Как пользоваться"),
    ])
    sweep_downloads()
    logger.info("yt-dlp: %s | ffmpeg: %s", yt_dlp.version.__version__,
                "есть" if FFMPEG else "НЕТ")
    logger.info("Бот запущен (Audius 320 + SoundCloud, предзагрузка топ-%d)", PREFETCH_TOP)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
