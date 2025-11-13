# database.py
import os
import base64
import hashlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config import DB_PATH, WORKING_HOURS, ADMIN_IDS, TIMEZONE

TZ = ZoneInfo(TIMEZONE)

# ---------- админ-утилиты ----------
def _admin_set_from_config():
    raw = ADMIN_IDS
    if isinstance(raw, (list, tuple, set)):
        items = [str(x) for x in raw]
    else:
        s = str(raw).strip()
        if s.startswith("[") and s.endswith("]"):
            s = s[1:-1]
        items = [p.strip().strip("'").strip('"') for p in s.split(",") if p.strip()]
    return set(items)

def is_admin(user_id: int | str) -> bool:
    try:
        return str(int(user_id)) in _admin_set_from_config()
    except Exception:
        return str(user_id).strip() in _admin_set_from_config()

# ---------- кодирование фамилии/комнаты ----------
def _b64e(s: str | None) -> str | None:
    if s is None:
        return None
    return base64.b64encode(s.encode("utf-8")).decode("ascii")

def _b64d_try(s: str | None) -> str | None:
    if s is None:
        return None
    try:
        return base64.b64decode(s).decode("utf-8")
    except Exception:
        return s

# ---------- TG-заглушки (для ручных добавлений по Фамилия+Комната) ----------
def _stub_tg_id(surname: str, room: str) -> int:
    seed = f"{surname}|{room}".encode("utf-8")
    val = int.from_bytes(hashlib.sha256(seed).digest()[:8], "big")
    return -max(1, val % 10**11)  # отрицательный, но уникальный

