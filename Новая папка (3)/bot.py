"""
Курс сататын Telegram бот.

Мүмкіндіктері:
  • Курстар каталогы
  • Төлем: Telegram Stars (автоматты) және Kaspi аударымы (чек + админ растауы)
  • Төлемнен кейін жабық арнаға бір реттік сілтеме береді
  • «Менің курстарым» — жоғалған сілтемені қайта алу
  • Қолдау: пайдаланушы жазады, админ reply арқылы жауап береді
  • Админ командалары: /stats, /grant <user_id> <course_id>

Іске қосу:
    pip install -r requirements.txt
    .env файлын толтырыңыз (.env.example қараңыз)
    python bot.py
"""

import asyncio
import html
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from dotenv import load_dotenv

load_dotenv()

# ======================= БАПТАУЛАР =======================

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN табылмады. .env файлына BOT_TOKEN=... деп жазыңыз.")

# Админдердің Telegram ID-лері, үтір арқылы: 123456,789012
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
}

# Kaspi төлем деректемелері (клиентке көрсетіледі)
KASPI_INFO = os.getenv("KASPI_INFO", "Kaspi Gold: +7 7XX XXX XX XX (Аты-жөніңіз)")

DB_PATH = os.getenv("DB_PATH", "bot.db")

# Сілтеме қанша сағат жарамды болады
INVITE_LIFETIME_HOURS = 24

# Курстар тізімі.
#   ключ      — қысқа латынша ID (callback үшін, 20 таңбадан аспасын)
#   title     — атауы (Stars төлемі үшін 32 таңбадан аспауы керек)
#   price_kzt — Kaspi бағасы, теңге
#   price_stars — Telegram Stars бағасы (0 қойсаңыз, Stars өшіріледі)
#   channel_id  — жабық арна ID-і (-100...). Бот сол арнада админ болуы керек
COURSES = {
    "python": {
        "title": "Python курсы",
        "description": "0-ден бастап Python үйренеміз: 30 сабақ, тапсырмалар, кері байланыс.",
        "price_kzt": 15000,
        "price_stars": 1500,
        "channel_id": -1001111111111,
    },
    "design": {
        "title": "Дизайн курсы",
        "description": "Figma және UI/UX негіздері: 20 сабақ және практикалық жобалар.",
        "price_kzt": 20000,
        "price_stars": 2000,
        "channel_id": -1002222222222,
    },
}

# ==========================================================

ALLOWED_STATUSES = {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.CREATOR,
}

router = Router()


class PayState(StatesGroup):
    receipt = State()


class SupportState(StatesGroup):
    message = State()


