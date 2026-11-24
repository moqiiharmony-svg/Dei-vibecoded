"""SQLite storage for the cogs (levels, tickets, giveaways, cases, panels, per-guild configs...).

DB_PATH env var controls the file location - point it at a Railway Volume (e.g. /data/bot.db)
so data survives redeploys.
"""
import json
import os
import sqlite3
import threading
import time
from typing import Optional

DB_PATH = os.getenv("DB_PATH", "bot.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS config(guild_id INTEGER, name TEXT, data TEXT, PRIMARY KEY(guild_id, name));
CREATE TABLE IF NOT EXISTS objs(guild_id INTEGER, kind TEXT, name TEXT, data TEXT, PRIMARY KEY(guild_id, kind, name));
CREATE TABLE IF NOT EXISTS levels(guild_id INTEGER, user_id INTEGER, xp INTEGER DEFAULT 0, level INTEGER DEFAULT 0,
    messages INTEGER DEFAULT 0, voice_seconds INTEGER DEFAULT 0, PRIMARY KEY(guild_id, user_id));
CREATE TABLE IF NOT EXISTS cases(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, case_no INTEGER, user_id INTEGER,
    mod_id INTEGER, action TEXT, reason TEXT, created INTEGER);
CREATE TABLE IF NOT EXISTS tickets(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, channel_id INTEGER, user_id INTEGER,
    ttype TEXT, number INTEGER, status TEXT DEFAULT 'open', claimed_by INTEGER, opened INTEGER, closed INTEGER, rating INTEGER);
CREATE TABLE IF NOT EXISTS giveaways(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, channel_id INTEGER,
    message_id INTEGER, data TEXT, end_time INTEGER, ended INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS giveaway_entries(giveaway_id INTEGER, user_id INTEGER, PRIMARY KEY(giveaway_id, user_id));
CREATE TABLE IF NOT EXISTS stars(guild_id INTEGER, message_id INTEGER, star_message_id INTEGER, PRIMARY KEY(guild_id, message_id));
CREATE TABLE IF NOT EXISTS suggestions(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, message_id INTEGER,
    author_id INTEGER, text TEXT, status TEXT DEFAULT 'open');
CREATE TABLE IF NOT EXISTS suggestion_votes(suggestion_id INTEGER, user_id INTEGER, vote INTEGER, PRIMARY KEY(suggestion_id, user_id));
CREATE TABLE IF NOT EXISTS birthdays(guild_id INTEGER, user_id INTEGER, month INTEGER, day INTEGER, PRIMARY KEY(guild_id, user_id));
CREATE TABLE IF NOT EXISTS schedules(id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, name TEXT, channel_id INTEGER,
    content TEXT, embed INTEGER, interval_s INTEGER, next_run INTEGER);
CREATE TABLE IF NOT EXISTS reminders(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, channel_id INTEGER, text TEXT, due INTEGER);
CREATE TABLE IF NOT EXISTS tempvc(channel_id INTEGER PRIMARY KEY, guild_id INTEGER, owner_id INTEGER);
"""

_lock = threading.RLock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")
_conn.executescript(SCHEMA)


def run(sql: str, params=()) -> int:
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur.lastrowid


def rows(sql: str, params=()) -> list:
    with _lock:
        return [dict(r) for r in _conn.execute(sql, params).fetchall()]


def row(sql: str, params=()) -> Optional[dict]:
    with _lock:
        r = _conn.execute(sql, params).fetchone()
        return dict(r) if r else None


# ---- per-guild config blobs -------------------------------------------------

def _merge(dst: dict, src: dict):
    for k, v in src.items():
        if k not in dst:
            dst[k] = json.loads(json.dumps(v))
        elif isinstance(v, dict) and v and isinstance(dst[k], dict):
            _merge(dst[k], v)


def cfg(gid: int, name: str, defaults: dict) -> dict:
    r = row("SELECT data FROM config WHERE guild_id=? AND name=?", (gid, name))
    data = json.loads(r["data"]) if r else {}
    _merge(data, defaults)
    return data


def save_cfg(gid: int, name: str, data: dict):
    run("INSERT INTO config(guild_id,name,data) VALUES(?,?,?) "
        "ON CONFLICT(guild_id,name) DO UPDATE SET data=excluded.data", (gid, name, json.dumps(data)))


# ---- named objects (ticket panels, role panels, tags...) -------------------

def obj_get(gid: int, kind: str, name: str) -> Optional[dict]:
    r = row("SELECT data FROM objs WHERE guild_id=? AND kind=? AND name=?", (gid, kind, name))
    return json.loads(r["data"]) if r else None


def obj_set(gid: int, kind: str, name: str, data: dict):
    run("INSERT INTO objs(guild_id,kind,name,data) VALUES(?,?,?,?) "
        "ON CONFLICT(guild_id,kind,name) DO UPDATE SET data=excluded.data", (gid, kind, name, json.dumps(data)))


def obj_del(gid: int, kind: str, name: str):
    run("DELETE FROM objs WHERE guild_id=? AND kind=? AND name=?", (gid, kind, name))


def obj_list(gid: int, kind: str) -> dict:
    return {r["name"]: json.loads(r["data"])
            for r in rows("SELECT name,data FROM objs WHERE guild_id=? AND kind=? ORDER BY name", (gid, kind))}


# ---- moderation cases -------------------------------------------------------

def add_case(gid: int, user_id: int, mod_id: int, action: str, reason: Optional[str]) -> int:
    with _lock:
        n = _conn.execute("SELECT COALESCE(MAX(case_no),0)+1 FROM cases WHERE guild_id=?", (gid,)).fetchone()[0]
        _conn.execute("INSERT INTO cases(guild_id,case_no,user_id,mod_id,action,reason,created) VALUES(?,?,?,?,?,?,?)",
                      (gid, n, user_id, mod_id, action, reason or "No reason provided", int(time.time())))
        _conn.commit()
    return n


def count_cases(gid: int, user_id: int, prefix: str, since: int) -> int:
    r = row("SELECT COUNT(*) AS c FROM cases WHERE guild_id=? AND user_id=? AND action LIKE ? AND created>=?",
            (gid, user_id, prefix + "%", since))
    return r["c"] if r else 0
