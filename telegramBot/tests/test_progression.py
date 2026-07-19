import os
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace

import aiosqlite

import database
import engagement
from modules import factory_orders


class ProgressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = os.path.join(self.tmp.name, "test.db")
        database.DB_NAME = self.db
        engagement.DB_NAME = self.db
        factory_orders.DB_NAME = self.db
        await database.create_tables()
        await engagement.create_engagement_tables()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def user(self, uid, xp=0, level=1):
        await database.get_user(uid, f"u{uid}", f"User {uid}")
        async with aiosqlite.connect(self.db) as db:
            await db.execute("UPDATE users SET xp=?,level=? WHERE user_id=?", (xp, level, uid)); await db.commit()

    async def test_old_level_and_xp_are_preserved(self):
        await self.user(1, 12345, 5)
        row = await database.get_user(1)
        self.assertEqual((row[3], row[4]), (12345, 5))
        await database.update_xp(1, 1000)
        row = await database.get_user(1)
        self.assertEqual((row[3], row[4]), (13345, 5))

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
        tomorrow = (datetime.now().date()).replace(day=datetime.now().day).strftime("%Y-%m-%d")
        async with aiosqlite.connect(self.db) as db:
            await db.execute("UPDATE rep_history SET date_str='2026-07-17' WHERE from_id=1")
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


if __name__ == "__main__":
    unittest.main()
