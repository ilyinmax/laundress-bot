from __future__ import annotations

import html, math
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton as B, InlineKeyboardMarkup as K
from apscheduler.triggers.date import DateTrigger

import database
from config import TIMEZONE
from database import _b64d_try, get_conn, get_machine_id_by_name, is_admin, mark_reminder_sent, was_reminder_sent
from scheduler import scheduler

TZ = ZoneInfo(TIMEZONE)
router = Router()
BOT: Bot | None = None
MIN_T, MAX_T, CARD_BEFORE, PICK_BEFORE, COOLDOWN, PAGE = 30, 60, 5, 2, 5, 10

class TimerInput(StatesGroup):
    minutes = State()

def attach_feature_bot(bot: Bot):
    global BOT; BOT = bot

def init_feature_tables():
    with get_conn() as c:
        c.execute("CREATE TABLE IF NOT EXISTS laundry_timers (booking_id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL,minutes INTEGER NOT NULL,started_at TEXT NOT NULL,ends_at TEXT NOT NULL,pickup_sent INTEGER NOT NULL DEFAULT 0)")
        c.execute("CREATE TABLE IF NOT EXISTS timer_history (user_id INTEGER NOT NULL,minutes INTEGER NOT NULL,used_at TEXT NOT NULL,PRIMARY KEY(user_id,minutes))")
        c.execute("CREATE TABLE IF NOT EXISTS reminder_cards (booking_id INTEGER PRIMARY KEY,chat_id BIGINT NOT NULL,message_id BIGINT NOT NULL,delay_until TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS foreign_nudges (previous_booking_id INTEGER PRIMARY KEY,last_sent_at TEXT NOT NULL)")
        c.execute("DELETE FROM laundry_timers WHERE booking_id NOT IN (SELECT id FROM bookings)")
        c.execute("DELETE FROM reminder_cards WHERE booking_id NOT IN (SELECT id FROM bookings)")
        c.execute("DELETE FROM foreign_nudges WHERE previous_booking_id NOT IN (SELECT id FROM bookings)")

def ds(v): return v.isoformat() if hasattr(v, "isoformat") else str(v)
def slot(v,h): return datetime.combine(datetime.fromisoformat(ds(v)).date(), time(int(h)), tzinfo=TZ)

def booking(bid):
    with get_conn() as c:
        return c.execute("SELECT b.id,b.user_id,u.tg_id,m.id,m.type,m.name,b.date,b.hour FROM bookings b JOIN users u ON u.id=b.user_id JOIN machines m ON m.id=b.machine_id WHERE b.id=? LIMIT 1",(int(bid),)).fetchone()

def booking_for(tg,mid,date,h):
    with get_conn() as c:
        return c.execute("SELECT b.id,b.user_id,u.tg_id,m.id,m.type,m.name,b.date,b.hour FROM bookings b JOIN users u ON u.id=b.user_id JOIN machines m ON m.id=b.machine_id WHERE u.tg_id=? AND b.machine_id=? AND b.date=? AND b.hour=? LIMIT 1",(int(tg),int(mid),str(date),int(h))).fetchone()

def adjacent(mid,date,h,delta):
    with get_conn() as c:
        return c.execute("SELECT b.id,b.user_id,u.tg_id FROM bookings b JOIN users u ON u.id=b.user_id WHERE b.machine_id=? AND b.date=? AND b.hour=? LIMIT 1",(int(mid),ds(date),int(h)+delta)).fetchone()

def timer_row(bid):
    with get_conn() as c: return c.execute("SELECT minutes,started_at,ends_at,pickup_sent FROM laundry_timers WHERE booking_id=?",(int(bid),)).fetchone()
def timer_end(bid):
    r=timer_row(bid)
    try: return datetime.fromisoformat(str(r[2])) if r else None
    except Exception: return None

def card_row(bid):
    with get_conn() as c: return c.execute("SELECT chat_id,message_id,delay_until FROM reminder_cards WHERE booking_id=?",(int(bid),)).fetchone()
def stored_delay(bid):
    r=card_row(bid)
    try: return datetime.fromisoformat(str(r[2])) if r and r[2] else None
    except Exception: return None

