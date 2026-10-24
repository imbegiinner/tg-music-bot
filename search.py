"""Поиск треков и скачивание аудио.

Основной источник — SoundCloud через его API: ищем прямо там, откуда потом качаем,
поэтому каждый результат в выдаче гарантированно скачивается целиком.
Платные отрывки (Go+) и треки с DRM отфильтровываются ещё до показа.

Опционально (USE_CATALOG=1) к выдаче добавляются треки из каталога Deezer/iTunes,
аудио для них ищется на YouTube Music / SoundCloud по названию и длительности.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import quote, urlencode, urlparse

from yt_dlp import YoutubeDL

from config import (AUDIO_SOURCES, CATALOGS, ITUNES_COUNTRY, SOUNDCLOUD_CLIENT_ID, USE_CATALOG,
                    YTDLP_COOKIES, YTDLP_COOKIES_BROWSER, YTDLP_PROXY)

log = logging.getLogger(__name__)

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"}
MIN_DUR, MAX_DUR = 15, 10 * 60


@dataclass
class Track:
    key: str
    source: str        # soundcloud | deezer | itunes | direct
    url: str           # soundcloud/direct — ссылка на аудио; каталог — страница трека
    title: str
    artist: str
    duration: int
    thumb: str | None = None


@dataclass
class Downloaded:
    path: str
    thumb: str | None
    duration: int


def make_key(source: str, ident) -> str:
    return hashlib.sha1(f"{source}:{ident}".encode()).hexdigest()[:16]


def fmt_dur(sec: int) -> str:
    return f"{sec // 60}:{sec % 60:02d}" if sec else "–:––"


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def short_error(e: BaseException) -> str:
    """Короткая человекочитаемая причина ошибки (для логов и сообщения пользователю)."""
    text = _ANSI.sub("", str(e) or e.__class__.__name__).strip()
    text = text.splitlines()[-1] if text else e.__class__.__name__
    text = re.sub(r"(Use --cookies|See https?://|Also see https?://).*", "", text)
    return text.removeprefix("ERROR: ").strip()[:300]


def norm(s: str) -> str:
    """'Батареи (feat. X) [Remix]' -> 'батареи' — для сравнения названий."""
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"[\(\[].*?[\)\]]", " ", s)
    s = re.sub(r"\b(feat|ft|prod)\b\.?.*", " ", s)
    return re.sub(r"\W+", "", s)


def _tokens(s: str) -> list[str]:
    return re.findall(r"\w+", (s or "").lower().replace("ё", "е"))


def _http(url: str, timeout: float = 8) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def _http_json(url: str, timeout: float = 8):
    return json.loads(_http(url, timeout))


def _ydl_opts(**extra) -> dict:
    o = {"quiet": True, "no_warnings": True, "noplaylist": True}
    if YTDLP_COOKIES:
        o["cookiefile"] = YTDLP_COOKIES
    if YTDLP_COOKIES_BROWSER:
        o["cookiesfrombrowser"] = (YTDLP_COOKIES_BROWSER,)
    if YTDLP_PROXY:
        o["proxy"] = YTDLP_PROXY
    o.update(extra)
    return o


def _split_title(title: str, fallback_artist: str) -> tuple[str, str]:
    for sep in (" - ", " – ", " — "):
        if sep in title:
            a, t = title.split(sep, 1)
            return a.strip(), t.strip()
    return fallback_artist, title.strip()


# ---------------- SoundCloud API ----------------

class SoundCloud:
    API = "https://api-v2.soundcloud.com"

    def __init__(self):
        self.client_id = SOUNDCLOUD_CLIENT_ID
        self.lock = threading.Lock()

    def _scrape_client_id(self) -> str:
        """client_id берётся из JS-файлов сайта — так же делает yt-dlp."""
        html = _http("https://soundcloud.com/").decode("utf-8", "ignore")
        scripts = re.findall(r'<script[^>]+src="(https://[^"]+\.js)"', html)
        for src in reversed(scripts):
            js = _http(src).decode("utf-8", "ignore")
            m = re.search(r'client_id\s*:\s*"([0-9a-zA-Z]{32})"', js) or \
                re.search(r'client_id=([0-9a-zA-Z]{32})', js)
            if m:
                log.info("получен client_id SoundCloud")
                return m.group(1)
        raise RuntimeError("не удалось получить client_id SoundCloud")

    def get(self, path: str, **params):
        for attempt in range(2):
            with self.lock:
                if not self.client_id or attempt:
                    self.client_id = self._scrape_client_id()
                cid = self.client_id
            try:
                return _http_json(f"{self.API}{path}?" + urlencode({**params, "client_id": cid}))
            except urllib.error.HTTPError as e:
                if e.code in (401, 403) and attempt == 0:  # client_id протух — берём новый
                    continue
                raise


sc = SoundCloud()


def _sc_playable(t: dict) -> bool:
    """Можно ли скачать трек целиком: не платный отрывок, не DRM, не заблокирован."""
    if t.get("policy") in ("BLOCK", "SNIP") or t.get("streamable") is False:
        return False
    for tr in (t.get("media") or {}).get("transcodings") or []:
        proto = (tr.get("format") or {}).get("protocol")
        if not tr.get("snipped") and proto in ("progressive", "hls"):
            return True
    return False


def _sc_track(t: dict) -> Track | None:
    if t.get("kind") != "track" or not _sc_playable(t):
        return None
    dur = int((t.get("full_duration") or t.get("duration") or 0) / 1000)
    url = (t.get("permalink_url") or "").split("?")[0]
    if not url or not MIN_DUR <= dur <= MAX_DUR:
        return None
    title = (t.get("title") or "").strip()
    user = (t.get("user") or {}).get("username") or "SoundCloud"
    artist = ((t.get("publisher_metadata") or {}).get("artist") or "").strip()
    if artist:
        for sep in (" - ", " – ", " — "):
            if title.lower().startswith(artist.lower() + sep):
                title = title[len(artist) + len(sep):].strip()
    else:
        artist, title = _split_title(title, user)
    art = t.get("artwork_url") or (t.get("user") or {}).get("avatar_url") or ""
    thumb = art.replace("-large.", "-t300x300.") or None
    return Track(make_key("soundcloud", url), "soundcloud", url, title, artist or user, dur, thumb)


def _sc_search_api(q: str, n: int = 50) -> list[Track]:
    d = sc.get("/search/tracks", q=q, limit=n, offset=0, linked_partitioning=1, app_locale="en")
    return [t for x in d.get("collection") or [] if (t := _sc_track(x))]


def _sc_search_ytdlp(q: str, n: int = 20) -> list[Track]:
    """Запасной вариант, если API недоступен: без фильтра платных/DRM треков."""
    with YoutubeDL(_ydl_opts(extract_flat="in_playlist", skip_download=True)) as ydl:
        info = ydl.extract_info(f"scsearch{n}:{q}", download=False)
    out = []
    for e in info.get("entries") or []:
        url = (e.get("webpage_url") or e.get("url") or "").split("?")[0]
        dur = int(e.get("duration") or 0)
        if not url or not e.get("title") or (dur and not 32 <= dur <= MAX_DUR):
            continue
        artist, title = _split_title(e["title"], e.get("uploader") or "SoundCloud")
        thumbs = e.get("thumbnails") or []
        thumb = e.get("thumbnail") or (thumbs[-1].get("url") if thumbs else None)
        out.append(Track(make_key("soundcloud", url), "soundcloud", url, title, artist, dur, thumb))
    return out


def _sc_search(q: str) -> list[Track]:
    try:
        return _sc_search_api(q)
    except Exception as e:
        log.warning("API SoundCloud недоступен (%s), ищу через yt-dlp", short_error(e))
        return _sc_search_ytdlp(q)


def _sc_resolve(url: str) -> list[Track]:
    d = sc.get("/resolve", url=url)
    kind = d.get("kind")
    if kind == "track":
        items = [d]
    elif kind in ("playlist", "system-playlist"):
        items = d.get("tracks") or []
        missing = [str(x["id"]) for x in items if "title" not in x and "id" in x]
        if missing:  # в плейлистах полные данные есть только у первых треков
            full = {}
            for i in range(0, len(missing), 50):
                for x in sc.get("/tracks", ids=",".join(missing[i:i + 50])) or []:
                    full[x["id"]] = x
            items = [full.get(x.get("id"), x) for x in items]
    else:
        items = []
    return [t for x in items if (t := _sc_track(x))][:30]


# ---------------- каталоги (опционально) ----------------

def _deezer_track(t: dict) -> Track:
    return Track(
        make_key("deezer", t["id"]), "deezer", t.get("link", ""),
        t.get("title") or "", (t.get("artist") or {}).get("name") or "",
        int(t.get("duration") or 0), (t.get("album") or {}).get("cover_medium"),
    )


def _deezer(q: str, n: int) -> list[Track]:
    d = _http_json("https://api.deezer.com/search?" + urlencode({"q": q, "limit": n}))
    if "error" in d:
        raise RuntimeError(f"deezer: {d['error']}")
    return [_deezer_track(t) for t in d.get("data") or [] if t.get("type", "track") == "track"]


def _itunes(q: str, n: int) -> list[Track]:
    d = _http_json("https://itunes.apple.com/search?" + urlencode(
        {"term": q, "entity": "song", "limit": n, "country": ITUNES_COUNTRY}))
    out = []
    for t in d.get("results") or []:
        if t.get("kind") != "song":
            continue
        art = (t.get("artworkUrl100") or "").replace("100x100bb", "300x300bb") or None
        out.append(Track(
            make_key("itunes", t["trackId"]), "itunes", t.get("trackViewUrl", ""),
            t.get("trackName") or "", t.get("artistName") or "",
            int(t.get("trackTimeMillis") or 0) // 1000, art,
        ))
    return out


CATALOG_FUNCS = {"deezer": _deezer, "itunes": _itunes}


def _catalog(q: str) -> list[Track]:
    for name in CATALOGS:
        fn = CATALOG_FUNCS.get(name)
        if not fn:
            continue
        try:
            res = fn(q, 15)
        except Exception as e:
            log.warning("каталог %s недоступен: %s", name, short_error(e))
            continue
        if res:
            return res
    return []


# ---------------- ранжирование ----------------

def rank(query: str, tracks: list[Track]) -> list[Track]:
    """Сортировка по совпадению слов запроса с «исполнитель + название».
    Последнее слово может быть недописанным (инлайн-режим) — засчитываем по префиксу."""
    q = _tokens(query)
    if not q:
        return tracks
    scored = []
    for i, t in enumerate(tracks):
        words = set(_tokens(f"{t.artist} {t.title}"))
        hit = 0.0
        for j, w in enumerate(q):
            if w in words:
                hit += 1
            elif any(x.startswith(w) or (len(w) > 3 and w.startswith(x) and len(x) > 3) for x in words):
                hit += 0.7 if j == len(q) - 1 else 0.4
        scored.append((hit / len(q), i, t))
    best = max(s for s, _, _ in scored)
    if best > 0:  # отбрасываем то, что почти не связано с запросом
        scored = [x for x in scored if x[0] >= best * 0.5]
    scored.sort(key=lambda x: (-round(x[0], 1), x[1]))
    return [t for _, _, t in scored]


def dedupe(tracks: list[Track]) -> list[Track]:
    out, seen = [], set()
    for t in tracks:
        sig = (re.sub(r"\W+", "", t.artist.lower()), re.sub(r"\W+", "", t.title.lower()), round(t.duration / 4))
        if t.title and sig not in seen:
            seen.add(sig)
            out.append(t)
    return out


# ---------------- поиск по ссылке ----------------

URL_RE = re.compile(r"https?://\S+", re.I)
SHORT_HOSTS = {"on.soundcloud.com", "link.deezer.com", "deezer.page.link", "dzr.page.link"}


def find_url(text: str) -> str | None:
    m = URL_RE.search(text or "")
    return m.group(0) if m else None


def _host(url: str) -> str:
    h = urlparse(url).netloc.lower()
    for p in ("www.", "m."):
        h = h.removeprefix(p)
    return h


def is_youtube(url: str) -> bool:
    return _host(url) in {"youtube.com", "youtu.be", "music.youtube.com"}


def _expand(url: str) -> str:
    """Короткие ссылки (on.soundcloud.com/…, link.deezer.com/…) -> полные."""
    if urlparse(url).netloc.lower() not in SHORT_HOSTS:
        return url
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=8) as r:
            return r.geturl()
    except urllib.error.HTTPError as e:
        return e.geturl()


def _by_url(url: str) -> list[Track]:
    url = _expand(url)
    host = _host(url)

    if host == "soundcloud.com":
        clean = "https://soundcloud.com" + urlparse(url).path.rstrip("/")
        return _sc_resolve(clean)

    if host.endswith("deezer.com"):
        m = re.search(r"/track/(\d+)", url)
        if m:
            t = _http_json(f"https://api.deezer.com/track/{m.group(1)}")
            if "error" not in t:
                dz = _deezer_track(t)
                # ищем этот трек на SoundCloud; сам трек из Deezer — только если включён каталог
                found = rank(f"{dz.artist} {dz.title}", _sc_search(f"{dz.artist} {dz.title}"))[:10]
                return dedupe(([dz] if USE_CATALOG else []) + found)

    return []  # YouTube и прочие ссылки не поддерживаем — как в оригинале


# ---------------- поиск ----------------

async def search(query: str) -> list[Track]:
    if url := find_url(query):
        if is_youtube(url):
            return []
        try:
            return await asyncio.wait_for(asyncio.to_thread(_by_url, url), 15)
        except Exception as e:
            log.warning("не разобрал ссылку %s: %s", url, short_error(e))
            return []

    q = " ".join(_tokens(query))  # «Артист — Трек» -> «артист трек»
    if not q:
        return []
    tasks = [asyncio.wait_for(asyncio.to_thread(_sc_search, q), 15)]
    if USE_CATALOG:
        tasks.append(asyncio.wait_for(asyncio.to_thread(_catalog, q), 10))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    lists = []
    for r in results:
        if isinstance(r, list):
            lists.append(r)
        else:
            log.warning("поиск не удался: %s", short_error(r))
            lists.append([])
    found = rank(query, lists[0])
    if USE_CATALOG:
        found += rank(query, lists[1])
    return dedupe(found)[:30]


# ---------------- скачивание ----------------

class WrongTrack(Exception):
    """Кандидат не подошёл по длительности — пробуем следующий."""


def _dur_ok(got: int, want: int) -> bool:
    return not want or not got or abs(got - want) <= max(6, want * 0.05)


def _flat(target: str, n: int = 5) -> list[dict]:
    with YoutubeDL(_ydl_opts(extract_flat="in_playlist", skip_download=True, playlistend=n)) as ydl:
        info = ydl.extract_info(target, download=False)
    return (info.get("entries") or [])[:n]


def _pick(entries: list[dict], title: str, duration: int) -> list[str]:
    """Кандидаты с совпадающим названием; сначала те, у кого длительность точно совпала."""
    want = norm(title)
    exact, unknown = [], []
    for e in entries:
        got = norm(e.get("title") or "")
        d = int(e.get("duration") or 0)
        if not (want and got and (want in got or got in want)):
            continue
        if 0 < d <= 31 and duration > 40:  # 30-секундный отрывок
            continue
        url = e.get("webpage_url") or e.get("url")
        if url and d and _dur_ok(d, duration):
            exact.append(url)
        elif url and not d:
            unknown.append(url)
    return (exact + unknown)[:3]


SOURCE_NAMES = {"ytmusic": "YouTube Music", "soundcloud": "SoundCloud", "youtube": "YouTube"}


def _search_target(src: str, q: str) -> str:
    return {
        "ytmusic": f"https://music.youtube.com/search?q={quote(q)}#songs",
        "soundcloud": f"scsearch5:{q}",
        "youtube": f"ytsearch5:{q}",
    }[src]


def _to_tg_thumb(src: str, dst: str) -> str | None:
    """Обложка для Telegram: jpg 320x320."""
    r = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", src,
         "-vf", "scale=320:320:force_original_aspect_ratio=increase,crop=320:320",
         "-q:v", "5", dst],
        capture_output=True,
    )
    return dst if r.returncode == 0 and os.path.exists(dst) else None


def _probe(path: str) -> int:
    """Реальная длительность готового файла (секунды) через ffprobe."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", path], capture_output=True, text=True, timeout=30)
        return int(float(r.stdout.strip()))
    except Exception:
        return 0


