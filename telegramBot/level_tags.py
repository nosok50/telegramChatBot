import asyncio
import logging
import time

import aiosqlite
from aiogram.exceptions import TelegramAPIError

from database import DB_NAME, _mark_dirty, get_user


TAG_RETRY_SECONDS = 6 * 60 * 60

_bot = None
_processor_task = None
_wake_event = None
_pending_users = set()
_chat_rights_cache = {}
_tag_state_cache = {}


def queue_level_tag_refresh(user_id: int):
    """Queue an event-driven refresh; no Telegram request is made if the level is unchanged."""
    user_id = int(user_id)
    _pending_users.add(user_id)
    for key in [key for key in _tag_state_cache if key[1] == user_id]:
        _tag_state_cache.pop(key, None)
    if _wake_event is not None:
        _wake_event.set()


async def _store_tag_state(chat_id, user_id, applied_level, retry_after=0, last_error=""):
    key = (int(chat_id), int(user_id))
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """INSERT INTO chat_level_tags(chat_id,user_id,applied_level,retry_after,last_error)
               VALUES(?,?,?,?,?)
               ON CONFLICT(chat_id,user_id) DO UPDATE SET
                   applied_level=excluded.applied_level,
                   retry_after=excluded.retry_after,
                   last_error=excluded.last_error""",
            (int(chat_id), int(user_id), int(applied_level), int(retry_after), str(last_error)[:300]),
        )
        await db.commit()
    _tag_state_cache[key] = (int(applied_level), int(retry_after))
    await _mark_dirty("chat_level_tags")


async def _register_chat_user(chat_id: int, user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            """INSERT OR IGNORE INTO chat_level_tags
               (chat_id,user_id,applied_level,retry_after,last_error)
               VALUES(?,?,0,0,'')""",
            (int(chat_id), int(user_id)),
        )
        await db.commit()
        inserted = cur.rowcount == 1
    if inserted:
        await _mark_dirty("chat_level_tags")


async def _bot_can_manage_tags(bot, chat_id: int) -> bool:
    now = int(time.time())
    cached = _chat_rights_cache.get(int(chat_id))
    if cached and cached[1] > now:
        return cached[0]
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=bot.id)
        allowed = (
            str(member.status) in ("administrator", "creator", "ChatMemberStatus.ADMINISTRATOR", "ChatMemberStatus.CREATOR")
            and bool(getattr(member, "can_manage_tags", False))
        )
    except Exception:
        allowed = False
    _chat_rights_cache[int(chat_id)] = (allowed, now + TAG_RETRY_SECONDS)
    return allowed


