import asyncio
import logging
import secrets
import shutil
import tempfile
import time
from collections import OrderedDict, defaultdict
from datetime import datetime
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery, ChatMemberUpdated, ChosenInlineResult, FSInputFile, InlineKeyboardButton,
    InlineKeyboardMarkup, InlineQuery, InlineQueryResultArticle, InlineQueryResultsButton,
    InputMediaAudio, InputTextMessageContent, Message,
)

import config
from db import DB
from search import Track, fetch_audio, find_url, fmt_dur, is_youtube, search, short_error

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

db = DB(config.DB_PATH)
router = Router()
dl_sem = asyncio.Semaphore(config.MAX_PARALLEL_DOWNLOADS)
dl_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
PAGE = 5


class TTLCache(OrderedDict):
    def __init__(self, ttl: float, maxsize: int):
        super().__init__()
        self.ttl, self.maxsize = ttl, maxsize

    def get_fresh(self, k):
        item = self.get(k)
        if item and time.time() - item[0] < self.ttl:
            return item[1]
        return None

    def put(self, k, v):
        self[k] = (time.time(), v)
        self.move_to_end(k)
        while len(self) > self.maxsize:
            self.popitem(last=False)


search_cache = TTLCache(ttl=15 * 60, maxsize=2000)   # запрос -> треки
sessions = TTLCache(ttl=60 * 60, maxsize=5000)       # id выдачи в личке -> треки
inflight: dict[str, asyncio.Task] = {}


async def search_cached(query: str) -> list[Track]:
    url = find_url(query)
    q = url or " ".join(query.lower().split())  # в ссылках регистр важен
    if (hit := search_cache.get_fresh(q)) is not None:
        return hit
    if q not in inflight:  # пока пользователь печатает, одинаковые запросы не дублируем
        inflight[q] = asyncio.create_task(search(q))
    try:
        tracks = await inflight[q]
    finally:
        inflight.pop(q, None)
    if tracks:
        await db.upsert_tracks(tracks)
    search_cache.put(q, tracks)
    return tracks


def track_kb(title: str, artist: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"💿 {title}"[:60],
                             switch_inline_query_current_chat=f"{artist} {title}"[:200]),
    ]])


def loading_text(row) -> str:
    return f"⏳ Загружаю <b>{escape(row['artist'])} — {escape(row['title'])}</b>…"


def error_text(row, e: BaseException) -> str:
    return (f"❌ Не удалось загрузить <b>{escape(row['artist'])} — {escape(row['title'])}</b>\n"
            f"<i>{escape(short_error(e))}</i>")


def ago(ts: float) -> str:
    d = int(time.time() - ts)
    if d < 60:
        return f"{d} сек. назад"
    if d < 3600:
        return f"{d // 60} мин. назад"
    if d < 86400:
        return f"{d // 3600} ч. назад"
    return datetime.fromtimestamp(ts).strftime("%d.%m в %H:%M")


async def ensure_file_id(bot: Bot, key: str) -> str:
    """Скачивает трек один раз, заливает в канал-хранилище и кэширует file_id."""
    async with dl_locks[key]:
        row = await db.get_track(key)
        if row is None:
            raise LookupError("трек не найден в базе, повтори поиск")
        if row["file_id"]:
            return row["file_id"]
        async with dl_sem:
            with tempfile.TemporaryDirectory() as tmp:
                t0 = time.monotonic()
                res = await fetch_audio(row["source"], row["url"], row["artist"], row["title"],
                                        row["duration"], row["thumb"], tmp)
                log.info("скачано за %.1fс: %s — %s", time.monotonic() - t0, row["artist"], row["title"])
                msg = await bot.send_audio(
                    config.STORAGE_CHAT_ID,
                    FSInputFile(res.path, filename=f"{row['artist']} - {row['title']}.mp3"),
                    title=row["title"],
                    performer=row["artist"],
                    duration=res.duration or row["duration"] or None,
                    thumbnail=FSInputFile(res.thumb) if res.thumb else None,
                )
        await db.set_file_id(key, msg.audio.file_id)
        return msg.audio.file_id


# ---------- личные сообщения ----------

@router.message(CommandStart())
async def start(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔎 Искать в любом чате", switch_inline_query=""),
    ]])
    me = await m.bot.me()
    await m.answer("Привет! 🩵\nПросто напиши артиста или название песни, и я найду их.\n\n"
                   f"В любом чате: <code>@{me.username} название</code>", reply_markup=kb)


