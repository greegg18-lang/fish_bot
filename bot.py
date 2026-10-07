import asyncio
import csv
import io
import logging
import os
import shutil
import sqlite3
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, StateFilter
from aiogram.types import (
    Message, CallbackQuery, BufferedInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web

# ---------- НАСТРОЙКИ ----------
TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = 186453903   # ⚠️ ЗАМЕНИ НА СВОЙ ID
DB_PATH = "fish.db"
PORT = int(os.getenv("PORT", 8080))

logging.basicConfig(level=logging.INFO)

# ---------- БАЗА ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS product_prices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL,
        price_sale REAL NOT NULL,
        price_cost REAL NOT NULL,
        valid_from TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS batches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        archived_at TEXT,
        status TEXT DEFAULT 'open'
    );
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER,
        batch_id INTEGER,
        is_own INTEGER DEFAULT 0,
        created_at TEXT NOT NULL,
        status TEXT DEFAULT 'open',
        archived_at TEXT,
        paid INTEGER DEFAULT 0,
        paid_at TEXT
    );
    CREATE TABLE IF NOT EXISTS order_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        qty_pcs INTEGER NOT NULL,
        weight_kg REAL,
        weighed INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """)

    # миграция orders
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(orders)").fetchall()}
    if "batch_id" not in cols:
        conn.execute("ALTER TABLE orders ADD COLUMN batch_id INTEGER")
    if "is_own" not in cols:
        conn.execute("ALTER TABLE orders ADD COLUMN is_own INTEGER DEFAULT 0")
    if "customer_id" not in cols:
        conn.execute("ALTER TABLE orders ADD COLUMN customer_id INTEGER")
    if "paid" not in cols:
        conn.execute("ALTER TABLE orders ADD COLUMN paid INTEGER DEFAULT 0")
    if "paid_at" not in cols:
        conn.execute("ALTER TABLE orders ADD COLUMN paid_at TEXT")

    # миграция batches
    bcols = {r["name"] for r in conn.execute("PRAGMA table_info(batches)").fetchall()}
    if "archived_at" not in bcols:
        conn.execute("ALTER TABLE batches ADD COLUMN archived_at TEXT")
    if "status" not in bcols:
        conn.execute("ALTER TABLE batches ADD COLUMN status TEXT DEFAULT 'open'")

    # дефолтные настройки
    defaults = {
        "packaging_cost": "0",
        "payment_number": "",
        "payment_bank": "",
    }
    for k, v in defaults.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

    # если есть открытые заказы без партии — создаём партию №1
    orphans = conn.execute(
        "SELECT COUNT(*) AS c FROM orders WHERE batch_id IS NULL"
    ).fetchone()["c"]
    if orphans > 0:
        first_date = conn.execute(
            "SELECT MIN(created_at) AS d FROM orders WHERE batch_id IS NULL"
        ).fetchone()["d"] or datetime.now().isoformat(timespec="seconds")
        cur = conn.cursor()
        cur.execute("INSERT INTO batches (created_at, status) VALUES (?, 'open')", (first_date,))
        bid = cur.lastrowid
        conn.execute("UPDATE orders SET batch_id = ? WHERE batch_id IS NULL", (bid,))

    conn.commit()
    conn.close()

init_db()

# ---------- НАСТРОЙКИ ----------
def get_setting(key: str, default: str = "") -> str:
    conn = db()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default

def set_setting(key: str, value: str):
    conn = db()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()

def packaging_cost() -> float:
    try:
        return float(get_setting("packaging_cost", "0") or 0)
    except ValueError:
        return 0.0

# ---------- ПАРТИИ ----------
def get_open_batch():
    conn = db()
    row = conn.execute(
        "SELECT * FROM batches WHERE status = 'open' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return row

def create_batch():
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT INTO batches (created_at, status) VALUES (?, 'open')",
                (datetime.now().isoformat(timespec="seconds"),))
    bid = cur.lastrowid
    conn.commit()
    conn.close()
    return bid

def batch_stats(batch_id: int):
    conn = db()
    row = conn.execute("""
        SELECT COUNT(*) AS orders_cnt,
               SUM(CASE WHEN is_own = 1 THEN 1 ELSE 0 END) AS own_cnt,
               SUM(CASE WHEN is_own = 0 THEN 1 ELSE 0 END) AS client_cnt
        FROM orders WHERE batch_id = ?
    """, (batch_id,)).fetchone()
    conn.close()
    return row

# ---------- СОСТОЯНИЯ ----------
class OrderFSM(StatesGroup):
    choosing_customer = State()
    choosing_product = State()
    entering_qty = State()
    more_items = State()

class WeighFSM(StatesGroup):
    choosing = State()
    entering_weight = State()

class ReweighFSM(StatesGroup):
    choosing = State()
    entering_weight = State()

class CheckFSM(StatesGroup):
    choosing = State()

class StatsFSM(StatesGroup):
    choosing = State()

class FinanceFSM(StatesGroup):
    choosing = State()

class PartyFSM(StatesGroup):
    choosing = State()

class AddProductFSM(StatesGroup):
    entering = State()

class AddCustomerFSM(StatesGroup):
    entering = State()

class EditProductFSM(StatesGroup):
    choosing = State()
    choosing_field = State()
    entering_value = State()

class EditOrderFSM(StatesGroup):
    choosing_product = State()
    entering_qty_add = State()
    entering_qty_set = State()
    choosing_item_qty = State()
    choosing_item_del = State()

class ImportFSM(StatesGroup):
    waiting_file = State()

class PackagingFSM(StatesGroup):
    entering = State()

class PaymentFSM(StatesGroup):
    entering_number = State()
    entering_bank = State()

class NewBatchFSM(StatesGroup):
    confirm = State()

class CloseBatchFSM(StatesGroup):
    confirm = State()

# ---------- БОТ ----------
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

def is_admin(msg_or_cb) -> bool:
    return msg_or_cb.from_user.id == ADMIN_ID

# ---------- МЕНЮ ----------
def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📦 Новый заказ"), KeyboardButton(text="🏠 Мой заказ")],
            [KeyboardButton(text="📋 Заказы"),      KeyboardButton(text="⚖️ Взвесить")],
            [KeyboardButton(text="🔄 Перевесить"),  KeyboardButton(text="🧾 Чек")],
            [KeyboardButton(text="📤 Поставщик"),   KeyboardButton(text="📂 Партии")],
            [KeyboardButton(text="🗂 Архив заказов"), KeyboardButton(text="📈 Итоги партии")],
            [KeyboardButton(text="💰 Финансы"),     KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="📁 Экспорт"),     KeyboardButton(text="💾 Бэкап базы")],
            [KeyboardButton(text="📥 Импорт базы"), KeyboardButton(text="⚙️ Настройки")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )

def settings_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📦 Товары"),    KeyboardButton(text="👥 Клиенты")],
            [KeyboardButton(text="➕ Товар"),     KeyboardButton(text="✏️ Изменить товар")],
            [KeyboardButton(text="➕ Клиент"),    KeyboardButton(text="📦 Стоимость упаковки")],
            [KeyboardButton(text="💳 Реквизиты"), KeyboardButton(text="🏠 Меню")],
        ],
        resize_keyboard=True,
    )

def batches_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🟢 Открыть новую партию"), KeyboardButton(text="🔴 Закрыть текущую")],
            [KeyboardButton(text="📋 Список партий"),        KeyboardButton(text="🏠 Меню")],
        ],
        resize_keyboard=True,
    )

def kb(items, prefix):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"{prefix}:{i}")]
        for i, label in items
    ])

# ---------- ХЕЛПЕРЫ ----------
def products_list():
    conn = db()
    rows = conn.execute("SELECT id, name FROM products ORDER BY id").fetchall()
    conn.close()
    return rows

def customers_list():
    conn = db()
    rows = conn.execute("SELECT id, name FROM customers ORDER BY id").fetchall()
    conn.close()
    return rows

def open_orders():
    conn = db()
    rows = conn.execute("""
        SELECT o.id, o.is_own, o.paid, o.created_at, c.name AS customer
        FROM orders o
        LEFT JOIN customers c ON c.id = o.customer_id
        WHERE o.status = 'open'
        ORDER BY o.id DESC
    """).fetchall()
    conn.close()
    return rows

def get_current_prices(product_id):
    conn = db()
    row = conn.execute("""
        SELECT price_sale, price_cost FROM product_prices
        WHERE product_id = ? ORDER BY valid_from DESC, id DESC LIMIT 1
    """, (product_id,)).fetchone()
    conn.close()
    return row

def add_price_if_changed(product_id, price_sale, price_cost):
    conn = db()
    cur = conn.execute("""
        SELECT price_sale, price_cost FROM product_prices
        WHERE product_id = ? ORDER BY valid_from DESC, id DESC LIMIT 1
    """, (product_id,)).fetchone()
    if cur and abs(cur["price_sale"] - price_sale) < 0.001 \
           and abs(cur["price_cost"] - price_cost) < 0.001:
        conn.close()
        return False
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO product_prices (product_id, price_sale, price_cost, valid_from) "
        "VALUES (?, ?, ?, ?)",
        (product_id, price_sale, price_cost, now),
    )
    conn.commit()
    conn.close()
    return True

async def send_backup(chat_id: int, caption_prefix: str = ""):
    try:
        with open(DB_PATH, "rb") as f:
            data = f.read()
        filename = f"fish_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.db"
        caption = (caption_prefix + "\n" if caption_prefix else "") + (
            f"💾 Бэкап базы от {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"
            f"Сохрани файл — это полная копия всех данных бота."
        )
        await bot.send_document(
            chat_id=chat_id,
            document=BufferedInputFile(data, filename=filename),
            caption=caption,
        )
        return True
    except Exception as e:
        logging.error(f"Backup failed: {e}")
        await bot.send_message(chat_id=chat_id, text=f"⚠️ Не удалось отправить бэкап: {e}")
        return False

# ---------- СТАРТ ----------
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message):
        await message.answer("Нет доступа.")
        return
    await message.answer("🐟 Бот учёта рыбы.\n\nПользуйся кнопками снизу.",
                        reply_markup=main_menu())

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu())

# ---------- МЕНЮ ----------
@dp.message(F.text == "🏠 Меню")
async def btn_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Главное меню:", reply_markup=main_menu())

@dp.message(F.text == "⚙️ Настройки")
async def btn_settings(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message):
        return
    await message.answer("Настройки:", reply_markup=settings_menu())

@dp.message(F.text == "📂 Партии")
async def btn_batches(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message):
        return
    b = get_open_batch()
    if b:
        st = batch_stats(b["id"])
        info = (f"Текущая партия: №{b['id']}\n"
                f"Открыта: {b['created_at']}\n"
                f"Заказов: {st['orders_cnt']} (клиентских: {st['client_cnt']}, моих: {st['own_cnt']})\n")
    else:
        info = "Открытой партии нет.\nПри создании заказа бот предложит создать новую.\n"
    await message.answer(info + "\nВыбери действие:", reply_markup=batches_menu())

# ---------- ПАРТИИ ----------
@dp.message(F.text == "🟢 Открыть новую партию")
async def btn_open_batch(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    cur = get_open_batch()
    if cur:
        await message.answer(f"Уже есть открытая партия №{cur['id']}.\n"
                             f"Сначала закрой её через «🔴 Закрыть текущую».")
        return
    bid = create_batch()
    await message.answer(f"🟢 Открыта новая партия №{bid}.", reply_markup=batches_menu())

@dp.message(F.text == "🔴 Закрыть текущую")
async def btn_close_batch(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    b = get_open_batch()
    if not b:
        await message.answer("Открытой партии нет.")
        return
    conn = db()
    not_weighed = conn.execute("""
        SELECT o.id, o.is_own, c.name AS customer
        FROM orders o
        LEFT JOIN customers c ON c.id = o.customer_id
        JOIN order_items oi ON oi.order_id = o.id
        JOIN products p ON p.id = oi.product_id
        WHERE o.batch_id = ? AND o.status = 'open' AND oi.weighed = 0
        ORDER BY o.id
    """, (b["id"],)).fetchall()
    total_open = conn.execute(
        "SELECT COUNT(*) AS c FROM orders WHERE batch_id = ? AND status = 'open'",
        (b["id"],)
    ).fetchone()["c"]
    conn.close()

    if not_weighed:
        lines = ["⚠️ Нельзя закрыть партию — есть незакрытые заказы:\n"]
        seen = set()
        for r in not_weighed:
            if r["id"] in seen:
                continue
            seen.add(r["id"])
            label = f"🏠 Мой заказ №{r['id']}" if r["is_own"] else f"№{r['id']} — {r['customer']}"
            lines.append(f"• {label}")
        lines.append("\nВведи вес через ⚖️ Взвесить, потом закрывай партию.")
        await message.answer("\n".join(lines))
        return

    if total_open == 0:
        conn = db()
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute("UPDATE batches SET status='archived', archived_at=? WHERE id=?",
                     (now, b["id"]))
        conn.commit()
        conn.close()
        await message.answer(f"🔴 Партия №{b['id']} закрыта.")
        await send_backup(message.chat.id, caption_prefix=f"Бэкап после закрытия партии №{b['id']}")
        return

    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Закрыть партию", callback_data=f"closebatch:yes:{b['id']}")],
        [InlineKeyboardButton(text="❌ Отмена",         callback_data="closebatch:no")],
    ])
    await state.set_state(CloseBatchFSM.confirm)
    await message.answer(
        f"Закрыть партию №{b['id']}?\nЗаказов в партии: {total_open}.\n"
        f"После закрытия партия уйдёт в архив, придёт файл базы.",
        reply_markup=markup,
    )

@dp.callback_query(CloseBatchFSM.confirm, F.data.startswith("closebatch:"))
async def close_batch_confirm(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    if parts[1] == "no":
        await cb.message.edit_text("Отменено.")
        await state.clear()
        return
    bid = int(parts[2])
    conn = db()
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("UPDATE orders SET status='archived', archived_at=? WHERE batch_id=? AND status='open'",
                 (now, bid))
    conn.execute("UPDATE batches SET status='archived', archived_at=? WHERE id=?", (now, bid))
    conn.commit()
    conn.close()
    await cb.message.edit_text(f"🔴 Партия №{bid} закрыта. Готовлю бэкап...")
    await send_backup(cb.from_user.id, caption_prefix=f"Бэкап после закрытия партии №{bid}")
    await state.clear()

@dp.message(F.text == "📋 Список партий")
async def btn_batches_list(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    conn = db()
    rows = conn.execute("SELECT * FROM batches ORDER BY id DESC").fetchall()
    conn.close()
    if not rows:
        await message.answer("Партий ещё нет.")
        return
    items = []
    for b in rows:
        st = batch_stats(b["id"])
        icon = "🟢" if b["status"] == "open" else "🗄"
        items.append((b["id"],
                      f"{icon} №{b['id']} — {st['orders_cnt']} зак. ({b['created_at'][:10]})"))
    await state.set_state(PartyFSM.choosing)
    await message.answer("Выбери партию:", reply_markup=kb(items, "batch"))

@dp.callback_query(PartyFSM.choosing, F.data.startswith("batch:"))
async def batch_show(cb: CallbackQuery, state: FSMContext):
    bid = int(cb.data.split(":")[1])
    conn = db()
    b = conn.execute("SELECT * FROM batches WHERE id = ?", (bid,)).fetchone()
    st = batch_stats(bid)
    conn.close()
    status = "🟢 Открыта" if b["status"] == "open" else "🗄 Архив"
    text = (f"Партия №{b['id']}\n"
            f"Статус: {status}\n"
            f"Открыта: {b['created_at']}\n"
            f"Закрыта: {b['archived_at'] or '—'}\n"
            f"Заказов: {st['orders_cnt']} (клиентских: {st['client_cnt']}, моих: {st['own_cnt']})")
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗂 Заказы партии", callback_data=f"batchorders:{bid}")],
        [InlineKeyboardButton(text="📈 Аналитика",     callback_data=f"batchanalytics:{bid}")],
    ])
    await cb.message.edit_text(text, reply_markup=markup)
    await state.clear()

# ---------- ЗАКАЗЫ ПАРТИИ ----------
@dp.callback_query(F.data.startswith("batchorders:"))
async def batch_orders(cb: CallbackQuery):
    bid = int(cb.data.split(":")[1])
    conn = db()
    orders = conn.execute("""
        SELECT o.id, o.is_own, o.status, o.created_at, o.paid, c.name AS customer
        FROM orders o LEFT JOIN customers c ON c.id = o.customer_id
        WHERE o.batch_id = ? ORDER BY o.id
    """, (bid,)).fetchall()
    conn.close()
    if not orders:
        await cb.message.edit_text("В партии нет заказов.")
        return
    lines = [f"🗂 Заказы партии №{bid}", ""]
    for o in orders:
        c2 = db()
        items = c2.execute("""
            SELECT oi.weight_kg, oi.product_id
            FROM order_items oi WHERE oi.order_id = ?
        """, (o["id"],)).fetchall()
        total = 0.0
        for it in items:
            price = c2.execute("""
                SELECT price_sale, price_cost FROM product_prices
                WHERE product_id = ? AND valid_from <= ?
                ORDER BY valid_from DESC, id DESC LIMIT 1
            """, (it["product_id"], o["created_at"])).fetchone()
            if not price or not it["weight_kg"]:
                continue
            if o["is_own"]:
                total += it["weight_kg"] * price["price_cost"]
            else:
                total += it["weight_kg"] * price["price_sale"]
        if not o["is_own"] and items:
            total += packaging_cost()
        c2.close()
        label = f"🏠 Мой заказ №{o['id']}" if o["is_own"] else f"№{o['id']} — {o['customer']}"
        mark = "✅" if o["status"] == "archived" else "🕓"
        lines.append(f"{mark} {label} — {round(total, 2)} ₽")
    await cb.message.edit_text("\n".join(lines))

# ---------- АНАЛИТИКА ПАРТИИ ----------
@dp.callback_query(F.data.startswith("batchanalytics:"))
async def batch_analytics(cb: CallbackQuery):
    bid = int(cb.data.split(":")[1])
    conn = db()
    rows = conn.execute("""
        WITH items_priced AS (
            SELECT p.id AS pid, p.name, o.is_own,
                   oi.qty_pcs, oi.weight_kg, o.created_at
            FROM order_items oi
            JOIN products p ON p.id = oi.product_id
            JOIN orders o ON o.id = oi.order_id
            WHERE o.batch_id = ?
        )
        SELECT pid, name, is_own,
               SUM(qty_pcs) AS pcs,
               SUM(weight_kg) AS kg,
               SUM(weight_kg * (SELECT price_cost FROM product_prices pp
                                WHERE pp.product_id = items_priced.pid
                                  AND pp.valid_from <= items_priced.created_at
                                ORDER BY pp.valid_from DESC, pp.id DESC LIMIT 1)) AS cost_sum,
               SUM(weight_kg * (SELECT price_sale FROM product_prices pp
                                WHERE pp.product_id = items_priced.pid
                                  AND pp.valid_from <= items_priced.created_at
                                ORDER BY pp.valid_from DESC, pp.id DESC LIMIT 1)) AS sale_sum
        FROM items_priced
        GROUP BY pid, is_own
        ORDER BY name, is_own
    """, (bid,)).fetchall()
    client_orders_cnt = conn.execute("""
        SELECT COUNT(DISTINCT o.id) AS c FROM orders o
        JOIN order_items oi ON oi.order_id = o.id
        WHERE o.batch_id = ? AND o.is_own = 0
    """, (bid,)).fetchone()["c"]
    paid_sum = conn.execute("""
        SELECT COUNT(DISTINCT o.id) AS c FROM orders o
        JOIN order_items oi ON oi.order_id = o.id
        WHERE o.batch_id = ? AND o.is_own = 0 AND o.paid = 1
    """, (bid,)).fetchone()["c"]
    conn.close()
    if not rows:
        await cb.message.edit_text("Нет данных.")
        return
    pack_total = client_orders_cnt * packaging_cost()
    pack_paid = paid_sum * packaging_cost()

    lines = [f"📈 Аналитика партии №{bid}", ""]
    tc = ts = 0
    for r in rows:
        kg = r["kg"] or 0
        cs = r["cost_sum"] or 0
        ss = r["sale_sum"] or 0
        if r["is_own"]:
            lines.append(f"🏠 {r['name']} (мой заказ)")
            lines.append(f"   шт: {r['pcs']}, кг: {round(kg, 2)}")
            lines.append(f"   закупка: {round(cs, 2)} ₽")
            tc += cs
        else:
            lines.append(f"• {r['name']}")
            lines.append(f"   шт: {r['pcs']}, кг: {round(kg, 2)}")
            lines.append(f"   закупка: {round(cs, 2)} ₽")
            lines.append(f"   реализация: {round(ss, 2)} ₽")
            lines.append(f"   доход: {round(ss - cs, 2)} ₽")
            tc += cs
            ts += ss
    lines.append("")
    lines.append("💰 ИТОГО по партии:")
    lines.append(f"Закупка: {round(tc, 2)} ₽")
    lines.append(f"Реализация: {round(ts, 2)} ₽")
    if pack_total:
        lines.append(f"Упаковка: {round(pack_total, 2)} ₽")
    lines.append(f"ДОХОД: {round(ts + pack_total - tc, 2)} ₽")
    # оплаты клиентов
    total_with_pack = ts + pack_total
    paid_approx = paid_sum and 0
    # посчитаем точнее: sum sale только для paid-заказов
    conn = db()
    paid_rows = conn.execute("""
        WITH items_priced AS (
            SELECT oi.weight_kg, o.created_at, p.id AS pid
            FROM order_items oi
            JOIN orders o ON o.id = oi.order_id
            JOIN products p ON p.id = oi.product_id
            WHERE o.batch_id = ? AND o.is_own = 0 AND o.paid = 1
        )
        SELECT SUM(weight_kg * (SELECT price_sale FROM product_prices pp
                WHERE pp.product_id = items_priced.pid AND pp.valid_from <= items_priced.created_at
                ORDER BY pp.valid_from DESC, pp.id DESC LIMIT 1)) AS s
        FROM items_priced
    """, (bid,)).fetchone()["s"] or 0
    conn.close()
    paid_total = paid_rows + pack_paid
    unpaid_total = (ts + pack_total) - paid_total
    lines.append("")
    lines.append(f"✅ Оплачено: {round(paid_total, 2)} ₽")
    lines.append(f"🕓 Не оплачено: {round(unpaid_total, 2)} ₽")
    await cb.message.edit_text("\n".join(lines))

# ---------- АРХИВ ЗАКАЗОВ ----------
@dp.message(F.text == "🗂 Архив заказов")
async def btn_archive_orders(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    conn = db()
    rows = conn.execute("SELECT * FROM batches ORDER BY id DESC").fetchall()
    conn.close()
    if not rows:
        await message.answer("Партий ещё нет.")
        return
    items = []
    for b in rows:
        st = batch_stats(b["id"])
        icon = "🟢" if b["status"] == "open" else "🗄"
        items.append((b["id"], f"{icon} №{b['id']} — {st['orders_cnt']} зак. ({b['created_at'][:10]})"))
    await state.set_state(PartyFSM.choosing)
    await message.answer("Выбери партию для просмотра заказов:",
                         reply_markup=kb(items, "batch"))

# ---------- ТОВАРЫ И КЛИЕНТЫ ----------
@dp.message(F.text == "📦 Товары")
async def btn_products(message: Message):
    if not is_admin(message):
        return
    conn = db()
    rows = conn.execute("""
        SELECT p.id, p.name,
               (SELECT price_sale FROM product_prices pp WHERE pp.product_id = p.id
                ORDER BY valid_from DESC, id DESC LIMIT 1) AS ps,
               (SELECT price_cost FROM product_prices pp WHERE pp.product_id = p.id
                ORDER BY valid_from DESC, id DESC LIMIT 1) AS pc
        FROM products p ORDER BY p.id
    """).fetchall()
    conn.close()
    if not rows:
        await message.answer("Список пуст.")
        return
    lines = ["📦 Товары:"]
    for r in rows:
        lines.append(f"{r['id']}. {r['name']} — прод. {r['ps']} / зак. {r['pc']} ₽/кг")
    await message.answer("\n".join(lines))

@dp.message(F.text == "👥 Клиенты")
async def btn_customers(message: Message):
    if not is_admin(message):
        return
    rows = customers_list()
    if not rows:
        await message.answer("Список пуст.")
        return
    lines = ["👥 Клиенты:"]
    for r in rows:
        lines.append(f"{r['id']}. {r['name']}")
    await message.answer("\n".join(lines))

@dp.message(F.text == "➕ Товар")
async def btn_addproduct(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    await message.answer("Введи: Название Цена_продажи Цена_закупки\nНапример: Скумбрия 2000 1200")
    await state.set_state(AddProductFSM.entering)

@dp.message(AddProductFSM.entering)
async def addproduct_input(message: Message, state: FSMContext):
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("Формат: Название Цена_продажи Цена_закупки")
        return
    name = parts[0]
    try:
        ps = float(parts[1].replace(",", "."))
        pc = float(parts[2].replace(",", "."))
    except ValueError:
        await message.answer("Цены должны быть числами.")
        return
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT INTO products (name) VALUES (?)", (name,))
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    add_price_if_changed(pid, ps, pc)
    await message.answer(f"Товар добавлен: {name}\nПродажа: {ps} ₽/кг\nЗакупка: {pc} ₽/кг",
                        reply_markup=settings_menu())
    await state.clear()

@dp.message(F.text == "➕ Клиент")
async def btn_addcustomer(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    await message.answer("Введи имя клиента:")
    await state.set_state(AddCustomerFSM.entering)

@dp.message(AddCustomerFSM.entering)
async def addcustomer_input(message: Message, state: FSMContext):
    conn = db()
    conn.execute("INSERT INTO customers (name) VALUES (?)", (message.text,))
    conn.commit()
    conn.close()
    await message.answer(f"Клиент добавлен: {message.text}", reply_markup=settings_menu())
    await state.clear()

# ---------- УПАКОВКА ----------
@dp.message(F.text == "📦 Стоимость упаковки")
async def btn_packaging(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    cur = packaging_cost()
    await message.answer(
        f"Текущая стоимость упаковки: {cur} ₽ (на заказ).\n\n"
        f"Введи новое значение или /cancel.",
    )
    await state.set_state(PackagingFSM.entering)

@dp.message(PackagingFSM.entering)
async def packaging_input(message: Message, state: FSMContext):
    try:
        v = float(message.text.replace(",", "."))
        if v < 0:
            raise ValueError
    except ValueError:
        await message.answer("Нужно неотрицательное число.")
        return
    set_setting("packaging_cost", str(v))
    await message.answer(f"✅ Стоимость упаковки: {v} ₽", reply_markup=settings_menu())
    await state.clear()

# ---------- РЕКВИЗИТЫ ----------
@dp.message(F.text == "💳 Реквизиты")
async def btn_payment(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    num = get_setting("payment_number", "")
    bank = get_setting("payment_bank", "")
    preview = f"Оплата: `{num}` {bank}".strip()
    text = (f"Текущие реквизиты:\n"
            f"Номер: {num or '—'}\n"
            f"Банк: {bank or '—'}\n\n"
            f"В чеке будет:\n{preview if num else '—'}")
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить номер", callback_data="pay:num")],
        [InlineKeyboardButton(text="✏️ Изменить банк",  callback_data="pay:bank")],
    ])
    await message.answer(text, reply_markup=markup)
    await state.clear()

@dp.callback_query(F.data.startswith("pay:"))
async def payment_edit(cb: CallbackQuery, state: FSMContext):
    field = cb.data.split(":")[1]
    if field == "num":
        await cb.message.edit_text("Отправь новый НОМЕР:")
        await state.set_state(PaymentFSM.entering_number)
    else:
        await cb.message.edit_text("Отправь название БАНКА:")
        await state.set_state(PaymentFSM.entering_bank)

@dp.message(PaymentFSM.entering_number)
async def payment_num_input(message: Message, state: FSMContext):
    set_setting("payment_number", message.text.strip())
    num = get_setting("payment_number", "")
    bank = get_setting("payment_bank", "")
    await message.answer(f"✅ Номер сохранён: `{num}`\nВ чеке: Оплата: `{num}` {bank}",
                        reply_markup=settings_menu())
    await state.clear()

@dp.message(PaymentFSM.entering_bank)
async def payment_bank_input(message: Message, state: FSMContext):
    set_setting("payment_bank", message.text.strip())
    num = get_setting("payment_number", "")
    bank = get_setting("payment_bank", "")
    await message.answer(f"✅ Банк сохранён: {bank}\nВ чеке: Оплата: `{num}` {bank}",
                        reply_markup=settings_menu())
    await state.clear()

# ---------- ИЗМЕНИТЬ ТОВАР ----------
@dp.message(F.text == "✏️ Изменить товар")
async def btn_editproduct(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    rows = products_list()
    if not rows:
        await message.answer("Список пуст.")
        return
    items = [(r["id"], r["name"]) for r in rows]
    await state.set_state(EditProductFSM.choosing)
    await message.answer("Какой товар изменить?", reply_markup=kb(items, "ep"))

@dp.callback_query(EditProductFSM.choosing, F.data.startswith("ep:"))
async def editproduct_choose(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data.split(":")[1])
    await state.update_data(pid=pid)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Цена продажи", callback_data="epf:sale")],
        [InlineKeyboardButton(text="🏭 Цена закупки", callback_data="epf:cost")],
        [InlineKeyboardButton(text="📝 Название",     callback_data="epf:name")],
        [InlineKeyboardButton(text="📜 История цен",  callback_data="epf:history")],
    ])
    await cb.message.edit_text("Что сделать?", reply_markup=markup)
    await state.set_state(EditProductFSM.choosing_field)

@dp.callback_query(EditProductFSM.choosing_field, F.data.startswith("epf:"))
async def editproduct_field(cb: CallbackQuery, state: FSMContext):
    field = cb.data.split(":")[1]
    data = await state.get_data()
    pid = data["pid"]
    if field == "history":
        conn = db()
        prod = conn.execute("SELECT name FROM products WHERE id = ?", (pid,)).fetchone()
        rows = conn.execute("""
            SELECT price_sale, price_cost, valid_from FROM product_prices
            WHERE product_id = ? ORDER BY valid_from DESC, id DESC
        """, (pid,)).fetchall()
        conn.close()
        lines = [f"📜 История цен: {prod['name']}", ""]
        for i, r in enumerate(rows):
            mark = " ⬅️ текущая" if i == 0 else ""
            lines.append(f"{r['valid_from']}{mark}\n"
                         f"   прод. {r['price_sale']} / зак. {r['price_cost']} ₽/кг")
        await cb.message.edit_text("\n".join(lines))
        await state.clear()
        return
    await state.update_data(field=field)
    await cb.message.edit_text("Введи новое значение:")
    await state.set_state(EditProductFSM.entering_value)

@dp.message(EditProductFSM.entering_value)
async def editproduct_value(message: Message, state: FSMContext):
    data = await state.get_data()
    pid = data["pid"]
    field = data["field"]
    conn = db()
    if field == "name":
        conn.execute("UPDATE products SET name = ? WHERE id = ?", (message.text, pid))
        conn.commit()
        conn.close()
        await message.answer("Название обновлено.", reply_markup=settings_menu())
        await state.clear()
        return
    try:
        new_val = float(message.text.replace(",", "."))
    except ValueError:
        conn.close()
        await message.answer("Нужно число.")
        return
    cur = conn.execute("""
        SELECT price_sale, price_cost FROM product_prices
        WHERE product_id = ? ORDER BY valid_from DESC, id DESC LIMIT 1
    """, (pid,)).fetchone()
    conn.close()
    sale = cur["price_sale"] if cur else 0
    cost = cur["price_cost"] if cur else 0
    if field == "sale":
        if abs(sale - new_val) < 0.001:
            await message.answer("Цена продажи уже такая.")
            await state.clear()
            return
        sale = new_val
    else:
        if abs(cost - new_val) < 0.001:
            await message.answer("Цена закупки уже такая.")
            await state.clear()
            return
        cost = new_val
    add_price_if_changed(pid, sale, cost)
    await message.answer(f"Обновлено: прод. {sale} / зак. {cost} ₽/кг",
                        reply_markup=settings_menu())
    await state.clear()

# ---------- НОВЫЙ ЗАКАЗ ----------
async def ensure_batch_for_order(message: Message, state: FSMContext, is_own: bool):
    b = get_open_batch()
    if b:
        await state.update_data(batch_id=b["id"], is_own=1 if is_own else 0)
        return True
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Создать партию", callback_data=f"newbatch:yes:{1 if is_own else 0}")],
        [InlineKeyboardButton(text="❌ Отмена",          callback_data="newbatch:no")],
    ])
    await state.set_state(NewBatchFSM.confirm)
    await message.answer("Открытой партии нет. Создать новую партию?", reply_markup=markup)
    return False

@dp.callback_query(NewBatchFSM.confirm, F.data.startswith("newbatch:"))
async def newbatch_confirm(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    if parts[1] == "no":
        await cb.message.edit_text("Отменено.")
        await state.clear()
        return
    is_own = bool(int(parts[2]))
    bid = create_batch()
    await state.update_data(batch_id=bid, is_own=1 if is_own else 0)
    await cb.message.edit_text(f"🟢 Создана партия №{bid}.")
    if is_own:
        await start_own_order_flow(cb.message, state)
    else:
        await start_client_order_flow(cb.message, state)

@dp.message(F.text == "📦 Новый заказ")
async def btn_neworder(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    if not await ensure_batch_for_order(message, state, is_own=False):
        return
    await start_client_order_flow(message, state)

async def start_client_order_flow(message, state: FSMContext):
    rows = customers_list()
    if not rows:
        await message.answer("Сначала добавь клиентов через ⚙️ Настройки → ➕ Клиент")
        await state.clear()
        return
    items = [(r["id"], r["name"]) for r in rows]
    await state.set_state(OrderFSM.choosing_customer)
    await message.answer("Выбери клиента:", reply_markup=kb(items, "cust"))

@dp.message(F.text == "🏠 Мой заказ")
async def btn_ownorder(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    if not await ensure_batch_for_order(message, state, is_own=True):
        return
    await start_own_order_flow(message, state)

async def start_own_order_flow(message, state: FSMContext):
    data = await state.get_data()
    bid = data.get("batch_id") or (get_open_batch() or {"id": None})["id"]
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO orders (customer_id, batch_id, is_own, created_at) VALUES (NULL, ?, 1, ?)",
        (bid, datetime.now().isoformat(timespec="seconds")),
    )
    oid = cur.lastrowid
    conn.commit()
    conn.close()
    await state.update_data(order_id=oid)
    await message.answer(f"🏠 Мой заказ №{oid} создан. Выбери товар:")
    await _show_products(message, state)

@dp.callback_query(OrderFSM.choosing_customer, F.data.startswith("cust:"))
async def order_customer(cb: CallbackQuery, state: FSMContext):
    cid = int(cb.data.split(":")[1])
    data = await state.get_data()
    bid = data.get("batch_id") or (get_open_batch() or {"id": None})["id"]
    conn = db()
    conn.execute("INSERT INTO orders (customer_id, batch_id, is_own, created_at) VALUES (?, ?, 0, ?)",
                 (cid, bid, datetime.now().isoformat(timespec="seconds")))
    oid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.commit()
    conn.close()
    await state.update_data(order_id=oid)
    await cb.message.edit_text(f"Заказ №{oid}. Выбери товар:")
    await _show_products(cb.message, state)

async def _show_products(message, state):
    rows = products_list()
    if not rows:
        await message.answer("Нет товаров. Добавь через ⚙️ Настройки → ➕ Товар")
        return
    items = []
    for r in rows:
        cur = get_current_prices(r["id"])
        label = f"{r['name']} ({cur['price_sale']} ₽/кг)" if cur else r["name"]
        items.append((r["id"], label))
    await message.answer("Выбери товар:", reply_markup=kb(items, "prod"))
    await state.set_state(OrderFSM.choosing_product)

@dp.callback_query(OrderFSM.choosing_product, F.data.startswith("prod:"))
async def order_product(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data.split(":")[1])
    await state.update_data(product_id=pid)
    await cb.message.edit_text("Введи количество в штуках:")
    await state.set_state(OrderFSM.entering_qty)

@dp.message(OrderFSM.entering_qty)
async def order_qty(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Нужно целое число.")
        return
    data = await state.get_data()
    conn = db()
    conn.execute(
        "INSERT INTO order_items (order_id, product_id, qty_pcs) VALUES (?, ?, ?)",
        (data["order_id"], data["product_id"], int(message.text)),
    )
    conn.commit()
    conn.close()
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Ещё позиция", callback_data="more:yes")],
        [InlineKeyboardButton(text="✅ Завершить",   callback_data="more:no")],
    ])
    await message.answer("Добавлено. Что дальше?", reply_markup=markup)
    await state.set_state(OrderFSM.more_items)

@dp.callback_query(OrderFSM.more_items, F.data.startswith("more:"))
async def order_more(cb: CallbackQuery, state: FSMContext):
    if cb.data == "more:yes":
        await cb.message.edit_text("Выбери товар:")
        await _show_products(cb.message, state)
    else:
        data = await state.get_data()
        await cb.message.edit_text(f"Заказ №{data['order_id']} сохранён.")
        await state.clear()

# ---------- СПИСОК ЗАКАЗОВ ----------
@dp.message(F.text == "📋 Заказы")
async def btn_orders(message: Message):
    if not is_admin(message):
        return
    rows = open_orders()
    if not rows:
        await message.answer("Нет открытых заказов.")
        return
    for r in rows:
        label = f"🏠 Мой заказ №{r['id']}" if r["is_own"] else f"№{r['id']} — {r['customer']}"
        paid_mark = ""
        if not r["is_own"]:
            paid_mark = " — ✅ Оплачено" if r["paid"] else " — 🕓 Не оплачено"
        buttons = [
            [InlineKeyboardButton(text="🧾 Чек",       callback_data=f"order:check:{r['id']}"),
             InlineKeyboardButton(text="✏️ Изменить",  callback_data=f"order:edit:{r['id']}")],
        ]
        # оплата (только для клиентских)
        if not r["is_own"]:
            if r["paid"]:
                buttons.append([InlineKeyboardButton(text="↩️ Снять оплату",
                                                     callback_data=f"order:unpay:{r['id']}"),
                                InlineKeyboardButton(text="📦 В архив",
                                                     callback_data=f"order:arch:{r['id']}")])
            else:
                buttons.append([InlineKeyboardButton(text="💳 Оплачен",
                                                     callback_data=f"order:pay:{r['id']}"),
                                InlineKeyboardButton(text="📦 В архив",
                                                     callback_data=f"order:arch:{r['id']}")])
        else:
            buttons.append([InlineKeyboardButton(text="📦 В архив",
                                                 callback_data=f"order:arch:{r['id']}")])
        buttons.append([InlineKeyboardButton(text="❌ Удалить",
                                             callback_data=f"order:del:{r['id']}")])
        markup = InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.answer(f"{label} ({r['created_at']}){paid_mark}", reply_markup=markup)

@dp.callback_query(F.data.startswith("order:"))
async def order_action(cb: CallbackQuery, state: FSMContext):
    _, action, oid = cb.data.split(":")
    oid = int(oid)
    if action == "check":
        await _show_check(cb, oid)
    elif action == "edit":
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить позицию", callback_data=f"editact:add:{oid}")],
            [InlineKeyboardButton(text="🔢 Изменить кол-во",  callback_data=f"editact:qty:{oid}")],
            [InlineKeyboardButton(text="❌ Удалить позицию",  callback_data=f"editact:del:{oid}")],
        ])
        await cb.message.edit_text(f"Заказ №{oid}. Что сделать?", reply_markup=markup)
    elif action == "arch":
        conn = db()
        conn.execute("UPDATE orders SET status='archived', archived_at=? WHERE id=?",
                     (datetime.now().isoformat(timespec="seconds"), oid))
        conn.commit()
        conn.close()
        await cb.message.edit_text(f"Заказ №{oid} в архиве.")
    elif action == "del":
        conn = db()
        conn.execute("DELETE FROM order_items WHERE order_id = ?", (oid,))
        conn.execute("DELETE FROM orders WHERE id = ?", (oid,))
        conn.commit()
        conn.close()
        await cb.message.edit_text(f"Заказ №{oid} удалён.")
    elif action == "pay":
        conn = db()
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute("UPDATE orders SET paid=1, paid_at=? WHERE id=?", (now, oid))
        conn.commit()
        conn.close()
        await cb.message.edit_text(f"✅ Заказ №{oid} отмечен как оплаченный ({now}).")
    elif action == "unpay":
        conn = db()
        conn.execute("UPDATE orders SET paid=0, paid_at=NULL WHERE id=?", (oid,))
        conn.commit()
        conn.close()
        await cb.message.edit_text(f"↩️ Отметка оплаты с заказа №{oid} снята.")

# ---------- РЕДАКТИРОВАНИЕ ЗАКАЗА ----------
@dp.callback_query(F.data.startswith("editact:"))
async def editact(cb: CallbackQuery, state: FSMContext):
    _, action, oid = cb.data.split(":")
    oid = int(oid)
    if action == "add":
        rows = products_list()
        if not rows:
            await cb.message.edit_text("Нет товаров.")
            return
        items = []
        for r in rows:
            cur = get_current_prices(r["id"])
            label = f"{r['name']} ({cur['price_sale']} ₽/кг)" if cur else r["name"]
            items.append((r["id"], label))
        await state.update_data(order_id=oid)
        await state.set_state(EditOrderFSM.choosing_product)
        await cb.message.edit_text("Выбери товар для добавления:",
                                   reply_markup=kb(items, "eadd"))
    elif action in ("qty", "del"):
        conn = db()
        rows = conn.execute("""
            SELECT oi.id, p.name, oi.qty_pcs
            FROM order_items oi JOIN products p ON p.id = oi.product_id
            WHERE oi.order_id = ? ORDER BY oi.id
        """, (oid,)).fetchall()
        conn.close()
        if not rows:
            await cb.message.edit_text("В заказе нет позиций.")
            return
        items = [(r["id"], f"{r['name']} — {r['qty_pcs']} шт.") for r in rows]
        await state.update_data(order_id=oid)
        if action == "qty":
            await state.set_state(EditOrderFSM.choosing_item_qty)
            await cb.message.edit_text("Какую позицию изменить?",
                                       reply_markup=kb(items, "eqty"))
        else:
            await state.set_state(EditOrderFSM.choosing_item_del)
            await cb.message.edit_text("Какую позицию удалить?",
                                       reply_markup=kb(items, "edel"))

@dp.callback_query(EditOrderFSM.choosing_product, F.data.startswith("eadd:"))
async def eadd_product(cb: CallbackQuery, state: FSMContext):
    pid = int(cb.data.split(":")[1])
    await state.update_data(product_id=pid)
    await cb.message.edit_text("Введи количество в штуках:")
    await state.set_state(EditOrderFSM.entering_qty_add)

@dp.message(EditOrderFSM.entering_qty_add)
async def eadd_qty(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Нужно целое число.")
        return
    data = await state.get_data()
    conn = db()
    conn.execute("INSERT INTO order_items (order_id, product_id, qty_pcs) VALUES (?, ?, ?)",
                 (data["order_id"], data["product_id"], int(message.text)))
    conn.commit()
    conn.close()
    await message.answer(f"✅ Позиция добавлена в заказ №{data['order_id']}.")
    await state.clear()

@dp.callback_query(EditOrderFSM.choosing_item_qty, F.data.startswith("eqty:"))
async def eqty_choose(cb: CallbackQuery, state: FSMContext):
    item_id = int(cb.data.split(":")[1])
    await state.update_data(item_id=item_id)
    await cb.message.edit_text("Введи новое количество в штуках:")
    await state.set_state(EditOrderFSM.entering_qty_set)

@dp.message(EditOrderFSM.entering_qty_set)
async def eqty_set(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Нужно целое число.")
        return
    data = await state.get_data()
    conn = db()
    conn.execute("UPDATE order_items SET qty_pcs = ? WHERE id = ?",
                 (int(message.text), data["item_id"]))
    conn.commit()
    conn.close()
    await message.answer(f"✅ Количество обновлено: {message.text} шт.")
    await state.clear()

@dp.callback_query(EditOrderFSM.choosing_item_del, F.data.startswith("edel:"))
async def edel_choose(cb: CallbackQuery, state: FSMContext):
    item_id = int(cb.data.split(":")[1])
    conn = db()
    conn.execute("DELETE FROM order_items WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()
    await cb.message.edit_text("❌ Позиция удалена из заказа.")
    await state.clear()

# ---------- ВЗВЕШИВАНИЕ ----------
@dp.message(F.text == "⚖️ Взвесить")
async def btn_weigh(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    conn = db()
    rows = conn.execute("""
        SELECT oi.id, o.is_own, c.name AS customer, p.name AS product, oi.qty_pcs
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        LEFT JOIN customers c ON c.id = o.customer_id
        JOIN products p ON p.id = oi.product_id
        WHERE oi.weighed = 0 AND o.status = 'open'
        ORDER BY oi.id
    """).fetchall()
    conn.close()
    if not rows:
        await message.answer("Нет позиций для взвешивания.")
        return
    items = []
    for r in rows:
        who = "🏠 Мой" if r["is_own"] else r["customer"]
        items.append((r["id"], f"{who} — {r['product']}, {r['qty_pcs']} шт."))
    await state.set_state(WeighFSM.choosing)
    await message.answer("Выбери позицию:", reply_markup=kb(items, "weigh"))

@dp.callback_query(WeighFSM.choosing, F.data.startswith("weigh:"))
async def weigh_choose(cb: CallbackQuery, state: FSMContext):
    item_id = int(cb.data.split(":")[1])
    await state.update_data(item_id=item_id)
    await cb.message.edit_text("Введи вес в кг (например, 1.2):")
    await state.set_state(WeighFSM.entering_weight)

@dp.message(WeighFSM.entering_weight)
async def weigh_input(message: Message, state: FSMContext):
    try:
        w = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("Введи число.")
        return
    data = await state.get_data()
    item_id = data["item_id"]
    conn = db()
    row = conn.execute("""
        SELECT oi.id, oi.order_id, oi.product_id, p.name, o.created_at, o.is_own
        FROM order_items oi
        JOIN products p ON p.id = oi.product_id
        JOIN orders o ON o.id = oi.order_id
        WHERE oi.id = ?
    """, (item_id,)).fetchone()
    price = conn.execute("""
        SELECT price_sale, price_cost FROM product_prices
        WHERE product_id = ? AND valid_from <= ?
        ORDER BY valid_from DESC, id DESC LIMIT 1
    """, (row["product_id"], row["created_at"])).fetchone()
    ps = price["price_sale"] if price else 0
    pc = price["price_cost"] if price else 0
    used = pc if row["is_own"] else ps
    conn.execute("UPDATE order_items SET weight_kg = ?, weighed = 1 WHERE id = ?",
                 (w, item_id))
    conn.commit()
    conn.close()
    note = " (мой)" if row["is_own"] else ""
    await message.answer(f"Сохранено: {row['name']}{note} — {w} кг × {used} ₽ = {round(w*used,2)} ₽")
    await state.clear()

# ---------- ПЕРЕВВЕСТИ ----------
@dp.message(F.text == "🔄 Перевесить")
async def btn_reweigh(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    conn = db()
    rows = conn.execute("""
        SELECT oi.id, o.is_own, c.name AS customer, p.name AS product, oi.weight_kg
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        LEFT JOIN customers c ON c.id = o.customer_id
        JOIN products p ON p.id = oi.product_id
        WHERE oi.weighed = 1 AND o.status = 'open'
        ORDER BY oi.id
    """).fetchall()
    conn.close()
    if not rows:
        await message.answer("Нет взвешенных позиций.")
        return
    items = []
    for r in rows:
        who = "🏠 Мой" if r["is_own"] else r["customer"]
        items.append((r["id"], f"{who} — {r['product']} ({r['weight_kg']} кг)"))
    await state.set_state(ReweighFSM.choosing)
    await message.answer("Выбери позицию:", reply_markup=kb(items, "rw"))

@dp.callback_query(ReweighFSM.choosing, F.data.startswith("rw:"))
async def reweigh_choose(cb: CallbackQuery, state: FSMContext):
    item_id = int(cb.data.split(":")[1])
    await state.update_data(item_id=item_id)
    await cb.message.edit_text("Введи новый вес:")
    await state.set_state(ReweighFSM.entering_weight)

@dp.message(ReweighFSM.entering_weight)
async def reweigh_input(message: Message, state: FSMContext):
    try:
        w = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("Введи число.")
        return
    data = await state.get_data()
    conn = db()
    conn.execute("UPDATE order_items SET weight_kg = ? WHERE id = ?", (w, data["item_id"]))
    conn.commit()
    conn.close()
    await message.answer(f"Вес обновлён: {w} кг")
    await state.clear()

# ---------- ЧЕК ----------
@dp.message(F.text == "🧾 Чек")
async def btn_check(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    rows = open_orders()
    if not rows:
        await message.answer("Нет открытых заказов.")
        return
    items = []
    for r in rows:
        label = f"🏠 Мой заказ №{r['id']}" if r["is_own"] else f"№{r['id']} — {r['customer']}"
        items.append((r["id"], label))
    await state.set_state(CheckFSM.choosing)
    await message.answer("Выбери заказ:", reply_markup=kb(items, "chk"))

@dp.callback_query(CheckFSM.choosing, F.data.startswith("chk:"))
async def check_choose(cb: CallbackQuery, state: FSMContext):
    oid = int(cb.data.split(":")[1])
    await _show_check(cb, oid)
    await state.clear()

async def _show_check(cb: CallbackQuery, oid: int):
    conn = db()
    order = conn.execute("""
        SELECT o.id, o.created_at, o.is_own, c.name AS customer
        FROM orders o LEFT JOIN customers c ON c.id = o.customer_id
        WHERE o.id = ?
    """, (oid,)).fetchone()
    items = conn.execute("""
        SELECT p.name, oi.qty_pcs, oi.weight_kg, oi.product_id
        FROM order_items oi JOIN products p ON p.id = oi.product_id
        WHERE oi.order_id = ?
    """, (oid,)).fetchall()

    header = f"🏠 Мой заказ №{order['id']}" if order["is_own"] else f"Чек — заказ №{order['id']}"
    lines = [header]
    if not order["is_own"]:
        lines.append(f"Клиент: {order['customer']}")
    lines.append("")

    total = 0.0
    not_weighed = 0
    for it in items:
        if it["weight_kg"] is None:
            not_weighed += 1
            lines.append(f"• {it['name']}: {it['qty_pcs']} шт. — вес не введён")
            continue
        price = conn.execute("""
            SELECT price_sale, price_cost FROM product_prices
            WHERE product_id = ? AND valid_from <= ?
            ORDER BY valid_from DESC, id DESC LIMIT 1
        """, (it["product_id"], order["created_at"])).fetchone()
        ps = price["price_sale"] if price else 0
        pc = price["price_cost"] if price else 0
        used = pc if order["is_own"] else ps
        amount = round(it["weight_kg"] * used, 2)
        total += amount
        lines.append(f"• {it['name']}: {it['qty_pcs']} шт., {it['weight_kg']} кг "
                     f"× {used} ₽ = {amount} ₽")

    if not order["is_own"] and items:
        pack = packaging_cost()
        if pack:
            total += pack
            lines.append("")
            lines.append(f"Упаковка: {pack} ₽")

    conn.close()
    lines.append("")
    lines.append(f"Итого: {round(total, 2)} ₽")
    if not_weighed:
        lines.append(f"\n⚠️ Не взвешено: {not_weighed}")
    text = "\n".join(lines)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Готовый чек", callback_data=f"send:{oid}")],
    ])
    await cb.message.edit_text(text, reply_markup=markup)

@dp.callback_query(F.data.startswith("send:"))
async def check_send(cb: CallbackQuery):
    oid = int(cb.data.split(":")[1])
    conn = db()
    order = conn.execute("""
        SELECT o.id, o.created_at, o.is_own, c.name AS customer
        FROM orders o LEFT JOIN customers c ON c.id = o.customer_id
        WHERE o.id = ?
    """, (oid,)).fetchone()
    items = conn.execute("""
        SELECT p.name, oi.qty_pcs, oi.weight_kg, oi.product_id
        FROM order_items oi JOIN products p ON p.id = oi.product_id
        WHERE oi.order_id = ?
    """, (oid,)).fetchall()

    header = f"🏠 Мой заказ №{order['id']}" if order["is_own"] else f"Чек — заказ №{order['id']}"
    lines = [header]
    if not order["is_own"]:
        lines.append(f"Клиент: {order['customer']}")
    lines.append("")

    total = 0.0
    for it in items:
        price = conn.execute("""
            SELECT price_sale, price_cost FROM product_prices
            WHERE product_id = ? AND valid_from <= ?
            ORDER BY valid_from DESC, id DESC LIMIT 1
        """, (it["product_id"], order["created_at"])).fetchone()
        ps = price["price_sale"] if price else 0
        pc = price["price_cost"] if price else 0
        used = pc if order["is_own"] else ps
        amount = round((it["weight_kg"] or 0) * used, 2)
        total += amount
        lines.append(f"• {it['name']}: {it['qty_pcs']} шт., {it['weight_kg']} кг "
                     f"× {used} ₽ = {amount} ₽")

    if not order["is_own"] and items:
        pack = packaging_cost()
        if pack:
            total += pack
            lines.append("")
            lines.append(f"Упаковка: {pack} ₽")

    conn.close()
    lines.append("")
    lines.append(f"Итого: {round(total, 2)} ₽")
    lines.append("")
    lines.append("Спасибо за покупку! 🐟")

    num = get_setting("payment_number", "").strip()
    bank = get_setting("payment_bank", "").strip()
    if num and not order["is_own"]:
        lines.append(f"Оплата: `{num}` {bank}".rstrip())

    await cb.message.edit_text("\n".join(lines))

# ---------- ПОСТАВЩИК ----------
@dp.message(F.text == "📤 Поставщик")
async def btn_supply(message: Message):
    if not is_admin(message):
        return
    b = get_open_batch()
    if not b:
        await message.answer("Нет открытой партии.")
        return
    conn = db()
    rows = conn.execute("""
        SELECT p.name, SUM(oi.qty_pcs) AS pcs
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        JOIN products p ON p.id = oi.product_id
        WHERE o.batch_id = ? AND o.status = 'open'
        GROUP BY p.id ORDER BY p.name
    """, (b["id"],)).fetchall()
    conn.close()
    if not rows:
        await message.answer("В открытой партии нет заказов.")
        return
    lines = [f"📦 Сводка для поставщика (партия №{b['id']}):", ""]
    for r in rows:
        lines.append(f"• {r['name']}: {r['pcs']} шт.")
    await message.answer("\n".join(lines))

# ---------- ИТОГИ ПАРТИИ ----------
@dp.message(F.text == "📈 Итоги партии")
async def btn_party(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Открытая",  callback_data="partytot:open")],
        [InlineKeyboardButton(text="📚 Все партии", callback_data="partytot:all")],
    ])
    await state.set_state(PartyFSM.choosing)
    await message.answer("Какие партии?", reply_markup=markup)

@dp.callback_query(PartyFSM.choosing, F.data.startswith("partytot:"))
async def party_totals(cb: CallbackQuery, state: FSMContext):
    scope = cb.data.split(":")[1]
    conn = db()
    if scope == "open":
        b = get_open_batch()
        batches = [b] if b else []
    else:
        batches = conn.execute("SELECT * FROM batches ORDER BY id").fetchall()
    conn.close()
    if not batches:
        await cb.message.edit_text("Партий нет.")
        await state.clear()
        return

    lines = ["📈 Итоги по партиям", ""]
    for b in batches:
        conn = db()
        rows = conn.execute("""
            WITH items_priced AS (
                SELECT p.id AS pid, p.name, o.is_own,
                       oi.qty_pcs, oi.weight_kg, o.created_at
                FROM order_items oi
                JOIN products p ON p.id = oi.product_id
                JOIN orders o ON o.id = oi.order_id
                WHERE o.batch_id = ?
            )
            SELECT SUM(qty_pcs) AS pcs, SUM(weight_kg) AS kg,
                   SUM(CASE WHEN is_own = 0 THEN weight_kg * (SELECT price_sale FROM product_prices pp
                        WHERE pp.product_id = items_priced.pid AND pp.valid_from <= items_priced.created_at
                        ORDER BY pp.valid_from DESC, pp.id DESC LIMIT 1) ELSE 0 END) AS sale_sum,
                   SUM(CASE WHEN is_own = 0 THEN weight_kg * (SELECT price_cost FROM product_prices pp
                        WHERE pp.product_id = items_priced.pid AND pp.valid_from <= items_priced.created_at
                        ORDER BY pp.valid_from DESC, pp.id DESC LIMIT 1) ELSE 0 END) AS cost_client,
                   SUM(CASE WHEN is_own = 1 THEN weight_kg * (SELECT price_cost FROM product_prices pp
                        WHERE pp.product_id = items_priced.pid AND pp.valid_from <= items_priced.created_at
                        ORDER BY pp.valid_from DESC, pp.id DESC LIMIT 1) ELSE 0 END) AS cost_own
            FROM items_priced
        """, (b["id"],)).fetchone()
        pack_cnt = conn.execute("""
            SELECT COUNT(DISTINCT o.id) AS c FROM orders o
            JOIN order_items oi ON oi.order_id = o.id
            WHERE o.batch_id = ? AND o.is_own = 0
        """, (b["id"],)).fetchone()["c"]
        paid_cnt = conn.execute("""
            SELECT COUNT(DISTINCT o.id) AS c FROM orders o
            JOIN order_items oi ON oi.order_id = o.id
            WHERE o.batch_id = ? AND o.is_own = 0 AND o.paid = 1
        """, (b["id"],)).fetchone()["c"]
        paid_sale = conn.execute("""
            WITH items_priced AS (
                SELECT oi.weight_kg, o.created_at, p.id AS pid
                FROM order_items oi
                JOIN orders o ON o.id = oi.order_id
                JOIN products p ON p.id = oi.product_id
                WHERE o.batch_id = ? AND o.is_own = 0 AND o.paid = 1
            )
            SELECT SUM(weight_kg * (SELECT price_sale FROM product_prices pp
                    WHERE pp.product_id = items_priced.pid AND pp.valid_from <= items_priced.created_at
                    ORDER BY pp.valid_from DESC, pp.id DESC LIMIT 1)) AS s
            FROM items_priced
        """, (b["id"],)).fetchone()["s"] or 0
        conn.close()
        pack_total = pack_cnt * packaging_cost()
        pack_paid = paid_cnt * packaging_cost()
        sale = rows["sale_sum"] or 0
        cost = (rows["cost_client"] or 0) + (rows["cost_own"] or 0)
        income = sale + pack_total - cost
        paid_total = paid_sale + pack_paid
        unpaid_total = (sale + pack_total) - paid_total
        icon = "🟢" if b["status"] == "open" else "🗄"
        lines.append(f"{icon} Партия №{b['id']} ({b['created_at'][:10]})")
        lines.append(f"   Закупка: {round(cost, 2)} ₽")
        lines.append(f"   Реализация: {round(sale, 2)} ₽")
        lines.append(f"   Упаковка: {round(pack_total, 2)} ₽")
        lines.append(f"   Доход: {round(income, 2)} ₽")
        lines.append(f"   ✅ Оплачено: {round(paid_total, 2)} ₽")
        lines.append(f"   🕓 Не оплачено: {round(unpaid_total, 2)} ₽")
        lines.append("")
    await cb.message.edit_text("\n".join(lines))
    await state.clear()

# ---------- ФИНАНСЫ ----------
@dp.message(F.text == "💰 Финансы")
async def btn_finance(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="7 дней",   callback_data="fin:7")],
        [InlineKeyboardButton(text="30 дней",  callback_data="fin:30")],
        [InlineKeyboardButton(text="Всё время", callback_data="fin:all")],
    ])
    await state.set_state(FinanceFSM.choosing)
    await message.answer("Период (по дате заказов, только клиентские):", reply_markup=markup)

@dp.callback_query(FinanceFSM.choosing, F.data.startswith("fin:"))
async def finance_show(cb: CallbackQuery, state: FSMContext):
    p = cb.data.split(":")[1]
    if p == "all":
        where = "o.is_own = 0"
        params = ()
        label = "за всё время"
    else:
        since = (datetime.now() - timedelta(days=int(p))).isoformat(timespec="seconds")
        where = "o.is_own = 0 AND o.created_at >= ?"
        params = (since,)
        label = f"за {p} дней"
    conn = db()
    rows = conn.execute(f"""
        SELECT oi.weight_kg, p.id AS pid, o.created_at, o.id AS oid, o.paid
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        JOIN products p ON p.id = oi.product_id
        WHERE {where}
    """, params).fetchall()
    cnt = conn.execute(f"SELECT COUNT(*) AS c FROM orders o WHERE {where}",
                       params).fetchone()["c"]
    conn.close()
    tc = ts = 0
    paid_orders = set()
    all_orders = set()
    for r in rows:
        all_orders.add(r["oid"])
        if r["paid"]:
            paid_orders.add(r["oid"])
        if not r["weight_kg"]:
            continue
        conn = db()
        cp = conn.execute("""
            SELECT price_cost FROM product_prices
            WHERE product_id = ? AND valid_from <= ?
            ORDER BY valid_from DESC, id DESC LIMIT 1
        """, (r["pid"], r["created_at"])).fetchone()
        sp = conn.execute("""
            SELECT price_sale FROM product_prices
            WHERE product_id = ? AND valid_from <= ?
            ORDER BY valid_from DESC, id DESC LIMIT 1
        """, (r["pid"], r["created_at"])).fetchone()
        conn.close()
        if cp: tc += r["weight_kg"] * cp["price_cost"]
        if sp: ts += r["weight_kg"] * sp["price_sale"]
    pack_total = cnt * packaging_cost()
    paid_pack = len(paid_orders) * packaging_cost()
    income = ts + pack_total - tc
    margin = (income / (ts + pack_total) * 100) if (ts + pack_total) else 0
    paid_total = (ts * (len(paid_orders)/len(all_orders)) if all_orders else 0) + paid_pack
    # точнее:
    conn = db()
    paid_sale = conn.execute(f"""
        WITH items_priced AS (
            SELECT oi.weight_kg, o.created_at, p.id AS pid
            FROM order_items oi
            JOIN orders o ON o.id = oi.order_id
            JOIN products p ON p.id = oi.product_id
            WHERE {where} AND o.paid = 1
        )
        SELECT SUM(weight_kg * (SELECT price_sale FROM product_prices pp
                WHERE pp.product_id = items_priced.pid AND pp.valid_from <= items_priced.created_at
                ORDER BY pp.valid_from DESC, pp.id DESC LIMIT 1)) AS s
        FROM items_priced
    """, params).fetchone()["s"] or 0
    conn.close()
    paid_total = paid_sale + paid_pack
    unpaid_total = (ts + pack_total) - paid_total
    text = (f"💰 Финансы {label}\n\n"
            f"Заказов клиентских: {cnt}\n\n"
            f"Закупка: {round(tc, 2)} ₽\n"
            f"Реализация: {round(ts, 2)} ₽\n"
            f"Упаковка: {round(pack_total, 2)} ₽\n"
            f"Доход: {round(income, 2)} ₽\n"
            f"Маржа: {round(margin, 1)} %\n\n"
            f"✅ Оплачено: {round(paid_total, 2)} ₽\n"
            f"🕓 Не оплачено: {round(unpaid_total, 2)} ₽")
    await cb.message.edit_text(text)
    await state.clear()

# ---------- СТАТИСТИКА ----------
@dp.message(F.text == "📊 Статистика")
async def btn_stats(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="7 дней",   callback_data="st:7")],
        [InlineKeyboardButton(text="30 дней",  callback_data="st:30")],
        [InlineKeyboardButton(text="Всё время", callback_data="st:all")],
    ])
    await state.set_state(StatsFSM.choosing)
    await message.answer("Период (только клиентские заказы):", reply_markup=markup)

@dp.callback_query(StatsFSM.choosing, F.data.startswith("st:"))
async def stats_show(cb: CallbackQuery, state: FSMContext):
    p = cb.data.split(":")[1]
    if p == "all":
        where = "o.is_own = 0"
        params = ()
        label = "за всё время"
    else:
        since = (datetime.now() - timedelta(days=int(p))).isoformat(timespec="seconds")
        where = "o.is_own = 0 AND o.created_at >= ?"
        params = (since,)
        label = f"за {p} дней"
    conn = db()
    top_p = conn.execute(f"""
        SELECT p.name, SUM(oi.qty_pcs) AS pcs
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        JOIN products p ON p.id = oi.product_id
        WHERE {where}
        GROUP BY p.id ORDER BY pcs DESC
    """, params).fetchall()
    top_c = conn.execute(f"""
        SELECT c.name, COUNT(DISTINCT o.id) AS cnt
        FROM orders o JOIN customers c ON c.id = o.customer_id
        WHERE {where} GROUP BY c.id ORDER BY cnt DESC
    """, params).fetchall()
    conn.close()
    lines = [f"📊 Статистика {label}", "", "По товарам (шт):"]
    for r in top_p:
        lines.append(f"• {r['name']}: {r['pcs']}")
    lines.append("")
    lines.append("По клиентам (заказов):")
    for r in top_c:
        lines.append(f"• {r['name']}: {r['cnt']}")
    await cb.message.edit_text("\n".join(lines))
    await state.clear()

# ---------- ЭКСПОРТ ----------
@dp.message(F.text == "📁 Экспорт")
async def btn_export(message: Message):
    if not is_admin(message):
        return
    conn = db()
    rows = conn.execute("""
        SELECT o.id AS order_id, o.created_at, o.status, o.is_own, o.batch_id,
               o.paid, o.paid_at, c.name AS customer, p.name AS product,
               oi.qty_pcs, oi.weight_kg, oi.product_id
        FROM orders o
        LEFT JOIN customers c ON c.id = o.customer_id
        JOIN order_items oi ON oi.order_id = o.id
        JOIN products p ON p.id = oi.product_id
        ORDER BY o.id, oi.id
    """).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["order_id","batch_id","is_own","paid","paid_at","created_at","status",
                "customer","product","qty_pcs","weight_kg","price_sale","price_cost",
                "sale_amount","cost_amount","income"])
    for r in rows:
        ps = conn.execute("""
            SELECT price_sale FROM product_prices
            WHERE product_id = ? AND valid_from <= ?
            ORDER BY valid_from DESC, id DESC LIMIT 1
        """, (r["product_id"], r["created_at"])).fetchone()
        pc = conn.execute("""
            SELECT price_cost FROM product_prices
            WHERE product_id = ? AND valid_from <= ?
            ORDER BY valid_from DESC, id DESC LIMIT 1
        """, (r["product_id"], r["created_at"])).fetchone()
        ps_v = ps["price_sale"] if ps else 0
        pc_v = pc["price_cost"] if pc else 0
        wkg = r["weight_kg"] or 0
        if r["is_own"]:
            sa = 0.0
            ca = round(wkg * pc_v, 2)
        else:
            sa = round(wkg * ps_v, 2)
            ca = round(wkg * pc_v, 2)
        w.writerow([r["order_id"], r["batch_id"], r["is_own"], r["paid"], r["paid_at"],
                    r["created_at"], r["status"], r["customer"], r["product"], r["qty_pcs"],
                    r["weight_kg"], ps_v, pc_v, sa, ca, round(sa - ca, 2)])
    conn.close()
    data = buf.getvalue().encode("utf-8-sig")
    filename = f"fish_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    await message.answer_document(BufferedInputFile(data, filename=filename))

# ---------- БЭКАП / ИМПОРТ ----------
@dp.message(F.text == "💾 Бэкап базы")
async def btn_backup(message: Message):
    if not is_admin(message):
        return
    await message.answer("Готовлю бэкап...")
    await send_backup(message.chat.id)

@dp.message(F.text == "📥 Импорт базы")
async def btn_import(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    await state.set_state(ImportFSM.waiting_file)
    await message.answer(
        "📥 Пришли файл бэкапа (`fish_backup_*.db`) документом.\n\n"
        "⚠️ Текущая база будет заменена.\n"
        "Отмена — /cancel"
    )

@dp.message(ImportFSM.waiting_file, F.document)
async def import_file(message: Message, state: FSMContext):
    doc = message.document
    if not doc.file_name.endswith(".db"):
        await message.answer("Нужен файл с расширением `.db`.")
        return
    if os.path.exists(DB_PATH):
        try:
            with open(DB_PATH, "rb") as f:
                old_data = f.read()
            if len(old_data) > 0:
                await bot.send_document(
                    chat_id=message.chat.id,
                    document=BufferedInputFile(
                        old_data,
                        filename=f"fish_before_import_{datetime.now().strftime('%Y%m%d_%H%M')}.db",
                    ),
                    caption="🛟 Страховочная копия ТЕКУЩЕЙ базы (до импорта).",
                )
        except Exception as e:
            logging.error(f"Страховочная копия не сделана: {e}")

    file = await bot.get_file(doc.file_id)
    tmp_path = "fish_import_tmp.db"
    await bot.download_file(file.file_path, tmp_path)

    try:
        conn = sqlite3.connect(tmp_path)
        conn.row_factory = sqlite3.Row
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        names = {t["name"] for t in tables}
        required = {"products", "product_prices", "customers", "orders", "order_items"}
        if not required.issubset(names):
            conn.close()
            os.remove(tmp_path)
            await message.answer("⚠️ Файл не похож на базу бота. Импорт отменён.")
            await state.clear()
            return
        p_cnt = conn.execute("SELECT COUNT(*) AS c FROM products").fetchone()["c"]
        c_cnt = conn.execute("SELECT COUNT(*) AS c FROM customers").fetchone()["c"]
        o_cnt = conn.execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"]
        conn.close()
    except Exception as e:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        await message.answer(f"⚠️ Ошибка чтения файла: {e}")
        await state.clear()
        return

    try:
        try:
            os.sync()
        except Exception:
            pass
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
        shutil.move(tmp_path, DB_PATH)
        try:
            os.sync()
        except Exception:
            pass
    except Exception as e:
        await message.answer(f"⚠️ Не удалось заменить базу: {e}")
        await state.clear()
        return

    init_db()

    await message.answer(
        f"✅ База восстановлена!\n\n"
        f"Товаров: {p_cnt}\n"
        f"Клиентов: {c_cnt}\n"
        f"Заказов (всего): {o_cnt}\n\n"
        f"Бот продолжает работу."
    )
    await state.clear()

@dp.message(ImportFSM.waiting_file)
async def import_wrong(message: Message, state: FSMContext):
    if message.text and message.text.strip().lower() in ("/cancel", "отмена"):
        await state.clear()
        await message.answer("Импорт отменён.", reply_markup=main_menu())
        return
    await message.answer("Пришли файл `.db` документом или /cancel.")

# ---------- PING ----------
async def handle_ping(request):
    return web.Response(text="ok")

async def start_web():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/ping", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

# ---------- ЗАПУСК ----------
async def main():
    await start_web()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
