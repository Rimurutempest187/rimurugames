# aiosqlite.py — lightweight shim using builtin sqlite3 + asyncio
# Drop this file beside bot.py to avoid installing the real aiosqlite package.
import sqlite3
import asyncio
from typing import Any, Iterable, Optional

Row = sqlite3.Row

class AsyncCursor:
    def __init__(self, cursor: sqlite3.Cursor, conn: sqlite3.Connection):
        self._cur = cursor
        self._conn = conn

    async def fetchone(self) -> Optional[sqlite3.Row]:
        def _fn(): 
            return self._cur.fetchone()
        return await asyncio.to_thread(_fn)

    async def fetchall(self) -> list:
        def _fn():
            return self._cur.fetchall()
        return await asyncio.to_thread(_fn)

    async def fetchmany(self, size: int):
        def _fn():
            return self._cur.fetchmany(size)
        return await asyncio.to_thread(_fn)

    async def close(self):
        def _fn():
            try:
                self._cur.close()
            except Exception:
                pass
        await asyncio.to_thread(_fn)

class AsyncConnection:
    def __init__(self, db_file: str):
        # allow multi-thread use (we call via asyncio.to_thread)
        self._conn = sqlite3.connect(db_file, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = asyncio.Lock()

    async def execute(self, sql: str, params: Iterable[Any] = ()):
        async with self._lock:
            def _fn():
                cur = self._conn.execute(sql, params)
                return cur
            cur = await asyncio.to_thread(_fn)
            return AsyncCursor(cur, self._conn)

    async def executescript(self, script: str):
        async with self._lock:
            await asyncio.to_thread(self._conn.executescript, script)

    async def commit(self):
        async with self._lock:
            await asyncio.to_thread(self._conn.commit)

    async def close(self):
        def _fn():
            try:
                self._conn.close()
            except Exception:
                pass
        await asyncio.to_thread(_fn)

    # allow `async with` usage: `async with await connect(...) as db:`
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        # don't close connection on exit — keep it open for reuse, or call .close() manually
        return False

# top-level connect function to mimic aiosqlite.connect()
async def connect(db_file: str):
    # creating connection is cheap — do it synchronously inside to_thread for safety
    return AsyncConnection(db_file)
