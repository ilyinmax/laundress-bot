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

    def test_hold_duration_policy(self):
        import waitlist_service as wl

        now = datetime.now(TZ).replace(microsecond=0)
        for minutes_left, expected in (
            (40, 5), (30, 5), (29, 2), (10, 2), (5, 2), (4, None)
        ):
            slot = now + timedelta(minutes=minutes_left)
            # Slot starts at a whole hour. Control the clock relative to it.
            slot_start = slot.replace(minute=0, second=0)
            if slot_start <= now:
                slot_start += timedelta(hours=1)
            clock = slot_start - timedelta(minutes=minutes_left)
            self.assertEqual(
                wl._hold_duration_minutes(
                    slot_start.date().isoformat(), slot_start.hour, clock
                ),
                expected,
            )

        future = now + timedelta(days=1)
        self.assertEqual(
            wl._hold_duration_minutes(future.date().isoformat(), future.hour, now),
            5,
        )
        self.assertIn("5 минут", wl._hold_deadline_text(now + timedelta(minutes=5), 5))
        self.assertIn("2 минуты", wl._hold_deadline_text(now + timedelta(minutes=2), 2))


    def test_edit_machine_preferences_preserves_priority(self):
        import waitlist_service as wl

        machine = self.mid("Стиральная №1")
        rid = wl.save_request(1001, [(18, 23)], [machine], False, "auto")
        before = "2026-09-20T12:00:00+03:00"
        with database.get_conn() as conn:
            conn.execute(
                "UPDATE waitlist_requests SET priority_since=? WHERE id=?",
                (before, rid),
            )
        same = wl.save_request(1001, [(18, 23)], [], True, "auto")
        self.assertEqual(same, rid)
        with database.get_conn() as conn:
            row = conn.execute(
                "SELECT priority_since,any_machine FROM waitlist_requests WHERE id=?",
                (rid,),
            ).fetchone()
        self.assertEqual(str(row[0]), before)
        self.assertEqual(int(row[1]), 1)

    def test_forecast_is_read_only_and_cached(self):
        import forecast_service as fs
        import waitlist_service as wl

        fs.invalidate_forecasts()
        today = datetime.now(TZ)
        rid = wl.save_request(
            1001, [], [], True, "auto",
            schedule={day: [(7, 23)] for day in range(7)},
        )
        with database.get_conn() as conn:
            before = conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
        first = asyncio.run(fs.get_forecast(rid))
        second = asyncio.run(fs.get_forecast(rid))
        with database.get_conn() as conn:
            after = conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
        self.assertIsNotNone(first)
        self.assertEqual(first, second)
        self.assertEqual(int(before), int(after))
        self.assertEqual(first["hours"], 16)
        self.assertIn("score", first)
        self.assertIn("date", first)
        fs.invalidate_forecasts()

    def test_subscription_summary_keeps_compact_settings(self):
        import waitlist_service as wl
        from handlers import pra4ka2 as ui
        from unittest.mock import AsyncMock, patch

        rid = wl.save_request(
            1001, [], [], True, "auto",
            schedule={0: [(18, 23)], 2: [(18, 23)], 4: [(18, 23)]},
        )
        forecast = {
            "score": 78, "percent": 62, "waiting": 109, "hours": 5,
            "chance": "🟢↗️ Шансы выше среднего", "date": "12–14 октября",
        }
        with patch("forecast_service.get_forecast", new=AsyncMock(return_value=forecast)):
            output = asyncio.run(ui._waitlist_summary(1001))
        self.assertIn("🔔 <b>Моя подписка</b>", output)
        self.assertIn("✅ <b>Подписка активна</b>", output)
        self.assertNotIn("Ваш приоритет", output)
        self.assertNotIn("баллов", output)
        self.assertNotIn("Шансы выше среднего", output)
        self.assertNotIn("Подходящих вариантов по времени", output)
        self.assertIn("📊 <b>Приоритет в очереди</b>", output)
        self.assertIn("Выше, чем у <b>62%</b> ожидающих.", output)
        self.assertIn("Это не вероятность записи", output)
        self.assertLess(output.index("Ориентировочная дата стирки"), output.index("Приоритет в очереди"))
        self.assertNotIn("⭐", output)
        self.assertIn("📆 Пн, Ср, Пт · 18:00–22:00", output)
        self.assertIn("🧺 Любая машинка", output)
        self.assertIn("⚡ Автоматическая запись", output)
        self.assertIn("Расчёт по текущей очереди, вашему расписанию и свободным слотам.", output)
        self.assertEqual(rid, int(wl.get_active_request_for_tg(1001)[0]))

    def test_subscription_summary_has_no_double_blank_after_heading(self):
        import waitlist_service as wl
        from handlers import pra4ka2 as ui
        from unittest.mock import AsyncMock, patch

        wl.save_request(
            1001, [], [], True, "auto",
            schedule={day: [(13, 23)] for day in range(7)},
        )
        forecast = {
            "score": -48, "percent": 17, "waiting": 110, "hours": 10,
            "chance": "↘️ Шансы ниже среднего", "date": "14 октября – 16 октября",
        }
        with patch("forecast_service.get_forecast", new=AsyncMock(return_value=forecast)):
            output = asyncio.run(ui._waitlist_summary(1001))
        self.assertTrue(
            output.startswith("🔔 <b>Моя подписка</b>\n✅ <b>Подписка активна</b>\n\n")
        )
        self.assertNotIn("\n\n\n", output)


    def test_main_menu_is_vertical_and_waitlist_first(self):
        from keyboards import build_main_menu
        menu = build_main_menu(True)
        labels = [[btn.text for btn in row] for row in menu.keyboard]
        self.assertEqual(labels, [
            ["🔔 Лист ожидания • активен"],
            ["📋 Мои записи"],
            ["🧺 Записаться"],
            ["ℹ️ Помощь"],
        ])


    def test_hold_deadline_displays_minutes_only(self):
        import waitlist_service as wl

        expires = datetime.now(TZ).replace(microsecond=0, second=37)
        text = wl._hold_deadline_text(expires, 5)
        self.assertIn(expires.strftime("%H:%M"), text)
        self.assertNotIn(expires.strftime("%H:%M:%S"), text)


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

    def test_atomic_booking_reassignment_keeps_slot_occupied_and_syncs_subscriptions(self):
        import booking_service as bs
        import waitlist_service as wl

        future = (datetime.now(TZ).date() + timedelta(days=2)).isoformat()
        m1 = self.mid("Стиральная №1")

        old_request = wl.save_request(1001, [(10, 12)], [], True, "auto")
        new_request = wl.save_request(1002, [(10, 12)], [], True, "auto")

        old_booking = asyncio.run(
            bs.create_booking_safe(self.uid(1001), m1, future, 10)
        )

        old, new = asyncio.run(
            bs.reassign_booking_safe(old_booking.booking_id, self.uid(1002))
        )

        self.assertEqual(old.booking_id, old_booking.booking_id)
        self.assertEqual(new.user_id, self.uid(1002))
        self.assertEqual(new.machine_id, m1)
        self.assertEqual(new.date, future)
        self.assertEqual(new.hour, 10)

        with database.get_conn() as conn:
            rows = conn.execute(
                """
                SELECT id,user_id FROM bookings
                WHERE machine_id=? AND date=? AND hour=?
                """,
                (m1, future, 10),
            ).fetchall()
            old_req = conn.execute(
                "SELECT status,matched_booking_id FROM waitlist_requests WHERE id=?",
                (old_request,),
            ).fetchone()
            new_req = conn.execute(
                "SELECT status,matched_booking_id FROM waitlist_requests WHERE id=?",
                (new_request,),
            ).fetchone()

        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0][0]), new.booking_id)
        self.assertEqual(int(rows[0][1]), self.uid(1002))
        self.assertEqual(str(old_req[0]), "active")
        self.assertIsNone(old_req[1])
        self.assertEqual(str(new_req[0]), "matched")
        self.assertEqual(int(new_req[1]), new.booking_id)


    def test_failed_reassignment_keeps_original_booking(self):
        import booking_service as bs

        future = (datetime.now(TZ).date() + timedelta(days=2)).isoformat()
        m1 = self.mid("Стиральная №1")
        m3 = self.mid("Стиральная №3")

        original = asyncio.run(
            bs.create_booking_safe(self.uid(1001), m1, future, 10)
        )
        asyncio.run(
            bs.create_booking_safe(self.uid(1002), m3, future, 11)
        )

        with self.assertRaises(bs.DailyLimit):
            asyncio.run(
                bs.reassign_booking_safe(original.booking_id, self.uid(1002))
            )

        with database.get_conn() as conn:
            row = conn.execute(
                "SELECT user_id FROM bookings WHERE id=?",
                (original.booking_id,),
            ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(int(row[0]), self.uid(1001))


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

    def test_fairness_beats_narrow_slot_count_without_losing_matching(self):
        import waitlist_service as wl

        now = datetime.now(TZ)
        m1 = self.mid("Стиральная №1")

        # Two high-priority users should receive the two available slots.
        # A frequently-washing user with a narrow interval must not jump
        # ahead merely because len(allowed) is smaller.
        high_flexible = wl.Request(
            id=9001,
            user_id=9001,
            tg_id=9001,
            mode="auto",
            any_machine=False,
            priority_since=now - timedelta(hours=80),
            intervals=[(20, 22)],
            machines={m1},
            usage_points=0,
        )
        high_narrow = wl.Request(
            id=9002,
            user_id=9002,
            tg_id=9002,
            mode="auto",
            any_machine=False,
            priority_since=now - timedelta(hours=78),
            intervals=[(21, 22)],
            machines={m1},
            usage_points=0,
        )
        low_narrow = wl.Request(
            id=9003,
            user_id=9003,
            tg_id=9003,
            mode="auto",
            any_machine=False,
            priority_since=now - timedelta(hours=70),
            intervals=[(20, 21)],
            machines={m1},
            usage_points=12,
        )

        matches = wl._match(
            [high_flexible, high_narrow, low_narrow],
            [(m1, 20), (m1, 21)],
        )

        self.assertEqual(set(matches), {9001, 9002})
        self.assertEqual(len(set(matches.values())), 2)


    def test_auto_booking_requires_thirty_minutes_notice(self):
        import waitlist_service as wl

        now = datetime(2026, 10, 1, 10, 30, 0, tzinfo=TZ)

        self.assertTrue(
            wl._auto_booking_allowed("2026-10-01", 11, now)
        )
        self.assertFalse(
            wl._auto_booking_allowed(
                "2026-10-01",
                11,
                now + timedelta(seconds=1),
            )
        )
        self.assertTrue(
            wl._auto_booking_allowed("2026-10-02", 7, now)
        )


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


    def test_expired_but_still_active_hold_remains_busy_until_expiry_job_finishes(self):
        import database

        future = (datetime.now(TZ).date() + timedelta(days=1)).isoformat()
        uid = self.uid(1001)
        mid = self.mid("Стиральная №1")
        with database.get_conn() as conn:
            conn.execute(
                """
                INSERT INTO slot_holds
                (request_id,user_id,machine_id,date,hour,expires_at,context,status,created_at)
                VALUES (NULL,?,?,?,?,?,'night','active',?)
                """,
                (
                    uid,
                    mid,
                    future,
                    10,
                    (datetime.now(TZ) - timedelta(seconds=5)).isoformat(timespec="seconds"),
                    datetime.now(TZ).isoformat(timespec="seconds"),
                ),
            )

        # The slot becomes free only when expire_hold changes status away from
        # active. This closes the small race between simultaneous HOLD jobs.
        self.assertNotIn(10, database.get_free_hours_effective(mid, future))


    def test_waitlist_interval_ui_uses_last_possible_start(self):
        from handlers import pra4ka2 as ui

        self.assertEqual(ui._interval_end_choices(18), [18, 19, 20, 21, 22])
        self.assertEqual(ui._interval_end_choices(22), [22])
        self.assertEqual(ui._format_intervals([(18, 23)]), "18:00-22:00")
        self.assertEqual(ui._format_intervals([(22, 23)]), "22:00")


    def test_flexible_schedule_uses_different_hours_per_weekday(self):
        import waitlist_service as wl

        first_date = datetime.now(TZ).date() + timedelta(days=1)
        second_date = first_date + timedelta(days=1)
        first_weekday = first_date.weekday()
        second_weekday = second_date.weekday()

        rid = wl.save_request(
            1001,
            [],
            [],
            True,
            "auto",
            schedule={
                first_weekday: [(10, 22)],
                second_weekday: [(18, 22)],
            },
        )
        self.assertTrue(rid)

        req = next(r for r in wl._active_requests() if r.tg_id == 1001)
        self.assertTrue(req.accepts_date(first_date.isoformat()))
        self.assertTrue(req.accepts_date(second_date.isoformat()))
        self.assertTrue(req.accepts_hour(10, first_date.isoformat()))
        self.assertFalse(req.accepts_hour(10, second_date.isoformat()))
        self.assertTrue(req.accepts_hour(19, second_date.isoformat()))

        m1 = self.mid("Стиральная №1")
        morning_match = wl._match(
            [req],
            [(m1, 10)],
            date_iso=second_date.isoformat(),
        )
        evening_match = wl._match(
            [req],
            [(m1, 19)],
            date_iso=second_date.isoformat(),
        )
        self.assertNotIn(req.id, morning_match)
        self.assertEqual(evening_match[req.id], (m1, 19))

        with database.get_conn() as conn:
            rows = conn.execute(
                """
                SELECT weekday,start_hour,end_hour
                FROM waitlist_schedule
                WHERE request_id=?
                ORDER BY weekday,start_hour
                """,
                (rid,),
            ).fetchall()
        self.assertEqual(
            [(int(d), int(a), int(b)) for d, a, b in rows],
            [
                (first_weekday, 10, 22),
                (second_weekday, 18, 22),
            ] if first_weekday < second_weekday else [
                (second_weekday, 18, 22),
                (first_weekday, 10, 22),
            ],
        )


    def test_schedule_edit_keeps_waitlist_priority(self):
        import waitlist_service as wl

        day = (datetime.now(TZ).date() + timedelta(days=1)).weekday()
        rid = wl.save_request(
            1001,
            [],
            [],
            True,
            "auto",
            schedule={day: [(10, 22)]},
        )
        old_priority = "2026-09-20T12:00:00+03:00"
        with database.get_conn() as conn:
            conn.execute(
                "UPDATE waitlist_requests SET priority_since=? WHERE id=?",
                (old_priority, rid),
            )

        rid2 = wl.save_request(
            1001,
            [],
            [],
            True,
            "auto",
            schedule={day: [(18, 22)]},
        )
        self.assertEqual(rid, rid2)
        with database.get_conn() as conn:
            priority = conn.execute(
                "SELECT priority_since FROM waitlist_requests WHERE id=?",
                (rid,),
            ).fetchone()[0]
        self.assertEqual(str(priority), old_priority)


    def test_legacy_waitlist_schedule_is_migrated_without_changing_availability(self):
        import waitlist_service as wl

        allowed_day = (datetime.now(TZ).date() + timedelta(days=1)).weekday()
        rid = wl.save_request(
            1001,
            [(18, 22)],
            [],
            True,
            "auto",
            weekdays=[allowed_day],
        )

        # Simulate a request created by the previous bot version.
        with database.get_conn() as conn:
            conn.execute("DELETE FROM waitlist_schedule WHERE request_id=?", (rid,))

        database.ensure_pra4ka2_tables()

        with database.get_conn() as conn:
            rows = conn.execute(
                """
                SELECT weekday,start_hour,end_hour
                FROM waitlist_schedule
                WHERE request_id=?
                """,
                (rid,),
            ).fetchall()
        self.assertEqual(
            [(int(d), int(a), int(b)) for d, a, b in rows],
            [(allowed_day, 18, 22)],
        )


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


    def test_startup_repairs_active_priority_older_than_last_wash(self):
        import waitlist_service as wl

        rid = wl.save_request(1001, [(10, 12)], [], True, "auto")
        uid = self.uid(1001)
        now = datetime.now(TZ)
        old_priority = now - timedelta(days=3)
        wash_start = now - timedelta(days=1)

        with database.get_conn() as conn:
            conn.execute(
                "UPDATE waitlist_requests SET priority_since=? WHERE id=?",
                (old_priority.isoformat(timespec="seconds"), rid),
            )
            conn.execute(
                """
                INSERT INTO laundry_usage_history(user_id,booking_id,occurred_at)
                VALUES (?,?,?)
                """,
                (uid, 999999, wash_start.isoformat(timespec="seconds")),
            )

        asyncio.run(wl.rebuild_waitlist_jobs())

        with database.get_conn() as conn:
            priority = conn.execute(
                "SELECT priority_since FROM waitlist_requests WHERE id=?",
                (rid,),
            ).fetchone()[0]

        repaired = datetime.fromisoformat(str(priority))
        if repaired.tzinfo is None:
            repaired = repaired.replace(tzinfo=TZ)
        expected = wash_start + timedelta(hours=1)
        self.assertLess(abs((repaired - expected).total_seconds()), 2)


    def test_subscription_reactivates_after_successful_wash_with_new_priority(self):
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

        finish = wl._booking_end(future, 10)
        reactivated = asyncio.run(
            wl.resume_subscription_after_wash(
                request_id,
                booking.booking_id,
                now=finish + timedelta(seconds=1),
                redistribute=False,
            )
        )
        self.assertTrue(reactivated)

        with database.get_conn() as conn:
            active = conn.execute(
                "SELECT status,matched_booking_id,priority_since FROM waitlist_requests WHERE id=?",
                (request_id,),
            ).fetchone()
        self.assertEqual(str(active[0]), "active")
        self.assertIsNone(active[1])
        self.assertEqual(str(active[2]), finish.isoformat(timespec="seconds"))


    def test_subscription_created_during_existing_booking_starts_priority_after_wash(self):
        import booking_service as bs
        import waitlist_service as wl

        future = (datetime.now(TZ).date() + timedelta(days=1)).isoformat()
        uid = self.uid(1001)
        mid = self.mid("Стиральная №1")
        booking = asyncio.run(bs.create_booking_safe(uid, mid, future, 10))

        request_id = wl.save_request(
            1001,
            [(18, 20)],
            [],
            True,
            "notify",
        )
        finish = wl._booking_end(future, 10)

        with database.get_conn() as conn:
            paused = conn.execute(
                "SELECT status,priority_since FROM waitlist_requests WHERE id=?",
                (request_id,),
            ).fetchone()
        self.assertEqual(str(paused[0]), "paused")
        self.assertEqual(str(paused[1]), finish.isoformat(timespec="seconds"))

        reactivated = asyncio.run(
            wl.resume_subscription_after_wash(
                request_id,
                booking.booking_id,
                now=finish + timedelta(seconds=1),
                redistribute=False,
            )
        )
        self.assertTrue(reactivated)

        with database.get_conn() as conn:
            active = conn.execute(
                "SELECT status,priority_since FROM waitlist_requests WHERE id=?",
                (request_id,),
            ).fetchone()
        self.assertEqual(str(active[0]), "active")
        self.assertEqual(str(active[1]), finish.isoformat(timespec="seconds"))


    def test_cancelling_preexisting_booking_activates_paused_subscription_now(self):
        import booking_service as bs
        import waitlist_service as wl

        future = (datetime.now(TZ).date() + timedelta(days=1)).isoformat()
        uid = self.uid(1001)
        mid = self.mid("Стиральная №1")
        booking = asyncio.run(bs.create_booking_safe(uid, mid, future, 10))
        request_id = wl.save_request(1001, [(18, 20)], [], True, "auto")

        before = datetime.now(TZ).replace(microsecond=0)
        cancelled = asyncio.run(bs.cancel_booking_safe(booking.booking_id))
        after = datetime.now(TZ).replace(microsecond=0)
        self.assertTrue(cancelled.waitlist_reopened)

        with database.get_conn() as conn:
            active = conn.execute(
                "SELECT status,matched_booking_id,priority_since FROM waitlist_requests WHERE id=?",
                (request_id,),
            ).fetchone()
        priority = datetime.fromisoformat(str(active[2]))
        if priority.tzinfo is None:
            priority = priority.replace(tzinfo=TZ)

        self.assertEqual(str(active[0]), "active")
        self.assertIsNone(active[1])
        self.assertGreaterEqual(priority, before)
        self.assertLessEqual(priority, after)


    def test_same_surname_room_accounts_stay_independent(self):
        import waitlist_service as wl

        database.save_user(2001, "Иванов", "101")

        first = wl.save_request(1001, [(18, 20)], [], True, "auto")
        second = wl.save_request(2001, [(18, 20)], [], True, "auto")
        self.assertTrue(first)
        self.assertTrue(second)

        active = [r for r in wl._active_requests() if r.tg_id in {1001, 2001}]
        self.assertEqual(len(active), 2)

    def test_same_surname_room_accounts_have_separate_usage_history(self):
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
        self.assertEqual(scores[second_uid], 0)

    def test_future_booking_pauses_only_same_account_subscription(self):
        import booking_service as bs
        import waitlist_service as wl

        database.save_user(2001, "Иванов", "101")
        future = (datetime.now(TZ).date() + timedelta(days=1)).isoformat()
        mid = self.mid("Стиральная №1")

        first_uid = self.uid(1001)
        booking = asyncio.run(bs.create_booking_safe(first_uid, mid, future, 10))

        first = wl.save_request(1001, [(18, 20)], [], True, "auto")
        second = wl.save_request(2001, [(18, 20)], [], True, "auto")
        self.assertTrue(first)
        self.assertTrue(second)

        with database.get_conn() as conn:
            paused = conn.execute(
                """
                SELECT status,matched_booking_id,priority_since
                FROM waitlist_requests WHERE id=?
                """,
                (first,),
            ).fetchone()
            active = conn.execute(
                "SELECT status FROM waitlist_requests WHERE id=?",
                (second,),
            ).fetchone()

        expected_start = wl._booking_end(future, 10).isoformat(timespec="seconds")
        self.assertEqual(str(paused[0]), "paused")
        self.assertEqual(int(paused[1]), booking.booking_id)
        self.assertEqual(str(paused[2]), expected_start)
        self.assertEqual(str(active[0]), "active")
        self.assertNotIn(first, {r.id for r in wl._active_requests()})

        wl.cancel_request_for_tg(1001)
        wl.cancel_request_for_tg(2001)

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


    def test_ban_cancels_waitlist_and_active_hold_but_keeps_booking(self):
        import booking_service as bs
        import waitlist_service as wl

        future = (datetime.now(TZ).date() + timedelta(days=1)).isoformat()
        uid = self.uid(1001)
        mid = self.mid("Стиральная №1")

        rid = wl.save_request(1001, [(10, 12)], [mid], False, "notify")
        expires = datetime.now(TZ) + timedelta(minutes=2)

        with database.get_conn() as conn:
            cur_booking = conn.execute(
                "INSERT INTO bookings(user_id,machine_id,date,hour) VALUES (?,?,?,?)",
                (uid, mid, future, 9),
            )
            booking_id = getattr(cur_booking, "lastrowid", None)
            if not booking_id:
                booking_id = conn.execute(
                    "SELECT id FROM bookings WHERE user_id=? AND machine_id=? AND date=? AND hour=?",
                    (uid, mid, future, 9),
                ).fetchone()[0]

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

        cleanup = database.ban_user(1001, reason="test", days=7)
        self.assertEqual(cleanup["cancelled_requests"], 1)
        self.assertEqual(len(cleanup["holds"]), 1)

        with database.get_conn() as conn:
            req_status = conn.execute(
                "SELECT status FROM waitlist_requests WHERE id=?",
                (rid,),
            ).fetchone()[0]
            hold_status = conn.execute(
                "SELECT status FROM slot_holds WHERE id=?",
                (int(hold_id),),
            ).fetchone()[0]
            booking_exists = conn.execute(
                "SELECT 1 FROM bookings WHERE id=?",
                (int(booking_id),),
            ).fetchone()

        self.assertEqual(str(req_status), "cancelled")
        self.assertEqual(str(hold_status), "cancelled")
        self.assertIsNotNone(booking_exists)


    def test_legacy_active_request_is_excluded_if_user_already_has_future_wash(self):
        import booking_service as bs
        import waitlist_service as wl

        tomorrow = (datetime.now(TZ).date() + timedelta(days=1)).isoformat()
        later = (datetime.now(TZ).date() + timedelta(days=3)).isoformat()
        uid = self.uid(1001)
        mid = self.mid("Стиральная №1")

        # Simulate an old active request that existed before the future-booking guard.
        rid = wl.save_request(1001, [(7, 9)], [mid], False, "auto")
        asyncio.run(bs.create_booking_safe(uid, mid, tomorrow, 7, close_waitlist=False))

        requests = wl._active_requests()
        self.assertNotIn(rid, {r.id for r in requests})

        matched = asyncio.run(wl.distribute_date(later, context="day"))
        self.assertEqual(matched, 0)

        with database.get_conn() as conn:
            future_washes = conn.execute(
                """
                SELECT COUNT(*)
                FROM bookings b
                JOIN machines m ON m.id=b.machine_id
                WHERE b.user_id=? AND m.type='wash' AND b.date>=?
                """,
                (uid, datetime.now(TZ).date().isoformat()),
            ).fetchone()[0]
        self.assertEqual(int(future_washes), 1)


if __name__ == "__main__":
    unittest.main()