@router.message(Command("check"))
async def check_cmd(m: Message):
    """Проверка, что бот может писать в канал-хранилище и ffmpeg на месте."""
    problems = await self_check(m.bot)
    await m.answer("✅ Всё в порядке" if not problems else "⚠️ " + "\n⚠️ ".join(map(escape, problems)))


def render_page(sid: str, tracks: list[Track], page: int) -> tuple[str, InlineKeyboardMarkup]:
    start = page * PAGE
    chunk = tracks[start:start + PAGE]
    lines = [f"<b>{i}.</b> {fmt_dur(t.duration)} · <b>{escape(t.title)}</b> · {escape(t.artist)}"
             for i, t in enumerate(chunk, start=start + 1)]
    rows = [[InlineKeyboardButton(text=str(i), callback_data=f"dl:{t.key}")
             for i, t in enumerate(chunk, start=start + 1)]]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="«", callback_data=f"pg:{sid}:{page - 1}"))
    if start + PAGE < len(tracks):
        nav.append(InlineKeyboardButton(text="»", callback_data=f"pg:{sid}:{page + 1}"))
    if nav:
        rows.append(nav)
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.chat.type == "private", F.text, ~F.text.startswith("/"), F.via_bot.is_(None))
async def private_search(m: Message):
    # сообщения «через @бот» (плашки инлайн-режима) сюда не попадают — это не запросы.
    # «@бот запрос», отправленный текстом (кнопка «Найти снова»), ищем как обычный запрос.
    me = await m.bot.me()
    text = m.text.strip()
    if text.lower().startswith(f"@{me.username.lower()}"):
        text = text[len(me.username) + 1:].strip()
    if len(text) < 2:
        return await m.answer("Напиши хотя бы пару букв названия или исполнителя 🙂")
    await m.bot.send_chat_action(m.chat.id, "typing")
    url = find_url(text)
    tracks = await search_cached(text)
    if not tracks:
        if url and is_youtube(url):
            return await m.answer("Ссылки на YouTube не поддерживаются 🙅\n"
                                  "Пришли название трека или ссылку на SoundCloud.")
        return await m.answer("Ничего не нашлось 😔\nПопробуй изменить запрос.")
    if url and len(tracks) == 1:  # ссылка на конкретный трек — сразу загружаем
        return await deliver(m.bot, m, m.from_user.id, tracks[0].key)
    sid = secrets.token_hex(4)
    sessions.put(sid, tracks)
    page_text, kb = render_page(sid, tracks, 0)
    await m.answer(page_text, reply_markup=kb)


