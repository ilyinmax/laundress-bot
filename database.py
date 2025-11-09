import sqlite3
import base64
from datetime import datetime, timedelta
from config import DB_PATH, WORKING_HOURS

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
        return s  # если это не base64 — вернём как есть (совместимость)

# публичный помощник для других модулей (admin.py)
def b64_decode_field(s: str | None) -> str | None:
    return _b64d_try(s)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_id INTEGER UNIQUE,
        surname TEXT,
        room TEXT
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS machines(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT,
        name TEXT
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS bookings(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        machine_id INTEGER,
        date TEXT,
        hour INTEGER,
        UNIQUE(user_id, date, machine_id)
    )""")

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uniq_slot "
        "ON bookings(machine_id, date, hour)"
    )

    conn.commit()
    conn.close()

def get_conn():
    return sqlite3.connect(DB_PATH)

def add_user(tg_id, surname, room):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (tg_id, surname, room) VALUES (?, ?, ?)",
            (tg_id, _b64e(surname), _b64e(room))
        )

def get_user(tg_id):
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))
        row = cur.fetchone()
        if not row:
            return None
        # row: (id, tg_id, surname, room)
        return (row[0], row[1], _b64d_try(row[2]), _b64d_try(row[3]))

def add_machine(type_, name):
    with get_conn() as conn:
        conn.execute("INSERT INTO machines (type, name) VALUES (?, ?)", (type_, name))

def get_machines_by_type(type_):
    with get_conn() as conn:
        cur = conn.execute("SELECT * FROM machines WHERE type=?", (type_,))
        return cur.fetchall()

def get_free_hours(machine_id, date):
    with get_conn() as conn:
        cur = conn.execute("SELECT hour FROM bookings WHERE machine_id=? AND date=?", (machine_id, date))
        busy = {r[0] for r in cur.fetchall()}
    return [h for h in WORKING_HOURS if h not in busy]

def make_booking(user_id, machine_id, date, hour):
    with get_conn() as conn:
        conn.execute("INSERT INTO bookings (user_id, machine_id, date, hour) VALUES (?, ?, ?, ?)",
                     (user_id, machine_id, date, hour))

def get_user_bookings_today(user_id, date, type_=None):
    with get_conn() as conn:
        if type_:
            cur = conn.execute("""
                SELECT b.* FROM bookings b
                JOIN machines m ON b.machine_id = m.id
                WHERE b.user_id = ? AND b.date = ? AND m.type = ?
            """, (user_id, date, type_))
        else:
            cur = conn.execute("SELECT * FROM bookings WHERE user_id=? AND date=?", (user_id, date))
        return cur.fetchone()

def cleanup_old_bookings():
    today = datetime.now().date()
    cutoff = today - timedelta(days=1)
    with get_conn() as conn:
        conn.execute("DELETE FROM bookings WHERE date < ?", (cutoff.isoformat(),))

def save_user(tg_id, surname, room):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO users (tg_id, surname, room)
            VALUES (?, ?, ?)
            ON CONFLICT(tg_id) DO UPDATE SET
                surname=excluded.surname,
                room=excluded.room
        """, (tg_id, _b64e(surname), _b64e(room)))
