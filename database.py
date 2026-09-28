# database.py
import os
import base64
import hashlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config import DB_PATH, WORKING_HOURS, ADMIN_IDS, TIMEZONE

TZ = ZoneInfo(TIMEZONE)

class DBUnavailable(Exception):
    """База недоступна (Neon sleep / сеть / connect timeout)."""
    pass

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
    """Возвращает id пользователя. Если его нет - создаёт 'стаб' с фиктивным tg_id."""
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

def set_machine_active(machine_id: int, active: bool) -> None:
    """
    Включить/выключить машину.
    active=True  -> машина доступна в /book
    active=False -> скрыта из записи, но старые записи и напоминания живут.
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE machines SET is_active=? WHERE id=?",
            (bool(active), machine_id),
        )


def get_all_machines():
    """
    Все машины для админки (и активные, и выключенные).
    """
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, type, name, is_active FROM machines ORDER BY type, name"
        ).fetchall()


# ---------- выбор backend: Postgres или SQLite ----------
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

def _rewrite_qmarks(sql: str) -> str:
    # SQLite использует '?', Postgres - %s
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


'''
if DATABASE_URL:
    import psycopg2
    from psycopg2 import OperationalError

    def _pg_connect():
        # важно: маленький таймаут, чтобы не "висеть" на первом запросе
        # sslmode обычно уже в DATABASE_URL у Neon, но connect_timeout лишним не будет
        return psycopg2.connect(DATABASE_URL, connect_timeout=3)

    class _PgConn:
        def __init__(self):
            try:
                self._conn = _pg_connect()
            except OperationalError as e:
                raise DBUnavailable(str(e)) from e
            self._conn.autocommit = True
            self._opened = []

        def execute(self, sql: str, params=()):
            sql = _rewrite_insert_or_ignore(sql)
            sql = _rewrite_qmarks(sql)
            try:
                cur = self._conn.cursor()
                cur.execute(sql, params)
            except OperationalError as e:
                raise DBUnavailable(str(e)) from e

            w = _CursorWrapper(cur)
            self._opened.append(w)
            return w

        def close(self):
            for w in self._opened:
                try:
                    w.close()
                except Exception:
                    pass
            self._opened.clear()
            try:
                self._conn.close()
            except Exception:
                pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self.close()

    def get_conn() -> _PgConn:
        return _PgConn()

    import psycopg2
    from psycopg2 import pool

    _pg_pool = pool.SimpleConnectionPool(1, 10, DATABASE_URL)

    class _PgConn:
        def __init__(self):
            # сразу берём коннект из пула
            self._conn = _pg_pool.getconn()
            self._conn.autocommit = True
            self._opened: list[_CursorWrapper] = []

        def _reset_conn(self):
            """
            Пересоздаёт соединение, если старое умерло
            (Neon его закрыл при scale-to-zero и т.п.).
            """
            # закрываем все открытые курсоры, если были
            for w in self._opened:
                try:
                    w.close()
                except Exception:
                    pass
            self._opened.clear()

            try:
                if self._conn is not None:
                    # пытаемся аккуратно вернуть коннект в пул и закрыть его
                    _pg_pool.putconn(self._conn, close=True)
            except Exception:
                # если пул уже в неадеквате - просто закрываем
                try:
                    self._conn.close()
                except Exception:
                    pass

            # берём новый коннект
            self._conn = _pg_pool.getconn()
            self._conn.autocommit = True

        def execute(self, sql: str, params=()):
            sql = _rewrite_insert_or_ignore(sql)
            sql = _rewrite_qmarks(sql)

            # если коннект уже помечен как закрытый - пересоздаём заранее
            if getattr(self._conn, "closed", 0):
                self._reset_conn()

            try:
                cur = self._conn.cursor()
                cur.execute(sql, params)
            except psycopg2.OperationalError:
                # соединение умерло (например, "SSL connection has been closed unexpectedly")
                # -> пересоздаём и пробуем ещё раз
                self._reset_conn()
                cur = self._conn.cursor()
                cur.execute(sql, params)

            w = _CursorWrapper(cur)
            self._opened.append(w)
            return w

        def commit(self):
            # autocommit=True, поэтому ничего не делаем
            pass

        def close(self):
            # закрываем все обёртки курсоров
            for w in self._opened:
                try:
                    w.close()
                except Exception:
                    pass
            self._opened.clear()

            # возвращаем коннект в пул (без close=True - он живой и пригодится)
            try:
                _pg_pool.putconn(self._conn)
            except Exception:
                # если что-то пошло не так - просто закрываем
                try:
                    self._conn.close()
                except Exception:
                    pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self.close()

    def get_conn() -> _PgConn:
        return _PgConn()
    '''
if DATABASE_URL:
    import psycopg2
    from psycopg2 import OperationalError
    from psycopg2 import pool

    # держим несколько постоянных коннектов, без пересоздания на каждый SELECT
    _pg_pool = pool.SimpleConnectionPool(
        1, 10,  # min/max
        DATABASE_URL,
        connect_timeout=3,
    )

    class _PgConn:
        def __init__(self):
            try:
                self._conn = _pg_pool.getconn()
            except Exception as e:
                raise DBUnavailable(str(e)) from e
            self._conn.autocommit = True
            self._opened: list[_CursorWrapper] = []

        def _reset_conn(self):
            try:
                _pg_pool.putconn(self._conn, close=True)
            except Exception:
                try:
                    self._conn.close()
                except Exception:
                    pass
            self._conn = _pg_pool.getconn()
            self._conn.autocommit = True

        def execute(self, sql: str, params=()):
            sql = _rewrite_insert_or_ignore(sql)
            sql = _rewrite_qmarks(sql)
            try:
                cur = self._conn.cursor()
                cur.execute(sql, params)
            except OperationalError as e:
                # Neon/сеть могло прибить коннект - пересоздаём и повторяем 1 раз
                self._reset_conn()
                cur = self._conn.cursor()
                cur.execute(sql, params)

            w = _CursorWrapper(cur)
            self._opened.append(w)
            return w

        def close(self):
            for w in self._opened:
                try:
                    w.close()
                except Exception:
                    pass
            self._opened.clear()
            try:
                _pg_pool.putconn(self._conn)
            except Exception:
                try:
                    self._conn.close()
                except Exception:
                    pass

        def __enter__(self): return self
        def __exit__(self, exc_type, exc, tb): self.close()

    def get_conn() -> _PgConn:
        return _PgConn()

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
    """
    Таблица для антидубликатов напоминаний.
    Храним именно tg_id (а не users.id).
    Если старая таблица была с другим набором колонок, она будет
    пересоздана (данные о прошлых напоминаниях нам не критичны).
    """
    with get_conn() as conn:
        # На случай старой схемы - пересоздаём таблицу.
        #conn.execute("DROP TABLE IF EXISTS reminders_sent")

        if DATABASE_URL:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reminders_sent (
                    tg_id          BIGINT      NOT NULL,
                    machine_id     INTEGER     NOT NULL,
                    date           DATE        NOT NULL,
                    hour           INTEGER     NOT NULL,
                    minutes_before INTEGER     NOT NULL,
                    sent_at        TIMESTAMPTZ DEFAULT now(),
                    PRIMARY KEY (tg_id, machine_id, date, hour, minutes_before)
                );
            """)
        else:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reminders_sent (
                    tg_id          INTEGER     NOT NULL,
                    machine_id     INTEGER     NOT NULL,
                    date           TEXT        NOT NULL,
                    hour           INTEGER     NOT NULL,
                    minutes_before INTEGER     NOT NULL,
                    sent_at        TEXT        DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
                    PRIMARY KEY (tg_id, machine_id, date, hour, minutes_before)
                );
            """)

def ensure_machines_active_column():
    """
    Добавляем колонку is_active в таблицу machines, если её ещё нет.
    TRUE / 1 = машина работает и доступна в /book.
    """
    with get_conn() as conn:
        if DATABASE_URL:
            # Postgres: есть IF NOT EXISTS
            conn.execute("""
                ALTER TABLE machines
                ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
            """)
        else:
            # SQLite: IF NOT EXISTS нет, просто ловим ошибку «duplicate column name»
            try:
                conn.execute("""
                    ALTER TABLE machines
                    ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1;
                """)
            except Exception:
                pass


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
    ensure_machines_active_column()
    ensure_pra4ka2_tables()

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
            try:
                conn.execute("ALTER TABLE banned ALTER COLUMN tg_id TYPE BIGINT USING tg_id::bigint;")
            except Exception:
                pass

            try:
                conn.execute("ALTER TABLE failed_attempts ALTER COLUMN tg_id TYPE BIGINT USING tg_id::bigint;")
            except Exception:
                pass

            #conn.execute("ALTER TABLE banned ALTER COLUMN tg_id TYPE BIGINT USING tg_id::bigint;")
            #conn.execute("ALTER TABLE failed_attempts ALTER COLUMN tg_id TYPE BIGINT USING tg_id::bigint;")
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
        stub = conn.execute("SELECT id FROM users WHERE surname=? AND room=? AND tg_id < 0",
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
        conn.execute("UPDATE laundry_usage_history SET user_id=? WHERE user_id=?", (real_id, stub_id))
        conn.execute("DELETE FROM users WHERE id=?", (stub_id,))

def add_user(tg_id, surname, room):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (tg_id, surname, room) VALUES (?, ?, ?)",
            (tg_id, _b64e(surname), _b64e(room))
        )

def _resident_key(surname: str | None, room: str | None):
    if not surname or not room:
        return None
    normalized_surname = " ".join(str(surname).strip().casefold().split())
    normalized_room = str(room).strip()
    return normalized_surname, normalized_room


def find_resident_profile_conflict(tg_id: int, surname: str, room: str):
    """Return another real Telegram user claiming the same surname+room."""
    wanted = _resident_key(surname, room)
    if not wanted:
        return None
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id,tg_id,surname,room,username FROM users WHERE tg_id>0 AND tg_id<>?",
            (int(tg_id),),
        ).fetchall()
    for user_id, other_tg, enc_surname, enc_room, username in rows:
        if _resident_key(_b64d_try(enc_surname), _b64d_try(enc_room)) == wanted:
            return int(user_id), int(other_tg), username
    return None


def resident_user_ids(user_id: int) -> list[int]:
    """All real Telegram accounts that claim the same normalized surname+room."""
    with get_conn() as conn:
        current = conn.execute(
            "SELECT surname,room FROM users WHERE id=?",
            (int(user_id),),
        ).fetchone()
        if not current:
            return [int(user_id)]
        wanted = _resident_key(_b64d_try(current[0]), _b64d_try(current[1]))
        if not wanted:
            return [int(user_id)]
        rows = conn.execute(
            "SELECT id,surname,room FROM users WHERE tg_id>0 AND surname IS NOT NULL AND room IS NOT NULL"
        ).fetchall()
    ids = [
        int(uid)
        for uid, enc_surname, enc_room in rows
        if _resident_key(_b64d_try(enc_surname), _b64d_try(enc_room)) == wanted
    ]
    return sorted(set(ids or [int(user_id)]))


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
'''
def add_machine(type_, name):
    with get_conn() as conn:
        conn.execute("INSERT INTO machines (type, name) VALUES (?, ?)", (type_, name))
'''
def add_machine(type_, name):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO machines (type, name) VALUES (?, ?)",
            (type_, name)
        )
'''
def get_machines_by_type(type_):
    with get_conn() as conn:
        return conn.execute("SELECT id, type, name FROM machines WHERE type=?", (type_,)).fetchall()
'''

def get_machines_by_type(type_):
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, type, name FROM machines WHERE type=? AND is_active",
            (type_,)
        ).fetchall()

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

def daily_limit_reached(user_id, date_iso, machine_type):
    """Normal users get one booking per machine type per day; admins keep their exemption."""
    with get_conn() as conn:
        row = conn.execute("SELECT tg_id FROM users WHERE id=?", (int(user_id),)).fetchone()
    if row and is_admin(row[0]):
        return False
    return get_user_bookings_today(user_id, date_iso, machine_type)

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
    """Keep bookings for 7 days, usage history for 30 days."""
    record_usage_history()
    today = datetime.now(TZ).date()
    bookings_cutoff = today - timedelta(days=6)
    usage_cutoff = datetime.now(TZ) - timedelta(days=30)
    offers_cutoff = datetime.now(TZ) - timedelta(days=30)
    with get_conn() as conn:
        conn.execute("DELETE FROM bookings WHERE date < ?", (bookings_cutoff.isoformat(),))
        conn.execute(
            "DELETE FROM laundry_usage_history WHERE occurred_at < ?",
            (usage_cutoff.isoformat(timespec="seconds"),),
        )
        conn.execute(
            "DELETE FROM waitlist_offer_history WHERE created_at < ?",
            (offers_cutoff.isoformat(timespec="seconds"),),
        )
        conn.execute(
            "DELETE FROM slot_holds WHERE created_at < ? AND status<>'active'",
            (offers_cutoff.isoformat(timespec="seconds"),),
        )
        conn.execute(
            "DELETE FROM pending_waitlist_notifications WHERE created_at < ? AND sent=1",
            (offers_cutoff.isoformat(timespec="seconds"),),
        )
        conn.execute(
            "DELETE FROM waitlist_rounds WHERE target_date < ?",
            ((today - timedelta(days=30)).isoformat(),),
        )
        conn.execute(
            "DELETE FROM waitlist_requests WHERE status<>'active' AND updated_at < ?",
            (offers_cutoff.isoformat(timespec="seconds"),),
        )

def was_reminder_sent(
    tg_id: int, machine_id: int, date_iso: str, hour: int, minutes_before: int
) -> bool:
    """
    Проверяем факт отправки напоминания по связке:
    tg_id + machine_id + дата + час + минут_до.
    """
    with get_conn() as conn:
        row = conn.execute("""
            SELECT 1
              FROM reminders_sent
             WHERE tg_id=? AND machine_id=? AND date=? AND hour=? AND minutes_before=?
             LIMIT 1
        """, (tg_id, machine_id, date_iso, hour, minutes_before)).fetchone()
    return bool(row)


def mark_reminder_sent(
    tg_id: int, machine_id: int, date_iso: str, hour: int, minutes_before: int
) -> None:
    """
    Помечаем напоминание как отправленное.
    """
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO reminders_sent (tg_id, machine_id, date, hour, minutes_before)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(tg_id, machine_id, date, hour, minutes_before) DO NOTHING
        """, (tg_id, machine_id, date_iso, hour, minutes_before))


