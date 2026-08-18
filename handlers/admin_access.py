from __future__ import annotations

from aiogram import Router, types
from aiogram.filters import Command

from config import ADMIN_IDS
from database import get_conn, is_admin, tg_id_by_username

router = Router()


def _parse_admin_ids(raw) -> set[int]:
    if isinstance(raw, (list, tuple, set)):
        items = raw
    else:
        text = str(raw).strip().strip("[]")
        items = [part.strip().strip("'\"") for part in text.split(",") if part.strip()]

    result: set[int] = set()
    for item in items:
        try:
            result.add(int(item))
        except (TypeError, ValueError):
            continue
    return result


# Снимок постоянных админов из config.py. Динамические админы сюда не попадают,
# поэтому они не смогут самостоятельно раздавать права дальше.
ROOT_ADMIN_IDS = frozenset(_parse_admin_ids(ADMIN_IDS))


def is_root_admin(user_id: int | str) -> bool:
    try:
        return int(user_id) in ROOT_ADMIN_IDS
    except (TypeError, ValueError):
        return False


def ensure_admin_access_table() -> None:
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_admins (
                tg_id BIGINT PRIMARY KEY,
                username TEXT,
                added_by BIGINT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)


def _activate_admin_id(tg_id: int) -> None:
    """Добавляет ID в живой список ADMIN_IDS, который использует database.is_admin()."""
    tg_id = int(tg_id)
    if isinstance(ADMIN_IDS, list):
        current = _parse_admin_ids(ADMIN_IDS)
        if tg_id not in current:
            ADMIN_IDS.append(tg_id)
    elif isinstance(ADMIN_IDS, set):
        ADMIN_IDS.add(tg_id)


def _deactivate_admin_id(tg_id: int) -> None:
    """Убирает только динамического админа; постоянные ID из config.py не трогаются."""
    tg_id = int(tg_id)
    if tg_id in ROOT_ADMIN_IDS:
        return
    if isinstance(ADMIN_IDS, list):
        ADMIN_IDS[:] = [item for item in ADMIN_IDS if _safe_int(item) != tg_id]
    elif isinstance(ADMIN_IDS, set):
        ADMIN_IDS.discard(tg_id)


def _safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def sync_dynamic_admins() -> list[int]:
    """Загружает сохранённых админов из БД в текущий процесс."""
    ensure_admin_access_table()
    with get_conn() as conn:
        rows = conn.execute("SELECT tg_id FROM bot_admins ORDER BY tg_id").fetchall()
    ids = [int(row[0]) for row in rows]
    for tg_id in ids:
        _activate_admin_id(tg_id)
    return ids