def history(uid):
    with get_conn() as c: rows=c.execute("SELECT minutes FROM timer_history WHERE user_id=? ORDER BY used_at DESC LIMIT 3",(int(uid),)).fetchall()
    return [int(x[0]) for x in rows]

def remember(uid,m):
    with get_conn() as c: c.execute("INSERT INTO timer_history(user_id,minutes,used_at) VALUES(?,?,?) ON CONFLICT(user_id,minutes) DO UPDATE SET used_at=excluded.used_at",(int(uid),int(m),datetime.now(TZ).isoformat()))

def delay_for(b):
    if not b or b[4] != "wash": return None
    p=adjacent(b[3],b[6],b[7],-1)
    e=timer_end(p[0]) if p else None
    return e if e and e>slot(b[6],b[7]) else None

def text_for(b,phase,delay=None):
    icon,kind=("🧺","стирка") if b[4]=="wash" else ("🌬️","сушка")
    if phase=="30": head,lead="⏰ <b>Напоминание</b>",f"Через <b>30 мин</b> у вас {kind}."
    elif phase=="5": head,lead="⏰ <b>Напоминание</b>",f"Через <b>5 мин</b> у вас {kind}."
    else: head,lead=f"{icon} <b>Ваша запись началась</b>",f"Сейчас у вас {kind}."
    out=[head,"",lead,f"{icon} Машина: <b>{html.escape(str(b[5]))}</b>",f"📅 Дата: {ds(b[6])}",f"🕒 Время: {int(b[7]):02d}:00–{(int(b[7])+1)%24:02d}:00"]
    if delay: out += ["",f"⚠️ Предыдущая стирка ориентировочно закончится в <b>{delay.astimezone(TZ).strftime('%H:%M')}</b>."]
    if b[4]=="wash" and phase in ("5","active"):
        t=timer_row(b[0]); out.append("")
        out.append(f"⏱ Таймер установлен на <b>{int(t[0])} мин</b>." if t else "⏱ После запуска машинки нажмите «Поставить таймер» и укажите время с дисплея — бот напомнит за 2 минуты до конца.")
    return "\n".join(out)

def foreign_now(b):
    if not b or b[4]!="wash" or datetime.now(TZ)<slot(b[6],b[7]): return None
    p=adjacent(b[3],b[6],b[7],-1)
    if not p: return None
    e=timer_end(p[0])
    return None if e and e>datetime.now(TZ) else int(p[0])

def keyboard(b,foreign=True):
    rows=[]
    if b and b[4]=="wash" and not timer_row(b[0]): rows.append([B(text="⏱ Поставить таймер",callback_data=f"lf_timer_{b[0]}")])
    p=foreign_now(b) if foreign else None
    if p: rows.append([B(text="⚠️ В машине чужие вещи",callback_data=f"lf_foreign_{b[0]}_{p}")])
    return K(inline_keyboard=rows) if rows else None

async def edit_card(bid,phase,foreign=True):
    if BOT is None: return
    b,r=booking(bid),card_row(bid)
    if not b or not r or b[4]!="wash": return
    try: await BOT.edit_message_text(chat_id=int(r[0]),message_id=int(r[1]),text=text_for(b,phase,stored_delay(bid)),parse_mode="HTML",reply_markup=keyboard(b,foreign))
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower() and "message to edit not found" not in str(e).lower(): raise
    except Exception: return

async def edit_at_five(bid):
    b=booking(bid)
    if b and slot(b[6],b[7])-timedelta(minutes=5)<=datetime.now(TZ)<slot(b[6],b[7])+timedelta(hours=1): await edit_card(bid,"5" if datetime.now(TZ)<slot(b[6],b[7]) else "active",False)

async def add_foreign(cur,prev):
    b,p=booking(cur),booking(prev)
    if not b or not p or b[3]!=p[3] or ds(b[6])!=ds(p[6]) or int(p[7])+1!=int(b[7]): return
    now=datetime.now(TZ); s=slot(b[6],b[7]); e=timer_end(prev)
    if s<=now<s+timedelta(hours=1) and not(e and e>now): await edit_card(cur,"active",True)

