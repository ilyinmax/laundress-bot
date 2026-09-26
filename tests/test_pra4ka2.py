import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import database
from config import TIMEZONE

TZ = ZoneInfo(TIMEZONE)


class Pra4ka2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        database.DB_PATH = self.tmp.name
        database.DATABASE_URL = ""
        database.init_db()
        database.add_machine("wash", "Стиральная №1")
        database.add_machine("wash", "Стиральная №3")
        database.add_machine("dry", "Сушилка №2")
        database.save_user(1001, "Иванов", "101")
        database.save_user(1002, "Петров", "102")
        database.save_user(1003, "Сидоров", "103")

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def uid(self, tg):
        return int(database.get_user(tg)[0])

    def mid(self, name):
        return int(database.get_machine_id_by_name(name))

    def test_repeated_waitlist_requests_and_matching(self):
        import waitlist_service as wl

        r1 = wl.save_request(1001, [(18, 23)], [], True, "auto")
        r2 = wl.save_request(1002, [(20, 21)], [], True, "notify")
        self.assertTrue(r1)
        self.assertTrue(r2)

        requests = wl._active_requests()
        m1 = self.mid("Стиральная №1")
        matches = wl._match(requests, [(m1, 20), (m1, 21)])
        by_tg = {r.tg_id: r.id for r in requests}
        self.assertEqual(matches[by_tg[1002]], (m1, 20))
        self.assertEqual(matches[by_tg[1001]], (m1, 21))

        self.assertTrue(wl.cancel_request_for_tg(1001))
        r3 = wl.save_request(1001, [(7, 10)], [], True, "auto")
        self.assertNotEqual(r1, r3)
        with database.get_conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM waitlist_requests WHERE user_id=?",
                (self.uid(1001),),
            ).fetchone()[0]
        self.assertEqual(int(count), 2)

    def test_booking_limit_and_hold(self):
        import booking_service as bs
        import waitlist_service as wl

        future = (datetime.now(TZ).date() + timedelta(days=1)).isoformat()
        m1 = self.mid("Стиральная №1")
        m3 = self.mid("Стиральная №3")
        uid = self.uid(1001)

        first = asyncio.run(bs.create_booking_safe(uid, m1, future, 10))
        self.assertTrue(first.booking_id)

        with self.assertRaises(bs.DailyLimit):
            asyncio.run(bs.create_booking_safe(uid, m3, future, 11))

        asyncio.run(bs.cancel_booking_safe(first.booking_id))

        rid = wl.save_request(1001, [(10, 12)], [m1], False, "notify")
        expires = datetime.now(TZ) + timedelta(minutes=2)
        with database.get_conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO slot_holds
                (request_id,user_id,machine_id,date,hour,expires_at,context,status,created_at)
                VALUES (?,?,?,?,?,?,'day','active',?)
                """,
                (
                    rid, uid, m1, future, 10,
                    expires.isoformat(timespec="seconds"),
                    datetime.now(TZ).isoformat(timespec="seconds"),
                ),
            )
            hold_id = getattr(cur, "lastrowid", None)
            if not hold_id:
                hold_id = conn.execute(
                    "SELECT id FROM slot_holds WHERE request_id=? ORDER BY id DESC LIMIT 1",
                    (rid,),
                ).fetchone()[0]

        self.assertNotIn(10, database.get_free_hours_effective(m1, future))
        result = asyncio.run(
            bs.create_booking_safe(uid, m1, future, 10, allowed_hold_id=int(hold_id))
        )
        self.assertEqual(result.hour, 10)

    def test_usage_history_weights(self):
        now = datetime.now(TZ).replace(microsecond=0)
        uid = self.uid(1003)
        mid = self.mid("Стиральная №1")
        yesterday = (now.date() - timedelta(days=1)).isoformat()
        with database.get_conn() as conn:
            conn.execute(
                "INSERT INTO bookings(user_id,machine_id,date,hour) VALUES (?,?,?,?)",
                (uid, mid, yesterday, 7),
            )
        database.record_usage_history(now)
        score = database.usage_penalties_for_users([uid], now)
        self.assertEqual(score[uid], 4)


    def test_matching_with_one_hundred_users(self):
        import waitlist_service as wl

        for i in range(100):
            tg = 2000 + i
            database.save_user(tg, f"Тест{i}", f"{200 + (i % 300):03d}")
            if i < 8:
                wl.save_request(tg, [(20, 21)], [], True, "auto")
            elif i < 24:
                wl.save_request(tg, [(18, 22)], [], True, "auto")
            else:
                wl.save_request(tg, [(7, 23)], [], True, "auto")

        requests = wl._active_requests()
        m1 = self.mid("Стиральная №1")
        m3 = self.mid("Стиральная №3")
        slots = [(mid, h) for mid in (m1, m3) for h in range(7, 23)]
        matches = wl._match(requests, slots)

        self.assertEqual(len(matches), len(slots))
        self.assertEqual(len(set(matches.values())), len(slots))

        narrow_ids = {r.id for r in requests if r.tg_id in range(2000, 2008)}
        narrow_matched = [matches[rid] for rid in narrow_ids if rid in matches]
        self.assertEqual(len(narrow_matched), 2)
        self.assertTrue(all(hour == 20 for _, hour in narrow_matched))

    def test_night_cutoff_excludes_late_requests(self):
        import waitlist_service as wl

        first = wl.save_request(1001, [(18, 20)], [], True, "auto")
        second = wl.save_request(1002, [(18, 20)], [], True, "auto")
        day = datetime.now(TZ).date().isoformat()
        cutoff = f"{day}T23:00:00+03:00"

        with database.get_conn() as conn:
            conn.execute(
                "UPDATE waitlist_requests SET priority_since=? WHERE id=?",
                (f"{day}T22:30:00+03:00", first),
            )
            conn.execute(
                "UPDATE waitlist_requests SET priority_since=? WHERE id=?",
                (f"{day}T23:01:00+03:00", second),
            )

        ids = {r.id for r in wl._active_requests(cutoff)}
        self.assertIn(first, ids)
        self.assertNotIn(second, ids)

    def test_mode_only_edit_keeps_priority(self):
        import waitlist_service as wl

        rid = wl.save_request(1001, [(18, 20)], [], True, "auto")
        old_priority = "2026-09-20T12:00:00+03:00"
        with database.get_conn() as conn:
            conn.execute(
                "UPDATE waitlist_requests SET priority_since=? WHERE id=?",
                (old_priority, rid),
            )
        rid2 = wl.save_request(1001, [(18, 20)], [], True, "notify")
        self.assertEqual(rid, rid2)
        with database.get_conn() as conn:
            priority = conn.execute(
                "SELECT priority_since FROM waitlist_requests WHERE id=?",
                (rid,),
            ).fetchone()[0]
        self.assertEqual(str(priority), old_priority)


if __name__ == "__main__":
    unittest.main()
