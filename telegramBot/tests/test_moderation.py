import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram import types
from aiogram.enums import MessageEntityType

from modules.moderation import (
    AutoModerationTracker,
    FloodMiddleware,
    _advertising_score,
    _is_short_loan_bait,
    _message_content_for_analysis,
)
from modules.user import can_view_foreign_profile
import utils


class ModerationTests(unittest.IsolatedAsyncioTestCase):
    def test_photo_without_caption_is_not_treated_as_a_link(self):
        message = SimpleNamespace(
            text=None,
            caption=None,
            content_type="ContentType.PHOTO",
            entities=None,
            caption_entities=None,
        )
        content = _message_content_for_analysis(message)
        self.assertEqual(content, "")
        self.assertEqual(_advertising_score(content), 0)

    async def test_human_verification_reminder_can_be_deleted_before_timer(self):
        chat_id = -1001
        message_id = 99
        scope_key = "custom:-1001:human_verification:42"
        bot = SimpleNamespace(delete_message=AsyncMock())
        source = SimpleNamespace(chat=SimpleNamespace(id=chat_id), from_user=None, bot=bot)
        utils._active_temp_messages[scope_key] = message_id
        utils._message_meta[f"{chat_id}:{message_id}"] = {
            "scope_key": scope_key,
            "ttl": 60,
        }

        deleted = await utils.delete_temp_by_scope(
            source,
            key="human_verification:42",
        )

        self.assertTrue(deleted)
        bot.delete_message.assert_awaited_once_with(chat_id=chat_id, message_id=message_id)
        self.assertNotIn(scope_key, utils._active_temp_messages)
        self.assertNotIn(f"{chat_id}:{message_id}", utils._message_meta)

    def test_moderator_rank_two_can_view_foreign_profiles(self):
        self.assertTrue(can_view_foreign_profile(game_level=1, moderator_level=2))
        self.assertTrue(can_view_foreign_profile(game_level=4, moderator_level=0))
        self.assertFalse(can_view_foreign_profile(game_level=3, moderator_level=1))

    def test_loan_bait_with_immediate_modifier_is_caught(self):
        self.assertTrue(_is_short_loan_bait("Дам в долг сейчас"))
        self.assertFalse(_is_short_loan_bait("Я дам другу в долг сейчас в игре"))

        decision = AutoModerationTracker().analyze(
            -1001,
            42,
            1,
            "Дам в долг сейчас",
            1,
            [],
            [],
            user_xp=0,
            strict_newcomer=True,
        )
        self.assertEqual(decision["action"], "advertising")
        self.assertTrue(decision["kick_newcomer"])

    def test_hidden_crypto_link_is_included_and_caught(self):
        message = SimpleNamespace(
            text="Чек на 50 USDT",
            caption=None,
            content_type="text",
            entities=[SimpleNamespace(
                type=MessageEntityType.TEXT_LINK,
                url="https://spam.example/claim",
            )],
            caption_entities=None,
        )
        content = _message_content_for_analysis(message)
        self.assertIn("https://spam.example/claim", content)
        self.assertGreaterEqual(_advertising_score(content), 4)

    async def test_unverified_message_never_reaches_activity_handlers(self):
        event = types.Message(
            message_id=10,
            date=datetime.now(timezone.utc),
            chat=types.Chat(id=-1001, type="supergroup", title="Test"),
            from_user=types.User(id=42, is_bot=False, first_name="New"),
            text="обычное сообщение",
        )
        handler = AsyncMock()

        with (
            patch("modules.moderation.is_human_verification_pending", AsyncMock(return_value=True)),
            patch("modules.moderation.answer_temp", AsyncMock()) as answer_temp,
        ):
            await FloodMiddleware()(handler, event, {})

        handler.assert_not_awaited()
        answer_temp.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