@router.callback_query(F.data.startswith("pg:"))
async def paginate(cb: CallbackQuery):
    _, sid, page = cb.data.split(":")
    tracks = sessions.get_fresh(sid)
    if not tracks:
        return await cb.answer("Выдача устарела, повтори поиск", show_alert=True)
    text, kb = render_page(sid, tracks, int(page))
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("dl:"))
async def send_track(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    await deliver(bot, cb.message, cb.from_user.id, cb.data[3:])


async def deliver(bot: Bot, where: Message, user_id: int, key: str):
    """Плашка «Загружаю…» в чат, затем подмена её на сам трек."""
    row = await db.get_track(key)
    if row is None:
        return await where.answer("Выдача устарела, повтори поиск")

    msg = await where.answer(loading_text(row))
    try:
        file_id = await ensure_file_id(bot, key)
    except Exception as e:
        log.exception("не удалось загрузить %s", key)
        return await msg.edit_text(error_text(row, e))

    kb = track_kb(row["title"], row["artist"])
    try:  # затем подменяем плашку на сам трек
        await msg.edit_media(InputMediaAudio(media=file_id), reply_markup=kb)
    except TelegramBadRequest:
        await msg.delete()
        await where.answer_audio(file_id, reply_markup=kb)
    await db.add_history(user_id, key)


# ---------- инлайн-режим ----------

def article(key: str, title: str, artist: str, duration: int, thumb: str | None,
            note: str | None = None) -> InlineQueryResultArticle:
    desc = f"{fmt_dur(duration)} · {artist}" + (f"\n· {note}" if note else "")
    return InlineQueryResultArticle(
        id=key,
        title=title,
        description=desc,
        thumbnail_url=thumb,
        input_message_content=InputTextMessageContent(
            message_text=f"⏳ Загружаю <b>{escape(artist)} — {escape(title)}</b>…"),
        # клавиатура обязательна: без неё Telegram не даст inline_message_id для редактирования
        reply_markup=track_kb(title, artist),
    )


@router.inline_query()
async def inline(q: InlineQuery):
    text = q.query.strip()
    pm = InlineQueryResultsButton(text="Открыть личные сообщения 💬", start_parameter="inline")

    if len(text) < 2:  # пустой запрос -> недавние треки пользователя
        rows = await db.recent(q.from_user.id)
        results = [article(r["key"], r["title"], r["artist"], r["duration"], r["thumb"], ago(r["used_at"]))
                   for r in rows]
        return await q.answer(results, cache_time=0, is_personal=True, button=pm)

    tracks = (await search_cached(text))[:30]
    results = [article(t.key, t.title, t.artist, t.duration, t.thumb) for t in tracks]
    if not results:
        url = find_url(text)
        hint = ("Ссылки на YouTube не поддерживаются" if url and is_youtube(url)
                else "Попробуйте изменить запрос :(")
        results = [InlineQueryResultArticle(
            id="nothing", title="Ничего не найдено", description=hint,
            input_message_content=InputTextMessageContent(
                message_text=f"🔍 Ничего не найдено: {escape(text[:200])}"),
        )]
    try:
        await q.answer(results, cache_time=60, is_personal=False, button=pm)
    except TelegramBadRequest as e:  # "query is too old" — пользователь уже печатает дальше
        log.info("inline answer skipped: %s", e)


@router.chosen_inline_result()
async def chosen(r: ChosenInlineResult, bot: Bot):
    """В чат ушла плашка «Загружаю…» — скачиваем трек и подменяем её на аудио."""
    key = r.result_id
    log.info("выбран трек %s пользователем %s", key, r.from_user.id)
    if not r.inline_message_id:
        log.warning("нет inline_message_id — у результата должна быть клавиатура")
        return
    row = await db.get_track(key)
    if row is None:
        return
    await db.add_history(r.from_user.id, key)
    try:
        file_id = await ensure_file_id(bot, key)
        await bot.edit_message_media(inline_message_id=r.inline_message_id,
                                     media=InputMediaAudio(media=file_id),
                                     reply_markup=track_kb(row["title"], row["artist"]))
    except Exception as e:
        log.exception("не удалось загрузить %s", key)
        retry = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="🔁 Найти снова",
            switch_inline_query_current_chat=f"{row['artist']} {row['title']}"[:200])]])
        try:
            await bot.edit_message_text(error_text(row, e), inline_message_id=r.inline_message_id,
                                        reply_markup=retry)
        except TelegramBadRequest:
            pass


# ---------- запуск ----------

async def self_check(bot: Bot) -> list[str]:
    problems = []
    if not shutil.which("ffmpeg"):
        problems.append("ffmpeg не найден в PATH — без него бот не сможет сделать mp3")
    me = await bot.me()
    try:
        chat = await bot.get_chat(config.STORAGE_CHAT_ID)
        member = await bot.get_chat_member(chat.id, me.id)
        if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
            problems.append(f"@{me.username} не админ в канале «{chat.title}» — сделайте его админом с правом публикации")
        elif chat.type == "channel" and not getattr(member, "can_post_messages", True):
            problems.append(f"у @{me.username} нет права публиковать сообщения в канале «{chat.title}»")
    except TelegramAPIError as e:
        problems.append(f"@{me.username} не видит канал {config.STORAGE_CHAT_ID} ({e.message}). "
                        f"Добавьте в канал администратором именно @{me.username}")
    return problems


@router.my_chat_member()
async def on_added(upd: ChatMemberUpdated):
    """Когда бота добавляют в канал/чат или меняют ему права — пишем ID в консоль."""
    log.info("Бота добавили/изменили права: «%s» (%s), ID = %s, статус: %s",
             upd.chat.title, upd.chat.type, upd.chat.id, upd.new_chat_member.status)
    if upd.chat.id == config.STORAGE_CHAT_ID:
        problems = await self_check(upd.bot)
        log.info("Канал-хранилище: %s", "всё в порядке ✅" if not problems else "; ".join(problems))


async def main():
    await db.init()
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML", link_preview_is_disabled=True))
    me = await bot.me()
    log.info("Запущен бот @%s (id %s)", me.username, me.id)
    problems = await self_check(bot)
    for p in problems:
        log.error("ПРОВЕРКА: %s", p)
    if not shutil.which("ffmpeg"):
        await bot.session.close()
        raise SystemExit("Установите ffmpeg и перезапустите бота")
    if problems:
        log.warning("Бот работает, но треки грузиться не будут, пока канал не настроен. "
                    "Добавьте бота в канал — ID канала появится в консоли.")
    else:
        log.info("Проверка пройдена: канал-хранилище и ffmpeg в порядке")

    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