def ensure_user_by_surname_room(surname: str, room: str) -> int:
    """Возвращает id пользователя. Если его нет — создаёт 'стаб' с фиктивным tg_id."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE surname=? AND room=?",
            (_b64e(surname), _b64e(room)),
        ).fetchone()
        if row:
            return row[0]
        tg_stub = _stub_tg_id(surname, room)
        conn.execute(
            "INSERT INTO users (tg_id, surname, room) VALUES (?, ?, ?)",
            (tg_stub, _b64e(surname), _b64e(room)),
        )
        return conn.execute("SELECT id FROM users WHERE tg_id=?", (tg_stub,)).fetchone()[0]

def get_machine_id_by_name(name: str) -> int | None:
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM machines WHERE name=?", (name,)).fetchone()
        return row[0] if row else None

# ---------- выбор backend: Postgres или SQLite ----------
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

def _rewrite_qmarks(sql: str) -> str:
    # SQLite использует '?', Postgres — %s
    return sql.replace("?", "%s")

def _rewrite_insert_or_ignore(sql: str) -> str:
    s = sql.lstrip()
    if s.upper().startswith("INSERT OR IGNORE"):
        s = "INSERT" + s[len("INSERT OR IGNORE"):]
        s = s + " ON CONFLICT DO NOTHING"
        return sql[:len(sql) - len(sql.lstrip())] + s
    return sql

class _CursorWrapper:
    def __init__(self, cur): self._cur = cur
    def fetchone(self): return self._cur.fetchone()
    def fetchall(self): return self._cur.fetchall()
    @property
    def lastrowid(self): return getattr(self._cur, "lastrowid", None)
    def close(self):
        try: self._cur.close()
        except Exception: pass

if DATABASE_URL:
    import psycopg2
    from psycopg2 import pool
    _pg_pool = pool.SimpleConnectionPool(1, 10, DATABASE_URL)

    class _PgConn:
        def __init__(self):
            self._conn = _pg_pool.getconn()
            self._conn.autocommit = True
            self._opened = []

        def execute(self, sql: str, params=()):
            sql = _rewrite_insert_or_ignore(sql)
            sql = _rewrite_qmarks(sql)
            cur = self._conn.cursor()
            cur.execute(sql, params)
            w = _CursorWrapper(cur)
            self._opened.append(w)
            return w

        def commit(self): pass
        def close(self):
            for w in self._opened: w.close()
            _pg_pool.putconn(self._conn)
        def __enter__(self): return self
        def __exit__(self, exc_type, exc, tb): self.close()

    def get_conn(): return _PgConn()

else:
    import sqlite3
    class _SqliteConn:
        def __init__(self):
            self._conn = sqlite3.connect(DB_PATH)
            self._conn.execute("PRAGMA foreign_keys=ON")  # важно для каскадов

        def execute(self, *args, **kwargs):
            return self._conn.execute(*args, **kwargs)
        def commit(self): self._conn.commit()
        def close(self): self._conn.close()
        def __enter__(self): return self
        def __exit__(self, exc_type, exc, tb):
            try:
                if exc_type is None: self._conn.commit()
                else: self._conn.rollback()
            finally:
                self._conn.close()

    def get_conn(): return _SqliteConn()

def ensure_reminders_table():
    if DATABASE_URL:
        with get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reminders_sent (
                    user_id INTEGER NOT NULL,
                    machine_id INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    hour INTEGER NOT NULL,
                    minutes_before INTEGER NOT NULL,
                    sent_at TIMESTAMPTZ DEFAULT now(),
                    UNIQUE (user_id, machine_id, date, hour, minutes_before)
                );
            """)
    else:
        with get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reminders_sent (
                    user_id INTEGER NOT NULL,
                    machine_id INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    hour INTEGER NOT NULL,
                    minutes_before INTEGER NOT NULL,
                    sent_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
                    UNIQUE (user_id, machine_id, date, hour, minutes_before)
                );
            """)

# ---------- инициализация схемы ----------
def init_db():
    if DATABASE_URL:
        ddl = [
            """
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                tg_id BIGINT UNIQUE NOT NULL,
                surname TEXT,
                room TEXT,
                username TEXT
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS machines (
                id SERIAL PRIMARY KEY,
                type TEXT NOT NULL CHECK (type IN ('wash','dry')),
                name TEXT NOT NULL UNIQUE
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                machine_id INTEGER NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
                date DATE NOT NULL,
                hour INTEGER NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now(),
                UNIQUE (machine_id, date, hour)
            );
            """,
            "CREATE INDEX IF NOT EXISTS idx_bookings_user_date ON bookings (user_id, date);",
            "CREATE INDEX IF NOT EXISTS idx_bookings_machine_date ON bookings (machine_id, date);",
        ]
    else:
        ddl = [
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER UNIQUE NOT NULL,
                surname TEXT,
                room TEXT,
                username TEXT
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS machines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                name TEXT NOT NULL UNIQUE
            );
            """,
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                machine_id INTEGER NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
                date TEXT NOT NULL,
                hour INTEGER NOT NULL,
                created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
                UNIQUE (machine_id, date, hour)
            );
            """,
            "CREATE INDEX IF NOT EXISTS idx_bookings_user_date ON bookings (user_id, date);",
            "CREATE INDEX IF NOT EXISTS idx_bookings_machine_date ON bookings (machine_id, date);",
        ]
    with get_conn() as conn:
        for stmt in ddl: conn.execute(stmt)
    ensure_ban_tables()
    ensure_reminders_table()

# ---------- бан/антиспам ----------
def ensure_ban_tables():
    if DATABASE_URL:
        with get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS banned (
                    tg_id BIGINT UNIQUE NOT NULL,
                    reason TEXT,
                    banned_until TEXT,
                    banned_at TIMESTAMPTZ DEFAULT now()
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS failed_attempts (
                    tg_id BIGINT UNIQUE NOT NULL,
                    count INTEGER DEFAULT 0,
                    last_attempt TIMESTAMPTZ DEFAULT now()
                );
            """)
            conn.execute("ALTER TABLE banned ALTER COLUMN tg_id TYPE BIGINT USING tg_id::bigint;")
            conn.execute("ALTER TABLE failed_attempts ALTER COLUMN tg_id TYPE BIGINT USING tg_id::bigint;")
    else:
        with get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS banned (
                    tg_id INTEGER UNIQUE NOT NULL,
                    reason TEXT,
                    banned_until TEXT,
                    banned_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS failed_attempts (
                    tg_id INTEGER UNIQUE NOT NULL,
                    count INTEGER DEFAULT 0,
                    last_attempt TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
                );
            """)

def ban_user(tg_id: int, reason: str | None = None, days: int = 7):
    until = (datetime.now(TZ) + timedelta(days=days)).isoformat(timespec="seconds")
    banned_at = datetime.now(TZ).isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO banned (tg_id, reason, banned_until, banned_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(tg_id) DO UPDATE SET
                reason=excluded.reason,
                banned_until=excluded.banned_until,
                banned_at=excluded.banned_at
        """, (tg_id, reason or "Без причины", until, banned_at))

