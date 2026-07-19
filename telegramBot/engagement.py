import hashlib
import re
import time

import aiosqlite

from database import DB_NAME, get_user, update_xp


URL_RE = re.compile(r"https?://\S+|t\.me/\S+", re.I)
WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё0-9]")


async def create_engagement_tables():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.executescript('''
        CREATE TABLE IF NOT EXISTS chat_activity_state(
            chat_id INTEGER PRIMARY KEY, last_user_id INTEGER, last_activity_ts INTEGER DEFAULT 0,
            wave_started_ts INTEGER DEFAULT 0, wave_last_ts INTEGER DEFAULT 0,
            wave_active INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS chat_wave_members(
            chat_id INTEGER, wave_started_ts INTEGER, user_id INTEGER, awarded INTEGER DEFAULT 0,
            joined_at INTEGER DEFAULT 0,
            PRIMARY KEY(chat_id,wave_started_ts,user_id));
        CREATE TABLE IF NOT EXISTS rewarded_messages(
            chat_id INTEGER, message_id INTEGER, author_id INTEGER, created_ts INTEGER,
            PRIMARY KEY(chat_id,message_id));
        CREATE TABLE IF NOT EXISTS reply_rewards(
            chat_id INTEGER, parent_message_id INTEGER, responder_id INTEGER,
            PRIMARY KEY(chat_id,parent_message_id,responder_id));
        CREATE TABLE IF NOT EXISTS content_fingerprints(
            chat_id INTEGER, user_id INTEGER, fingerprint TEXT, last_ts INTEGER,
            PRIMARY KEY(chat_id,user_id,fingerprint));
        ''')
        for statement in (
            "ALTER TABLE chat_activity_state ADD COLUMN wave_active INTEGER DEFAULT 0",
            "ALTER TABLE chat_wave_members ADD COLUMN joined_at INTEGER DEFAULT 0",
        ):
            try:
                await db.execute(statement)
            except Exception:
                pass
        await db.commit()


def meaningful_text(text: str) -> tuple[bool, str]:
    cleaned = URL_RE.sub("", text or "")
    cleaned = " ".join(cleaned.split()).strip().lower()
    return bool(cleaned and WORD_RE.search(cleaned)), cleaned


