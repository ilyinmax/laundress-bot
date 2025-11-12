# database.py
import os
import base64
from datetime import datetime, timedelta
from config import DB_PATH, WORKING_HOURS  # DB_PATH останется как локальный fallback
import hashlib

def _stub_tg_id(surname: str, room: str) -> int:
    seed = f"{surname}|{room}".encode("utf-8")
    val = int.from_bytes(hashlib.sha256(seed).digest()[:8], "big")
    return -max(1, val % 10**11)  # отрицательный, но уникальный

def ensure_user_by_surname_room(surname: str, room: str) -> int:
    """Возвращает id пользователя. Если его нет — создаёт 'стаб' с фиктивным tg_id."""
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT id FROM users WHERE surname=? AND room=?",
            (_b64e(surname), _b64e(room)),
        )
        row = cur.fetchone()
        if row:
            return row[0]
        tg_stub = _stub_tg_id(surname, room)
        conn.execute(
            "INSERT INTO users (tg_id, surname, room) VALUES (?, ?, ?)",
            (tg_stub, _b64e(surname), _b64e(room)),
        )
        cur = conn.execute("SELECT id FROM users WHERE tg_id=?", (tg_stub,))
        return cur.fetchone()[0]

def get_machine_id_by_name(name: str) -> int | None:
    with get_conn() as conn:
        cur = conn.execute("SELECT id FROM machines WHERE name=?", (name,))
        row = cur.fetchone()
        return row[0] if row else None

# Определяем, есть ли внешний Postgres
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# ---------- утилиты кодирования (как было) ----------
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

# ---------- совместимый слой для conn.execute(...) ----------
def _rewrite_qmarks(sql: str) -> str:
    # SQLite использует '?', Postgres — %s
    return sql.replace("?", "%s")

def _rewrite_insert_or_ignore(sql: str) -> str:
    s = sql.lstrip()
    if s.upper().startswith("INSERT OR IGNORE"):
        # превращаем в INSERT ... ON CONFLICT DO NOTHING
        # (будет работать при наличии уникальных ограничений)
        s = "INSERT" + s[len("INSERT OR IGNORE"):]
        s = s + " ON CONFLICT DO NOTHING"
        return sql[:len(sql) - len(sql.lstrip())] + s
    return sql

class _CursorWrapper:
    def __init__(self, cur):
        self._cur = cur
    def fetchone(self):
        return self._cur.fetchone()
    def fetchall(self):
        return self._cur.fetchall()
    @property
    def lastrowid(self):
        # В Postgres корректнее получать id через RETURNING,
        # но для совместимости попытаемся взять атрибут, если он есть
        return getattr(self._cur, "lastrowid", None)
    def close(self):
        try:
            self._cur.close()
        except Exception:
            pass

# ----- два backend’a: Postgres (Neon) и локальный SQLite fallback -----
if DATABASE_URL:
    import psycopg2
    from psycopg2 import pool

    _pg_pool = pool.SimpleConnectionPool(1, 10, DATABASE_URL)  # 1–10 одновременных коннектов


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

        def commit(self):
            pass  # autocommit включён

        def close(self):
            for w in self._opened:
                w.close()
            _pg_pool.putconn(self._conn)

        def __enter__(self): return self

        def __exit__(self, exc_type, exc, tb): self.close()


    def get_conn():
        return _PgConn()

else:
    # локальный режим — как раньше
    import sqlite3
    class _SqliteConn:
        def __init__(self):
            self._conn = sqlite3.connect(DB_PATH)
            # вариант 1: классический транзакционный режим с commit/rollback в __exit__
            # (если хочешь автокоммит, сними комментарий со строки ниже и убери логику в __exit__)
            # self._conn.isolation_level = None  # <- вариант 2: автокоммит

        def execute(self, *args, **kwargs):
            return self._conn.execute(*args, **kwargs)

        def commit(self):
            self._conn.commit()

        def close(self):
            self._conn.close()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            try:
                if exc_type is None:
                    self._conn.commit()
                else:
                    self._conn.rollback()
            finally:
                self._conn.close()


    def get_conn():
        return _SqliteConn()

# ---------- инициализация схемы (будет вызвана при старте) ----------
def init_db():
    if DATABASE_URL:
        # Postgres
        ddl = [
            """
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                tg_id BIGINT UNIQUE NOT NULL,
                surname TEXT,
                room TEXT
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
        # SQLite
        ddl = [
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER UNIQUE NOT NULL,
                surname TEXT,
                room TEXT
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
                created_at TIMESTAMPTZ DEFAULT now(),
                UNIQUE (machine_id, date, hour)
            );
            """,
            "CREATE INDEX IF NOT EXISTS idx_bookings_user_date ON bookings (user_id, date);",
            "CREATE INDEX IF NOT EXISTS idx_bookings_machine_date ON bookings (machine_id, date);",
        ]
    with get_conn() as conn:
        for stmt in ddl:
            conn.execute(stmt)
    ensure_username_column()


