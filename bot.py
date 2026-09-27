"""
Kunlik xarajatlarni kuzatuvchi Telegram bot
Texnologiya: Python 3.10+, aiogram 3.x, SQLite

O'RNATISH:
    pip install aiogram

ISHGA TUSHIRISH:
    Linux/Mac:  export BOT_TOKEN="sizning_tokeningiz"
    Windows:    set BOT_TOKEN=sizning_tokeningiz
    python bot.py

ESLATMA: Tokenni hech qachon kodga yoki chatga ochiq yozmang.
Muhit o'zgaruvchisi (environment variable) orqali bering.
"""

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN muhit o'zgaruvchisi topilmadi. "
        "Terminalda: export BOT_TOKEN='sizning_tokeningiz' (Linux/Mac) "
        "yoki set BOT_TOKEN=sizning_tokeningiz (Windows)"
    )

DB_PATH = "expenses.db"

DEFAULT_CATEGORIES = ["Yo'l kira", "Ovqatlanish", "Bozorlik", "Aloqa", "Boshqa"]


# ---------------------------------------------------------------------------
# Ma'lumotlar bazasi
# ---------------------------------------------------------------------------

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            amount INTEGER NOT NULL,
            date TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            UNIQUE(user_id, name)
        )
        """
    )
    conn.commit()
    conn.close()


def ensure_default_categories(user_id: int):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM categories WHERE user_id = ?", (user_id,))
    if cur.fetchone()["c"] == 0:
        for cat in DEFAULT_CATEGORIES:
            cur.execute(
                "INSERT OR IGNORE INTO categories (user_id, name) VALUES (?, ?)",
                (user_id, cat),
            )
        conn.commit()
    conn.close()


def get_categories(user_id: int) -> list[str]:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT name FROM categories WHERE user_id = ?", (user_id,))
    rows = [r["name"] for r in cur.fetchall()]
    conn.close()
    return rows


def add_category(user_id: int, name: str) -> bool:
    conn = db_connect()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO categories (user_id, name) VALUES (?, ?)", (user_id, name)
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def remove_category(user_id: int, name: str) -> bool:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM categories WHERE user_id = ? AND name = ?", (user_id, name)
    )
    changed = cur.rowcount > 0
    conn.commit()
    conn.close()
    return changed


def add_expense(user_id: int, category: str, amount: int) -> int:
    conn = db_connect()
    cur = conn.cursor()
    today = datetime.now().strftime("%Y-%m-%d")
    cur.execute(
        "INSERT INTO expenses (user_id, category, amount, date) VALUES (?, ?, ?, ?)",
        (user_id, category, amount, today),
    )
    conn.commit()
    expense_id = cur.lastrowid
    conn.close()
    return expense_id


def delete_last_expense(user_id: int) -> sqlite3.Row | None:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM expenses WHERE user_id = ? ORDER BY id DESC LIMIT 1",
        (user_id,),
    )
    row = cur.fetchone()
    if row:
        cur.execute("DELETE FROM expenses WHERE id = ?", (row["id"],))
        conn.commit()
    conn.close()
    return row


def get_expenses_between(user_id: int, start_date: str, end_date: str) -> list[sqlite3.Row]:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM expenses
        WHERE user_id = ? AND date >= ? AND date <= ?
        ORDER BY created_at
        """,
        (user_id, start_date, end_date),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Yordamchi funksiyalar
# ---------------------------------------------------------------------------

def fmt_amount(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def build_report(rows: list[sqlite3.Row], title: str) -> str:
    if not rows:
        return f"📊 {title}\n\nHozircha xarajat kiritilmagan."

    totals: dict[str, int] = {}
    grand_total = 0
    for r in rows:
        totals[r["category"]] = totals.get(r["category"], 0) + r["amount"]
        grand_total += r["amount"]

    lines = [f"📊 {title}\n"]
    for cat, amt in sorted(totals.items(), key=lambda x: -x[1]):
        lines.append(f"• {cat}: {fmt_amount(amt)} so'm")
    lines.append(f"\n💰 Jami: {fmt_amount(grand_total)} so'm")
    return "\n".join(lines)


def categories_keyboard(user_id: int) -> InlineKeyboardMarkup:
    cats = get_categories(user_id)
    buttons = [
        [InlineKeyboardButton(text=cat, callback_data=f"cat:{cat}")] for cat in cats
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def after_add_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Oxirgisini bekor qilish", callback_data="undo")]
        ]
    )