async def activate(bid):
    b=booking(bid)
    if not b or b[4]!="wash": return
    now,s=datetime.now(TZ),slot(b[6],b[7])
    if not(s<=now<s+timedelta(hours=1)): return
    p=adjacent(b[3],b[6],b[7],-1); allow=True
    if p:
        e=timer_end(p[0])
        if e and e>now:
            allow=False; scheduler.add_job(add_foreign,DateTrigger(run_date=e),id=f"lf_foreignjob_{bid}",args=[bid,int(p[0])],replace_existing=True,misfire_grace_time=60)
    await edit_card(bid,"active",allow)

def schedule_card_jobs(b):
    if not b or b[4]!="wash": return
    now,s=datetime.now(TZ),slot(b[6],b[7]); t=s-timedelta(minutes=5)
    if t>now: scheduler.add_job(edit_at_five,DateTrigger(run_date=t),id=f"lf_card5_{b[0]}",args=[int(b[0])],replace_existing=True,misfire_grace_time=60)
    elif now<s: scheduler.add_job(edit_at_five,DateTrigger(run_date=now+timedelta(seconds=1)),id=f"lf_card5_{b[0]}",args=[int(b[0])],replace_existing=True,misfire_grace_time=60)
    if s>now: scheduler.add_job(activate,DateTrigger(run_date=s),id=f"lf_active_{b[0]}",args=[int(b[0])],replace_existing=True,misfire_grace_time=60)
    elif now<s+timedelta(hours=1): scheduler.add_job(activate,DateTrigger(run_date=now+timedelta(seconds=1)),id=f"lf_active_{b[0]}",args=[int(b[0])],replace_existing=True,misfire_grace_time=60)

async def schedule_reminder(tg_id,machine_name,date_str,hour,minutes_before=30):
    mid=get_machine_id_by_name(machine_name)
    if mid is None: return
    b=booking_for(tg_id,mid,date_str,hour)
    if not b: return
    schedule_card_jobs(b)
    if was_reminder_sent(int(tg_id),int(mid),str(date_str),int(hour),int(minutes_before)): return
    now=datetime.now(TZ); rd=slot(b[6],b[7])-timedelta(minutes=int(minutes_before))
    if rd>now: scheduler.add_job(send_reminder,DateTrigger(run_date=rd),id=f"lf_rem_{b[0]}",args=[int(b[0]),int(minutes_before)],replace_existing=True,misfire_grace_time=300)
    elif now<slot(b[6],b[7]) and (now-rd).total_seconds()<=300: await send_reminder(b[0],minutes_before)

async def send_reminder(bid,minutes_before=30):
    if BOT is None: return
    b=booking(bid)
    if not b or datetime.now(TZ)>=slot(b[6],b[7]): return
    if b[4]=="dry" and int(b[7])>0:
        with get_conn() as c: w=c.execute("SELECT 1 FROM bookings x JOIN users u ON u.id=x.user_id JOIN machines m ON m.id=x.machine_id WHERE u.tg_id=? AND x.date=? AND x.hour=? AND m.type='wash' LIMIT 1",(int(b[2]),ds(b[6]),int(b[7])-1)).fetchone()
        if w: return
    if was_reminder_sent(int(b[2]),int(b[3]),ds(b[6]),int(b[7]),int(minutes_before)): return
    d=delay_for(b)
    try: sent=await BOT.send_message(int(b[2]),text_for(b,"30",d),parse_mode="HTML")
    except Exception: return
    if b[4]=="wash":
        with get_conn() as c: c.execute("INSERT INTO reminder_cards(booking_id,chat_id,message_id,delay_until) VALUES(?,?,?,?) ON CONFLICT(booking_id) DO UPDATE SET chat_id=excluded.chat_id,message_id=excluded.message_id,delay_until=excluded.delay_until",(int(b[0]),int(b[2]),int(sent.message_id),d.isoformat() if d else None))
    mark_reminder_sent(int(b[2]),int(b[3]),ds(b[6]),int(b[7]),int(minutes_before))

def schedule_pickup(bid,end):
    run=end-timedelta(minutes=2)
    if run>datetime.now(TZ): scheduler.add_job(send_pickup,DateTrigger(run_date=run),id=f"lf_pickrem_{bid}",args=[int(bid)],replace_existing=True,misfire_grace_time=60)