# === Бан пользователей и фильтрация ===
def ensure_ban_tables():
    """Создаёт/мигрирует таблицы для банов и попыток (если ещё нет)."""
    with get_conn() as conn:
        if DATABASE_URL:
            # --- Postgres: используем BIGINT и мигрируем, если были INTEGER ---
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
            # миграция типов на BIGINT (безошибочно, если уже BIGINT)
            conn.execute("ALTER TABLE banned ALTER COLUMN tg_id TYPE BIGINT USING tg_id::bigint;")
            conn.execute("ALTER TABLE failed_attempts ALTER COLUMN tg_id TYPE BIGINT USING tg_id::bigint;")
        else:
            # --- SQLite: обычный INTEGER подходит (он 64-битный) ---
            conn.execute("""
                CREATE TABLE IF NOT EXISTS banned (
                    tg_id INTEGER UNIQUE NOT NULL,
                    reason TEXT,
                    banned_until TEXT,
                    banned_at TIMESTAMPTZ DEFAULT now()
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS failed_attempts (
                    tg_id INTEGER UNIQUE NOT NULL,
                    count INTEGER DEFAULT 0,
                    last_attempt TIMESTAMPTZ DEFAULT now()
                );
            """)


ensure_ban_tables()


from datetime import datetime, timedelta


