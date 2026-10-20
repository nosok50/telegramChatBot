import os
import asyncio
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher, Router, types

import database
from modules import user
from verification_gate import HumanVerificationMiddleware


class VerificationGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "gate.db")
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript('''
                CREATE TABLE human_verifications (
                    chat_id INTEGER, user_id INTEGER, welcome_message_id INTEGER,
                    joined_at INTEGER, challenge_token TEXT, challenge_code TEXT,
                    entered_code TEXT, attempts INTEGER, expires_at INTEGER,
                    PRIMARY KEY(chat_id,user_id));
                CREATE TABLE chat_verification_access (
                    chat_id INTEGER, user_id INTEGER, verified_at INTEGER,
                    joined_at INTEGER, source TEXT, PRIMARY KEY(chat_id,user_id));
                CREATE TABLE chat_user_activity (
                    chat_id INTEGER, user_id INTEGER, message_count INTEGER,
                    first_message_at INTEGER, last_message_at INTEGER);
                CREATE TABLE data_migrations (migration_key TEXT PRIMARY KEY, applied_at INTEGER);
            ''')
        self.patches = [
            patch.object(database, "DB_NAME", self.path),
            patch.object(database, "_mark_dirty", AsyncMock()),
            patch("modules.moderation.is_admin", AsyncMock(return_value=False)),
            patch("verification_gate.answer_temp", AsyncMock()),
        ]
        for active_patch in self.patches:
            active_patch.start()
        self.bot = Bot("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
        self.bot.session.make_request = AsyncMock(return_value=True)
        self.bot.get_chat_member = AsyncMock(return_value=types.ChatMemberMember(
            user=types.User(id=42, first_name="New", is_bot=False),
        ))
        self.bot.send_photo = AsyncMock(return_value=SimpleNamespace(message_id=900))
        self.bot.delete_message = AsyncMock(return_value=True)
        self.bot.edit_message_caption = AsyncMock(return_value=True)
        self.bot.ban_chat_member = AsyncMock(return_value=True)
        self.bot.unban_chat_member = AsyncMock(return_value=True)
        self.chat = types.Chat(id=-1001, type="supergroup", title="Test")
        self.member = types.User(id=42, first_name="New", is_bot=False)

    async def asyncTearDown(self):
        for key in list(user._captcha_timeout_tasks):
            user._cancel_captcha_timeout(*key)
        user._captcha_locks.clear()
        await self.bot.session.close()
        for active_patch in reversed(self.patches):
            active_patch.stop()
        self.tmp.cleanup()

    def message(self, text="Привет", message_id=10):
        return types.Message(
            message_id=message_id, date=datetime.now(timezone.utc),
            chat=self.chat, from_user=self.member, text=text,
        ).as_(self.bot)

    async def captcha_callback(self, action):
        pending = await database.get_human_verification(-1001,42)
        return SimpleNamespace(
            data=f'captcha:{pending["challenge_token"]}:{action}',
            message=SimpleNamespace(chat=self.chat, message_id=900),
            from_user=self.member,
            bot=self.bot,
            answer=AsyncMock(),
        )

    async def test_fast_buttons_are_recorded_while_telegram_ack_is_still_waiting(self):
        await user.begin_human_verification(self.bot, self.chat, self.member)
        release_network = asyncio.Event()
        callbacks = [await self.captcha_callback(f"digit:{digit}") for digit in "24682"]
        for callback in callbacks:
            async def delayed_ack():
                await release_network.wait()
            callback.answer.side_effect = delayed_ack

        tasks = [asyncio.create_task(user.confirm_human(callback)) for callback in callbacks]
        try:
            async def wait_for_all_digits():
                while (await database.get_human_verification(-1001,42))["entered_code"] != "24682":
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(wait_for_all_digits(), timeout=3)
            self.assertTrue(all(not task.done() for task in tasks))
            self.assertEqual((await database.get_human_verification(-1001,42))["entered_code"], "24682")
        finally:
            release_network.set()
            await asyncio.gather(*tasks)
        view_task = user._captcha_view_tasks.get((-1001,42))
        if view_task:
            await view_task
        caption = self.bot.edit_message_caption.await_args.kwargs["caption"]
        self.assertIn("2 4 6 8 2", caption)
        self.bot.edit_message_caption.assert_awaited_once()
        for callback in callbacks:
            callback.answer.assert_awaited_once_with()

    async def test_slow_screen_update_does_not_block_typing_and_latest_input_wins(self):
        await user.begin_human_verification(self.bot, self.chat, self.member)
        editing = asyncio.Event()
        release_edit = asyncio.Event()
        async def delayed_edit(**kwargs):
            editing.set()
            await release_edit.wait()
            return True
        self.bot.edit_message_caption.side_effect = delayed_edit
        await user.confirm_human(await self.captcha_callback("digit:2"))
        await asyncio.wait_for(editing.wait(), timeout=3)
        callbacks = [await self.captcha_callback(f"digit:{digit}") for digit in "2288"]
        try:
            await asyncio.wait_for(asyncio.gather(*[
                user.confirm_human(callback) for callback in callbacks
            ]), timeout=3)
            self.assertEqual((await database.get_human_verification(-1001,42))["entered_code"], "22288")
        finally:
            release_edit.set()
        await user._captcha_view_tasks[(-1001,42)]
        self.assertIn("2 2 2 8 8", self.bot.edit_message_caption.await_args.kwargs["caption"])

    async def test_rapid_backspace_clear_and_new_digits_keep_exact_input(self):
        await user.begin_human_verification(self.bot, self.chat, self.member)
        actions = ("digit:2", "digit:4", "digit:6", "back", "digit:8", "clear", "digit:9", "digit:3")
        callbacks = [await self.captcha_callback(action) for action in actions]
        await asyncio.gather(*[user.confirm_human(callback) for callback in callbacks])
        self.assertEqual((await database.get_human_verification(-1001,42))["entered_code"], "93")
        view_task = user._captcha_view_tasks.get((-1001,42))
        if view_task:
            await view_task
        self.assertIn("9 3 □ □ □", self.bot.edit_message_caption.await_args.kwargs["caption"])

    async def test_submit_queued_immediately_after_digits_uses_latest_code(self):
        await user.begin_human_verification(self.bot, self.chat, self.member)
        pending = await database.get_human_verification(-1001,42)
        callbacks = [await self.captcha_callback(f"digit:{digit}") for digit in pending["challenge_code"]]
        callbacks.append(await self.captcha_callback("submit"))
        with (
            patch("modules.user.answer_temp", AsyncMock()),
            patch("modules.user.delete_temp_by_scope", AsyncMock()),
        ):
            await asyncio.gather(*[user.confirm_human(callback) for callback in callbacks])
        self.assertIsNone(await database.get_human_verification(-1001,42))
        self.assertIsNotNone(await database.get_chat_verification_access(-1001,42))
        self.assertNotIn((-1001,42), user._captcha_view_tasks)

    async def test_wrong_code_error_stays_visible_in_captcha(self):
        await user.begin_human_verification(self.bot, self.chat, self.member)
        pending = await database.get_human_verification(-1001,42)
        await database.update_human_verification_input(-1001,42,pending["challenge_token"],"00000")
        callback = await self.captcha_callback("submit")
        await user.confirm_human(callback)
        await user._captcha_view_tasks[(-1001,42)]
        updated = await database.get_human_verification(-1001,42)
        self.assertEqual(updated["attempts"], 1)
        self.assertEqual(updated["entered_code"], "")
        caption = self.bot.edit_message_caption.await_args.kwargs["caption"]
        self.assertNotIn("Неверный код", caption)
        self.assertIn("□ □ □ □ □", caption)
        self.assertIn("Осталось попыток: <b>2</b>", caption)
        callback.answer.assert_awaited_once_with(
            text="Неверный код. Осталось попыток: 2.", show_alert=True,
        )

    async def solve(self):
        pending = await database.get_human_verification(-1001, 42)
        await database.update_human_verification_input(
            -1001, 42, pending["challenge_token"], pending["challenge_code"],
        )
        return await database.complete_human_verification(
            -1001, 42, challenge_token=pending["challenge_token"],
        )

    async def test_missing_join_event_cannot_reach_any_command_or_activity(self):
        handler = AsyncMock()
        for index, text in enumerate(("/profile", "Реклама", "Фото")):
            await HumanVerificationMiddleware()(handler, self.message(text, index+10), {})
        handler.assert_not_awaited()
        self.assertEqual(self.bot.session.make_request.await_count, 3)
        self.bot.send_photo.assert_awaited_once()
        self.assertIsNotNone(await database.get_human_verification(-1001, 42))
        self.assertIsNone(await database.get_chat_verification_access(-1001, 42))

    async def test_captcha_is_required_after_process_restart_until_explicit_success(self):
        await HumanVerificationMiddleware()(AsyncMock(), self.message(), {})
        handler = AsyncMock()
        user._cancel_captcha_timeout(-1001, 42)
        await HumanVerificationMiddleware()(handler, self.message(message_id=11), {})
        handler.assert_not_awaited()
        self.assertEqual((await self.solve())[0], True)
        await HumanVerificationMiddleware()(handler, self.message(message_id=12), {})
        handler.assert_awaited_once()
        self.assertEqual((await database.get_chat_verification_access(-1001,42))["source"], "captcha")

    async def test_both_join_events_create_one_captcha(self):
        timestamp = int(time.time())
        await user.begin_human_verification(self.bot, self.chat, self.member, timestamp, source="service_join")
        event = SimpleNamespace(
            old_chat_member=SimpleNamespace(status="left"),
            new_chat_member=SimpleNamespace(status="member", user=self.member),
            date=datetime.fromtimestamp(timestamp, timezone.utc), bot=self.bot, chat=self.chat,
        )
        await user.on_membership_change(event)
        self.bot.send_photo.assert_awaited_once()
        await self.solve()
        await user.on_membership_change(event)
        self.bot.send_photo.assert_awaited_once()

    async def test_tag_update_is_not_a_join(self):
        event = SimpleNamespace(
            old_chat_member=SimpleNamespace(status="member"),
            new_chat_member=SimpleNamespace(status="member", user=self.member),
            date=datetime.now(timezone.utc), bot=self.bot, chat=self.chat,
        )
        await user.on_membership_change(event)
        self.bot.send_photo.assert_not_awaited()

    async def test_send_failure_keeps_user_blocked_and_retries(self):
        self.bot.send_photo.side_effect = RuntimeError("Telegram unavailable")
        handler = AsyncMock()
        with self.assertLogs("human_verification", level="ERROR"):
            await HumanVerificationMiddleware()(handler, self.message(), {})
        handler.assert_not_awaited()
        self.assertIsNotNone(await database.get_human_verification(-1001,42))
        self.bot.send_photo.side_effect = None
        await HumanVerificationMiddleware()(handler, self.message(message_id=11), {})
        self.assertEqual(self.bot.send_photo.await_count, 2)
        handler.assert_not_awaited()

    async def test_cleanup_does_not_mean_success_and_stale_token_cannot_pass(self):
        await user.begin_human_verification(self.bot, self.chat, self.member)
        pending = await database.get_human_verification(-1001,42)
        await database.update_human_verification_input(-1001,42,pending["challenge_token"],pending["challenge_code"])
        self.assertFalse((await database.complete_human_verification(-1001,42,challenge_token="old-token"))[0])
        await database.complete_human_verification(-1001,42)
        self.assertIsNone(await database.get_chat_verification_access(-1001,42))
        handler = AsyncMock()
        await HumanVerificationMiddleware()(handler, self.message(), {})
        handler.assert_not_awaited()

    async def test_global_gate_precedes_an_earlier_router(self):
        dp = Dispatcher()
        dp.message.outer_middleware(HumanVerificationMiddleware())
        early = Router()
        command_handler = AsyncMock()
        async def handle_command(message):
            await command_handler(message)
        early.message.register(handle_command)
        dp.include_router(early)
        await dp.feed_update(self.bot, types.Update(update_id=1, message=self.message("/games")))
        command_handler.assert_not_awaited()
        self.bot.send_photo.assert_awaited_once()
        await self.solve()
        await dp.feed_update(self.bot, types.Update(update_id=2, message=self.message("Привет", 11)))
        command_handler.assert_awaited_once()

    async def test_lookup_failure_never_grants_unknown_member_access(self):
        self.bot.get_chat_member.side_effect = RuntimeError("Telegram temporarily unavailable")
        handler = AsyncMock()
        with self.assertLogs("human_verification", level="ERROR"):
            await HumanVerificationMiddleware()(handler, self.message(), {})
        handler.assert_not_awaited()
        self.bot.send_photo.assert_awaited_once()

    async def test_concurrent_entry_signals_do_not_replace_captcha(self):
        await asyncio.gather(*[
            user.begin_human_verification(self.bot, self.chat, self.member)
            for _ in range(4)
        ])
        self.bot.send_photo.assert_awaited_once()

    async def test_expired_or_exhausted_challenge_cannot_create_access(self):
        for exhausted, expired in ((True, False), (False, True)):
            await user.begin_human_verification(self.bot, self.chat, self.member)
            pending = await database.get_human_verification(-1001,42)
            await database.update_human_verification_input(-1001,42,pending["challenge_token"],pending["challenge_code"])
            with closing(sqlite3.connect(self.path)) as db:
                db.execute(
                    'UPDATE human_verifications SET attempts=?, expires_at=?',
                    (3 if exhausted else 0, 1 if expired else int(time.time())+180),
                )
                db.commit()
            self.assertFalse((await database.complete_human_verification(
                -1001,42,challenge_token=pending["challenge_token"],
            ))[0])
            self.assertIsNone(await database.get_chat_verification_access(-1001,42))
            await database.complete_human_verification(-1001,42)

    async def test_migration_keeps_only_established_preexisting_speakers(self):
        now = int(time.time())
        with closing(sqlite3.connect(self.path)) as db:
            db.executemany('INSERT INTO chat_user_activity VALUES (?,?,?,?,?)', [
                (-1001,42,1,now-99999,now), (-1001,43,30,now-99999,now),
            ])
            db.commit()
        await database.migrate_existing_chat_access()
        self.assertIsNone(await database.get_chat_verification_access(-1001,42))
        self.assertIsNotNone(await database.get_chat_verification_access(-1001,43))
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('INSERT INTO chat_user_activity VALUES (?,?,?,?,?)', (-1001,44,30,now-99999,now))
            db.commit()
        await database.migrate_existing_chat_access()
        self.assertIsNone(await database.get_chat_verification_access(-1001,44))