async def send_pickup(bid):
    if BOT is None: return
    b,t=booking(bid),timer_row(bid)
    if not b or not t or int(t[3]): return
    try: await BOT.send_message(int(b[2]),"🧺 <b>До конца стирки осталось 2 минуты.</b>\nПора спускаться за вещами.",parse_mode="HTML")
    except Exception: return
    with get_conn() as c: c.execute("UPDATE laundry_timers SET pickup_sent=1 WHERE booking_id=?",(int(bid),))

async def set_timer(bid,m):
    b=booking(bid)
    if not b or b[4]!="wash" or timer_row(bid): return False
    now=datetime.now(TZ); s=slot(b[6],b[7])
    if now<s-timedelta(minutes=5) or now>=s+timedelta(hours=1): return False
    end=now+timedelta(minutes=int(m))
    with get_conn() as c: c.execute("INSERT INTO laundry_timers(booking_id,user_id,minutes,started_at,ends_at,pickup_sent) VALUES(?,?,?,?,?,0) ON CONFLICT(booking_id) DO UPDATE SET user_id=excluded.user_id,minutes=excluded.minutes,started_at=excluded.started_at,ends_at=excluded.ends_at,pickup_sent=0",(int(bid),int(b[1]),int(m),now.isoformat(),end.isoformat()))
    remember(b[1],m); schedule_pickup(bid,end)
    n=adjacent(b[3],b[6],b[7],1)
    if n:
        nb=booking(n[0]); ns=slot(nb[6],nb[7]); show=end if end>ns else ns
        if show>now: scheduler.add_job(add_foreign,DateTrigger(run_date=show),id=f"lf_foreignjob_{nb[0]}",args=[int(nb[0]),int(bid)],replace_existing=True,misfire_grace_time=60)
        if now>=ns and end>now: await edit_card(nb[0],"active",False)
    await edit_card(bid,"active" if now>=s else "5",True)
    return True

def choice_kb(bid,vals):
    rows=[]
    if vals: rows.append([B(text=f"{x} мин",callback_data=f"lf_pick_{bid}_{x}") for x in vals[:3]])
    rows.append([B(text="Другое",callback_data=f"lf_other_{bid}")]); return K(inline_keyboard=rows)

@router.callback_query(F.data.startswith("lf_timer_"))
async def timer_start(cb:types.CallbackQuery,state:FSMContext):
    try: bid=int(cb.data.removeprefix("lf_timer_"))
    except Exception: return await cb.answer("Некорректная кнопка.",show_alert=True)
    b=booking(bid)
    if not b or int(b[2])!=int(cb.from_user.id) or b[4]!="wash": return await cb.answer("Запись не найдена.",show_alert=True)
    if timer_row(bid): return await cb.answer("Таймер уже установлен.",show_alert=True)
    now,s=datetime.now(TZ),slot(b[6],b[7])
    if now<s-timedelta(minutes=5) or now>=s+timedelta(hours=1): return await cb.answer("Сейчас таймер для этой записи недоступен.",show_alert=True)
    vals=history(b[1]); await cb.answer()
    if not vals:
        await state.set_state(TimerInput.minutes); await state.update_data(bid=bid); return await cb.message.answer("⏱ Сколько минут показывает машинка?")
    await cb.message.answer("⏱ Сколько минут показывает машинка?",reply_markup=choice_kb(bid,vals))

@router.callback_query(F.data.startswith("lf_pick_"))
async def pick_timer(cb:types.CallbackQuery,state:FSMContext):
    try: bid_s,m_s=cb.data.removeprefix("lf_pick_").rsplit("_",1); bid,m=int(bid_s),int(m_s)
    except Exception: return await cb.answer("Некорректная кнопка.",show_alert=True)
    b=booking(bid)
    if not b or int(b[2])!=int(cb.from_user.id): return await cb.answer("Запись не найдена.",show_alert=True)
    if not(MIN_T<=m<=MAX_T): return await cb.answer("Некорректное время.",show_alert=True)
    if not await set_timer(bid,m): return await cb.answer("Таймер уже недоступен или установлен.",show_alert=True)
    await state.clear(); await cb.answer()
    try: await cb.message.edit_text(f"✅ Таймер установлен на {m} минут.")
    except Exception: await cb.message.answer(f"✅ Таймер установлен на {m} минут.")

