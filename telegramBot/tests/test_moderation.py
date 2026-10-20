import unittest
import io
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
from modules.user import (
    CAPTCHA_LENGTH,
    build_captcha_png,
    can_view_foreign_profile,
    confirm_human,
)
from PIL import Image
import utils


class ModerationTests(unittest.IsolatedAsyncioTestCase):
    def test_sending_media_in_private_is_not_advertising_at_any_level(self):
        messages = (
            "Nosok, я в лс скину видео или сюда лучше ? Но это вечером уже",
            "Пишите в личку, каждому скину видео",
            "В лс скину фото",
        )
        for level in (1, 2, 3, 4, 5):
            for text in messages:
                with self.subTest(level=level, text=text):
                    result = AutoModerationTracker().analyze(
                        -1001, 42, 1, text, level, [], [],
                    )
                    self.assertEqual(result["action"], "allow")
                    self.assertLess(_advertising_score(text, compact=True), 4)

    def test_level_three_screenshot_conversation_is_allowed_in_both_orders(self):
        messages = [
            "Nosok, я в лс скину видео или сюда лучше ? Но это вечером уже",
            "Уже октябрь, если не ошибаюсь, 20 ноября день рождения ? Или 30-го, не помню😅",
            "Но схема расширения карты ясна, теперь фиг карту выбиришь под свой вкус😅 Кстати, это мой 181 город, когда начал переустановил игру в том году в сентябре",
            "У меня банк переполнен, монетки не могу собрать, они не пропадают... В чём-то +, в чём-то - этого, мешает обзору😅",
            "Столько информации, столько нового, что не описать словами... Как будто я попал в будущее, хаха",
            "Nosok, с этой обновление у меня голова кругом...",
        ]
        for sequence in (messages, list(reversed(messages))):
            tracker = AutoModerationTracker()
            with patch("modules.moderation.time.time") as clock:
                for index, text in enumerate(sequence):
                    clock.return_value = 2_000_000_000 + index * 2
                    result = tracker.analyze(-1001, 42, index + 1, text, 3, [], [])
                    self.assertEqual(result["action"], "allow", text)

    def test_level_three_needs_stronger_ad_evidence_than_level_one(self):
        text = "Работа курьером, ищем людей"
        for level, expected in ((1, "advertising"), (3, "allow")):
            result = AutoModerationTracker().analyze(
                -1001, 42, 1, text, level, [], [],
            )
            self.assertEqual(result["action"], expected)
        result = AutoModerationTracker().analyze(
            -1001, 42, 1, "Давайте обсудим обновление", 3, [], [],
            strict_newcomer=True,
        )
        self.assertEqual(result["action"], "allow")

    def test_clear_ads_are_still_caught_for_new_and_established_players(self):
        for level in (1, 3):
            for text in (
                "РАЗДАЮ КАЖДОМУ ПО 500 РУБЛЕЙ, КИДАЙТЕ + В ЛС",
                "Первым 10 человек кто кинет + В ЛС я скину по 750 рублей",
                "Уборка на даче, 5200 в день пишите",
            ):
                with self.subTest(level=level, text=text):
                    result = AutoModerationTracker().analyze(-1001, 42, 1, text, level, [], [])
                    self.assertEqual(result["action"], "advertising")

    def test_newcomer_split_ad_and_money_offer_are_still_caught(self):
        tracker = AutoModerationTracker()
        tracker.analyze(-1001, 42, 1, "Каждому скину 750 рублей", 1, [], [])
        result = tracker.analyze(-1001, 42, 2, "Пишите в лс", 1, [], [])
        self.assertEqual(result["action"], "advertising")
        result = AutoModerationTracker().analyze(
            -1001, 42, 1, "Дам денег, пишите в лс", 1, [], [],
        )
        self.assertEqual(result["action"], "advertising")

    def test_native_captcha_is_a_real_png_with_expected_size(self):
        payload = build_captcha_png("24682")
        image = Image.open(io.BytesIO(payload))
        self.assertEqual(image.format, "PNG")
        self.assertEqual(image.size, (420, 150))

    async def test_captcha_cannot_be_completed_for_another_user(self):
        callback = SimpleNamespace(
            data="captcha:private-token:submit",
            from_user=SimpleNamespace(id=99),
            message=SimpleNamespace(chat=SimpleNamespace(id=-1001)),
            answer=AsyncMock(),
        )
        with patch("modules.user.get_human_verification", AsyncMock(return_value=None)):
            await confirm_human(callback)
        callback.answer.assert_awaited_once_with(
            text="Эта проверка предназначена другому пользователю.",
            show_alert=True,
        )

    async def test_correct_captcha_completes_verification(self):
        callback = SimpleNamespace(
            data="captcha:private-token:submit",
            from_user=SimpleNamespace(id=42, mention_html=lambda: '<a href="tg://user?id=42">New</a>'),
            message=SimpleNamespace(
                chat=SimpleNamespace(id=-1001),
                message_id=777,
                delete=AsyncMock(),
            ),
            bot=SimpleNamespace(delete_message=AsyncMock()),
            answer=AsyncMock(),
        )
        pending = {
            "challenge_token": "private-token",
            "challenge_code": "24682",
            "entered_code": "24682",
            "expires_at": 9999999999,
            "welcome_message_id": 777,
        }
        with (
            patch("modules.user.get_human_verification", AsyncMock(return_value=pending)),
            patch("modules.user.complete_human_verification", AsyncMock(return_value=(True, 777))) as complete,
            patch("modules.user.delete_temp_by_scope", AsyncMock()),
            patch("modules.user.answer_temp", AsyncMock()) as answer_temp,
        ):
            await confirm_human(callback)

        complete.assert_awaited_once_with(-1001, 42, challenge_token="private-token")
        callback.bot.delete_message.assert_awaited_once_with(-1001, 777)
        answer_temp.assert_awaited_once()
        self.assertIn("Здесь можно обсудить посты с канала", answer_temp.await_args.args[1])
        self.assertEqual(
            answer_temp.await_args.kwargs["photo"],
            "https://i.ibb.co/Q3GG72fN/Frame-26085986.png",
        )
        self.assertEqual(answer_temp.await_args.kwargs["delay"], 120)
        callback.answer.assert_awaited_once_with()

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
