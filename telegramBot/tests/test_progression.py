import os
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

import aiosqlite

import database
import engagement
import level_tags
from modules import admin_factory, factory_orders


class FakeSentMessage:
    def __init__(self, message_id):
        self.message_id = message_id
        self.deleted = False

    async def delete(self):
        self.deleted = True


class FakeFactoryBot:
    def __init__(self):
        self.next_message_id = 100
        self.sent = []
        self.deleted = []

    async def send_message(self, chat_id, text, reply_markup=None, **_kwargs):
        message = FakeSentMessage(self.next_message_id)
        self.next_message_id += 1
        self.sent.append((int(chat_id), text, reply_markup, message))
        return message

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((int(chat_id), int(message_id)))

    async def get_chat(self, chat_id):
        return SimpleNamespace(title=f"Group {chat_id}", full_name=None)


class FakeCallback:
    def __init__(self, bot, data, user_id, message_id=0):
        self.bot = bot
        self.data = data
        self.from_user = SimpleNamespace(
            id=user_id,
            username=f"u{user_id}",
            full_name=f"User {user_id}",
        )
        self.message = SimpleNamespace(message_id=message_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class ProgressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = os.path.join(self.tmp.name, "test.db")
        database.DB_NAME = self.db
        engagement.DB_NAME = self.db
        level_tags.DB_NAME = self.db
        factory_orders.DB_NAME = self.db
        admin_factory.DB_NAME = self.db
        level_tags._pending_users.clear()
        level_tags._chat_rights_cache.clear()
        level_tags._tag_state_cache.clear()
        level_tags._bot = None
        await database.create_tables()
        await engagement.create_engagement_tables()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def user(self, uid, xp=0, level=1):
        await database.get_user(uid, f"u{uid}", f"User {uid}")
        async with aiosqlite.connect(self.db) as db:
            await db.execute("UPDATE users SET xp=?,level=? WHERE user_id=?", (xp, level, uid)); await db.commit()

    async def order_status(self, order_id):
        async with aiosqlite.connect(self.db) as db:
            row = await (await db.execute(
                "SELECT status,distributed_xp FROM factory_orders WHERE id=?",
                (order_id,),
            )).fetchone()
        return row

    def test_factory_command_accepts_natural_russian_form(self):
        parsed = factory_orders.parse_factory_order_args(
            "обсуждения малый я(zeariar) нормальный модератор?"
        )
        self.assertEqual(
            parsed,
            ("discussion", "small", "я(zeariar) нормальный модератор?", None),
        )

    def test_factory_command_reports_exact_bad_argument(self):
        self.assertEqual(
            factory_orders.parse_factory_order_args("болтовня малый Нормальная тема")[3],
            "type",
        )
        self.assertEqual(
            factory_orders.parse_factory_order_args("обсуждение огромный Нормальная тема")[3],
            "size",
        )
        self.assertEqual(
            factory_orders.parse_factory_order_args("обсуждение малый нет")[3],
            "topic",
        )

    async def complete_farm(self, user_id, coins=60000):
        async with aiosqlite.connect(self.db) as db:
            await db.execute(
                """INSERT OR REPLACE INTO farm_players
                   (user_id,coins,opened_cells,created_at,updated_at)
                   VALUES(?,?,?,?,?)""",
                (user_id, coins, json.dumps(list(range(9))), 0, 0),
            )
            await db.executemany(
                """INSERT OR REPLACE INTO farm_cells
                   (user_id,cell_idx,module_type,level,spec,status)
                   VALUES(?,?,?,?,?,?)""",
                [(user_id, cell_idx, "generator", 3, "stable", "ok") for cell_idx in range(9)],
            )
            await db.commit()

    async def test_old_level_and_xp_are_preserved(self):
        await self.user(1, 12345, 5)
        row = await database.get_user(1)
        self.assertEqual((row[3], row[4]), (12345, 5))
        await database.update_xp(1, 1000)
        row = await database.get_user(1)
        self.assertEqual((row[3], row[4]), (13345, 5))

    async def test_legacy_level_fives_are_recalculated_once(self):
        legacy_tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        legacy_db = os.path.join(legacy_tmp.name, "legacy.db")
        conn = sqlite3.connect(legacy_db)
        conn.execute("""CREATE TABLE users (
            user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
            xp INTEGER DEFAULT 0, level INTEGER DEFAULT 1, warns INTEGER DEFAULT 0,
            mod_level INTEGER DEFAULT 0, reputation INTEGER DEFAULT 0,
            last_wipe_date TEXT, last_free_dice_ts INTEGER DEFAULT 0
        )""")
        conn.executemany(
            """INSERT INTO users(user_id,username,full_name,xp,level)
               VALUES(?,?,?,?,?)""",
            [
                (1, "u1", "U1", 0, 5),
                (2, "u2", "U2", 64500, 5),
                (3, "u3", "U3", 264500, 5),
                (4, "u4", "U4", 1234, 4),
            ],
        )
        conn.commit()
        conn.close()

        original_db = database.DB_NAME
        database.DB_NAME = legacy_db
        level_tags.DB_NAME = legacy_db
        try:
            await database.create_tables()
            conn = sqlite3.connect(legacy_db)
            rows = conn.execute("SELECT user_id,xp,level FROM users ORDER BY user_id").fetchall()
            marker = conn.execute(
                "SELECT COUNT(*) FROM data_migrations WHERE migration_key=?",
                (database.LEGACY_LEVEL_FIVE_MIGRATION,),
            ).fetchone()[0]
            conn.close()
            self.assertEqual(rows, [
                (1, 5500, 3),
                (2, 0, 4),
                (3, 0, 5),
                (4, 1234, 4),
            ])
            self.assertEqual(marker, 1)

            await database.create_tables()
            conn = sqlite3.connect(legacy_db)
            rows_again = conn.execute("SELECT user_id,xp,level FROM users ORDER BY user_id").fetchall()
            conn.close()
            self.assertEqual(rows_again, rows)
        finally:
            database.DB_NAME = original_db
            level_tags.DB_NAME = original_db
            legacy_tmp.cleanup()

    async def test_new_level_thresholds(self):
        await self.user(1, 9990, 1)
        await database.update_xp(1, 20)
        row = await database.get_user(1)
        self.assertEqual((row[3], row[4]), (10, 2))

    async def test_month_score_counts_only_marked_positive_xp(self):
        await self.user(1)
        await database.update_xp(1, 50, count_monthly=True)
        await database.update_xp(1, 500)
        await database.update_xp(1, -10, count_monthly=True)
        self.assertEqual(await database.get_month_score(1), (50, 1))

    async def test_reputation_full_then_reduced_for_seven_days(self):
        await self.user(1, level=4); await self.user(2)
        self.assertEqual(await database.give_reputation(1, 2), "success_full")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        async with aiosqlite.connect(self.db) as db:
            await db.execute("UPDATE rep_history SET date_str=? WHERE from_id=1", (yesterday,))
            await db.commit()
        # A different calendar date inside seven days remains a reduced reward.
        self.assertEqual(await database.give_reputation(1, 2), "success_repeat")

    async def test_month_prizes_are_finalized_once(self):
        for uid in (1, 2, 3): await self.user(uid)
        async with aiosqlite.connect(self.db) as db:
            await db.executemany("INSERT INTO month_scores VALUES('2026-06',?,?)", [(1, 300), (2, 200), (3, 100)])
            await db.commit()
        await database.finalize_closed_months(datetime(2026, 7, 1))
        await database.finalize_closed_months(datetime(2026, 7, 2))
        first = await database.get_user(1)
        self.assertEqual(database.total_available_xp(first[3], first[4]), 10000)
        self.assertEqual((await database.get_user(2))[3], 5000)
        self.assertEqual((await database.get_user(3))[3], 2500)

    async def test_factory_payout_is_monthly_and_conserves_bank(self):
        await self.user(1); await self.user(2); await self.user(3)
        async with aiosqlite.connect(self.db) as db:
            cur = await db.execute('''INSERT INTO factory_orders
                (chat_id,owner_id,order_type,size,coin_cost,xp_bank,status,created_at,stage_ends_at)
                VALUES(1,1,'discussion','small',50000,500,'active',0,0)''')
            oid = cur.lastrowid; await db.commit()
        order = await factory_orders._order(order_id=oid)
        await factory_orders._pay(order, {1: 100, 2: 200, 3: 200})
        balances = [(await database.get_user(uid))[3] for uid in (1, 2, 3)]
        scores = [(await database.get_month_score(uid))[0] for uid in (1, 2, 3)]
        self.assertEqual(sum(balances), 500)
        self.assertEqual(sum(scores), 500)

    async def test_duel_escrow_is_refunded_on_restart(self):
        await self.user(1, 1000, 2); await self.user(2, 1000, 2)
        duel = {"chat_id": 10, "initiator": 1, "target": 2, "initiator_name": "A",
                "target_name": "B", "bet": 100, "state": "waiting_accept", "escrowed": False}
        await database.save_active_duel(duel)
        self.assertTrue(await database.escrow_active_duel(10))
        self.assertEqual((await database.get_user(1))[3], 900)
        await database.refund_interrupted_duels()
        self.assertEqual((await database.get_user(1))[3], 1000)
        self.assertEqual((await database.get_user(2))[3], 1000)
        self.assertIsNone(await database.load_active_duel(10))

    async def test_duel_pair_counter_ignores_draws_and_opponent_order(self):
        await self.user(1); await self.user(2)
        base = {"chat_id": 10, "initiator": 2, "target": 1, "initiator_name": "B",
                "target_name": "A", "bet": 10, "created_at": int(datetime.now().timestamp())}
        await database.record_duel(base, "1", 10)
        self.assertEqual(await database.duel_pair_count_today(1, 2), 1)
        await database.record_duel(base, "draw", 0)
        self.assertEqual(await database.duel_pair_count_today(2, 1), 1)

    async def test_duel_settlement_is_idempotent(self):
        await self.user(1, 1000, 2); await self.user(2, 1000, 2)
        duel = {"chat_id": 11, "initiator": 1, "target": 2, "initiator_name": "A",
                "target_name": "B", "bet": 100, "state": "waiting_accept", "escrowed": False}
        await database.save_active_duel(duel); await database.escrow_active_duel(11)
        self.assertEqual(await database.settle_active_duel(duel, 1, 10), 180)
        self.assertIsNone(await database.settle_active_duel(duel, 1, 10))
        self.assertEqual((await database.get_user(1))[3], 1080)
        self.assertEqual((await database.get_user(2))[3], 900)

    async def test_chat_turn_reply_and_wave(self):
        users = [SimpleNamespace(id=i, username=f"u{i}", full_name=f"U{i}", is_bot=False) for i in range(1, 5)]
        messages = []
        for i, user in enumerate(users, 1):
            parent = messages[-1] if messages else None
            msg = SimpleNamespace(chat=SimpleNamespace(id=55), from_user=user, text=f"содержательное сообщение номер {i}",
                                  caption=None, photo=None, video=None, message_id=i, reply_to_message=parent)
            messages.append(msg)
            result = await engagement.process_chat_activity(msg)
        self.assertEqual(set(result["wave_users"]), {1, 2, 3, 4})
        # User 1: turn + reply reward + wave = 35.
        self.assertEqual((await database.get_user(1))[3], 35)

    async def test_level_tag_is_sent_only_initially_and_after_level_change(self):
        class FakeBot:
            id = 999

            def __init__(self):
                self.tags = []

            async def get_chat_member(self, chat_id, user_id):
                return SimpleNamespace(status="administrator", can_manage_tags=True)

            async def set_chat_member_tag(self, chat_id, user_id, tag):
                self.tags.append((chat_id, user_id, tag))
                return True

        bot = FakeBot()
        await self.user(50, xp=9990, level=1)
        message = SimpleNamespace(
            bot=bot,
            chat=SimpleNamespace(id=-100500, type="supergroup"),
            from_user=SimpleNamespace(id=50, username="u50", full_name="U50", is_bot=False),
        )
        await level_tags.ensure_level_tag(message)
        await level_tags.ensure_level_tag(message)
        self.assertEqual(bot.tags, [(-100500, 50, "Уровень 1")])

        await database.update_xp(50, 20)
        await level_tags.ensure_level_tag(message)
        self.assertEqual(bot.tags[-1], (-100500, 50, "Уровень 2"))
        self.assertEqual(len(bot.tags), 2)

    async def test_forced_level_tag_repairs_stale_telegram_tag(self):
        class FakeBot:
            id = 999

            def __init__(self):
                self.tags = []
                self.current_tag = "Уровень 1"

            async def get_chat_member(self, chat_id, user_id):
                if user_id == self.id:
                    return SimpleNamespace(status="administrator", can_manage_tags=True)
                return SimpleNamespace(status="member", tag=self.current_tag)

            async def set_chat_member_tag(self, chat_id, user_id, tag):
                self.tags.append((chat_id, user_id, tag))
                self.current_tag = tag

        bot = FakeBot()
        await self.user(60, xp=10, level=2)
        async with aiosqlite.connect(self.db) as db:
            await db.execute(
                """INSERT INTO chat_level_tags(chat_id,user_id,applied_level,retry_after,last_error)
                   VALUES(-100600,60,2,0,'')"""
            )
            await db.commit()
        self.assertTrue(await level_tags.sync_level_tag(bot, -100600, 60, 2, force=True))
        self.assertEqual(bot.tags, [
            (-100600, 60, ""),
            (-100600, 60, "Уровень 2"),
        ])

    async def test_factory_admin_discussion_uses_real_completion_rules(self):
        bot = FakeFactoryBot()
        await self.user(1)
        for user_id in range(2, 7):
            await self.user(user_id)
        order, error = await factory_orders.launch_factory_order(
            bot, -1001, 1, "Owner", "discussion", "small",
            "Какая игра вас разочаровала?", charge_factory=False,
        )
        self.assertFalse(error)
        panel_text, panel_kb = await admin_factory.build_factory_admin_view(-1001)
        self.assertIn(f"Заказ:</b> #{order['id']}", panel_text)
        self.assertTrue(panel_kb.inline_keyboard)
        picker_text, picker_kb = await admin_factory.build_factory_chat_picker(bot)
        self.assertIn("Выберите группу", picker_text)
        self.assertEqual(picker_kb.inline_keyboard[0][0].callback_data, "facadm_chat:-1001")
        ok, message = await factory_orders.admin_advance_factory_order(bot, order["id"])
        self.assertFalse(ok)
        self.assertIn("участников 0/5", message)

        async with aiosqlite.connect(self.db) as db:
            await db.executemany(
                """INSERT INTO factory_order_participants
                   (order_id,user_id,display_name,replies_received,joined_at)
                   VALUES(?,?,?,?,?)""",
                [(order["id"], user_id, f"U{user_id}", 1, user_id) for user_id in range(2, 7)],
            )
            await db.executemany(
                """INSERT INTO factory_order_messages
                   (order_id,message_id,author_id,parent_author_id)
                   VALUES(?,?,?,?)""",
                [
                    (order["id"], 1000 + index, 2 + index % 5, 2 + (index + 1) % 5)
                    for index in range(10)
                ],
            )
            await db.commit()
        ok, _message = await factory_orders.admin_advance_factory_order(bot, order["id"])
        self.assertTrue(ok)
        self.assertEqual((await self.order_status(order["id"]))[0], "completed")
        self.assertEqual((await self.order_status(order["id"]))[1], 500)

    async def test_factory_admin_photo_runs_collection_vote_and_payout(self):
        bot = FakeFactoryBot()
        await self.user(1)
        for user_id in range(2, 6):
            await self.user(user_id)
        order, error = await factory_orders.launch_factory_order(
            bot, -1002, 1, "Owner", "photo", "small",
            "Покажите рабочее место", charge_factory=False,
        )
        self.assertFalse(error)
        async with aiosqlite.connect(self.db) as db:
            await db.executemany(
                """INSERT INTO factory_order_participants
                   (order_id,user_id,display_name,submission_message_id,joined_at)
                   VALUES(?,?,?,?,?)""",
                [(order["id"], user_id, f"U{user_id}", 2000 + user_id, user_id) for user_id in range(2, 6)],
            )
            await db.commit()
        ok, _ = await factory_orders.admin_advance_factory_order(bot, order["id"])
        self.assertTrue(ok)
        active = await factory_orders._order(order_id=order["id"])
        self.assertEqual(active["status"], "voting")
        ok, _ = await factory_orders.admin_advance_factory_order(bot, order["id"])
        self.assertTrue(ok)
        self.assertEqual((await self.order_status(order["id"]))[0], "completed")
        self.assertEqual((await self.order_status(order["id"]))[1], 500)

    async def test_factory_tournament_full_player_path_and_stale_button_guard(self):
        bot = FakeFactoryBot()
        await self.user(1)
        for user_id in range(10, 14):
            await self.user(user_id, xp=1000)
        order, error = await factory_orders.launch_factory_order(
            bot, -1003, 1, "Owner", "tournament", "small", charge_factory=False,
        )
        self.assertFalse(error)
        for user_id in range(10, 14):
            await factory_orders.tournament_join(
                FakeCallback(bot, f"fjoin:{order['id']}", user_id)
            )
        active = await factory_orders._order(order_id=order["id"])
        self.assertEqual(active["status"], "tournament")

        first_pair = None
        for match_no in ("1", "2", "3"):
            active = await factory_orders._order(order_id=order["id"])
            meta = json.loads(active["metadata"])
            self.assertEqual(factory_orders._current_tournament_match(meta), match_no)
            pair = meta["matches"][match_no]
            if match_no == "1":
                first_pair = pair
            stage_id = int(meta["stage_message_id"])
            await factory_orders.tournament_tactic(
                FakeCallback(bot, f"ftac:{order['id']}:{match_no}:atk", pair[0], stage_id)
            )
            await factory_orders.tournament_tactic(
                FakeCallback(bot, f"ftac:{order['id']}:{match_no}:trick", pair[1], stage_id)
            )
            if match_no == "1":
                stale = FakeCallback(bot, f"ftac:{order['id']}:1:def", first_pair[0], stage_id)
                await factory_orders.tournament_tactic(stale)
                self.assertIn("уже завершён", stale.answers[-1][0])

        self.assertEqual((await self.order_status(order["id"]))[0], "completed")
        total = 0
        for user_id in (1, 10, 11, 12, 13):
            user = await database.get_user(user_id)
            total += database.total_available_xp(user[3], user[4])
        self.assertEqual(total, 4500)

    async def test_factory_admin_cancel_refunds_tournament_stakes(self):
        bot = FakeFactoryBot()
        await self.user(1)
        for user_id in (20, 21):
            await self.user(user_id, xp=500)
        order, _ = await factory_orders.launch_factory_order(
            bot, -1004, 1, "Owner", "tournament", "small", charge_factory=False,
        )
        for user_id in (20, 21):
            await factory_orders.tournament_join(
                FakeCallback(bot, f"fjoin:{order['id']}", user_id)
            )
        self.assertEqual((await database.get_user(20))[3], 400)
        duplicate = FakeCallback(bot, f"fjoin:{order['id']}", 20)
        await factory_orders.tournament_join(duplicate)
        self.assertEqual((await database.get_user(20))[3], 400)
        self.assertIn("уже участвуете", duplicate.answers[-1][0])
        ok, _ = await factory_orders.admin_cancel_factory_order(bot, order["id"])
        self.assertTrue(ok)
        self.assertEqual((await database.get_user(20))[3], 500)
        self.assertEqual((await database.get_user(21))[3], 500)
        self.assertEqual((await self.order_status(order["id"]))[0], "cancelled")
        again, _ = await factory_orders.admin_cancel_factory_order(bot, order["id"])
        self.assertFalse(again)

    async def test_tournament_stage_publish_failure_refunds_every_stake(self):
        class StageFailBot(FakeFactoryBot):
            async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
                if len(self.sent) == 1:
                    raise RuntimeError("stage publish failed")
                return await super().send_message(chat_id, text, reply_markup, **kwargs)

        bot = StageFailBot()
        await self.user(1)
        for user_id in range(30, 34):
            await self.user(user_id, xp=500)
        order, error = await factory_orders.launch_factory_order(
            bot, -1006, 1, "Owner", "tournament", "small", charge_factory=False,
        )
        self.assertFalse(error)
        for user_id in range(30, 34):
            await factory_orders.tournament_join(
                FakeCallback(bot, f"fjoin:{order['id']}", user_id)
            )
        self.assertEqual((await self.order_status(order["id"]))[0], "cancelled")
        for user_id in range(30, 34):
            self.assertEqual((await database.get_user(user_id))[3], 500)

    async def test_factory_launch_failure_leaves_no_active_order(self):
        class FailingBot(FakeFactoryBot):
            async def send_message(self, *args, **kwargs):
                raise RuntimeError("telegram unavailable")

        bot = FailingBot()
        await self.user(1)
        order, error = await factory_orders.launch_factory_order(
            bot, -1005, 1, "Owner", "discussion", "small",
            "Проверка отката запуска", charge_factory=False,
        )
        self.assertIsNone(order)
        self.assertIn("не смог", error)
        self.assertIsNone(await factory_orders._order(chat_id=-1005))

    async def test_admin_stop_of_player_order_returns_full_coin_cost(self):
        bot = FakeFactoryBot()
        await self.user(70)
        await self.complete_farm(70)
        order, error = await factory_orders.launch_factory_order(
            bot, -1007, 70, "Player", "discussion", "small",
            "Тема обычного игрока", charge_factory=True,
        )
        self.assertFalse(error)
        async with aiosqlite.connect(self.db) as db:
            coins = (await (await db.execute(
                "SELECT coins FROM farm_players WHERE user_id=70"
            )).fetchone())[0]
        self.assertEqual(coins, 10000)
        ok, _ = await factory_orders.admin_cancel_factory_order(bot, order["id"])
        self.assertTrue(ok)
        async with aiosqlite.connect(self.db) as db:
            coins = (await (await db.execute(
                "SELECT coins FROM farm_players WHERE user_id=70"
            )).fetchone())[0]
        self.assertEqual(coins, 60000)

    async def test_player_order_publish_failure_returns_full_coin_cost(self):
        class FailingBot(FakeFactoryBot):
            async def send_message(self, *args, **kwargs):
                raise RuntimeError("telegram unavailable")

        bot = FailingBot()
        await self.user(71)
        await self.complete_farm(71)
        order, error = await factory_orders.launch_factory_order(
            bot, -1008, 71, "Player", "photo", "small",
            "Тема с ошибкой Telegram", charge_factory=True,
        )
        self.assertIsNone(order)
        self.assertTrue(error)
        async with aiosqlite.connect(self.db) as db:
            coins = (await (await db.execute(
                "SELECT coins FROM farm_players WHERE user_id=71"
            )).fetchone())[0]
        self.assertEqual(coins, 60000)

    async def test_factory_cleanup_removes_all_stage_messages(self):
        class FakeBot:
            def __init__(self):
                self.deleted = []

            async def delete_message(self, chat_id, message_id):
                self.deleted.append((chat_id, message_id))

        bot = FakeBot()
        order = {
            "chat_id": -1001,
            "message_id": 10,
            "vote_message_id": 11,
            "metadata": '{"stage_message_id": 12}',
        }
        await factory_orders._cleanup_order_messages(bot, order, [13])
        self.assertEqual(
            set(bot.deleted),
            {(-1001, 10), (-1001, 11), (-1001, 12), (-1001, 13)},
        )


if __name__ == "__main__":
    unittest.main()