@router.callback_query(F.data.startswith("lf_other_"))
async def other_timer(cb:types.CallbackQuery,state:FSMContext):
    try: bid=int(cb.data.removeprefix("lf_other_"))
    except Exception: return await cb.answer("Некорректная кнопка.",show_alert=True)
    b=booking(bid)
    if not b or int(b[2])!=int(cb.from_user.id): return await cb.answer("Запись не найдена.",show_alert=True)
    await cb.answer(); await state.set_state(TimerInput.minutes); await state.update_data(bid=bid); await cb.message.answer("⏱ Сколько минут показывает машинка?")

@router.message(TimerInput.minutes)
async def typed_timer(msg:types.Message,state:FSMContext):
    d=await state.get_data()
    try: bid,m=int(d.get("bid")),int((msg.text or "").strip())
    except Exception: return await msg.answer("❌ Укажите время от 30 до 60 минут.")
    b=booking(bid)
    if not b or int(b[2])!=int(msg.from_user.id): await state.clear(); return await msg.answer("Запись больше недоступна.")
    if not(MIN_T<=m<=MAX_T): return await msg.answer("❌ Укажите время от 30 до 60 минут.")
    ok=await set_timer(bid,m); await state.clear(); await msg.answer(f"✅ Таймер установлен на {m} минут." if ok else "Таймер уже недоступен или установлен.")

@router.callback_query(F.data.startswith("lf_foreign_"))
async def foreign(cb:types.CallbackQuery):
    try: a,z=cb.data.removeprefix("lf_foreign_").rsplit("_",1); cur,prev=int(a),int(z)
    except Exception: return await cb.answer("Некорректная кнопка.",show_alert=True)
    b,p=booking(cur),booking(prev)
    if not b or not p or int(b[2])!=int(cb.from_user.id) or b[3]!=p[3] or ds(b[6])!=ds(p[6]) or int(p[7])+1!=int(b[7]): return await cb.answer("Предыдущая запись уже неактуальна.",show_alert=True)
    now=datetime.now(TZ); s=slot(b[6],b[7]); e=timer_end(prev)
    if not(s<=now<s+timedelta(hours=1)) or (e and e>now): return await cb.answer("Эта кнопка сейчас недоступна.",show_alert=True)
    with get_conn() as c: r=c.execute("SELECT last_sent_at FROM foreign_nudges WHERE previous_booking_id=?",(prev,)).fetchone()
    if r:
        try:
            left=timedelta(minutes=COOLDOWN)-(now-datetime.fromisoformat(str(r[0])))
            if left.total_seconds()>0: return await cb.answer(f"Уведомление уже отправлено. Повторить можно через {max(1,math.ceil(left.total_seconds()/60))} мин.",show_alert=True)
        except Exception: pass
    if BOT is None or int(p[2])<=0: return await cb.answer("Не удалось уведомить предыдущего пользователя.",show_alert=True)
    try: await BOT.send_message(int(p[2]),f"⚠️ <b>Ваша запись уже закончилась.</b>\nСледующий пользователь сообщает, что вещи всё ещё находятся в машине <b>{html.escape(str(p[5]))}</b>.\nПожалуйста, заберите их.",parse_mode="HTML")
    except Exception: return await cb.answer("Не удалось уведомить предыдущего пользователя.",show_alert=True)
    with get_conn() as c: c.execute("INSERT INTO foreign_nudges(previous_booking_id,last_sent_at) VALUES(?,?) ON CONFLICT(previous_booking_id) DO UPDATE SET last_sent_at=excluded.last_sent_at",(prev,now.isoformat()))
    await cb.answer("Предыдущему пользователю отправлено уведомление ✅",show_alert=True)

