# Telegram-боты: музыка и видео

Два личных Telegram-бота на Python (aiogram 3 + yt-dlp).

| Бот | Что делает | Подробности |
|---|---|---|
| 🎵 [`music_bot`](music_bot/) | Поиск музыки на Audius (MP3 320 kbps) и SoundCloud (до AAC 160 kbps), отправка трека с обложкой, подборки по настроению (`/mood`), прогресс скачивания, предзагрузка | [README](music_bot/README.md) |
| 📥 [`video_bot`](video_bot/) | Скачивание видео по ссылке с YouTube / TikTok / Instagram, автоподбор качества под лимит Telegram | [README](video_bot/README.md) |

## Быстрый старт
```bash
python3 -m venv venv
venv/bin/pip install -r music_bot/requirements.txt
cd music_bot
cp .env.example .env   # впиши токен бота от @BotFather
../venv/bin/python bot.py
```
Для каждого бота нужен свой токен от [@BotFather](https://t.me/BotFather) и
`ffmpeg` (`brew install ffmpeg`). Детали — в README каждого бота.

## Важно
Боты сделаны для **личного использования**. Соблюдай авторские права и правила
платформ. Защищённый (DRM) контент боты не скачивают.