# =========================================================
# PRA4KA 2.0
# =========================================================

def ensure_pra4ka2_tables():
    """
    Additive schema only. Old bot versions can keep using users/machines/bookings.
    """
    statements = [
        """
        CREATE TABLE IF NOT EXISTS waitlist_requests (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            mode TEXT NOT NULL DEFAULT 'notify',
            status TEXT NOT NULL DEFAULT 'active',
            any_machine INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            priority_since TEXT NOT NULL,
            matched_booking_id INTEGER,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS waitlist_intervals (
            id INTEGER PRIMARY KEY,
            request_id INTEGER NOT NULL REFERENCES waitlist_requests(id) ON DELETE CASCADE,
            start_hour INTEGER NOT NULL,
            end_hour INTEGER NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS waitlist_machines (
            request_id INTEGER NOT NULL REFERENCES waitlist_requests(id) ON DELETE CASCADE,
            machine_id INTEGER NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
            PRIMARY KEY (request_id, machine_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS waitlist_weekdays (
            request_id INTEGER NOT NULL REFERENCES waitlist_requests(id) ON DELETE CASCADE,
            weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
            PRIMARY KEY (request_id, weekday)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS notification_settings (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            quiet_enabled INTEGER NOT NULL DEFAULT 0,
            quiet_start INTEGER NOT NULL DEFAULT 23,
            quiet_end INTEGER NOT NULL DEFAULT 8,
            earlier_offer_enabled INTEGER NOT NULL DEFAULT 1,
            reminder_30_enabled INTEGER NOT NULL DEFAULT 1
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS slot_holds (
            id INTEGER PRIMARY KEY,
            request_id INTEGER REFERENCES waitlist_requests(id) ON DELETE SET NULL,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            machine_id INTEGER NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
            date TEXT NOT NULL,
            hour INTEGER NOT NULL,
            expires_at TEXT NOT NULL,
            context TEXT NOT NULL DEFAULT 'day',
            status TEXT NOT NULL DEFAULT 'active',
            current_booking_id INTEGER,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS waitlist_offer_history (
            id INTEGER PRIMARY KEY,
            request_id INTEGER REFERENCES waitlist_requests(id) ON DELETE CASCADE,
            machine_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            hour INTEGER NOT NULL,
            result TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS waitlist_rounds (
            target_date TEXT PRIMARY KEY,
            cutoff_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS laundry_usage_history (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            booking_id INTEGER UNIQUE,
            occurred_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS pending_waitlist_notifications (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            booking_id INTEGER,
            text TEXT NOT NULL,
            send_at TEXT NOT NULL,
            sent INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_waitlist_status ON waitlist_requests(status, priority_since)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_waitlist_one_active_user ON waitlist_requests(user_id) WHERE status='active'",
        "CREATE INDEX IF NOT EXISTS idx_waitlist_intervals_req ON waitlist_intervals(request_id)",
        "CREATE INDEX IF NOT EXISTS idx_waitlist_weekdays_req ON waitlist_weekdays(request_id)",
        "CREATE INDEX IF NOT EXISTS idx_holds_slot ON slot_holds(machine_id, date, hour, status)",
        "CREATE INDEX IF NOT EXISTS idx_holds_user ON slot_holds(user_id, status)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_holds_one_active_slot ON slot_holds(machine_id, date, hour) WHERE status='active'",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_holds_one_active_user ON slot_holds(user_id) WHERE status='active'",
        "CREATE INDEX IF NOT EXISTS idx_usage_user_time ON laundry_usage_history(user_id, occurred_at)",
        "CREATE INDEX IF NOT EXISTS idx_pending_notice ON pending_waitlist_notifications(sent, send_at)",
    ]
    with get_conn() as conn:
        for stmt in statements:
            try:
                conn.execute(stmt)
            except Exception:
                # PostgreSQL SERIAL is not required here because all inserts use explicit
                # auto-generated ids only where the backend supports it. Migrations below
                # repair the id columns for PostgreSQL installations created from scratch.
                raise

    if DATABASE_URL:
        # INTEGER PRIMARY KEY does not auto-increment in PostgreSQL. Convert the new id
        # columns to identity-like sequences only when they do not already have defaults.
        with get_conn() as conn:
            for table in (
                "waitlist_requests",
                "waitlist_intervals",
                "slot_holds",
                "waitlist_offer_history",
                "laundry_usage_history",
                "pending_waitlist_notifications",
            ):
                seq = f"{table}_id_seq"
                try:
                    conn.execute(f"CREATE SEQUENCE IF NOT EXISTS {seq}")
                    conn.execute(
                        f"ALTER TABLE {table} ALTER COLUMN id SET DEFAULT nextval('{seq}')"
                    )
                    conn.execute(
                        f"SELECT setval('{seq}', GREATEST(COALESCE((SELECT MAX(id) FROM {table}), 0), 1), "
                        f"COALESCE((SELECT MAX(id) FROM {table}), 0) > 0)"
                    )
                except Exception:
                    pass


def get_notification_settings(user_id: int) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT quiet_enabled, quiet_start, quiet_end,
                   earlier_offer_enabled, reminder_30_enabled
            FROM notification_settings
            WHERE user_id=?
            """,
            (int(user_id),),
        ).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO notification_settings (user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING",
                (int(user_id),),
            )
            row = (0, 23, 8, 1, 1)
    return {
        "quiet_enabled": bool(row[0]),
        "quiet_start": int(row[1]),
        "quiet_end": int(row[2]),
        "earlier_offer_enabled": bool(row[3]),
        "reminder_30_enabled": bool(row[4]),
    }