def users_kb(page,total):
    mp=max(0,(total-1)//PAGE); nav=[]
    if page>0: nav.append(B(text="‹",callback_data=f"lf_users_{page-1}"))
    nav.append(B(text=f"{page+1}/{mp+1}",callback_data="lf_users_noop"))
    if page<mp: nav.append(B(text="›",callback_data=f"lf_users_{page+1}"))
    return K(inline_keyboard=[nav,[B(text="⬅️ В админку",callback_data="admin_extra_home")]])

@router.callback_query(F.data=="lf_users_noop")
async def users_noop(cb): await cb.answer()

@router.callback_query(F.data.startswith("lf_users_"))
async def users(cb:types.CallbackQuery):
    if not is_admin(cb.from_user.id): return await cb.answer("🚫 Нет доступа.",show_alert=True)
    try: page=max(0,int(cb.data.removeprefix("lf_users_")))
    except Exception: page=0
    with get_conn() as c:
        total=int(c.execute("SELECT COUNT(*) FROM users").fetchone()[0]); mp=max(0,(total-1)//PAGE); page=min(page,mp)
        rows=c.execute("SELECT tg_id,surname,room,username FROM users ORDER BY id LIMIT ? OFFSET ?",(PAGE,page*PAGE)).fetchall()
    lines=[f"👥 <b>Пользователи бота</b> — {total}"]
    for i,(tg,su,ro,un) in enumerate(rows,start=page*PAGE+1):
        surname,room=html.escape(str(_b64d_try(su) or "—")),html.escape(str(_b64d_try(ro) or "—")); uname=f"@{html.escape(str(un))}" if un else "без username"
        lines.append(f"<b>{i}. {surname}</b> · комн. {room}\n{uname} · <code>{int(tg)}</code>")
    await cb.answer()
    try: await cb.message.edit_text("\n\n".join(lines),parse_mode="HTML",reply_markup=users_kb(page,total))
    except TelegramBadRequest: pass

@router.message(F.text=="🧺 Записаться")
async def book_sync(msg:types.Message):
    with get_conn() as c: c.execute("UPDATE users SET username=? WHERE tg_id=?",(msg.from_user.username or None,int(msg.from_user.id)))
    from handlers.booking import choose_date_first
    await choose_date_first(msg)

ORIG=database.get_user_bookings_today
INSTALLED=False

def admin_limit(uid,date,typ):
    with get_conn() as c: r=c.execute("SELECT tg_id FROM users WHERE id=?",(int(uid),)).fetchone()
    return False if r and is_admin(r[0]) else ORIG(uid,date,typ)

def install_feature_hooks():
    global INSTALLED
    if INSTALLED: return
    from handlers import booking,admin_extra,admin
    booking.get_user_bookings_today=admin_limit; booking.schedule_reminder=schedule_reminder; admin_extra.get_user_bookings_today=admin_limit
    if hasattr(admin,"get_user_bookings_today"): admin.get_user_bookings_today=admin_limit
    if not hasattr(admin_extra,"_lf_original_menu"):
        admin_extra._lf_original_menu=admin_extra._admin_menu; original=admin_extra._admin_menu
        def menu():
            kb=original(); rows=[list(x) for x in kb.inline_keyboard]; rows.insert(max(0,len(rows)-1),[B(text="👥 Пользователи",callback_data="lf_users_0")]); return K(inline_keyboard=rows)
        admin_extra._admin_menu=menu
    INSTALLED=True

async def rebuild_feature_jobs(hours=48):
    now,end=datetime.now(TZ),datetime.now(TZ)+timedelta(hours=hours)
    with get_conn() as c:
        rows=c.execute("SELECT u.tg_id,m.name,b.date,b.hour FROM bookings b JOIN users u ON u.id=b.user_id JOIN machines m ON m.id=b.machine_id WHERE (b.date>? OR (b.date=? AND b.hour>=?)) AND (b.date<? OR (b.date=? AND b.hour<=?))",(now.date().isoformat(),now.date().isoformat(),max(0,now.hour-1),end.date().isoformat(),end.date().isoformat(),end.hour)).fetchall()
    for tg,name,date,h in rows: await schedule_reminder(int(tg),str(name),ds(date),int(h),30)
    with get_conn() as c: timers=c.execute("SELECT lt.booking_id,lt.ends_at FROM laundry_timers lt JOIN bookings b ON b.id=lt.booking_id WHERE lt.pickup_sent=0").fetchall()
    for bid,e in timers:
        try: enddt=datetime.fromisoformat(str(e))
        except Exception: continue
        if enddt>now: schedule_pickup(int(bid),enddt)