def category_manage_keyboard(user_id: int) -> InlineKeyboardMarkup:
    cats = get_categories(user_id)
    buttons = [
        [InlineKeyboardButton(text=f"❌ {cat}", callback_data=f"delcat:{cat}")]
        for cat in cats
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------------------------------------------------------------------------
# FSM holatlari
# ---------------------------------------------------------------------------

class AddExpense(StatesGroup):
    waiting_amount = State()


class AddCategory(StatesGroup):
    waiting_name = State()


# ---------------------------------------------------------------------------
# Bot va dispatcher
# ---------------------------------------------------------------------------

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


@dp.message(CommandStart())
async def cmd_start(message: Message):
    ensure_default_categories(message.from_user.id)
    text = (
        "👋 Salom! Men kunlik xarajatlaringizni kuzatib boruvchi botman.\n\n"
        "Buyruqlar:\n"
        "➕ Xarajat qo'shish uchun pastdagi tugmalardan foydalaning\n"
        "/xarajat — yangi xarajat kiritish\n"
        "/bugun — bugungi hisobot\n"
        "/hafta — haftalik hisobot\n"
        "/oy — oylik hisobot\n"
        "/kategoriya — kategoriyalarni boshqarish"
    )
    await message.answer(text)
    await cmd_add_expense(message)


@dp.message(Command("xarajat"))
async def cmd_add_expense(message: Message):
    ensure_default_categories(message.from_user.id)
    await message.answer(
        "Qaysi kategoriyaga xarajat qo'shmoqchisiz?",
        reply_markup=categories_keyboard(message.from_user.id),
    )


@dp.callback_query(F.data.startswith("cat:"))
async def on_category_chosen(callback: CallbackQuery, state: FSMContext):
    category = callback.data.split("cat:", 1)[1]
    await state.update_data(category=category)
    await state.set_state(AddExpense.waiting_amount)
    await callback.message.answer(f"«{category}» tanlandi. Summani kiriting (faqat raqam):")
    await callback.answer()


@dp.message(AddExpense.waiting_amount)
async def on_amount_entered(message: Message, state: FSMContext):
    text = message.text.strip().replace(" ", "").replace(",", "")
    if not text.isdigit():
        await message.answer("⚠️ Iltimos, faqat raqam kiriting (masalan: 15000).")
        return

    amount = int(text)
    data = await state.get_data()
    category = data["category"]
    add_expense(message.from_user.id, category, amount)
    await state.clear()

    await message.answer(
        f"✅ {category} — {fmt_amount(amount)} so'm qo'shildi",
        reply_markup=after_add_keyboard(),
    )


@dp.callback_query(F.data == "undo")
async def on_undo(callback: CallbackQuery):
    row = delete_last_expense(callback.from_user.id)
    if row:
        await callback.message.answer(
            f"🗑 O'chirildi: {row['category']} — {fmt_amount(row['amount'])} so'm"
        )
    else:
        await callback.message.answer("O'chiradigan yozuv topilmadi.")
    await callback.answer()


@dp.message(Command("bugun"))
async def cmd_today(message: Message):
    today = datetime.now().strftime("%Y-%m-%d")
    rows = get_expenses_between(message.from_user.id, today, today)
    await message.answer(build_report(rows, "Bugungi xarajatlar"))


@dp.message(Command("hafta"))
async def cmd_week(message: Message):
    today = datetime.now()
    start = today - timedelta(days=today.weekday())
    rows = get_expenses_between(
        message.from_user.id,
        start.strftime("%Y-%m-%d"),
        today.strftime("%Y-%m-%d"),
    )
    await message.answer(build_report(rows, "Haftalik xarajatlar"))


@dp.message(Command("oy"))
async def cmd_month(message: Message):
    today = datetime.now()
    start = today.replace(day=1)
    rows = get_expenses_between(
        message.from_user.id,
        start.strftime("%Y-%m-%d"),
        today.strftime("%Y-%m-%d"),
    )
    await message.answer(build_report(rows, "Oylik xarajatlar"))


@dp.message(Command("kategoriya"))
async def cmd_category(message: Message):
    ensure_default_categories(message.from_user.id)
    kb = category_manage_keyboard(message.from_user.id)
    kb.inline_keyboard.append(
        [InlineKeyboardButton(text="➕ Yangi kategoriya", callback_data="newcat")]
    )
    await message.answer(
        "Kategoriyalarni boshqarish.\nO'chirish uchun bosing, yoki yangi qo'shing:",
        reply_markup=kb,
    )


@dp.callback_query(F.data == "newcat")
async def on_new_category(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddCategory.waiting_name)
    await callback.message.answer("Yangi kategoriya nomini kiriting:")
    await callback.answer()


@dp.message(AddCategory.waiting_name)
async def on_category_name_entered(message: Message, state: FSMContext):
    name = message.text.strip()
    await state.clear()
    if add_category(message.from_user.id, name):
        await message.answer(f"✅ «{name}» kategoriyasi qo'shildi.")
    else:
        await message.answer("⚠️ Bu kategoriya allaqachon mavjud.")


@dp.callback_query(F.data.startswith("delcat:"))
async def on_delete_category(callback: CallbackQuery):
    name = callback.data.split("delcat:", 1)[1]
    if remove_category(callback.from_user.id, name):
        await callback.message.answer(f"🗑 «{name}» kategoriyasi o'chirildi.")
    else:
        await callback.message.answer("Kategoriya topilmadi.")
    await callback.answer()


# ---------------------------------------------------------------------------
# Ishga tushirish
# ---------------------------------------------------------------------------

async def main():
    init_db()
    print("Bot ishga tushdi...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