def set_notification_setting(user_id: int, field: str, value) -> None:
    allowed = {
        "quiet_enabled",
        "quiet_start",
        "quiet_end",
        "earlier_offer_enabled",
        "reminder_30_enabled",
    }
    if field not in allowed:
        raise ValueError("Unknown notification setting")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO notification_settings (user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING",
            (int(user_id),),
        )
        conn.execute(
            f"UPDATE notification_settings SET {field}=? WHERE user_id=?",
            (value, int(user_id)),
        )


def active_hold_for_user(user_id: int):
    now = datetime.now(TZ).isoformat(timespec="seconds")
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT id, request_id, machine_id, date, hour, expires_at, context, current_booking_id
            FROM slot_holds
            WHERE user_id=? AND status='active' AND expires_at>?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (int(user_id), now),
        ).fetchone()


def slot_has_active_hold(machine_id: int, date_iso: str, hour: int) -> bool:
    now = datetime.now(TZ).isoformat(timespec="seconds")
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM slot_holds
            WHERE machine_id=? AND date=? AND hour=?
              AND status='active' AND expires_at>?
            LIMIT 1
            """,
            (int(machine_id), str(date_iso), int(hour), now),
        ).fetchone()
    return bool(row)


def get_free_hours_effective(machine_id: int, date_iso: str) -> list[int]:
    with get_conn() as conn:
        busy = {
            int(r[0])
            for r in conn.execute(
                "SELECT hour FROM bookings WHERE machine_id=? AND date=?",
                (int(machine_id), str(date_iso)),
            ).fetchall()
        }
        now_s = datetime.now(TZ).isoformat(timespec="seconds")
        held = {
            int(r[0])
            for r in conn.execute(
                """
                SELECT hour FROM slot_holds
                WHERE machine_id=? AND date=? AND status='active' AND expires_at>?
                """,
                (int(machine_id), str(date_iso), now_s),
            ).fetchall()
        }
    return [h for h in WORKING_HOURS if h not in busy and h not in held]


def get_availability_bulk(date_isos: list[str]):
    """
    Load active machines and free hours for several dates with three SQL queries
    total instead of querying Neon separately for every machine/date pair.
    """
    dates = [str(x) for x in dict.fromkeys(date_isos) if x]
    if not dates:
        return [], {}

    marks = ",".join("?" for _ in dates)
    now_s = datetime.now(TZ).isoformat(timespec="seconds")
    with get_conn() as conn:
        machines = conn.execute(
            "SELECT id,type,name FROM machines WHERE is_active ORDER BY type,name"
        ).fetchall()
        booking_rows = conn.execute(
            f"""
            SELECT machine_id,date,hour
            FROM bookings
            WHERE date IN ({marks})
            """,
            tuple(dates),
        ).fetchall()
        hold_rows = conn.execute(
            f"""
            SELECT machine_id,date,hour
            FROM slot_holds
            WHERE date IN ({marks})
              AND status='active' AND expires_at>?
            """,
            tuple(dates) + (now_s,),
        ).fetchall()

    busy: dict[tuple[int, str], set[int]] = {}
    for machine_id, date_value, hour in booking_rows:
        ds = date_value.isoformat() if hasattr(date_value, "isoformat") else str(date_value)
        busy.setdefault((int(machine_id), ds), set()).add(int(hour))
    for machine_id, date_value, hour in hold_rows:
        ds = date_value.isoformat() if hasattr(date_value, "isoformat") else str(date_value)
        busy.setdefault((int(machine_id), ds), set()).add(int(hour))

    availability: dict[str, dict[int, list[int]]] = {d: {} for d in dates}
    for machine_id, _machine_type, _machine_name in machines:
        mid = int(machine_id)
        for date_iso in dates:
            used = busy.get((mid, date_iso), set())
            availability[date_iso][mid] = [h for h in WORKING_HOURS if h not in used]

    return machines, availability


def record_usage_history(now: datetime | None = None) -> int:
    """
    Record a wash once its booked start time has arrived.
    Cancellation handlers reject already-started slots, so reaching the slot
    is the objective signal used by the fairness system.
    """
    now = now or datetime.now(TZ)
    oldest = (now.date() - timedelta(days=7)).isoformat()
    newest = now.date().isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT b.id, b.user_id, b.date, b.hour
            FROM bookings b
            JOIN machines m ON m.id=b.machine_id
            WHERE m.type='wash' AND b.date BETWEEN ? AND ?
            """,
            (oldest, newest),
        ).fetchall()
    inserted = 0
    for booking_id, user_id, date_value, hour in rows:
        try:
            d = datetime.fromisoformat(str(date_value)).date()
            occurred = datetime.combine(d, datetime.min.time(), tzinfo=TZ).replace(hour=int(hour))
        except Exception:
            continue
        if occurred > now:
            continue
        with get_conn() as conn:
            before = conn.execute(
                "SELECT 1 FROM laundry_usage_history WHERE booking_id=? LIMIT 1",
                (int(booking_id),),
            ).fetchone()
            if before:
                continue
            conn.execute(
                """
                INSERT INTO laundry_usage_history (user_id, booking_id, occurred_at)
                VALUES (?, ?, ?)
                ON CONFLICT(booking_id) DO NOTHING
                """,
                (int(user_id), int(booking_id), occurred.isoformat(timespec="seconds")),
            )
        inserted += 1
    return inserted