async def process_chat_activity(message, is_media=False):
    """Awards the approved chat loop and returns a human-readable event summary."""
    now = int(time.time())
    chat_id = message.chat.id
    user = message.from_user
    raw = message.caption or "" if is_media else message.text or ""
    ok, cleaned = meaningful_text(raw)
    if is_media:
        ok = True
        fingerprint_source = f"media:{getattr(message.photo[-1], 'file_unique_id', '') if message.photo else getattr(message.video, 'file_unique_id', '')}"
    else:
        fingerprint_source = cleaned
    if not ok:
        return {"user_xp": 0, "reply_author": None, "wave_users": [], "revived": False}

    fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()
    await get_user(user.id, user.username, user.full_name)

    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        duplicate = await (await db.execute('''SELECT 1 FROM content_fingerprints
            WHERE chat_id=? AND user_id=? AND fingerprint=? AND last_ts>?''',
            (chat_id, user.id, fingerprint, now - 86400))).fetchone()
        if duplicate:
            await db.rollback()
            return {"user_xp": 0, "reply_author": None, "wave_users": [], "revived": False}

        state = await (await db.execute(
            "SELECT * FROM chat_activity_state WHERE chat_id=?", (chat_id,))).fetchone()
        last_user = int(state["last_user_id"]) if state and state["last_user_id"] is not None else None
        last_activity = int(state["last_activity_ts"]) if state else 0
        wave_start = int(state["wave_started_ts"]) if state else now
        wave_last = int(state["wave_last_ts"]) if state else 0
        wave_active = bool(state["wave_active"]) if state else False
        if not wave_last or now - wave_last >= 900:
            wave_start = now
            wave_active = False

        amount = 0
        if last_user != user.id:
            amount += 5
        parent_author = None
        if message.reply_to_message and message.reply_to_message.from_user and not message.reply_to_message.from_user.is_bot:
            parent_author = message.reply_to_message.from_user.id
            if parent_author != user.id:
                amount += 5
        if is_media:
            amount += 15
        elif len(cleaned) > 50:
            amount += 10
        revived = bool(last_activity and now - last_activity > 3600)
        if revived:
            amount += 50

        await db.execute('''INSERT OR REPLACE INTO chat_activity_state
            (chat_id,last_user_id,last_activity_ts,wave_started_ts,wave_last_ts,wave_active) VALUES(?,?,?,?,?,?)''',
            (chat_id, user.id, now, wave_start, now, int(wave_active)))
        await db.execute('''INSERT INTO content_fingerprints(chat_id,user_id,fingerprint,last_ts)
            VALUES(?,?,?,?) ON CONFLICT(chat_id,user_id,fingerprint) DO UPDATE SET last_ts=excluded.last_ts''',
            (chat_id, user.id, fingerprint, now))
        await db.execute('''INSERT OR IGNORE INTO rewarded_messages(chat_id,message_id,author_id,created_ts)
            VALUES(?,?,?,?)''', (chat_id, message.message_id, user.id, now))
        await db.execute('''INSERT INTO chat_wave_members(chat_id,wave_started_ts,user_id,awarded,joined_at)
            VALUES(?,?,?,0,?) ON CONFLICT(chat_id,wave_started_ts,user_id)
            DO UPDATE SET joined_at=CASE WHEN awarded=0 THEN excluded.joined_at ELSE joined_at END''',
            (chat_id, wave_start, user.id, now))

        reply_author = None
        if message.reply_to_message and parent_author and parent_author != user.id:
            tracked = await (await db.execute('''SELECT author_id FROM rewarded_messages
                WHERE chat_id=? AND message_id=?''', (chat_id, message.reply_to_message.message_id))).fetchone()
            if tracked:
                inserted = await db.execute('''INSERT OR IGNORE INTO reply_rewards
                    (chat_id,parent_message_id,responder_id) VALUES(?,?,?)''',
                    (chat_id, message.reply_to_message.message_id, user.id))
                if inserted.rowcount:
                    reply_author = int(tracked[0])

        members = await (await db.execute('''SELECT user_id,awarded,joined_at FROM chat_wave_members
            WHERE chat_id=? AND wave_started_ts=?''', (chat_id, wave_start))).fetchall()
        wave_users = []
        recent_members = [r for r in members if int(r[2]) >= now - 1200]
        if not wave_active and len(recent_members) >= 4:
            wave_active = True
            await db.execute("UPDATE chat_activity_state SET wave_active=1 WHERE chat_id=?", (chat_id,))
            await db.execute('''UPDATE chat_wave_members SET awarded=1 WHERE chat_id=?
                AND wave_started_ts=? AND joined_at<?''', (chat_id, wave_start, now - 1200))
        if wave_active:
            eligible_members = members if state and bool(state["wave_active"]) else recent_members
            wave_users = [int(r[0]) for r in eligible_members if not int(r[1])]
            if wave_users:
                marks = ",".join("?" for _ in wave_users)
                await db.execute(f'''UPDATE chat_wave_members SET awarded=1 WHERE chat_id=?
                    AND wave_started_ts=? AND user_id IN ({marks})''', (chat_id, wave_start, *wave_users))
        await db.execute("DELETE FROM rewarded_messages WHERE created_ts<?", (now - 172800,))
        await db.execute("DELETE FROM content_fingerprints WHERE last_ts<?", (now - 172800,))
        await db.commit()

    if amount:
        await update_xp(user.id, amount, count_monthly=True)
    if reply_author:
        await update_xp(reply_author, 5, count_monthly=True)
    for uid in wave_users:
        await update_xp(uid, 25, count_monthly=True)
    return {"user_xp": amount, "reply_author": reply_author, "wave_users": wave_users, "revived": revived}