def ban_user(tg_id: int, reason: str = None, days: int = 7):
    until = (datetime.now() + timedelta(days=days)).isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO banned (tg_id, reason, banned_until, banned_at)
            VALUES (?, ?, ?, %s)
            ON CONFLICT(tg_id) DO UPDATE SET
                reason=excluded.reason,
                banned_until=excluded.banned_until,
                banned_at=%s
        """.replace("%s", "now()" if DATABASE_URL else "excluded.banned_at"),
        (tg_id, reason or "Без причины", until))


def is_banned(tg_id: int) -> bool:
    """Проверяет, забанен ли пользователь (и снимает бан, если срок истёк)."""
    with get_conn() as conn:
        cur = conn.execute("SELECT banned_until FROM banned WHERE tg_id=?", (tg_id,))
        row = cur.fetchone()
        if not row:
            return False
        until = row[0]
        if not until:
            return False
        try:
            dt_until = datetime.fromisoformat(until)
            if datetime.now() > dt_until:
                conn.execute("DELETE FROM banned WHERE tg_id=?", (tg_id,))
                return False
        except Exception:
            pass
        return True


def unban_user(tg_id: int):
    """Снимает бан вручную."""
    with get_conn() as conn:
        conn.execute("DELETE FROM banned WHERE tg_id=?", (tg_id,))


def register_failed_attempt(tg_id: int) -> int:
    with get_conn() as conn:
        cur = conn.execute("SELECT count FROM failed_attempts WHERE tg_id=?", (tg_id,))
        row = cur.fetchone()
        count = (row[0] if row else 0) + 1
        conn.execute("""
            INSERT INTO failed_attempts (tg_id, count, last_attempt)
            VALUES (?, ?, ?)
            ON CONFLICT(tg_id) DO UPDATE SET
                count=excluded.count,
                last_attempt=excluded.last_attempt
        """, (tg_id, count, datetime.now().isoformat(timespec="seconds")))
    return count



def reset_failed_attempts(tg_id: int):
    """Сбрасывает счётчик неудачных попыток."""
    with get_conn() as conn:
        conn.execute("DELETE FROM failed_attempts WHERE tg_id=?", (tg_id,))











def bind_stub_user_to_real(tg_id, surname, room):
    # Ищем существующую запись по Фамилия+Комната (это “заглушка”)
    with get_conn() as conn:
        cur = conn.execute("SELECT id FROM users WHERE surname=? AND room=?", (_b64e(surname), _b64e(room)))
        stub = cur.fetchone()
        if not stub:
            return
        stub_id = stub[0]

        # Создаём/гарантируем реального пользователя по tg_id
        conn.execute("""
            INSERT INTO users (tg_id, surname, room)
            VALUES (?, ?, ?)
            ON CONFLICT(tg_id) DO UPDATE SET surname=excluded.surname, room=excluded.room
        """, (tg_id, _b64e(surname), _b64e(room)))

        # Узнаём id реального пользователя
        cur = conn.execute("SELECT id FROM users WHERE tg_id=?", (tg_id,))
        real_id = cur.fetchone()[0]

        # Переносим брони и удаляем заглушку
        conn.execute("UPDATE bookings SET user_id=? WHERE user_id=?", (real_id, stub_id))
        conn.execute("DELETE FROM users WHERE id=?", (stub_id,))

# ниже — функции, которые уже использует твой код
def add_user(tg_id, surname, room):
    with get_conn() as conn:
        # было: INSERT OR IGNORE — в Postgres превращается в ON CONFLICT DO NOTHING
        conn.execute(
            "INSERT OR IGNORE INTO users (tg_id, surname, room) VALUES (?, ?, ?)",
            (tg_id, _b64e(surname), _b64e(room))
        )

def save_user(tg_id, surname, room):
    bind_stub_user_to_real(tg_id, surname, room)
    with get_conn() as conn:
        # синтаксис одинаков и для Postgres, и для SQLite(>=3.24)
        conn.execute("""
            INSERT INTO users (tg_id, surname, room)
            VALUES (?, ?, ?)
            ON CONFLICT(tg_id) DO UPDATE SET
                surname=excluded.surname,
                room=excluded.room
        """, (tg_id, _b64e(surname), _b64e(room)))

def get_user(tg_id):
    with get_conn() as conn:
        cur = conn.execute("SELECT id, tg_id, surname, room FROM users WHERE tg_id=?", (tg_id,))
        row = cur.fetchone()
        if not row:
            return None
        return (row[0], row[1], _b64d_try(row[2]), _b64d_try(row[3]))

def add_machine(type_, name):
    with get_conn() as conn:
        conn.execute("INSERT INTO machines (type, name) VALUES (?, ?)", (type_, name))

def get_machines_by_type(type_):
    with get_conn() as conn:
        cur = conn.execute("SELECT id, type, name FROM machines WHERE type=?", (type_,))
        return cur.fetchall()

def get_user_bookings_today(user_id, date_iso, machine_type):
    """Есть ли у пользователя запись на этот день по типу машины.
    Не даём записываться, если уже была запись и время слота прошло."""
    from datetime import datetime, time
    now = datetime.now().time()

    with get_conn() as conn:
        cur = conn.execute("""
            SELECT b.hour
            FROM bookings b
            JOIN machines m ON m.id = b.machine_id
            WHERE b.user_id = ? AND b.date = ? AND m.type = ?
            LIMIT 1
        """, (user_id, date_iso, machine_type))
        row = cur.fetchone()

    if row:
        # если запись есть — нельзя повторно
        return True

    # проверим, был ли уже слот сегодня в прошлом (чтобы не записывать повторно)
    today = datetime.now().date().isoformat()
    if date_iso == today:
        # если текущий час > 9 (начало рабочего дня) — значит время уже шло
        if now > time(9, 0):
            # пользователь уже мог стираться, поэтому не даём повторно
            return True

    return False


def get_free_hours(machine_id, date_iso):
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT hour FROM bookings WHERE machine_id=? AND date=?",
            (machine_id, date_iso)
        )
        busy = {r[0] for r in cur.fetchall()}
    return [h for h in WORKING_HOURS if h not in busy]

def create_booking(user_id, machine_id, date_iso, hour):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO bookings (user_id, machine_id, date, hour)
            VALUES (?, ?, ?, ?)
        """, (user_id, machine_id, date_iso, hour))

def cleanup_old_bookings():
    today = datetime.now().date()
    cutoff = today - timedelta(days=1)
    with get_conn() as conn:
        conn.execute("DELETE FROM bookings WHERE date < ?", (cutoff.isoformat(),))






# --- usernames support ---
def ensure_username_column():
    with get_conn() as conn:
        if DATABASE_URL:
            conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT;")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users (LOWER(username));")
        else:
            # SQLite: добавляем колонку, если её нет
            cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
            if "username" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN username TEXT;")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users (username);")

def update_username(tg_id: int, username: str | None):
    if not username:
        return
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO users (tg_id, username)
            VALUES (?, ?)
            ON CONFLICT(tg_id) DO UPDATE SET username=excluded.username
        """, (tg_id, username))

def tg_id_by_username(username: str) -> int | None:
    u = username.lstrip("@")
    with get_conn() as conn:
        cur = conn.execute("SELECT tg_id FROM users WHERE LOWER(username)=LOWER(?) LIMIT 1", (u,))
        row = cur.fetchone()
        return row[0] if row else None