def grant_dynamic_admin(tg_id: int, username: str | None, added_by: int) -> None:
    ensure_admin_access_table()
    clean_username = (username or "").lstrip("@") or None
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO bot_admins (tg_id, username, added_by)
            VALUES (?, ?, ?)
            ON CONFLICT(tg_id) DO UPDATE SET
                username=excluded.username,
                added_by=excluded.added_by
        """, (int(tg_id), clean_username, int(added_by)))
    _activate_admin_id(int(tg_id))


def revoke_dynamic_admin(tg_id: int) -> bool:
    ensure_admin_access_table()
    tg_id = int(tg_id)
    with get_conn() as conn:
        existed = conn.execute(
            "SELECT 1 FROM bot_admins WHERE tg_id=? LIMIT 1", (tg_id,)
        ).fetchone()
        if not existed:
            return False
        conn.execute("DELETE FROM bot_admins WHERE tg_id=?", (tg_id,))
    _deactivate_admin_id(tg_id)
    return True


def _username_for_id(tg_id: int) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT username FROM users WHERE tg_id=? LIMIT 1", (int(tg_id),)
        ).fetchone()
    return row[0] if row and row[0] else None


def _resolve_target(value: str) -> tuple[int | None, str | None]:
    raw = (value or "").strip()
    if not raw:
        return None, None

    if raw.startswith("@"):
        username = raw.lstrip("@")
        tg_id = tg_id_by_username(username)
        if tg_id is None:
            ensure_admin_access_table()
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT tg_id FROM bot_admins WHERE LOWER(username)=LOWER(?) LIMIT 1",
                    (username,),
                ).fetchone()
            tg_id = int(row[0]) if row else None
        return (int(tg_id), username) if tg_id is not None else (None, username)

    try:
        tg_id = int(raw)
    except ValueError:
        return None, None
    if tg_id <= 0:
        return None, None
    return tg_id, _username_for_id(tg_id)


def list_dynamic_admins() -> list[tuple[int, str | None, int | None]]:
    ensure_admin_access_table()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT tg_id, username, added_by FROM bot_admins ORDER BY added_at, tg_id"
        ).fetchall()
    return [
        (int(tg_id), username or None, int(added_by) if added_by is not None else None)
        for tg_id, username, added_by in rows
    ]


@router.message(Command("addadmin"))
async def cmd_addadmin(msg: types.Message):
    if not is_root_admin(msg.from_user.id):
        return await msg.answer("🚫 Добавлять администраторов могут только постоянные администраторы.")

    parts = (msg.text or "").strip().split(maxsplit=1)
    if len(parts) < 2:
        return await msg.answer("Формат: <code>/addadmin @username</code>", parse_mode="HTML")

    tg_id, username = _resolve_target(parts[1])
    if tg_id is None:
        return await msg.answer(
            "❗ Не нашёл этого пользователя.\n\n"
            "Пусть он хотя бы один раз откроет бота и нажмёт /start — после этого "
            "я смогу определить его Telegram ID по @username."
        )

    if is_admin(tg_id):
        label = f"@{username}" if username else f"<code>{tg_id}</code>"
        return await msg.answer(f"ℹ️ {label} уже является администратором.", parse_mode="HTML")

    grant_dynamic_admin(tg_id, username, msg.from_user.id)

    menu_updated = True
    try:
        from handlers.bot_commands import set_admin_commands_for_chat
        await set_admin_commands_for_chat(msg.bot, tg_id)
    except Exception:
        menu_updated = False

    label = f"@{username}" if username else f"<code>{tg_id}</code>"
    text = (
        f"✅ {label} добавлен в администраторы.\n"
        f"Telegram ID: <code>{tg_id}</code>\n\n"
        "Теперь ему доступны /admin, ранняя запись и остальные функции администратора."
    )
    if not menu_updated:
        text += "\n\n⚠️ Права выданы, но синее меню команд обновится после следующего перезапуска бота."
    await msg.answer(text, parse_mode="HTML")


@router.message(Command("deladmin"))
async def cmd_deladmin(msg: types.Message):
    if not is_root_admin(msg.from_user.id):
        return await msg.answer("🚫 Удалять администраторов могут только постоянные администраторы.")

    parts = (msg.text or "").strip().split(maxsplit=1)
    if len(parts) < 2:
        return await msg.answer("Формат: <code>/deladmin @username</code>", parse_mode="HTML")

    tg_id, username = _resolve_target(parts[1])
    if tg_id is None:
        return await msg.answer("❗ Не нашёл такого администратора.")
    if tg_id in ROOT_ADMIN_IDS:
        return await msg.answer("🚫 Постоянного администратора из config.py этой командой удалить нельзя.")

    if not revoke_dynamic_admin(tg_id):
        return await msg.answer("ℹ️ Этот пользователь не является добавленным администратором.")

    try:
        from handlers.bot_commands import remove_admin_commands_for_chat
        await remove_admin_commands_for_chat(msg.bot, tg_id)
    except Exception:
        pass

    label = f"@{username}" if username else f"<code>{tg_id}</code>"
    await msg.answer(f"✅ {label} удалён из администраторов.", parse_mode="HTML")


@router.message(Command("admins"))
async def cmd_admins(msg: types.Message):
    if not is_root_admin(msg.from_user.id):
        return await msg.answer("🚫 Список администраторов доступен только постоянным администраторам.")

    lines = ["👑 <b>Администраторы бота</b>\n", "<b>Постоянные (config.py):</b>"]
    for tg_id in sorted(ROOT_ADMIN_IDS):
        username = _username_for_id(tg_id)
        label = f"@{username}" if username else "без username"
        lines.append(f"• {label} — <code>{tg_id}</code>")

    lines.append("\n<b>Добавленные через /addadmin:</b>")
    dynamic = list_dynamic_admins()
    if not dynamic:
        lines.append("• нет")
    else:
        for tg_id, username, added_by in dynamic:
            label = f"@{username}" if username else "без username"
            lines.append(f"• {label} — <code>{tg_id}</code>")

    await msg.answer("\n".join(lines), parse_mode="HTML")