def _download(url: str, outdir: str, want: int = 0) -> Downloaded:
    for f in os.listdir(outdir):  # остатки прошлой попытки
        os.remove(os.path.join(outdir, f))
    opts = _ydl_opts(
        format="bestaudio[protocol^=http]/bestaudio/best",
        outtmpl=os.path.join(outdir, "audio.%(ext)s"),
        writethumbnail=True,
        retries=5,
        fragment_retries=5,
        skip_unavailable_fragments=False,  # лучше ошибка, чем дырявый файл
        postprocessors=[
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
        ],
    )
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        meta = int(info.get("duration") or 0)
        # защита сервера: длинные видео, миксы и стримы не качаем
        if info.get("is_live") or meta > MAX_DUR:
            raise RuntimeError(f"слишком длинный ({fmt_dur(meta)}), лимит {fmt_dur(MAX_DUR)}")
        if not _dur_ok(meta, want):
            raise WrongTrack(f"другая версия ({fmt_dur(meta)} вместо {fmt_dur(want)})")
        ydl.process_ie_result(info, download=True)

    mp3 = os.path.join(outdir, "audio.mp3")
    if not os.path.exists(mp3):
        raise RuntimeError("ffmpeg не создал mp3 — проверьте, что ffmpeg установлен")
    real = _probe(mp3)
    expected = want or meta
    if real and expected and real < expected * 0.9:
        raise WrongTrack(f"скачался только отрывок ({fmt_dur(real)} из {fmt_dur(expected)})")
    src_thumb = os.path.join(outdir, "audio.jpg")
    thumb = _to_tg_thumb(src_thumb, os.path.join(outdir, "thumb_yt.jpg")) if os.path.exists(src_thumb) else None
    return Downloaded(mp3, thumb, real or meta)


