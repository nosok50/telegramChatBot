"""Check positive entry permission before any command, XP, tag or activity handler."""
import logging

from aiogram import BaseMiddleware, types
from aiogram.enums import ContentType

from database import get_chat_verification_access, get_human_verification
from utils import answer_temp


log = logging.getLogger("human_verification")


class HumanVerificationMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if not isinstance(event, types.Message) or event.chat.type not in {"group", "supergroup"}:
            return await handler(event, data)
        from modules.user import begin_human_verification, _captcha_lock

        if event.new_chat_members:
            # Run before router selection: another router must not swallow a join.
            for member in event.new_chat_members:
                await begin_human_verification(
                    event.bot, event.chat, member, int(event.date.timestamp()), source="service_join",
                )
            return await handler(event, data)
        if event.content_type == ContentType.LEFT_CHAT_MEMBER:
            return await handler(event, data)
        if not event.from_user or event.from_user.is_bot or event.sender_chat:
            return await handler(event, data)

        from modules.moderation import is_admin
        user_id, chat_id = event.from_user.id, event.chat.id
        if await is_admin(event.chat, user_id, event.sender_chat, required_level=1):
            return await handler(event, data)

        async with _captcha_lock(chat_id, user_id):
            pending = await get_human_verification(chat_id, user_id)
            access = await get_chat_verification_access(chat_id, user_id)
            if not pending and access:
                allowed = True
            else:
                allowed = False
                if not pending:
                    try:
                        member = await event.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
                        status = getattr(member.status, "value", str(member.status))
                        # Non-member channel comments retain their existing policy.
                        allowed = status == "left" or (
                            status == "restricted" and not member.is_member
                        )
                    except Exception:
                        log.exception("membership_lookup_failed chat=%s user=%s", chat_id, user_id)

            if not allowed:
                try:
                    await event.delete()
                except Exception:
                    log.exception("unverified_message_delete_failed chat=%s user=%s", chat_id, user_id)
                await begin_human_verification(event.bot, event.chat, event.from_user, _lock_held=True)
                await answer_temp(
                    event,
                    "🧩 Сначала введите код с картинки в сообщении проверки.",
                    key=f"human_verification:{user_id}",
                )
                log.info("unverified_message_blocked chat=%s user=%s message=%s", chat_id, user_id, event.message_id)
                return None
        return await handler(event, data)