def usage_penalties_for_users(user_ids: list[int], now: datetime | None = None) -> dict[int, int]:
    """
    Return weighted 30-day usage points: 4/3/2/1 by recency week.
    Accounts with the same normalized surname+room share one fairness history,
    so a second Telegram account cannot reset the usage penalty.
    """
    ids = sorted({int(x) for x in user_ids})
    if not ids:
        return {}

    now = now or datetime.now(TZ)
    cutoff = now - timedelta(days=30)

    with get_conn() as conn:
        profiles = conn.execute(
            "SELECT id,surname,room FROM users WHERE tg_id>0 AND surname IS NOT NULL AND room IS NOT NULL"
        ).fetchall()

    key_by_id = {}
    ids_by_key = {}
    for uid, enc_surname, enc_room in profiles:
        key = _resident_key(_b64d_try(enc_surname), _b64d_try(enc_room))
        if not key:
            continue
        key_by_id[int(uid)] = key
        ids_by_key.setdefault(key, set()).add(int(uid))

    groups = {}
    peer_ids = set()
    for uid in ids:
        key = key_by_id.get(uid)
        group = set(ids_by_key.get(key, {uid})) if key else {uid}
        groups[uid] = group
        peer_ids.update(group)

    peers = sorted(peer_ids)
    if not peers:
        return {uid: 0 for uid in ids}

    placeholders = ",".join(["?"] * len(peers))
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT user_id, occurred_at
            FROM laundry_usage_history
            WHERE user_id IN ({placeholders}) AND occurred_at>=?
            """,
            tuple(peers) + (cutoff.isoformat(timespec="seconds"),),
        ).fetchall()

    points_by_user = {uid: 0 for uid in peers}
    for uid, occurred_at in rows:
        try:
            age_days = max(0, (now - datetime.fromisoformat(str(occurred_at))).days)
        except Exception:
            continue
        if age_days <= 7:
            weight = 4
        elif age_days <= 14:
            weight = 3
        elif age_days <= 21:
            weight = 2
        elif age_days <= 30:
            weight = 1
        else:
            weight = 0
        points_by_user[int(uid)] = points_by_user.get(int(uid), 0) + weight

    return {
        uid: sum(points_by_user.get(peer_id, 0) for peer_id in groups[uid])
        for uid in ids
    }