def friendly(name: str, e: BaseException) -> str:
    msg = short_error(e)
    if "Sign in to confirm" in msg or "not a bot" in msg:
        return f"{name}: требует вход — нужны cookies или прокси"
    if "DRM" in msg:
        return f"{name}: трек защищён DRM"
    if "HTTP Error 403" in msg:
        return f"{name}: доступ запрещён (403)"
    return f"{name}: {msg}"


def _fetch_sync(source, url, artist, title, duration, cover, outdir) -> Downloaded:
    errors: list[str] = []
    res = None
    if source in ("soundcloud", "direct", None):
        try:
            res = _download(url, outdir, duration)
        except Exception as e:
            errors.append(friendly("SoundCloud" if source == "soundcloud" else "источник", e))
    else:
        q = f"{artist} {title}"
        for src in AUDIO_SOURCES:
            name = SOURCE_NAMES.get(src)
            if not name or res:
                continue
            try:
                candidates = _pick(_flat(_search_target(src, q)), title, duration)
            except Exception as e:
                errors.append(friendly(name, e))
                continue
            if not candidates:
                errors.append(f"{name}: не нашёлся")
                continue
            last = None
            for media_url in candidates:
                log.info("«%s — %s»: пробую %s %s", artist, title, name, media_url)
                try:
                    res = _download(media_url, outdir, duration)
                    break
                except WrongTrack as e:
                    log.info("  не подошёл: %s", e)
                    last = e
                except Exception as e:
                    log.warning("«%s — %s»: %s не сработал: %s", artist, title, name, short_error(e))
                    last = e
                    break
            if not res and last:
                errors.append(friendly(name, last))
    if res is None:
        raise RuntimeError("; ".join(errors) or "не нашёл аудио")

    if cover:  # обложка из каталога / SoundCloud в хорошем качестве
        try:
            raw = os.path.join(outdir, "cover_src")
            with open(raw, "wb") as f:
                f.write(_http(cover))
            res.thumb = _to_tg_thumb(raw, os.path.join(outdir, "thumb.jpg")) or res.thumb
        except Exception as e:
            log.info("обложка не скачалась: %s", short_error(e))
    return res


async def fetch_audio(source, url, artist, title, duration, cover, outdir) -> Downloaded:
    return await asyncio.to_thread(_fetch_sync, source, url, artist, title, duration, cover, outdir)
