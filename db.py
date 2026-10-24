import time

import aiosqlite

from search import Track

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    key      TEXT PRIMARY KEY,
    url      TEXT NOT NULL,
    title    TEXT NOT NULL,
    artist   TEXT NOT NULL,
    duration INTEGER DEFAULT 0,
    thumb    TEXT,
    file_id  TEXT,
    source   TEXT DEFAULT 'direct'
);
CREATE TABLE IF NOT EXISTS history (
    user_id INTEGER NOT NULL,
    key     TEXT NOT NULL,
    ts      REAL NOT NULL,
    PRIMARY KEY (user_id, key)
);
CREATE INDEX IF NOT EXISTS history_user_ts ON history(user_id, ts DESC);
"""


class DB:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def init(self):
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        # миграция базы от первой версии бота
        async with self.conn.execute("PRAGMA table_info(tracks)") as cur:
            cols = {r[1] for r in await cur.fetchall()}
        if "source" not in cols:
            await self.conn.execute("ALTER TABLE tracks ADD COLUMN source TEXT DEFAULT 'direct'")
        await self.conn.commit()

    async def upsert_tracks(self, tracks: list[Track]):
        await self.conn.executemany(
            """INSERT INTO tracks(key, url, title, artist, duration, thumb, source)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 url=excluded.url, title=excluded.title, artist=excluded.artist,
                 duration=excluded.duration, thumb=excluded.thumb, source=excluded.source""",
            [(t.key, t.url, t.title, t.artist, t.duration, t.thumb, t.source) for t in tracks],
        )
        await self.conn.commit()

    async def get_track(self, key: str):
        async with self.conn.execute("SELECT * FROM tracks WHERE key=?", (key,)) as cur:
            return await cur.fetchone()

    async def set_file_id(self, key: str, file_id: str):
        await self.conn.execute("UPDATE tracks SET file_id=? WHERE key=?", (file_id, key))
        await self.conn.commit()

    async def add_history(self, user_id: int, key: str):
        await self.conn.execute(
            """INSERT INTO history(user_id, key, ts) VALUES (?, ?, ?)
               ON CONFLICT(user_id, key) DO UPDATE SET ts=excluded.ts""",
            (user_id, key, time.time()),
        )
        await self.conn.commit()

    async def recent(self, user_id: int, limit: int = 30):
        async with self.conn.execute(
            """SELECT t.*, h.ts AS used_at FROM history h JOIN tracks t ON t.key = h.key
               WHERE h.user_id=? AND t.file_id IS NOT NULL
               ORDER BY h.ts DESC LIMIT ?""",
            (user_id, limit),
        ) as cur:
            return await cur.fetchall()