# ----------------------- ДЕРЕКТЕР ҚОРЫ -----------------------


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(db()) as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                joined_at TEXT
            );
            CREATE TABLE IF NOT EXISTS purchases(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                course_id TEXT,
                method TEXT,
                amount INTEGER,
                currency TEXT,
                charge_id TEXT,
                created_at TEXT,
                UNIQUE(user_id, course_id)
            );
            CREATE TABLE IF NOT EXISTS pending(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                course_id TEXT,
                status TEXT,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS support_map(
                admin_id INTEGER,
                msg_id INTEGER,
                user_id INTEGER,
                PRIMARY KEY(admin_id, msg_id)
            );
            """
        )
        c.commit()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save_user(user) -> None:
    with closing(db()) as c:
        c.execute(
            "INSERT INTO users(user_id, username, full_name, joined_at) "
            "VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
            "username=excluded.username, full_name=excluded.full_name",
            (user.id, user.username, user.full_name, now()),
        )
        c.commit()


def has_course(user_id: int, course_id: str) -> bool:
    with closing(db()) as c:
        row = c.execute(
            "SELECT 1 FROM purchases WHERE user_id=? AND course_id=?",
            (user_id, course_id),
        ).fetchone()
    return row is not None


def user_courses(user_id: int) -> list[str]:
    with closing(db()) as c:
        rows = c.execute(
            "SELECT course_id FROM purchases WHERE user_id=?", (user_id,)
        ).fetchall()
    return [r["course_id"] for r in rows if r["course_id"] in COURSES]


def add_purchase(user_id, course_id, method, amount, currency, charge_id=None) -> None:
    with closing(db()) as c:
        c.execute(
            "INSERT OR IGNORE INTO purchases"
            "(user_id, course_id, method, amount, currency, charge_id, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (user_id, course_id, method, amount, currency, charge_id, now()),
        )
        c.commit()


def save_map(admin_id: int, msg_id: int, user_id: int) -> None:
    with closing(db()) as c:
        c.execute(
            "INSERT OR REPLACE INTO support_map(admin_id, msg_id, user_id) VALUES(?,?,?)",
            (admin_id, msg_id, user_id),
        )
        c.commit()


def lookup_map(admin_id: int, msg_id: int):
    with closing(db()) as c:
        row = c.execute(
            "SELECT user_id FROM support_map WHERE admin_id=? AND msg_id=?",
            (admin_id, msg_id),
        ).fetchone()
    return row["user_id"] if row else None


# ----------------------- КӨМЕКШІЛЕР -----------------------


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def money(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def user_label(user) -> str:
    name = html.escape(user.full_name)
    uname = f" (@{html.escape(user.username)})" if user.username else ""
    return f"{name}{uname}"


def menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📚 Курстар", callback_data="catalog")],
            [InlineKeyboardButton(text="🎓 Менің курстарым", callback_data="my")],
            [InlineKeyboardButton(text="💬 Қолдау", callback_data="support")],
        ]
    )


def back_kb(target: str = "menu", text: str = "⬅️ Артқа") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=target)]]
    )


async def show(call: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    """Хабарламаны өңдейді, мүмкін болмаса жаңасын жібереді."""
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except TelegramAPIError:
        await call.message.answer(text, reply_markup=kb)


async def notify_admins(bot: Bot, text: str) -> None:
    for admin in ADMIN_IDS:
        try:
            await bot.send_message(admin, text)
        except TelegramAPIError as e:
            logging.warning("Админге жіберілмеді (%s): %s", admin, e)


async def send_invite(bot: Bot, user_id: int, course_id: str) -> bool:
    """Жабық арнаға бір реттік сілтеме жасап, пайдаланушыға жібереді."""
    course = COURSES[course_id]
    channel = course["channel_id"]

    try:
        member = await bot.get_chat_member(channel, user_id)
        if member.status in ALLOWED_STATUSES:
            await bot.send_message(
                user_id, f"✅ Сіз «{html.escape(course['title'])}» арнасында бұрыннан барсыз."
            )
            return True
    except TelegramAPIError:
        pass  # пайдаланушы арнада жоқ — сілтеме жасаймыз

    try:
        invite = await bot.create_chat_invite_link(
            chat_id=channel,
            name=f"u{user_id}",
            expire_date=datetime.now(timezone.utc)
            + timedelta(hours=INVITE_LIFETIME_HOURS),
            member_limit=1,
        )
    except TelegramAPIError as e:
        logging.error("Сілтеме жасау қатесі (%s): %s", course_id, e)
        await bot.send_message(
            user_id,
            "⚠️ Сілтеме жасау кезінде қате шықты. «🎓 Менің курстарым» бөлімінен "
            "қайталап көріңіз немесе қолдауға жазыңыз.",
        )
        await notify_admins(
            bot,
            f"⚠️ <code>{user_id}</code> үшін «{html.escape(course['title'])}» сілтемесі "
            f"жасалмады: {html.escape(str(e))}\nБот арнада админ ба және "
            f"«Invite users» құқығы бар ма?",
        )
        return False

    await bot.send_message(
        user_id,
        f"🎉 <b>{html.escape(course['title'])}</b> курсына қош келдіңіз!\n\n"
        f"🔐 Жабық арнаға сілтеме (бір рет қана, {INVITE_LIFETIME_HOURS} сағат жарамды):\n"
        f"{invite.invite_link}\n\n"
        "Сілтемені басқа адамға бермеңіз — ол тек бір адамға жұмыс істейді.",
    )
    return True


# ----------------------- НЕГІЗГІ МӘЗІР -----------------------


@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    save_user(message.from_user)
    if is_admin(message.from_user.id):
        await message.answer(
            "🛠 <b>Админ панель</b>\n\nҚажетті бөлімді таңдаңыз:",
            reply_markup=admin_menu_kb(),
        )
        return
    await message.answer(
        f"👋 Сәлем, {html.escape(message.from_user.first_name)}!\n\n"
        "Мұнда курстарға жазылып, жабық арналарға қолжетімділік ала аласыз.",
        reply_markup=menu_kb(),
    )


@router.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(call, "Басты мәзір 👇", menu_kb())
    await call.answer()


@router.callback_query(F.data == "catalog")
async def cb_catalog(call: CallbackQuery):
    rows = [
        [
            InlineKeyboardButton(
                text=f"{c['title']} — {money(c['price_kzt'])} ₸",
                callback_data=f"course:{cid}",
            )
        ]
        for cid, c in COURSES.items()
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Артқа", callback_data="menu")])
    await show(call, "📚 <b>Курстар каталогы</b>\n\nКурсты таңдаңыз:", InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@router.callback_query(F.data.startswith("course:"))
async def cb_course(call: CallbackQuery):
    cid = call.data.split(":", 1)[1]
    course = COURSES.get(cid)
    if not course:
        await call.answer("Курс табылмады", show_alert=True)
        return

    price_line = f"💰 Бағасы: <b>{money(course['price_kzt'])} ₸</b>"
    if course["price_stars"]:
        price_line += f" немесе <b>{course['price_stars']} ⭐</b>"

    text = f"<b>{html.escape(course['title'])}</b>\n\n{html.escape(course['description'])}\n\n{price_line}"

    if has_course(call.from_user.id, cid):
        buttons = [[InlineKeyboardButton(text="🔐 Арнаға сілтеме алу", callback_data=f"get:{cid}")]]
        text += "\n\n✅ Бұл курс сізде бар."
    else:
        buttons = []
        if course["price_stars"]:
            buttons.append([InlineKeyboardButton(text="⭐ Telegram Stars-пен төлеу", callback_data=f"stars:{cid}")])
        buttons.append([InlineKeyboardButton(text="💳 Kaspi-мен төлеу", callback_data=f"kaspi:{cid}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Артқа", callback_data="catalog")])

    await show(call, text, InlineKeyboardMarkup(inline_keyboard=buttons))
    await call.answer()


@router.callback_query(F.data == "my")
async def cb_my(call: CallbackQuery):
    owned = user_courses(call.from_user.id)
    if not owned:
        await show(
            call,
            "Сізде әзірге курс жоқ. «📚 Курстар» бөлімінен таңдаңыз.",
            back_kb(),
        )
    else:
        rows = [
            [
                InlineKeyboardButton(
                    text=f"🔐 {COURSES[cid]['title']}", callback_data=f"get:{cid}"
                )
            ]
            for cid in owned
        ]
        rows.append([InlineKeyboardButton(text="⬅️ Артқа", callback_data="menu")])
        await show(
            call,
            "🎓 <b>Менің курстарым</b>\n\nСілтемені қайта алу үшін курсты басыңыз:",
            InlineKeyboardMarkup(inline_keyboard=rows),
        )
    await call.answer()


@router.callback_query(F.data.startswith("get:"))
async def cb_get(call: CallbackQuery, bot: Bot):
    cid = call.data.split(":", 1)[1]
    if cid not in COURSES or not has_course(call.from_user.id, cid):
        await call.answer("Бұл курс сізде жоқ", show_alert=True)
        return
    await call.answer()
    await send_invite(bot, call.from_user.id, cid)


# ----------------------- TELEGRAM STARS ТӨЛЕМІ -----------------------


@router.callback_query(F.data.startswith("stars:"))
async def cb_stars(call: CallbackQuery, bot: Bot):
    cid = call.data.split(":", 1)[1]
    course = COURSES.get(cid)
    if not course or not course["price_stars"]:
        await call.answer("Қолжетімсіз", show_alert=True)
        return
    if has_course(call.from_user.id, cid):
        await call.answer("Бұл курс сізде бар", show_alert=True)
        return

    await call.answer()
    await bot.send_invoice(
        chat_id=call.from_user.id,
        title=course["title"][:32],
        description=course["description"][:255],
        payload=f"course:{cid}",
        provider_token="",  # Stars үшін бос
        currency="XTR",
        prices=[LabeledPrice(label=course["title"][:32], amount=course["price_stars"])],
    )


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    cid = query.invoice_payload.split(":", 1)[-1]
    if cid not in COURSES:
        await query.answer(ok=False, error_message="Курс табылмады")
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def paid(message: Message, bot: Bot):
    sp = message.successful_payment
    cid = sp.invoice_payload.split(":", 1)[-1]
    if cid not in COURSES:
        return
    uid = message.from_user.id
    add_purchase(uid, cid, "stars", sp.total_amount, sp.currency, sp.telegram_payment_charge_id)
    await message.answer("✅ Төлем сәтті өтті! Рахмет!")
    await send_invite(bot, uid, cid)
    await notify_admins(
        bot,
        f"💰 Жаңа төлем (Stars)\n👤 {user_label(message.from_user)} <code>{uid}</code>\n"
        f"📚 {html.escape(COURSES[cid]['title'])} — {sp.total_amount} ⭐",
    )


# ----------------------- KASPI ТӨЛЕМІ (ЧЕК АРҚЫЛЫ) -----------------------


@router.callback_query(F.data.startswith("kaspi:"))
async def cb_kaspi(call: CallbackQuery, state: FSMContext):
    cid = call.data.split(":", 1)[1]
    course = COURSES.get(cid)
    if not course:
        await call.answer("Курс табылмады", show_alert=True)
        return
    if has_course(call.from_user.id, cid):
        await call.answer("Бұл курс сізде бар", show_alert=True)
        return

    await state.set_state(PayState.receipt)
    await state.update_data(course_id=cid)
    await show(
        call,
        f"💳 <b>Kaspi арқылы төлем</b>\n\n"
        f"Курс: {html.escape(course['title'])}\n"
        f"Сома: <b>{money(course['price_kzt'])} ₸</b>\n\n"
        f"Төлем жасайтын деректеме:\n<code>{html.escape(KASPI_INFO)}</code>\n\n"
        "Төлегеннен кейін <b>чектің скриншотын (фото немесе PDF)</b> осы чатқа жіберіңіз. "
        "Админ тексеріп, растағаннан кейін сілтеме келеді.",
        back_kb("cancel", "❌ Бас тарту"),
    )
    await call.answer()


@router.callback_query(F.data == "cancel")
async def cb_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(call, "Бас тартылды. Басты мәзір 👇", menu_kb())
    await call.answer()


@router.message(PayState.receipt, F.photo | F.document)
async def got_receipt(message: Message, state: FSMContext, bot: Bot):
    if not ADMIN_IDS:
        await message.answer("⚠️ Қазір төлемді тексеретін админ жоқ. Кейінірек қайталаңыз.")
        return

    data = await state.get_data()
    cid = data.get("course_id")
    if cid not in COURSES:
        await state.clear()
        await message.answer("Қате шықты, қайта бастаңыз: /start")
        return

    with closing(db()) as c:
        cur = c.execute(
            "INSERT INTO pending(user_id, course_id, status, created_at) VALUES(?,?,?,?)",
            (message.from_user.id, cid, "pending", now()),
        )
        pid = cur.lastrowid
        c.commit()

    course = COURSES[cid]
    caption = (
        f"🧾 <b>Жаңа төлем чегі №{pid}</b>\n"
        f"👤 {user_label(message.from_user)}\n"
        f"🆔 <code>{message.from_user.id}</code>\n"
        f"📚 {html.escape(course['title'])}\n"
        f"💰 {money(course['price_kzt'])} ₸"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Растау", callback_data=f"ok:{pid}"),
                InlineKeyboardButton(text="❌ Қабылдамау", callback_data=f"no:{pid}"),
            ]
        ]
    )
    sent = 0
    for admin in ADMIN_IDS:
        try:
            await message.copy_to(admin, caption=caption, reply_markup=kb)
            sent += 1
        except TelegramAPIError as e:
            logging.warning("Админге чек жіберілмеді (%s): %s", admin, e)

    await state.clear()
    if sent:
        await message.answer(
            "✅ Чек қабылданды. Тексеру аяқталғанда сізге хабарлаймыз.",
            reply_markup=back_kb("menu", "🏠 Басты мәзір"),
        )
    else:
        await message.answer("⚠️ Чекті админге жеткізу мүмкін болмады. Қолдауға жазыңыз.")


@router.message(PayState.receipt)
async def receipt_wrong(message: Message):
    await message.answer("Чектің фотосын немесе файлын жіберіңіз (немесе /start).")


@router.callback_query(F.data.startswith("ok:") | F.data.startswith("no:"))
async def admin_decision(call: CallbackQuery, bot: Bot):
    if not is_admin(call.from_user.id):
        await call.answer("Рұқсат жоқ", show_alert=True)
        return

    action, pid_str = call.data.split(":", 1)
    pid = int(pid_str)

    with closing(db()) as c:
        row = c.execute("SELECT * FROM pending WHERE id=?", (pid,)).fetchone()
        if not row or row["status"] != "pending":
            await call.answer("Бұл өтінім өңделіп қойған", show_alert=True)
            return
        new_status = "approved" if action == "ok" else "rejected"
        c.execute("UPDATE pending SET status=? WHERE id=?", (new_status, pid))
        c.commit()

    uid, cid = row["user_id"], row["course_id"]
    course = COURSES.get(cid)

    if action == "ok" and course:
        add_purchase(uid, cid, "kaspi", course["price_kzt"], "KZT")
        ok = await send_invite(bot, uid, cid)
        if not ok:
            with closing(db()) as c:  # сілтеме жасалмаса, өтінімді қайта ашамыз
                c.execute("UPDATE pending SET status='pending' WHERE id=?", (pid,))
                c.commit()
            await call.answer("Сілтеме жасалмады. Бот арнада админ ба?", show_alert=True)
            return
        suffix = f"\n\n✅ Расталды ({html.escape(call.from_user.full_name)})"
    else:
        try:
            await bot.send_message(
                uid,
                "❌ Төлемді растау мүмкін болмады. Чек дұрыс емес болса, "
                "«💬 Қолдау» арқылы жазыңыз.",
            )
        except TelegramAPIError:
            pass
        suffix = f"\n\n❌ Қабылданбады ({html.escape(call.from_user.full_name)})"

    try:
        await call.message.edit_caption(
            caption=(call.message.html_text or "") + suffix, reply_markup=None
        )
    except TelegramAPIError:
        pass
    await call.answer("Дайын")


# ----------------------- ҚОЛДАУ -----------------------


@router.callback_query(F.data == "support")
async def cb_support(call: CallbackQuery, state: FSMContext):
    await state.set_state(SupportState.message)
    await show(
        call,
        "💬 Сұрағыңызды жазыңыз — қолдау қызметі жақын арада осы чатта жауап береді.",
        back_kb("cancel", "❌ Бас тарту"),
    )
    await call.answer()


@router.message(SupportState.message)
async def support_message(message: Message, state: FSMContext, bot: Bot):
    if not ADMIN_IDS:
        await message.answer("⚠️ Қазір қолдау қызметі қолжетімсіз.")
        await state.clear()
        return

    uid = message.from_user.id
    header = f"💬 <b>Қолдау сұрауы</b>\n👤 {user_label(message.from_user)}\n🆔 <code>{uid}</code>"
    if message.text:
        header += f"\n\n{html.escape(message.text[:3500])}"

    for admin in ADMIN_IDS:
        try:
            h = await bot.send_message(admin, header)
            save_map(admin, h.message_id, uid)
            if not message.text:
                m = await message.copy_to(admin)
                save_map(admin, m.message_id, uid)
        except TelegramAPIError as e:
            logging.warning("Қолдау хабары админге жетпеді (%s): %s", admin, e)

    await state.clear()
    await message.answer(
        "✅ Хабарламаңыз жіберілді. Жауапты осы чатта аласыз.",
        reply_markup=back_kb("menu", "🏠 Басты мәзір"),
    )


# Админ қолдау хабарламасына reply жасаса — жауап пайдаланушыға барады
@router.message(F.reply_to_message, F.from_user.id.in_(ADMIN_IDS))
async def admin_reply(message: Message, bot: Bot):
    uid = lookup_map(message.chat.id, message.reply_to_message.message_id)
    if not uid:
        return
    try:
        await bot.send_message(uid, "💬 <b>Қолдау қызметінің жауабы:</b>")
        await message.copy_to(uid)
        await message.reply("✅ Жіберілді")
    except TelegramAPIError as e:
        await message.reply(f"⚠️ Жіберілмеді: {html.escape(str(e))}")


# ----------------------- АДМИН КОМАНДАЛАРЫ -----------------------


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not is_admin(message.from_user.id):
        return
    with closing(db()) as c:
        users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        rows = c.execute(
            "SELECT currency, COUNT(*) AS n, SUM(amount) AS s FROM purchases GROUP BY currency"
        ).fetchall()
        pend = c.execute("SELECT COUNT(*) FROM pending WHERE status='pending'").fetchone()[0]
    lines = [f"👥 Пайдаланушылар: <b>{users}</b>", f"🕓 Күтіп тұрған чектер: <b>{pend}</b>", ""]
    for r in rows:
        lines.append(f"{r['currency'] or '—'}: {r['n']} сатылым, сома {r['s'] or 0}")
    await message.answer("\n".join(lines))


@router.message(Command("grant"))
async def cmd_grant(message: Message, bot: Bot):
    """/grant <user_id> <course_id> — қолмен қолжетімділік беру"""
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in COURSES:
        await message.answer(
            "Қолданылуы: <code>/grant user_id course_id</code>\n"
            f"Курстар: {', '.join(COURSES)}"
        )
        return
    uid, cid = int(parts[1]), parts[2]
    add_purchase(uid, cid, "manual", 0, "-")
    ok = await send_invite(bot, uid, cid)
    await message.answer("✅ Жіберілді" if ok else "⚠️ Сілтеме жіберілмеді (лог қараңыз)")


# ----------------------- БАСҚА ХАБАРЛАМАЛАР -----------------------


@router.message(StateFilter(None), F.text)
async def fallback(message: Message):
    await message.answer("Төмендегі мәзірді қолданыңыз 👇", reply_markup=menu_kb())


def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
            [InlineKeyboardButton(text="🎓 Курс беру", callback_data="admin_grant_help")],
            [InlineKeyboardButton(text="👤 Пайдаланушы мәзірі", callback_data="user_menu")],
        ]
    )


@router.callback_query(F.data == "admin_menu")
async def cb_admin_menu(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer("Рұқсат жоқ", show_alert=True)
        return
    await state.clear()
    await show(call, "🛠 <b>Админ панель</b>", admin_menu_kb())
    await call.answer()


@router.callback_query(F.data == "user_menu")
async def cb_user_menu(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(call, "👤 <b>Пайдаланушы мәзірі</b>", menu_kb())
    await call.answer()


@router.callback_query(F.data == "admin_grant_help")
async def cb_admin_grant_help(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Рұқсат жоқ", show_alert=True)
        return
    courses = ", ".join(COURSES)
    await show(
        call,
        "🎓 Курс беру үшін мына команданы қолданыңыз:\n"
        f"<code>/grant user_id course_id</code>\n\n"
        f"Қолжетімді курстар: <code>{html.escape(courses)}</code>",
        back_kb("admin_menu", "⬅️ Админ панеліне қайту"),
    )
    await call.answer()


@router.callback_query(F.data == "admin_stats")
async def cb_admin_stats(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("Рұқсат жоқ", show_alert=True)
        return
    with closing(db()) as c:
        users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        rows = c.execute(
            "SELECT currency, COUNT(*) AS n, SUM(amount) AS s FROM purchases GROUP BY currency"
        ).fetchall()
        pend = c.execute("SELECT COUNT(*) FROM pending WHERE status='pending'").fetchone()[0]
    lines = [
        f"👥 Пайдаланушылар: <b>{users}</b>",
        f"🕓 Күтудегі чектер: <b>{pend}</b>",
        "",
    ]
    for row in rows:
        lines.append(f"{row['currency'] or '—'}: {row['n']} сатып алу, сома {row['s'] or 0}")
    await show(
        call,
        "📊 <b>Статистика</b>\n\n" + "\n".join(lines),
        back_kb("admin_menu", "⬅️ Админ панеліне қайту"),
    )
    await call.answer()


async def main():
    logging.basicConfig(level=logging.INFO)
    init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