def is_banned(tg_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT banned_until FROM banned WHERE tg_id=?", (tg_id,)).fetchone()
        if not row: return False
        until = row[0]
        if not until: return False
        try:
            if datetime.fromisoformat(until) <= datetime.now(TZ):
                conn.execute("DELETE FROM banned WHERE tg_id=?", (tg_id,))
                return False
        except Exception:
            pass
        return True

def unban_user(tg_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM banned WHERE tg_id=?", (tg_id,))

def register_failed_attempt(tg_id: int) -> int:
    now = datetime.now(TZ).isoformat(timespec="seconds")
    with get_conn() as conn:
        row = conn.execute("SELECT count FROM failed_attempts WHERE tg_id=?", (tg_id,)).fetchone()
        count = (row[0] if row else 0) + 1
        conn.execute("""
            INSERT INTO failed_attempts (tg_id, count, last_attempt)
            VALUES (?, ?, ?)
            ON CONFLICT(tg_id) DO UPDATE SET
                count=excluded.count,
                last_attempt=excluded.last_attempt
        """, (tg_id, count, now))
    return count

def reset_failed_attempts(tg_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM failed_attempts WHERE tg_id=?", (tg_id,))

# ---------- пользователи ----------
def bind_stub_user_to_real(tg_id, surname, room):
    with get_conn() as conn:
        stub = conn.execute("SELECT id FROM users WHERE surname=? AND room=?",
                            (_b64e(surname), _b64e(room))).fetchone()
        if not stub: return
        stub_id = stub[0]

        conn.execute("""
            INSERT INTO users (tg_id, surname, room)
            VALUES (?, ?, ?)
            ON CONFLICT(tg_id) DO UPDATE SET surname=excluded.surname, room=excluded.room
        """, (tg_id, _b64e(surname), _b64e(room)))

        real_id = conn.execute("SELECT id FROM users WHERE tg_id=?", (tg_id,)).fetchone()[0]
        conn.execute("UPDATE bookings SET user_id=? WHERE user_id=?", (real_id, stub_id))
        conn.execute("DELETE FROM users WHERE id=?", (stub_id,))

def add_user(tg_id, surname, room):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (tg_id, surname, room) VALUES (?, ?, ?)",
            (tg_id, _b64e(surname), _b64e(room))
        )

def save_user(tg_id, surname, room):
    bind_stub_user_to_real(tg_id, surname, room)
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO users (tg_id, surname, room)
            VALUES (?, ?, ?)
            ON CONFLICT(tg_id) DO UPDATE SET
                surname=excluded.surname,
                room=excluded.room
        """, (tg_id, _b64e(surname), _b64e(room)))

def update_username(tg_id: int, username: str | None):
    if not username: return
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO users (tg_id, username)
            VALUES (?, ?)
            ON CONFLICT(tg_id) DO UPDATE SET username=excluded.username
        """, (tg_id, username))

def tg_id_by_username(username: str) -> int | None:
    u = username.lstrip("@")
    with get_conn() as conn:
        row = conn.execute("SELECT tg_id FROM users WHERE LOWER(username)=LOWER(?) LIMIT 1", (u,)).fetchone()
        return row[0] if row else None

def get_user(tg_id):
    with get_conn() as conn:
        row = conn.execute("SELECT id, tg_id, surname, room FROM users WHERE tg_id=?", (tg_id,)).fetchone()
        if not row: return None
        return (row[0], row[1], _b64d_try(row[2]), _b64d_try(row[3]))

def get_incomplete_users():
    """Пользователи без фамилии или комнаты."""
    with get_conn() as conn:
        return conn.execute("""
            SELECT tg_id, COALESCE(username, '')
            FROM users
            WHERE surname IS NULL OR room IS NULL
        """).fetchall()

# ---------- машины/бронирования ----------
def add_machine(type_, name):
    with get_conn() as conn:
        conn.execute("INSERT INTO machines (type, name) VALUES (?, ?)", (type_, name))

def get_machines_by_type(type_):
    with get_conn() as conn:
        return conn.execute("SELECT id, type, name FROM machines WHERE type=?", (type_,)).fetchall()

def get_user_bookings_today(user_id, date_iso, machine_type):
    with get_conn() as conn:
        row = conn.execute("""
            SELECT 1
            FROM bookings b
            JOIN machines m ON m.id = b.machine_id
            WHERE b.user_id = ? AND b.date = ? AND m.type = ?
            LIMIT 1
        """, (user_id, date_iso, machine_type)).fetchone()
    return bool(row)

def get_user_booking_exact(user_id: int, machine_id: int, date_iso: str, hour: int) -> bool:
    with get_conn() as conn:
        row = conn.execute("""
            SELECT 1 FROM bookings
            WHERE user_id=? AND machine_id=? AND date=? AND hour=?
            LIMIT 1
        """, (user_id, machine_id, date_iso, hour)).fetchone()
    return bool(row)

def get_free_hours(machine_id, date_iso):
    with get_conn() as conn:
        busy = {r[0] for r in conn.execute(
            "SELECT hour FROM bookings WHERE machine_id=? AND date=?",
            (machine_id, date_iso)
        ).fetchall()}
    return [h for h in WORKING_HOURS if h not in busy]

def create_booking(user_id, machine_id, date_iso, hour):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO bookings (user_id, machine_id, date, hour)
            VALUES (?, ?, ?, ?)
        """, (user_id, machine_id, date_iso, hour))

def cleanup_old_bookings():
    today = datetime.now(TZ).date()
    cutoff = today - timedelta(days=1)
    with get_conn() as conn:
        conn.execute("DELETE FROM bookings WHERE date < ?", (cutoff.isoformat(),))

def was_reminder_sent(user_id: int, machine_id: int, date_iso: str, hour: int, minutes_before: int) -> bool:
    with get_conn() as conn:
        row = conn.execute("""
            SELECT 1 FROM reminders_sent
             WHERE user_id=? AND machine_id=? AND date=? AND hour=? AND minutes_before=?
             LIMIT 1
        """, (user_id, machine_id, date_iso, hour, minutes_before)).fetchone()
    return bool(row)

def mark_reminder_sent(user_id: int, machine_id: int, date_iso: str, hour: int, minutes_before: int):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO reminders_sent (user_id, machine_id, date, hour, minutes_before)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, machine_id, date, hour, minutes_before) DO NOTHING
        """, (user_id, machine_id, date_iso, hour, minutes_before))
