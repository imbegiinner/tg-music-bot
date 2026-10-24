import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
# Приватный канал, куда бот заливает mp3, чтобы получить file_id (бот — админ с правом публикации)
STORAGE_CHAT_ID = int(os.environ["STORAGE_CHAT_ID"])

DB_PATH = os.getenv("DB_PATH", "bot.db")
MAX_PARALLEL_DOWNLOADS = int(os.getenv("MAX_PARALLEL_DOWNLOADS", "3"))
# Если YouTube пишет «Sign in to confirm you're not a bot»:
# файл cookies в формате Netscape...
YTDLP_COOKIES = os.getenv("YTDLP_COOKIES") or None
# ...или взять cookies прямо из браузера (firefox, edge, chrome...). Firefox работает надёжнее всего.
YTDLP_COOKIES_BROWSER = os.getenv("YTDLP_COOKIES_BROWSER") or None
# Прокси для yt-dlp, например socks5://127.0.0.1:1080 или http://user:pass@host:port
YTDLP_PROXY = os.getenv("YTDLP_PROXY") or None

# Где искать аудио для треков из каталога, по порядку. Если источник не сработал — берётся следующий.
AUDIO_SOURCES = [s.strip() for s in os.getenv("AUDIO_SOURCES", "ytmusic,soundcloud").split(",") if s.strip()]

# Основной поиск — SoundCloud. client_id бот получает сам; задавать вручную не нужно.
SOUNDCLOUD_CLIENT_ID = os.getenv("SOUNDCLOUD_CLIENT_ID") or None

# 1 — добавлять в конец выдачи треки из каталога Deezer/iTunes.
# Аудио для них ищется на YouTube Music / SoundCloud (см. AUDIO_SOURCES), поэтому они грузятся не всегда.
USE_CATALOG = os.getenv("USE_CATALOG", "0") == "1"
CATALOGS = [s.strip() for s in os.getenv("CATALOGS", "deezer,itunes").split(",") if s.strip()]
ITUNES_COUNTRY = os.getenv("ITUNES_COUNTRY", "RU")
