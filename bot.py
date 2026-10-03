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
ADMIN_ID = 186453903   # ⚠️ ЗАМЕНИ НА СВОЙ ID (186453903)
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
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        status TEXT DEFAULT 'open',
        archived_at TEXT
    );
    CREATE TABLE IF NOT EXISTS order_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        qty_pcs INTEGER NOT NULL,
        weight_kg REAL,
        weighed INTEGER DEFAULT 0
    );
    """)
    conn.commit()
    conn.close()

init_db()

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

class ArchiveFSM(StatesGroup):
    confirm = State()

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

# ---------- БОТ ----------
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

def is_admin(msg_or_cb) -> bool:
    return msg_or_cb.from_user.id == ADMIN_ID

# ---------- МЕНЮ ----------
def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📦 Новый заказ"), KeyboardButton(text="📋 Заказы")],
            [KeyboardButton(text="⚖️ Взвесить"),   KeyboardButton(text="🔄 Перевесить")],
            [KeyboardButton(text="🧾 Чек"),        KeyboardButton(text="📤 Поставщик")],
            [KeyboardButton(text="📈 Итоги партии"), KeyboardButton(text="💰 Финансы")],
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="📁 Экспорт")],
            [KeyboardButton(text="🗄 Архив партии"), KeyboardButton(text="💾 Бэкап базы")],
            [KeyboardButton(text="📥 Импорт базы"),  KeyboardButton(text="⚙️ Настройки")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )

def settings_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📦 Товары"), KeyboardButton(text="👥 Клиенты")],
            [KeyboardButton(text="➕ Товар"),  KeyboardButton(text="✏️ Изменить товар")],
            [KeyboardButton(text="➕ Клиент"), KeyboardButton(text="🏠 Меню")],
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
        SELECT o.id, c.name AS customer, o.created_at
        FROM orders o JOIN customers c ON c.id = o.customer_id
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
        await bot.send_message(chat_id=chat_id,
                               text=f"⚠️ Не удалось отправить бэкап: {e}")
        return False

# ---------- СТАРТ ----------
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message):
        await message.answer("Нет доступа.")
        return
    await message.answer(
        "🐟 Бот учёта рыбы.\n\nПользуйся кнопками снизу.",
        reply_markup=main_menu(),
    )

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu())

@dp.message(Command("cleanprices"))
async def cmd_cleanprices(message: Message):
    if not is_admin(message):
        return
    conn = db()
    products = conn.execute("SELECT id, name FROM products").fetchall()
    removed_total = 0
    report = []
    for p in products:
        rows = conn.execute("""
            SELECT id, price_sale, price_cost FROM product_prices
            WHERE product_id = ? ORDER BY valid_from ASC, id ASC
        """, (p["id"],)).fetchall()
        to_delete = []
        prev_sale = prev_cost = None
        for r in rows:
            if (prev_sale is not None
                    and abs(prev_sale - r["price_sale"]) < 0.001
                    and abs(prev_cost - r["price_cost"]) < 0.001):
                to_delete.append(r["id"])
            else:
                prev_sale = r["price_sale"]
                prev_cost = r["price_cost"]
        if to_delete:
            conn.executemany("DELETE FROM product_prices WHERE id = ?",
                             [(i,) for i in to_delete])
            removed_total += len(to_delete)
            report.append(f"• {p['name']}: удалено {len(to_delete)}")
    conn.commit()
    conn.close()
    if removed_total == 0:
        await message.answer("Дублей не найдено.")
    else:
        await message.answer(f"🧹 Удалено: {removed_total}\n" + "\n".join(report))

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

# ---------- БЭКАП ----------
@dp.message(F.text == "💾 Бэкап базы")
async def btn_backup(message: Message):
    if not is_admin(message):
        return
    await message.answer("Готовлю бэкап...")
    await send_backup(message.chat.id)

# ---------- ИМПОРТ ----------
@dp.message(F.text == "📥 Импорт базы")
async def btn_import(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    await state.set_state(ImportFSM.waiting_file)
    await message.answer(
        "📥 Пришли файл бэкапа (`fish_backup_*.db`) документом.\n\n"
        "⚠️ Текущая база будет заменена.\n"
        "Если нужно — сначала сделай «💾 Бэкап базы».\n\n"
        "Отмена — /cancel"
    )

@dp.message(ImportFSM.waiting_file, F.document)
async def import_file(message: Message, state: FSMContext):
    doc = message.document
    if not doc.file_name.endswith(".db"):
        await message.answer("Нужен файл с расширением `.db`.")
        return

    # 1) страховочная копия текущей базы + отправляем её в чат
    if os.path.exists(DB_PATH):
        try:
            shutil.copy(DB_PATH, DB_PATH + ".before_import")
            with open(DB_PATH, "rb") as f:
                old_data = f.read()
            if len(old_data) > 0:
                await bot.send_document(
                    chat_id=message.chat.id,
                    document=BufferedInputFile(
                        old_data,
                        filename=f"fish_before_import_{datetime.now().strftime('%Y%m%d_%H%M')}.db",
                    ),
                    caption="🛟 Страховочная копия ТЕКУЩЕЙ базы (до импорта).\n"
                            "Сохрани, если вдруг прислал не тот файл.",
                )
        except Exception as e:
            logging.error(f"Страховочная копия не сделана: {e}")

    # 2) скачиваем новый файл
    file = await bot.get_file(doc.file_id)
    tmp_path = "fish_import_tmp.db"
    await bot.download_file(file.file_path, tmp_path)

    # 3) проверяем структуру
    try:
        conn = sqlite3.connect(tmp_path)
        conn.row_factory = sqlite3.Row
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {t["name"] for t in tables}
        required = {"products", "product_prices", "customers", "orders", "order_items"}
        if not required.issubset(names):
            conn.close()
            os.remove(tmp_path)
            await message.answer(
                "⚠️ Файл не похож на базу бота — нет нужных таблиц.\n"
                "Импорт отменён, текущая база не тронута."
            )
            await state.clear()
            return
        p_cnt = conn.execute("SELECT COUNT(*) AS c FROM products").fetchone()["c"]
        c_cnt = conn.execute("SELECT COUNT(*) AS c FROM customers").fetchone()["c"]
        o_cnt = conn.execute("SELECT COUNT(*) AS c FROM orders").fetchone()["c"]
        oi_cnt = conn.execute("SELECT COUNT(*) AS c FROM order_items").fetchone()["c"]
        conn.close()
    except Exception as e:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        await message.answer(f"⚠️ Ошибка чтения файла: {e}")
        await state.clear()
        return

    # 4) подменяем базу с принудительным сбросом на диск
    try:
        # сброс буферов ОС
        try:
            os.sync()
        except Exception:
            pass
        # удаляем старый файл явно, потом переносим новый
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
        shutil.move(tmp_path, DB_PATH)
        # снова сброс, чтобы замена точно попала на диск
        try:
            os.sync()
        except Exception:
            pass
    except Exception as e:
        await message.answer(f"⚠️ Не удалось заменить базу: {e}")
        await state.clear()
        return

    await message.answer(
        f"✅ База восстановлена!\n\n"
        f"Товаров: {p_cnt}\n"
        f"Клиентов: {c_cnt}\n"
        f"Заказов (всего): {o_cnt}\n"
        f"Позиций в заказах: {oi_cnt}\n\n"
        f"Бот продолжает работу — можно пользоваться."
    )
    await state.clear()

@dp.message(ImportFSM.waiting_file)
async def import_wrong(message: Message, state: FSMContext):
    if message.text and message.text.strip().lower() in ("/cancel", "отмена"):
        await state.clear()
        await message.answer("Импорт отменён.", reply_markup=main_menu())
        return
    await message.answer("Пришли файл `.db` документом или /cancel для отмены.")

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
    await message.answer(
        "Введи в формате: Название Цена_продажи Цена_закупки\n"
        "Например: Скумбрия 2000 1200"
    )
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
    await message.answer(
        f"Товар добавлен: {name}\nПродажа: {ps} ₽/кг\nЗакупка: {pc} ₽/кг",
        reply_markup=settings_menu(),
    )
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
    await message.answer(f"Клиент добавлен: {message.text}",
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
        today = datetime.now().date().isoformat()
        lines = [f"📜 История цен: {prod['name']}", ""]
        for i, r in enumerate(rows):
            mark = " ⬅️ текущая" if i == 0 else ""
            recent = " 🆕" if r["valid_from"].startswith(today) else ""
            lines.append(f"{r['valid_from']}{recent}{mark}\n"
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
@dp.message(F.text == "📦 Новый заказ")
async def btn_neworder(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    rows = customers_list()
    if not rows:
        await message.answer("Сначала добавь клиентов через ⚙️ Настройки → ➕ Клиент")
        return
    items = [(r["id"], r["name"]) for r in rows]
    await state.set_state(OrderFSM.choosing_customer)
    await message.answer("Выбери клиента:", reply_markup=kb(items, "cust"))

@dp.callback_query(OrderFSM.choosing_customer, F.data.startswith("cust:"))
async def order_customer(cb: CallbackQuery, state: FSMContext):
    cid = int(cb.data.split(":")[1])
    conn = db()
    conn.execute("INSERT INTO orders (customer_id, created_at) VALUES (?, ?)",
                 (cid, datetime.now().isoformat(timespec="seconds")))
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
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🧾 Чек",       callback_data=f"order:check:{r['id']}"),
             InlineKeyboardButton(text="✏️ Изменить",  callback_data=f"order:edit:{r['id']}")],
            [InlineKeyboardButton(text="📦 В архив",   callback_data=f"order:arch:{r['id']}"),
             InlineKeyboardButton(text="❌ Удалить",   callback_data=f"order:del:{r['id']}")],
        ])
        await message.answer(f"№{r['id']} — {r['customer']} ({r['created_at']})",
                             reply_markup=markup)

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

    elif action == "qty":
        conn = db()
        items_rows = conn.execute("""
            SELECT oi.id, p.name, oi.qty_pcs
            FROM order_items oi JOIN products p ON p.id = oi.product_id
            WHERE oi.order_id = ?
            ORDER BY oi.id
        """, (oid,)).fetchall()
        conn.close()
        if not items_rows:
            await cb.message.edit_text("В заказе нет позиций.")
            return
        items = [(r["id"], f"{r['name']} — {r['qty_pcs']} шт.") for r in items_rows]
        await state.update_data(order_id=oid)
        await state.set_state(EditOrderFSM.choosing_item_qty)
        await cb.message.edit_text("Какую позицию изменить?",
                                   reply_markup=kb(items, "eqty"))

    elif action == "del":
        conn = db()
        items_rows = conn.execute("""
            SELECT oi.id, p.name, oi.qty_pcs
            FROM order_items oi JOIN products p ON p.id = oi.product_id
            WHERE oi.order_id = ?
            ORDER BY oi.id
        """, (oid,)).fetchall()
        conn.close()
        if not items_rows:
            await cb.message.edit_text("В заказе нет позиций.")
            return
        items = [(r["id"], f"{r['name']} — {r['qty_pcs']} шт.") for r in items_rows]
        await state.update_data(order_id=oid)
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
    conn.execute(
        "INSERT INTO order_items (order_id, product_id, qty_pcs) VALUES (?, ?, ?)",
        (data["order_id"], data["product_id"], int(message.text)),
    )
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
        SELECT oi.id, c.name AS customer, p.name AS product, oi.qty_pcs
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        JOIN customers c ON c.id = o.customer_id
        JOIN products p ON p.id = oi.product_id
        WHERE oi.weighed = 0 AND o.status = 'open'
        ORDER BY oi.id
    """).fetchall()
    conn.close()
    if not rows:
        await message.answer("Нет позиций для взвешивания.")
        return
    items = [(r["id"], f"{r['customer']} — {r['product']}, {r['qty_pcs']} шт.")
             for r in rows]
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
        SELECT oi.id, oi.order_id, oi.product_id, p.name, o.created_at
        FROM order_items oi
        JOIN products p ON p.id = oi.product_id
        JOIN orders o ON o.id = oi.order_id
        WHERE oi.id = ?
    """, (item_id,)).fetchone()
    price = conn.execute("""
        SELECT price_sale FROM product_prices
        WHERE product_id = ? AND valid_from <= ?
        ORDER BY valid_from DESC, id DESC LIMIT 1
    """, (row["product_id"], row["created_at"])).fetchone()
    ps = price["price_sale"] if price else 0
    conn.execute("UPDATE order_items SET weight_kg = ?, weighed = 1 WHERE id = ?",
                 (w, item_id))
    conn.commit()
    conn.close()
    await message.answer(f"Сохранено: {row['name']} — {w} кг × {ps} ₽ = {round(w*ps,2)} ₽")
    await state.clear()

# ---------- ПЕРЕВВЕСТИ ----------
@dp.message(F.text == "🔄 Перевесить")
async def btn_reweigh(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    conn = db()
    rows = conn.execute("""
        SELECT oi.id, c.name AS customer, p.name AS product, oi.weight_kg
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        JOIN customers c ON c.id = o.customer_id
        JOIN products p ON p.id = oi.product_id
        WHERE oi.weighed = 1 AND o.status = 'open'
        ORDER BY oi.id
    """).fetchall()
    conn.close()
    if not rows:
        await message.answer("Нет взвешенных позиций.")
        return
    items = [(r["id"], f"{r['customer']} — {r['product']} ({r['weight_kg']} кг)")
             for r in rows]
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
    conn.execute("UPDATE order_items SET weight_kg = ? WHERE id = ?",
                 (w, data["item_id"]))
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
    items = [(r["id"], f"№{r['id']} — {r['customer']}") for r in rows]
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
        SELECT o.id, o.created_at, c.name AS customer
        FROM orders o JOIN customers c ON c.id = o.customer_id
        WHERE o.id = ?
    """, (oid,)).fetchone()
    items = conn.execute("""
        SELECT p.name, oi.qty_pcs, oi.weight_kg, oi.product_id
        FROM order_items oi JOIN products p ON p.id = oi.product_id
        WHERE oi.order_id = ?
    """, (oid,)).fetchall()
    lines = [f"Чек — заказ №{order['id']}", f"Клиент: {order['customer']}", ""]
    total = 0.0
    not_weighed = 0
    for it in items:
        if it["weight_kg"] is None:
            not_weighed += 1
            lines.append(f"• {it['name']}: {it['qty_pcs']} шт. — вес не введён")
            continue
        price = conn.execute("""
            SELECT price_sale FROM product_prices
            WHERE product_id = ? AND valid_from <= ?
            ORDER BY valid_from DESC, id DESC LIMIT 1
        """, (it["product_id"], order["created_at"])).fetchone()
        ps = price["price_sale"] if price else 0
        amount = round(it["weight_kg"] * ps, 2)
        total += amount
        lines.append(f"• {it['name']}: {it['qty_pcs']} шт., {it['weight_kg']} кг "
                     f"× {ps} ₽ = {amount} ₽")
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
        SELECT o.id, o.created_at, c.name AS customer
        FROM orders o JOIN customers c ON c.id = o.customer_id
        WHERE o.id = ?
    """, (oid,)).fetchone()
    items = conn.execute("""
        SELECT p.name, oi.qty_pcs, oi.weight_kg, oi.product_id
        FROM order_items oi JOIN products p ON p.id = oi.product_id
        WHERE oi.order_id = ?
    """, (oid,)).fetchall()
    lines = [f"Чек — заказ №{order['id']}", f"Клиент: {order['customer']}", ""]
    total = 0.0
    for it in items:
        price = conn.execute("""
            SELECT price_sale FROM product_prices
            WHERE product_id = ? AND valid_from <= ?
            ORDER BY valid_from DESC, id DESC LIMIT 1
        """, (it["product_id"], order["created_at"])).fetchone()
        ps = price["price_sale"] if price else 0
        amount = round((it["weight_kg"] or 0) * ps, 2)
        total += amount
        lines.append(f"• {it['name']}: {it['qty_pcs']} шт., {it['weight_kg']} кг "
                     f"× {ps} ₽ = {amount} ₽")
    conn.close()
    lines.append("")
    lines.append(f"Итого: {round(total, 2)} ₽")
    lines.append("")
    lines.append("Спасибо за покупку! 🐟")
    await cb.message.edit_text("\n".join(lines))

# ---------- ПОСТАВЩИК ----------
@dp.message(F.text == "📤 Поставщик")
async def btn_supply(message: Message):
    if not is_admin(message):
        return
    conn = db()
    rows = conn.execute("""
        SELECT p.name, SUM(oi.qty_pcs) AS pcs
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        JOIN products p ON p.id = oi.product_id
        WHERE o.status = 'open'
        GROUP BY p.id ORDER BY p.name
    """).fetchall()
    conn.close()
    if not rows:
        await message.answer("Нет открытых заказов.")
        return
    lines = ["📦 Сводка для поставщика:", ""]
    for r in rows:
        lines.append(f"• {r['name']}: {r['pcs']} шт.")
    await message.answer("\n".join(lines))

# ---------- ИТОГИ ПАРТИИ ----------
@dp.message(F.text == "📈 Итоги партии")
async def btn_party(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Открытые",  callback_data="party:open")],
        [InlineKeyboardButton(text="🗄 Архивные",  callback_data="party:arch")],
        [InlineKeyboardButton(text="📚 Всё вместе", callback_data="party:all")],
    ])
    await state.set_state(PartyFSM.choosing)
    await message.answer("Какие заказы?", reply_markup=markup)

@dp.callback_query(PartyFSM.choosing, F.data.startswith("party:"))
async def party_show(cb: CallbackQuery, state: FSMContext):
    scope = cb.data.split(":")[1]
    where = {"open": "o.status='open'",
             "arch": "o.status='archived'",
             "all": "1=1"}[scope]
    label = {"open": "Открытые", "arch": "Архивные", "all": "Все"}[scope]
    conn = db()
    rows = conn.execute(f"""
        WITH items_priced AS (
            SELECT p.id AS pid, p.name,
                   oi.qty_pcs, oi.weight_kg, o.created_at
            FROM order_items oi
            JOIN products p ON p.id = oi.product_id
            JOIN orders o ON o.id = oi.order_id
            WHERE {where}
        )
        SELECT pid, name,
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
        GROUP BY pid ORDER BY name
    """).fetchall()
    conn.close()
    if not rows:
        await cb.message.edit_text("Нет данных.")
        await state.clear()
        return
    lines = [f"📈 Итоги партии ({label})", ""]
    tc = ts = 0
    for r in rows:
        kg = r["kg"] or 0
        cs = r["cost_sum"] or 0
        ss = r["sale_sum"] or 0
        tc += cs
        ts += ss
        lines.append(f"• {r['name']}")
        lines.append(f"   шт: {r['pcs']}, кг: {round(kg, 2)}")
        lines.append(f"   закупка: {round(cs,2)} ₽")
        lines.append(f"   реализация: {round(ss,2)} ₽")
        lines.append(f"   доход: {round(ss-cs,2)} ₽")
    lines.append("")
    lines.append(f"💰 ИТОГО")
    lines.append(f"Закупка: {round(tc,2)} ₽")
    lines.append(f"Реализация: {round(ts,2)} ₽")
    lines.append(f"ДОХОД: {round(ts-tc,2)} ₽")
    await cb.message.edit_text("\n".join(lines))
    await state.clear()

# ---------- ФИНАНСЫ ----------
@dp.message(F.text == "💰 Финансы")
async def btn_finance(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="7 дней",  callback_data="fin:7")],
        [InlineKeyboardButton(text="30 дней", callback_data="fin:30")],
        [InlineKeyboardButton(text="Всё время", callback_data="fin:all")],
    ])
    await state.set_state(FinanceFSM.choosing)
    await message.answer("Период:", reply_markup=markup)

@dp.callback_query(FinanceFSM.choosing, F.data.startswith("fin:"))
async def finance_show(cb: CallbackQuery, state: FSMContext):
    p = cb.data.split(":")[1]
    if p == "all":
        where = "1=1"
        params = ()
        label = "за всё время"
    else:
        since = (datetime.now() - timedelta(days=int(p))).isoformat(timespec="seconds")
        where = "o.created_at >= ?"
        params = (since,)
        label = f"за {p} дней"
    conn = db()
    rows = conn.execute(f"""
        SELECT oi.weight_kg, p.id AS pid, o.created_at
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        JOIN products p ON p.id = oi.product_id
        WHERE {where}
    """, params).fetchall()
    cnt = conn.execute(f"SELECT COUNT(*) AS c FROM orders o WHERE {where}",
                       params).fetchone()["c"]
    conn.close()
    tc = ts = 0
    for r in rows:
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
    income = ts - tc
    margin = (income / ts * 100) if ts else 0
    text = (f"💰 Финансы {label}\n\n"
            f"Заказов: {cnt}\n\n"
            f"Расход: {round(tc,2)} ₽\n"
            f"Приход: {round(ts,2)} ₽\n"
            f"Доход: {round(income,2)} ₽\n"
            f"Маржа: {round(margin,1)} %")
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
    await message.answer("Период:", reply_markup=markup)

@dp.callback_query(StatsFSM.choosing, F.data.startswith("st:"))
async def stats_show(cb: CallbackQuery, state: FSMContext):
    p = cb.data.split(":")[1]
    if p == "all":
        where = "1=1"
        params = ()
        label = "за всё время"
    else:
        since = (datetime.now() - timedelta(days=int(p))).isoformat(timespec="seconds")
        where = "o.created_at >= ?"
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

# ---------- АРХИВ ПАРТИИ (с авто-бэкапом) ----------
@dp.message(F.text == "🗄 Архив партии")
async def btn_archive(message: Message, state: FSMContext):
    if not is_admin(message):
        return
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ В архив", callback_data="arch:yes")],
        [InlineKeyboardButton(text="❌ Отмена",  callback_data="arch:no")],
    ])
    await state.set_state(ArchiveFSM.confirm)
    await message.answer(
        "Все открытые заказы в архив?\nПосле архивации придёт файл базы.",
        reply_markup=markup,
    )

@dp.callback_query(ArchiveFSM.confirm, F.data.startswith("arch:"))
async def archive_confirm(cb: CallbackQuery, state: FSMContext):
    if cb.data == "arch:no":
        await cb.message.edit_text("Отменено.")
        await state.clear()
        return

    conn = db()
    cnt = conn.execute("SELECT COUNT(*) AS c FROM orders WHERE status='open'").fetchone()["c"]
    conn.execute("UPDATE orders SET status='archived', archived_at=? WHERE status='open'",
                 (datetime.now().isoformat(timespec="seconds"),))
    conn.commit()
    conn.close()

    await cb.message.edit_text(
        f"🗄 В архив отправлено заказов: {cnt}.\nГотовлю бэкап..."
    )
    await send_backup(cb.from_user.id, caption_prefix=f"Бэкап после архивации ({cnt} зак.)")
    await state.clear()

# ---------- ЭКСПОРТ ----------
@dp.message(F.text == "📁 Экспорт")
async def btn_export(message: Message):
    if not is_admin(message):
        return
    conn = db()
    rows = conn.execute("""
        SELECT o.id AS order_id, o.created_at, o.status,
               c.name AS customer, p.name AS product,
               oi.qty_pcs, oi.weight_kg, oi.product_id
        FROM orders o
        JOIN customers c ON c.id = o.customer_id
        JOIN order_items oi ON oi.order_id = o.id
        JOIN products p ON p.id = oi.product_id
        ORDER BY o.id, oi.id
    """).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["order_id","created_at","status","customer","product",
                "qty_pcs","weight_kg","price_sale","price_cost",
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
        sa = round(wkg * ps_v, 2)
        ca = round(wkg * pc_v, 2)
        w.writerow([r["order_id"], r["created_at"], r["status"],
                    r["customer"], r["product"], r["qty_pcs"],
                    r["weight_kg"], ps_v, pc_v, sa, ca, round(sa-ca,2)])
    conn.close()
    data = buf.getvalue().encode("utf-8-sig")
    filename = f"fish_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    await message.answer_document(BufferedInputFile(data, filename=filename))

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