async def _is_chat_administrator(bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        status = str(member.status)
        return status in (
            "administrator",
            "creator",
            "ChatMemberStatus.ADMINISTRATOR",
            "ChatMemberStatus.CREATOR",
        )
    except Exception:
        return False


async def _apply_tag(bot, chat_id: int, user_id: int, desired_level: int,
                     applied_level: int, retry_after: int, *, force_refresh=False):
    desired_level = max(1, min(5, int(desired_level)))
    if int(applied_level or 0) == desired_level and not force_refresh:
        return True
    now = int(time.time())
    if int(retry_after or 0) > now:
        return False
    if not await _bot_can_manage_tags(bot, chat_id):
        await _store_tag_state(
            chat_id, user_id, applied_level or 0,
            retry_after=now + TAG_RETRY_SECONDS,
            last_error="У бота нет права can_manage_tags",
        )
        return False

    try:
        if force_refresh:
            # Re-applying the same value can return True without producing a
            # participant update. Clearing first forces Telegram clients to
            # receive a real tag change instead of keeping a stale cached tag.
            await bot.set_chat_member_tag(
                chat_id=chat_id,
                user_id=user_id,
                tag="",
            )
        await bot.set_chat_member_tag(
            chat_id=chat_id,
            user_id=user_id,
            tag=f"Уровень {desired_level}",
        )
        if force_refresh:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            actual_tag = getattr(member, "tag", None)
            if actual_tag != f"Уровень {desired_level}":
                raise RuntimeError(
                    f"Telegram вернул тег {actual_tag!r} вместо 'Уровень {desired_level}'"
                )
    except TelegramAPIError as exc:
        # Member tags are for regular members. Existing administrator titles
        # are deliberately left untouched.
        if await _is_chat_administrator(bot, chat_id, user_id):
            await _store_tag_state(
                chat_id, user_id, desired_level,
                retry_after=0,
                last_error="Администратор: игровой тег не заменяет админское звание",
            )
            return True
        await _store_tag_state(
            chat_id, user_id, applied_level or 0,
            retry_after=now + TAG_RETRY_SECONDS,
            last_error=str(exc),
        )
        return False
    except Exception as exc:
        await _store_tag_state(
            chat_id, user_id, applied_level or 0,
            retry_after=now + TAG_RETRY_SECONDS,
            last_error=str(exc),
        )
        return False

    await _store_tag_state(chat_id, user_id, desired_level)
    return True


async def ensure_level_tag(message, level=None):
    """Initial lazy assignment for an active regular member."""
    if not message.from_user or message.from_user.is_bot or message.chat.type == "private":
        return False
    user_id = int(message.from_user.id)
    chat_id = int(message.chat.id)
    key = (chat_id, user_id)
    cached = _tag_state_cache.get(key)
    if cached:
        applied_level, retry_after = cached
        if applied_level > 0:
            return True
        if retry_after > int(time.time()):
            return False
    if level is None:
        data = await get_user(user_id, message.from_user.username, message.from_user.full_name)
        level = data[4] if data else 1
    await _register_chat_user(chat_id, user_id)
    async with aiosqlite.connect(DB_NAME) as db:
        row = await (await db.execute(
            """SELECT applied_level,retry_after FROM chat_level_tags
               WHERE chat_id=? AND user_id=?""",
            (chat_id, user_id),
        )).fetchone()
    _tag_state_cache[key] = (int(row[0]), int(row[1]))
    return await _apply_tag(_bot or message.bot, chat_id, user_id, level, row[0], row[1])


async def sync_level_tag(bot, chat_id: int, user_id: int, level=None, *, force=False):
    """Synchronously apply a tag after an explicit administrative XP change."""
    chat_id = int(chat_id)
    user_id = int(user_id)
    if chat_id > 0:
        return False
    if level is None:
        data = await get_user(user_id)
        level = data[4] if data else 1
    await _register_chat_user(chat_id, user_id)
    async with aiosqlite.connect(DB_NAME) as db:
        row = await (await db.execute(
            """SELECT applied_level,retry_after FROM chat_level_tags
               WHERE chat_id=? AND user_id=?""",
            (chat_id, user_id),
        )).fetchone()
    _tag_state_cache.pop((chat_id, user_id), None)
    applied_level = int(row[0])
    retry_after = 0 if force else int(row[1])
    return await _apply_tag(
        bot,
        chat_id,
        user_id,
        int(level),
        applied_level,
        retry_after,
        force_refresh=force,
    )


async def _refresh_user(bot, user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        rows = await (await db.execute(
            """SELECT t.chat_id,t.applied_level,t.retry_after,u.level
               FROM chat_level_tags t
               JOIN users u ON u.user_id=t.user_id
               WHERE t.user_id=? AND t.applied_level!=u.level""",
            (int(user_id),),
        )).fetchall()
    for chat_id, applied_level, retry_after, level in rows:
        await _apply_tag(bot, chat_id, user_id, level, applied_level, retry_after)


async def _processor(bot):
    while True:
        await _wake_event.wait()
        _wake_event.clear()
        while _pending_users:
            user_id = _pending_users.pop()
            try:
                await _refresh_user(bot, user_id)
            except Exception:
                logging.exception("Не удалось обновить тег уровня для user_id=%s", user_id)


def start_level_tag_processor(bot):
    global _bot, _processor_task, _wake_event
    _bot = bot
    if _wake_event is None:
        _wake_event = asyncio.Event()
    if not _processor_task or _processor_task.done():
        _processor_task = asyncio.create_task(_processor(bot))
    if _pending_users:
        _wake_event.set()
