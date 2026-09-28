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


    def test_dryer_requires_wash_booking_and_next_hour_offer(self):
        import booking_service as bs
        from dryer_service import find_next_dryer

        future = (datetime.now(TZ).date() + timedelta(days=1)).isoformat()
        wash = self.mid("Стиральная №1")
        dry = self.mid("Сушилка №2")
        uid = self.uid(1001)

        with self.assertRaises(bs.InvalidBooking):
            asyncio.run(bs.create_booking_safe(uid, dry, future, 13))

        wash_booking = asyncio.run(bs.create_booking_safe(uid, wash, future, 12))
        self.assertTrue(wash_booking.booking_id)

        offer = find_next_dryer(uid, future, 12)
        self.assertIsNotNone(offer)
        self.assertEqual(offer.machine_id, dry)
        self.assertEqual(offer.hour, 13)

        dry_booking = asyncio.run(bs.create_booking_safe(uid, dry, future, 13))
        self.assertTrue(dry_booking.booking_id)


    def test_existing_free_slot_is_found_after_request_creation(self):
        import waitlist_service as wl

        wl.save_request(1001, [(13, 14)], [], True, "auto")
        matched = asyncio.run(wl.check_active_waitlist())
        self.assertGreaterEqual(matched, 1)

        uid = self.uid(1001)
        with database.get_conn() as conn:
            booking = conn.execute(
                """
                SELECT b.date,b.hour,m.type
                FROM bookings b
                JOIN machines m ON m.id=b.machine_id
                WHERE b.user_id=? AND m.type='wash'
                ORDER BY b.date,b.hour
                LIMIT 1
                """,
                (uid,),
            ).fetchone()
        self.assertIsNotNone(booking)
        self.assertEqual(int(booking[1]), 13)
        self.assertEqual(str(booking[2]), "wash")


    def test_waitlist_jobs_are_event_driven(self):
        import waitlist_service as wl
        from scheduler import scheduler

        future = (datetime.now(TZ) + timedelta(minutes=2)).replace(microsecond=0)
        uid = self.uid(1001)
        mid = self.mid("Стиральная №1")
        rid = wl.save_request(1001, [(7, 23)], [mid], False, "notify")

        with database.get_conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO slot_holds
                (request_id,user_id,machine_id,date,hour,expires_at,context,status,created_at)
                VALUES (?,?,?,?,?,?,'day','active',?)
                """,
                (
                    rid, uid, mid,
                    (datetime.now(TZ).date() + timedelta(days=1)).isoformat(),
                    12,
                    future.isoformat(),
                    datetime.now(TZ).isoformat(timespec="seconds"),
                ),
            )
            hold_id = getattr(cur, "lastrowid", None)
            if not hold_id:
                hold_id = conn.execute(
                    "SELECT id FROM slot_holds WHERE request_id=? ORDER BY id DESC LIMIT 1",
                    (rid,),
                ).fetchone()[0]

        wl._schedule_hold_expiry(int(hold_id), future)
        job = scheduler.get_job(f"waitlist_hold_{int(hold_id)}")
        self.assertIsNotNone(job)
        self.assertEqual(job.trigger.run_date.replace(microsecond=0), future)

        # PRA4KA 2.0 must not keep Neon awake with periodic database polling.
        forbidden = {
            "waitlist_expire_holds",
            "waitlist_pending_notifications",
            "waitlist_active_poll",
            "usage_history_tick",
        }
        self.assertTrue(forbidden.isdisjoint({j.id for j in scheduler.get_jobs()}))

        scheduler.remove_job(f"waitlist_hold_{int(hold_id)}")


    def test_waitlist_weekday_preferences(self):
        import waitlist_service as wl

        first_date = datetime.now(TZ).date() + timedelta(days=1)
        second_date = first_date + timedelta(days=1)
        allowed_weekday = second_date.weekday()

        wl.save_request(
            1001,
            [(7, 23)],
            [],
            True,
            "auto",
            weekdays=[allowed_weekday],
        )

        requests = wl._active_requests()
        req = next(r for r in requests if r.tg_id == 1001)
        self.assertEqual(req.weekdays, {allowed_weekday})
        self.assertFalse(req.accepts_date(first_date.isoformat()))
        self.assertTrue(req.accepts_date(second_date.isoformat()))

        first_result = asyncio.run(
            wl.distribute_date(first_date.isoformat(), context="day")
        )
        self.assertEqual(first_result, 0)
        self.assertFalse(
            database.get_user_bookings_today(
                self.uid(1001), first_date.isoformat(), "wash"
            )
        )

        second_result = asyncio.run(
            wl.distribute_date(second_date.isoformat(), context="day")
        )
        self.assertGreaterEqual(second_result, 1)
        self.assertTrue(
            database.get_user_bookings_today(
                self.uid(1001), second_date.isoformat(), "wash"
            )
        )

    def test_waitlist_without_weekdays_means_any_day(self):
        import waitlist_service as wl

        wl.save_request(1002, [(7, 23)], [], True, "auto")
        req = next(r for r in wl._active_requests() if r.tg_id == 1002)
        self.assertEqual(req.weekdays, set())
        self.assertTrue(req.accepts_date(
            (datetime.now(TZ).date() + timedelta(days=1)).isoformat()
        ))


    def test_cancelling_future_waitlist_booking_reopens_same_request_with_priority(self):
        import booking_service as bs
        import waitlist_service as wl

        future = (datetime.now(TZ).date() + timedelta(days=1)).isoformat()
        uid = self.uid(1001)
        mid = self.mid("Стиральная №1")

        request_id = wl.save_request(1001, [(10, 12)], [mid], False, "auto")
        old_priority = "2026-09-20T12:00:00+03:00"
        with database.get_conn() as conn:
            conn.execute(
                "UPDATE waitlist_requests SET priority_since=? WHERE id=?",
                (old_priority, request_id),
            )

        booking = asyncio.run(bs.create_booking_safe(uid, mid, future, 10))
        with database.get_conn() as conn:
            matched = conn.execute(
                "SELECT status,matched_booking_id,priority_since FROM waitlist_requests WHERE id=?",
                (request_id,),
            ).fetchone()
        self.assertEqual(str(matched[0]), "matched")
        self.assertEqual(int(matched[1]), booking.booking_id)
        self.assertEqual(str(matched[2]), old_priority)

        cancelled = asyncio.run(bs.cancel_booking_safe(booking.booking_id))
        self.assertTrue(cancelled.waitlist_reopened)

        with database.get_conn() as conn:
            reopened = conn.execute(
                "SELECT status,matched_booking_id,priority_since FROM waitlist_requests WHERE id=?",
                (request_id,),
            ).fetchone()
            history = conn.execute(
                """
                SELECT result FROM waitlist_offer_history
                WHERE request_id=? AND machine_id=? AND date=? AND hour=?
                ORDER BY id DESC LIMIT 1
                """,
                (request_id, mid, future, 10),
            ).fetchone()

        self.assertEqual(str(reopened[0]), "active")
        self.assertIsNone(reopened[1])
        self.assertEqual(str(reopened[2]), old_priority)
        self.assertEqual(str(history[0]), "cancelled_booking")
        self.assertFalse(
            database.usage_penalties_for_users([uid]).get(uid, 0)
        )


    def test_duplicate_resident_profiles_cannot_double_queue(self):
        import waitlist_service as wl

        database.save_user(2001, "Иванов", "101")
        conflict = database.find_resident_profile_conflict(2001, "Иванов", "101")
        self.assertIsNotNone(conflict)
        self.assertEqual(int(conflict[1]), 1001)

        first = wl.save_request(1001, [(18, 20)], [], True, "auto")
        self.assertTrue(first)
        with self.assertRaises(ValueError):
            wl.save_request(2001, [(18, 20)], [], True, "auto")

        active = [r for r in wl._active_requests() if r.tg_id in {1001, 2001}]
        self.assertEqual(len(active), 1)

    def test_duplicate_resident_accounts_share_usage_penalty(self):
        now = datetime.now(TZ).replace(microsecond=0)
        database.save_user(2001, "Иванов", "101")
        first_uid = self.uid(1001)
        second_uid = self.uid(2001)
        mid = self.mid("Стиральная №1")
        yesterday = (now.date() - timedelta(days=1)).isoformat()

        with database.get_conn() as conn:
            conn.execute(
                "INSERT INTO bookings(user_id,machine_id,date,hour) VALUES (?,?,?,?)",
                (first_uid, mid, yesterday, 7),
            )
        database.record_usage_history(now)
        scores = database.usage_penalties_for_users([first_uid, second_uid], now)
        self.assertEqual(scores[first_uid], 4)
        self.assertEqual(scores[second_uid], 4)

    def test_hold_acceptance_is_idempotent(self):
        import waitlist_service as wl

        future = (datetime.now(TZ).date() + timedelta(days=1)).isoformat()
        uid = self.uid(1001)
        mid = self.mid("Стиральная №1")
        rid = wl.save_request(1001, [(10, 12)], [mid], False, "notify")
        expires = datetime.now(TZ) + timedelta(minutes=2)

        with database.get_conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO slot_holds
                (request_id,user_id,machine_id,date,hour,expires_at,context,status,created_at)
                VALUES (?,?,?,?,?,?,'day','active',?)
                """,
                (
                    rid, uid, mid, future, 10,
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

        first = asyncio.run(wl.accept_hold(int(hold_id), 1001))
        second = asyncio.run(wl.accept_hold(int(hold_id), 1001))
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(first.booking_id, second.booking_id)

        with database.get_conn() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM bookings WHERE user_id=? AND machine_id=? AND date=? AND hour=?",
                (uid, mid, future, 10),
            ).fetchone()[0]
        self.assertEqual(int(count), 1)

    def test_bulk_availability_matches_effective_hours(self):
        future = (datetime.now(TZ).date() + timedelta(days=1)).isoformat()
        mid = self.mid("Стиральная №1")
        uid = self.uid(1001)
        with database.get_conn() as conn:
            conn.execute(
                "INSERT INTO bookings(user_id,machine_id,date,hour) VALUES (?,?,?,?)",
                (uid, mid, future, 10),
            )

        machines, availability = database.get_availability_bulk([future])
        self.assertTrue(machines)
        self.assertNotIn(10, availability[future][mid])
        self.assertEqual(
            availability[future][mid],
            database.get_free_hours_effective(mid, future),
        )


if __name__ == "__main__":
    unittest.main()
