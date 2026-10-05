"""
Telegram-бот для скачивания видео (YouTube / TikTok / Instagram).

Как работает:
  1. Пришли боту ссылку на видео.
  2. Бот скачивает его через yt-dlp (сначала качество до 720p; если файл
     больше лимита Telegram ~50 МБ — автоматически пробует 480p, потом 360p).
  3. Присылает видео в чат — играет прямо в Telegram.

Скачивание делает standalone-бинарник yt-dlp (общий с музыкальным ботом,
лежит в ../music_bot/yt-dlp) — он самый свежий и не зависит от Python в системе.
Для YouTube используются cookies из браузера и deno (уже настроены).

Только для личного использования. См. README.md для запуска.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, FSInputFile, Message
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
# Список разрешённых Telegram ID через запятую. Если пусто — отвечает всем.
OWNER_IDS = {
    int(x) for x in os.getenv("OWNER_IDS", "").replace(" ", "").split(",") if x
}

MAX_FILE_MB = 49                        # лимит Telegram на отправку ботом (~50 МБ)
HEIGHT_STEPS = [720, 480, 360]          # понижаем качество, пока не влезет в лимит
DOWNLOAD_TIMEOUT = 900                  # сек на одно скачивание (15 минут)

HERE = Path(__file__).parent
DOWNLOAD_DIR = HERE / "downloads"
FFMPEG = shutil.which("ffmpeg")         # для склейки видео+звука
DENO = shutil.which("deno")             # JS-движок для челленджа YouTube

# Бинарник yt-dlp: свой локальный или общий из музыкального бота.
_CANDIDATES = [HERE / "yt-dlp", HERE.parent / "music_bot" / "yt-dlp"]
YTDLP_BIN = next((str(p) for p in _CANDIDATES if os.access(p, os.X_OK)), None)

# Cookies из браузера — нужны для YouTube (бот-чек) и Instagram (логин).
COOKIES_FROM_BROWSER = os.getenv("COOKIES_FROM_BROWSER", "").strip()
COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip()

# Какие сайты принимаем (поддомены вроде vm.tiktok.com / m.youtube.com подходят).
ALLOWED_HOSTS = ("youtube.com", "youtu.be", "tiktok.com", "instagram.com")

URL_RE = re.compile(r"https?://\S+")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

HELP_TEXT = (
    "📥 <b>Как пользоваться</b>\n\n"
    "Просто пришли мне ссылку на видео из YouTube, TikTok или Instagram — "
    "я скачаю его и пришлю в чат.\n\n"
    "• Качество подбираю автоматически: до 720p, а если видео не влезает "
    f"в лимит Telegram ({MAX_FILE_MB} МБ) — беру версию поменьше.\n"
    "• Длинные ролики могут не влезть даже в 360p — тогда я честно скажу.\n"
    "• Для Instagram нужны cookies браузера, где ты залогинен (см. README)."
)

DOWNLOAD_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("video_bot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def is_allowed(user_id: int) -> bool:
    return not OWNER_IDS or user_id in OWNER_IDS


def allowed_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


def cookie_args() -> list[str]:
    if COOKIES_FROM_BROWSER:
        return ["--cookies-from-browser", COOKIES_FROM_BROWSER]
    if COOKIES_FILE:
        return ["--cookies", COOKIES_FILE]
    return []


def subprocess_env() -> dict:
    """Гарантирует, что ffmpeg и deno видны в PATH дочернего процесса."""
    env = os.environ.copy()
    extra = [str(Path(p).parent) for p in (FFMPEG, DENO) if p]
    if extra:
        env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    return env


def clean_error(stderr: str) -> str:
    """Достаёт понятную строку ошибки из вывода yt-dlp."""
    text = ANSI_RE.sub("", stderr or "")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in reversed(lines):
        if "ERROR" in ln:
            return ln
    return lines[-1] if lines else "неизвестная ошибка"


def friendly_error(err: str) -> str:
    """Переводит типичные ошибки yt-dlp в понятные сообщения."""
    low = err.lower()
    if "sign in to confirm" in low:
        return (
            "YouTube требует подтвердить, что ты не бот. Проверь, что в .env "
            "задан COOKIES_FROM_BROWSER=chrome и ты залогинен в YouTube."
        )
    if "login required" in low or "rate-limit" in low or "requested content is not available" in low:
        return (
            "Instagram не отдаёт это видео: нужен логин (cookies браузера) "
            "или пост приватный. Проверь COOKIES_FROM_BROWSER в .env."
        )
    if "unsupported url" in low:
        return "Эта ссылка не похожа на страницу с видео."
    if "private" in low:
        return "Видео приватное — у твоего аккаунта нет к нему доступа."
    if "drm" in low:
        return "Видео защищено DRM и не качается."
    return err


# ---------------------------------------------------------------------------
# Скачивание (блокирующее — запускаем в отдельном потоке)
# ---------------------------------------------------------------------------
def download_video(url: str) -> tuple[Path, dict]:
    """Скачивает видео, понижая качество, пока файл не влезет в лимит.

    Возвращает (путь к файлу, метаданные). Файл лежит во временной папке —
    вызывающий обязан удалить её через cleanup_dir().
    """
    tmpdir = Path(tempfile.mkdtemp(dir=DOWNLOAD_DIR))
    last_size_mb = 0.0

    for cap in HEIGHT_STEPS:
        # Чистим папку перед попыткой (могли остаться файлы прошлого качества).
        for p in tmpdir.iterdir():
            p.unlink(missing_ok=True)

        cmd = [
            YTDLP_BIN,
            url,
            "-o", str(tmpdir / "%(id)s.%(ext)s"),
            "--no-warnings", "--no-progress", "--no-playlist",
            # --dump-json + --no-simulate: скачивает И печатает метаданные.
            "--dump-json", "--no-simulate",
            "--merge-output-format", "mp4",
            "-f", "bv*+ba/b",
            # Сортировка вместо жёстких фильтров:
            #  vcodec:h264 — H.264 в приоритете (VP9/AV1/H.265 у многих
            #                клиентов Telegram дают «звук без картинки»);
            #  res:{cap}   — разрешение до cap по МЕНЬШЕЙ стороне
            #                (корректно и для вертикальных роликов);
            #  ext         — предпочесть mp4/m4a.
            "-S", f"vcodec:h264,res:{cap},ext",
            *cookie_args(),
        ]
        if FFMPEG:
            cmd += ["--ffmpeg-location", FFMPEG]

        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                env=subprocess_env(), timeout=DOWNLOAD_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            cleanup_dir(tmpdir)
            raise RuntimeError("скачивание заняло слишком долго и было прервано")

        if proc.returncode != 0:
            cleanup_dir(tmpdir)
            raise RuntimeError(clean_error(proc.stderr))

        # Метаданные (название, длительность, размеры кадра).
        info: dict = {}
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    info = json.loads(line)
                    break
                except json.JSONDecodeError:
                    pass

        files = [p for p in tmpdir.iterdir() if p.is_file() and p.suffix != ".part"]
        if not files:
            cleanup_dir(tmpdir)
            raise RuntimeError("не удалось найти скачанный файл")
        path = max(files, key=lambda p: p.stat().st_size)

        last_size_mb = path.stat().st_size / 1024 / 1024
        if last_size_mb <= MAX_FILE_MB:
            logger.info("Скачано: %s (%.0f МБ, кап %sp)", path.name, last_size_mb, cap)
            return path, info
        logger.info("Файл %.0f МБ > %d МБ, пробую качество ниже %sp",
                    last_size_mb, MAX_FILE_MB, cap)

    cleanup_dir(tmpdir)
    raise RuntimeError(
        f"видео слишком большое даже в {HEIGHT_STEPS[-1]}p "
        f"({last_size_mb:.0f} МБ при лимите {MAX_FILE_MB} МБ)"
    )


def cleanup_dir(tmpdir: Path) -> None:
    shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Хэндлеры
# ---------------------------------------------------------------------------
@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        return
    await message.answer(
        "📥 Привет! Я скачиваю видео из YouTube, TikTok и Instagram.\n\n"
        "Просто пришли мне ссылку на видео.\n\n"
        "Подсказка — /help\n"
        f"Твой Telegram ID: {message.from_user.id}"
    )


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        return
    await message.answer(HELP_TEXT, parse_mode="HTML")


@dp.message(F.text)
async def handle_link(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        return

    match = URL_RE.search(message.text)
    if not match:
        await message.answer("Пришли мне ссылку на видео (YouTube / TikTok / Instagram).")
        return

    url = match.group(0)
    if not allowed_url(url):
        await message.answer(
            "Я умею качать только с YouTube, TikTok и Instagram. "
            "Эта ссылка на другой сайт."
        )
        return

    status = await message.answer("⏬ Скачиваю видео… (может занять минуту-другую)")
    await message.bot.send_chat_action(message.chat.id, "upload_video")

    tmpdir = None
    try:
        path, info = await asyncio.to_thread(download_video, url)
        tmpdir = path.parent
    except Exception as e:
        logger.exception("Ошибка скачивания")
        await status.edit_text(f"❌ Не получилось: {friendly_error(str(e))}")
        return

    try:
        title = (info.get("title") or "").strip()
        duration = info.get("duration")
        await message.answer_video(
            FSInputFile(path),
            caption=title[:1000] or None,
            duration=int(duration) if duration else None,
            width=info.get("width"),
            height=info.get("height"),
            supports_streaming=True,
        )
        await status.delete()
    finally:
        if tmpdir:
            cleanup_dir(tmpdir)  # чистим временную папку со скачанным файлом


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN. Скопируй .env.example в .env и впиши токен.")
    if not YTDLP_BIN:
        raise RuntimeError(
            "Не найден бинарник yt-dlp (искал ./yt-dlp и ../music_bot/yt-dlp). "
            "Скачай его: см. README."
        )
    await bot.set_my_commands([
        BotCommand(command="start", description="Запустить / приветствие"),
        BotCommand(command="help", description="Как пользоваться"),
    ])
    version = subprocess.run(
        [YTDLP_BIN, "--version"], capture_output=True, text=True
    ).stdout.strip()
    logger.info(
        "yt-dlp: %s (%s) | ffmpeg: %s | deno: %s",
        version, YTDLP_BIN,
        "есть" if FFMPEG else "НЕТ",
        "есть" if DENO else "НЕТ (YouTube не сможет отдать видео!)",
    )
    logger.info("Бот запущен (YouTube / TikTok / Instagram)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
