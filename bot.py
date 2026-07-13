import logging 
import os
import asyncio
import asyncpg
import random
import aiohttp
import string as _string
from datetime import date, timedelta, datetime
from urllib.parse import quote

from aiogram import Bot, Dispatcher, types
from aiogram.dispatcher.middlewares import BaseMiddleware
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup

# ========== КОНФИГ ==========

BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(token=BOT_TOKEN)

# ========== КОНФИГУРАЦИЯ ГОРОДОВ И АДМИНИСТРАТОРОВ ==========
# Города и их админы хранятся в таблице cities (БД).
# При старте загружаются в CITIES; обновляются командами /addcity, /removecity, /editcity.
# Чтобы добавить нового высшего админа — добавить ID в SUPER_ADMINS.

SUPER_ADMINS: list[int] = [7805603791]   # высшие админы (видят все города)

# CITIES заполняется из БД при init_db() и обновляется командами управления городами.
# Структура: {city_key: {"name": str, "stock_pool": str, "admins": [int, ...]}}
CITIES: dict[str, dict] = {}

# Fallback-значения для первого запуска до загрузки БД (overridden by load_cities_from_db)
_CITIES_DEFAULTS: dict[str, dict] = {
    "buerhausen": {
        "name": "Buerhausen",
        "stock_pool": "default",
        "admins": [8283121468],
    },
    "munich": {
        "name": "Munich",
        "stock_pool": "munich",
        "admins": [1518888796],
    },
}

# ADMIN_IDS пересчитывается при каждом изменении CITIES
ADMIN_IDS: list[int] = list(SUPER_ADMINS)


def _rebuild_admin_ids():
    """Пересчитывает ADMIN_IDS из текущего состояния CITIES."""
    global ADMIN_IDS
    city_admins = [uid for c in CITIES.values() for uid in c.get("admins", [])]
    ADMIN_IDS = list({*SUPER_ADMINS, *city_admins})


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def is_super_admin(uid: int) -> bool:
    return uid in SUPER_ADMINS

def get_city_for_admin(uid: int) -> str | None:
    """Возвращает city_key для городского админа, или None если это высший админ."""
    for key, cfg in CITIES.items():
        if uid in cfg.get("admins", []):
            return key
    return None

def get_city_admins(city_key: str) -> list[int]:
    """Список городских админов для указанного города."""
    return CITIES.get(city_key, {}).get("admins", [])

def get_order_city(city_key: str | None) -> str:
    """
    Возвращает city_key для маршрутизации заказа.
    delivery и неизвестный ключ → дефолтный город (хранится в БД).
    """
    if city_key and city_key in CITIES:
        return city_key
    return _DEFAULT_CITY

def get_stock_pool(city_key: str | None) -> str:
    """
    Возвращает ключ пула наличия для города.
    'default' → products.stock; иное → city_stock.city_key.
    """
    resolved = get_order_city(city_key)
    return CITIES.get(resolved, {}).get("stock_pool", "default")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== КОНСТАНТЫ ==========
BASE_PRICE: int = 15   # Базовая цена товара в EUR. Менять только здесь.

storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# ========== THROTTLING MIDDLEWARE ==========
# Ограничивает частоту запросов: 1 action / THROTTLE_RATE секунд на пользователя.
# Защищает pool от перегрузки при спаме кнопками.

THROTTLE_RATE = 0.7          # секунды между разрешёнными запросами
THROTTLE_WARN_AFTER = 3      # сколько раз предупредить пользователя, прежде чем молча игнорировать

_throttle_map: dict[int, float] = {}      # uid → timestamp последнего action
_throttle_warn:  dict[int, int]  = {}     # uid → сколько раз уже предупреждён


class ThrottlingMiddleware(BaseMiddleware):
    """aiogram 2.x process_update middleware — не требует декораторов на хендлерах."""

    async def on_pre_process_update(self, update: types.Update, data: dict):
        # Определяем uid из любого типа апдейта
        if update.message:
            uid = update.message.from_user.id
        elif update.callback_query:
            uid = update.callback_query.from_user.id
        else:
            return

        now = asyncio.get_event_loop().time()
        last = _throttle_map.get(uid, 0.0)
        diff = now - last

        if diff < THROTTLE_RATE:
            warns = _throttle_warn.get(uid, 0)
            if warns < THROTTLE_WARN_AFTER:
                _throttle_warn[uid] = warns + 1
                try:
                    if update.callback_query:
                        await update.callback_query.answer("⏳", show_alert=False)
                    elif update.message:
                        await update.message.answer("⏳")
                except Exception:
                    pass
            # Пропускаем апдейт — не передаём дальше по цепочке
            raise CancelHandler()

        _throttle_map[uid] = now
        _throttle_warn.pop(uid, None)   # сброс счётчика предупреждений


from aiogram.dispatcher.handler import CancelHandler

dp.middleware.setup(ThrottlingMiddleware())

_bot_username = None


_DEFAULT_CITY: str = "buerhausen"  # обновляется при load_cities_from_db


async def load_cities_from_db(conn=None) -> None:
    """Загружает города из таблицы cities в глобальный CITIES и пересчитывает ADMIN_IDS."""
    global CITIES, _DEFAULT_CITY
    if conn is not None:
        rows = await conn.fetch("SELECT city_key, name, stock_pool, admin_ids, is_default FROM cities")
    else:
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT city_key, name, stock_pool, admin_ids, is_default FROM cities")
    new_cities = {}
    default_city = None
    for r in rows:
        raw = r["admin_ids"] or ""
        admins = [int(x) for x in raw.split(",") if x.strip().isdigit()]
        new_cities[r["city_key"]] = {
            "name": r["name"],
            "stock_pool": r["stock_pool"],
            "admins": admins,
        }
        if r["is_default"]:
            default_city = r["city_key"]
    CITIES.clear()
    CITIES.update(new_cities)
    if default_city:
        _DEFAULT_CITY = default_city
    elif CITIES:
        _DEFAULT_CITY = next(iter(CITIES))
    _rebuild_admin_ids()


async def alert_super_admins(text: str) -> None:
    """Отправляет важное сообщение об ошибке всем высшим админам в Telegram."""
    for sa in SUPER_ADMINS:
        try:
            await bot.send_message(sa, f"🚨 {text}")
        except Exception:
            pass

class CitySelectContext(StatesGroup):
    """Состояние выбора города (запускается из /start или профиля)."""
    choosing = State()   # ожидаем нажатия кнопки города

async def get_bot_username() -> str:
    """Username бота для реферальных ссылок (кэшируется после первого запроса)."""
    global _bot_username
    if not _bot_username:
        me = await bot.get_me()
        _bot_username = me.username
    return _bot_username

# ========== БАЗА ДАННЫХ ==========

pool = None

async def init_db():
    global pool

    pool = await asyncpg.create_pool(
        dsn=os.getenv("DATABASE_URL"),
        min_size=1,
        max_size=10
    )

    async with pool.acquire() as conn:

        # ========== ГОРОДА В БД ==========
        # Города хранятся в БД: добавляются/удаляются командами без редеплоя.
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS cities (
            city_key  TEXT PRIMARY KEY,
            name      TEXT NOT NULL,
            stock_pool TEXT NOT NULL DEFAULT 'default',
            admin_ids TEXT NOT NULL DEFAULT '',
            is_default BOOLEAN NOT NULL DEFAULT false
        )
        """)
        await conn.execute("""
            ALTER TABLE cities ADD COLUMN IF NOT EXISTS is_default BOOLEAN NOT NULL DEFAULT false
        """)

        # Загружаем или инициализируем дефолтные города
        existing = await conn.fetchval("SELECT COUNT(*) FROM cities")
        if existing == 0:
            for i, (ck, cfg) in enumerate(_CITIES_DEFAULTS.items()):
                admin_ids_str = ",".join(str(a) for a in cfg["admins"])
                await conn.execute("""
                    INSERT INTO cities (city_key, name, stock_pool, admin_ids, is_default)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (city_key) DO NOTHING
                """, ck, cfg["name"], cfg["stock_pool"], admin_ids_str, i == 0)  # первый — дефолтный

        await load_cities_from_db(conn)

        # ================= USERS =================
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            language TEXT,
            total_spent REAL DEFAULT 0,
            level TEXT DEFAULT 'none',
            discount INTEGER DEFAULT 0,
            streak_weeks INTEGER DEFAULT 0,
            last_order_date DATE,
            max_streak_weeks INTEGER DEFAULT 0,
            total_saved REAL DEFAULT 0,
            referrals INTEGER DEFAULT 0,
            ref_earned REAL DEFAULT 0,
            spin_count INTEGER DEFAULT 0,
            spin_progress INTEGER DEFAULT 0,
            total_items INTEGER DEFAULT 0,
            total_orders INTEGER DEFAULT 0,
            saved_money REAL DEFAULT 0,
            current_discount REAL DEFAULT 0,
            free_jar_bonus INTEGER DEFAULT 0
        )
        """)

        # ========== КОРЗИНА ==========
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS cart (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            product_id INTEGER,
            quantity INTEGER DEFAULT 1,
            position INTEGER,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, product_id)
        )
        """)
        
        # ================= PRODUCTS =================
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY,
            name_ru TEXT,
            name_ua TEXT,
            name_de TEXT,
            desc_ru TEXT,
            desc_ua TEXT,
            desc_de TEXT,
            price REAL,
            image TEXT,
            in_stock INTEGER DEFAULT 1
        )
        """)

        # Раздел магазина (Elfliq / Elfworld). Существующие товары по
        # умолчанию относятся к Elfliq — текущему каталогу.
        await conn.execute("""
            ALTER TABLE products
            ADD COLUMN IF NOT EXISTS category TEXT DEFAULT 'elfliq'
        """)

        # ================= ORDERS =================
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            items TEXT,
            total REAL,
            payment TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        # ================= FAVORITES =================
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            product_id INTEGER,
            position INTEGER,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        # ================= ORDER ITEMS =================
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS order_items (
            id SERIAL PRIMARY KEY,
            order_id INTEGER,
            product_id INTEGER,
            quantity INTEGER
        )
        """)

        # ================= REFERRALS =================
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            referrer_id BIGINT,
            new_user_id BIGINT,
            activated INTEGER DEFAULT 0
        )
        """)

        # ================= MIGRATIONS =================
        # Добавляем поле для хранения message_id у админов (синхронизация)
        await conn.execute("""
            ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS admin_message_ids TEXT DEFAULT ''
        """)

        # Сумма скидки, применённой к конкретному заказу (нужна для подсчёта
        # "Всего сэкономлено" в момент подтверждения заказа админом)
        await conn.execute("""
            ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS discount REAL DEFAULT 0
        """)

        # Одноразовая скидка новому пользователю, пришедшему по реферальной
        # ссылке (1€ на первый заказ). Сбрасывается после первого подтверждённого заказа.
        await conn.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS ref_bonus REAL DEFAULT 0
        """)

        # Гарантируем, что один и тот же приглашённый не попадёт в таблицу
        # referrals дважды (защита от повторного начисления).
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS referrals_new_user_unique
            ON referrals (new_user_id)
        """)

        # Username нужен для /ban и /unban по @username (обновляется при /start).
        await conn.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS username TEXT
        """)

        # Блокировка пользователя администрацией.
        await conn.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS banned BOOLEAN DEFAULT false
        """)

        # ======= ГОРОДА =======
        # city — выбранный город пользователя (city_key из CITIES, или NULL если не выбран).
        # delivery — специальное значение: пользователь выбрал доставку (не самовывоз).
        await conn.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS city TEXT DEFAULT NULL
        """)

        # Отдельные пулы наличия по городам.
        # city_key = ключ из CITIES; product_id = id товара из products.
        # stock = остаток. Если для города stock_pool='default', используется products.stock.
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS city_stock (
            city_key TEXT NOT NULL,
            product_id INTEGER NOT NULL,
            stock INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (city_key, product_id)
        )
        """)

        # Добавляем city_key в orders для маршрутизации
        await conn.execute("""
            ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS city_key TEXT DEFAULT NULL
        """)
        await conn.execute("""
            ALTER TABLE gift_requests
            ADD COLUMN IF NOT EXISTS city_key TEXT DEFAULT NULL
        """)

        # ======= ДОСТАВКА =======
        # Текущий режим UI (delivery_mode=true → пользователь видит магазин
        # в режиме доставки). cart_mode хранит режим ПЕРВОГО добавленного в
        # корзину товара: 'pickup' или 'delivery'. Если cart_mode не совпадает
        # с текущим delivery_mode при добавлении нового товара — блокируем.
        await conn.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS delivery_mode BOOLEAN DEFAULT false
        """)
        await conn.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS cart_mode TEXT DEFAULT 'pickup'
        """)

        # Сохранённые данные доставки пользователя (заполняются один раз,
        # затем используются повторно при каждом следующем заказе с доставкой).
        await conn.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS delivery_name TEXT
        """)
        await conn.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS delivery_phone TEXT
        """)
        await conn.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS delivery_address TEXT
        """)
        await conn.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS delivery_tracking BOOLEAN
        """)
        await conn.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS delivery_saved BOOLEAN DEFAULT false
        """)

        # Флаг и данные доставки непосредственно на заказе.
        await conn.execute("""
            ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS is_delivery BOOLEAN DEFAULT false
        """)
        # На старых деплоях колонка могла быть создана как INTEGER — приводим к BOOLEAN.
        await conn.execute("""
            ALTER TABLE orders ALTER COLUMN is_delivery DROP DEFAULT
        """)
        await conn.execute("""
            ALTER TABLE orders
            ALTER COLUMN is_delivery TYPE BOOLEAN USING is_delivery::boolean
        """)
        await conn.execute("""
            ALTER TABLE orders ALTER COLUMN is_delivery SET DEFAULT false
        """)
        await conn.execute("""
            ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS delivery_name TEXT
        """)
        await conn.execute("""
            ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS delivery_phone TEXT
        """)
        await conn.execute("""
            ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS delivery_address TEXT
        """)
        await conn.execute("""
            ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS delivery_tracking BOOLEAN
        """)



        # Глобальная заморозка Buy Streak: каждая строка — один период
        # заморозки. ended_at IS NULL означает, что заморозка активна сейчас.
        # Заморозка действует на всех пользователей сразу, без отдельного
        # состояния на каждого.
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS streak_freezes (
            id SERIAL PRIMARY KEY,
            city_key TEXT DEFAULT NULL,
            started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            ended_at TIMESTAMP
        )
        """)
        await conn.execute("""
            ALTER TABLE streak_freezes
            ADD COLUMN IF NOT EXISTS city_key TEXT DEFAULT NULL
        """)

        # Таблица заявок на бесплатные банки (для синхронизации между админами)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS gift_requests (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            product_id INTEGER,
            username TEXT,
            status TEXT DEFAULT 'pending',
            admin_message_ids TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        # ========== УПРАВЛЕНИЕ ОСТАТКАМИ ==========
        # stock — фактическое количество товара в наличии
        await conn.execute("""
            ALTER TABLE products
            ADD COLUMN IF NOT EXISTS stock INTEGER DEFAULT 0
        """)
        # Временный резерв: при оформлении заказа уменьшаем stock,
        # при подтверждении — окончательно; при отмене — возвращаем
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS reserved_stock (
            order_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            city_key TEXT DEFAULT NULL,
            PRIMARY KEY (order_id, product_id)
        )
        """)
        await conn.execute("""
            ALTER TABLE reserved_stock
            ADD COLUMN IF NOT EXISTS city_key TEXT DEFAULT NULL
        """)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS promocodes (
            code TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            discount REAL DEFAULT 0,
            used BOOLEAN DEFAULT false,
            used_by BIGINT DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        # Скидка от промокода, хранится у пользователя пока не применена
        await conn.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS promo_discount REAL DEFAULT 0
        """)
        # Код промокода, который активировал пользователь (для отображения)
        await conn.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS promo_code TEXT DEFAULT NULL
        """)
        # Тип промокода: 'discount' или 'free_jar'
        await conn.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS promo_type TEXT DEFAULT NULL
        """)
        
        count = await conn.fetchval("SELECT COUNT(*) FROM products WHERE category='elfliq'")

        if count == 0:
            await conn.executemany("""
                INSERT INTO products 
                (name_ru,name_ua,name_de,desc_ru,desc_ua,desc_de,price,image,in_stock,category)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'elfliq')
            """, products)

        # Заготовка раздела Elfworld: структура полностью готова, контент
        # (названия/описания/фото) добавляется позже. По умолчанию товары
        # не в наличии, чтобы пустые карточки не показывались покупателям
        # раньше времени — админ включит их через /unstock после заполнения.
        # Upsert по name_ru: обновляет старые заглушки и добавляет новые вкусы.
        # in_stock не перезаписывается — чтобы не сбросить статус включённых товаров.
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS products_elfworld_name_ru_unique
            ON products (name_ru, category)
            WHERE category = 'elfworld'
        """)
        for row in products_elfworld:
            await conn.execute("""
                INSERT INTO products
                    (name_ru,name_ua,name_de,desc_ru,desc_ua,desc_de,price,image,in_stock,category)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'elfworld')
                ON CONFLICT (name_ru, category) WHERE category = 'elfworld' DO UPDATE SET
                    name_ua = EXCLUDED.name_ua,
                    name_de = EXCLUDED.name_de,
                    desc_ru = EXCLUDED.desc_ru,
                    desc_ua = EXCLUDED.desc_ua,
                    desc_de = EXCLUDED.desc_de,
                    price   = EXCLUDED.price,
                    image   = CASE WHEN EXCLUDED.image <> '' THEN EXCLUDED.image
                                   ELSE products.image END
            """, *row)

        # position — порядковый номер товара внутри категории (1, 2, 3...).
        # Позволяет использовать /addstock elfworld 1 вместо реального id из БД.
        # Перезаписывается при каждом старте, чтобы учитывать новые товары.
        await conn.execute("""
            ALTER TABLE products
            ADD COLUMN IF NOT EXISTS position INTEGER DEFAULT 0
        """)
        await conn.execute("""
            UPDATE products p
            SET position = sub.rn
            FROM (
                SELECT id, ROW_NUMBER() OVER (PARTITION BY category ORDER BY id) AS rn
                FROM products
            ) sub
            WHERE p.id = sub.id
        """)

        # ========== ИНДЕКСЫ ==========
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_cart_user_id ON cart(user_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders(user_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_city_key ON orders(city_key)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_favorites_user_id ON favorites(user_id)")
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_streak_freezes_active
            ON streak_freezes(city_key) WHERE ended_at IS NULL
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")

# ========== ТОВАРЫ ==========

products = [
    ("Watermelon Cherry","Watermelon Cherry","Watermelon Cherry",
"Сочный спелый арбуз переплетается с насыщенной сладкой вишней, создавая яркий, освежающий вкус с лёгкой кислинкой и приятным послевкусием.",
"Соковитий стиглий кавун поєднується з насиченою солодкою вишнею, створюючи яскравий освіжаючий смак з легкою кислинкою та приємним післясмаком.",
"Saftige reife Wassermelone kombiniert mit intensiver süßer Kirsche – ein frischer Geschmack mit leichter Säure und angenehmem Nachgeschmack.",
15,"AgACAgIAAxkBAAIFnWnOpcYhB8LLjMYEv0fgiIQdd29SAAKGGWsbjEh5ShBKAgrhlRemAQADAgADeQADOgQ",1),

("Strawberry Cherry Lemon","Strawberry Cherry Lemon","Strawberry Cherry Lemon",
"Гармоничное сочетание сладкой клубники, глубокой вишни и освежающего лимона — идеальный баланс сладости и цитрусовой свежести.",
"Гармонійне поєднання солодкої полуниці, глибокої вишні та освіжаючого лимона — ідеальний баланс солодкості й цитрусової свіжості.",
"Die perfekte Mischung aus süßer Erdbeere, tiefer Kirsche und erfrischender Zitrone.",
15,"AgACAgIAAxkBAAIF3WnOq_MqSklljPyDvzkoUsPexBLBAALVE2sb4PR4Sp3by9C9aUX9AQADAgADeAADOgQ",1),

("Strawberry Banana","Strawberry Banana","Strawberry Banana",
"Нежная клубника в паре с мягким сливочным бананом создаёт тёплый, сладкий и очень приятный вкус без резких нот.",
"Ніжна полуниця в парі з м’яким вершковим бананом створює теплий, солодкий і дуже приємний смак без різких нот.",
"Zarte Erdbeere mit cremiger Banane – weich, süß und sehr angenehm.",
15,"AgACAgIAAxkBAAIF32nOrBQcfdNc6oKFd7HTAbVlx2JUAAIJFGsb4PR4SvsCnMIgUe1lAQADAgADeAADOgQ",1),

("Green Grape Rose","Green Grape Rose","Green Grape Rose",
"Свежий зелёный виноград с утончёнными цветочными оттенками розы — лёгкий, ароматный и необычный вкус.",
"Свіжий зелений виноград з витонченими квітковими нотами троянди — легкий, ароматний і незвичайний смак.",
"Frische grüne Trauben mit feinen Rosennoten – leicht, aromatisch und ungewöhnlich.",
15,"AgACAgIAAxkBAAIF4WnOrCwee8WnzSA3t-HM4cWDUVltAALkE2sb4PR4Sn-ci7Qc2OyQAQADAgADeAADOgQ",1),

("Cherry Lemon Peach","Cherry Lemon Peach","Cherry Lemon Peach",
"Сочная вишня, спелый персик и лёгкая кислинка лимона — насыщенный фруктовый микс с освежающим холодком на выдохе.",
"Соковита вишня, стиглий персик і легка кислинка лимона — насичений фруктовий мікс з освіжаючим холодком на видиху.",
"Saftige Kirsche, reifer Pfirsich und ein Hauch Zitrone – eine reichhaltige Fruchtmischung mit erfrischender Kühle beim Ausatmen.",
15,"AgACAgIAAxkBAAIF42nOrDxyOFSJPsi8I7V8xRzlzrPEAALnE2sb4PR4Su5RZB3sDe0pAQADAgADeAADOgQ",1),

("Blueberry Raspberry Pomegranate","Blueberry Raspberry Pomegranate","Blueberry Raspberry Pomegranate",
"Глубокий ягодный вкус черники и малины дополняется терпкими нотами граната, создавая насыщенный и многослойный профиль.",
"Глибокий ягідний смак чорниці та малини доповнюється терпкими нотами граната, створюючи насичений багатошаровий профіль.",
"Heidelbeere und Himbeere mit Granatapfel – intensiv, fruchtig und vielschichtig.",
15,"AgACAgIAAxkBAAIF5WnOrEhXUufNUWwxc7by1ToyV6nVAAIRFGsb4PR4SkzCVTpOhcNhAQADAgADeQADOgQ",1),

("Blue Razz Ice","Blue Razz Ice","Blue Razz Ice",
"Сладкая голубая малина с холодящим эффектом — яркий и освежающий вкус, который отлично заходит на каждый день.",
"Солодка блакитна малина з холодним ефектом — яскравий і освіжаючий смак на кожен день.",
"Süße blaue Himbeere mit kühlendem Effekt – frisch und perfekt für jeden Tag.",
15,"AgACAgIAAxkBAAIF52nOrFYfcruJ7JNPSm23hqSJVPgLAAL3E2sb4PR4SuVWSJOyQyBmAQADAgADeAADOgQ",1),

("Sour Watermelon Gummy","Sour Watermelon Gummy","Sour Watermelon Gummy",
"Кисло-сладкий вкус арбузного мармелада — насыщенный и очень запоминающийся.",
"Кисло-солодкий смак кавунового мармеладу — насичений і дуже запам’ятовується.",
"Der süß-saure Geschmack von Haribo mit Wassermelone ist reichhaltig und sehr einprägsam.",
15,"AgACAgIAAxkBAAIF6WnOrGmqc_j9iBE-r6nc2zJw2-V6AAITFGsb4PR4SlzH-zQvTnHMAQADAgADeAADOgQ",1),

("Raspberry Lychee","Raspberry Lychee","Raspberry Lychee",
"Сочная малина в сочетании с экзотическим личи — сладкий, лёгкий и слегка тропический вкус.",
"Соковита малина у поєднанні з екзотичним лічі — солодкий, легкий і трохи тропічний смак.",
"Himbeere mit exotischer Litschi – süß, leicht und tropisch.",
15,"AgACAgIAAxkBAAIF62nOrHQjczrNpymyde_65cvV6BgQAAIVFGsb4PR4Shob8rLaP-XLAQADAgADeAADOgQ",1),

("Pineapple Ice","Pineapple Ice","Pineapple Ice",
"Спелый ананас с освежающим холодком — яркий тропический вкус с прохладным эффектом.",
"Стиглий ананас з освіжаючим холодком — яскравий тропічний смак з прохолодним ефектом.",
"Reife Ananas mit kühlendem Effekt – tropisch und erfrischend.",
15,"AgACAgIAAxkBAAIF7WnOrH7VIkZJimOhIrJbEzIFmu_dAAIWFGsb4PR4Spuib2VWpEtXAQADAgADeAADOgQ",1),

("Pineapple Colada","Pineapple Colada","Pineapple Colada",
"Ананас с мягкими кокосовыми нотами — вкус классического тропического коктейля.",
"Ананас з м’якими кокосовими нотами — смак класичного тропічного коктейлю.",
"Ananas mit Kokos – klassischer tropischer Cocktailgeschmack.",
15,"AgACAgIAAxkBAAIF72nOrIvKlhazUR-wwrXW0yHBreICAAIXFGsb4PR4Sil1O-fz37cuAQADAgADeAADOgQ",1),

("Mango Peach","Mango Peach","Mango Peach",
"Сладкий манго и сочный персик создают насыщенный фруктовый дуэт с мягким и приятным послевкусием.",
"Солодке манго і соковитий персик створюють насичений фруктовий дует з м’яким післясмаком.",
"Mango und Pfirsich – süß, saftig und mit angenehmem Nachgeschmack.",
15,"AgACAgIAAxkBAAIF8WnOrJWNiq-6jXyqxXjFQDjYQF9lAAIYFGsb4PR4StGad0LIOGxIAQADAgADeQADOgQ",1),

("Lemon Lime","Lemon Lime","Lemon Lime",
"Мощный цитрусовый микс лимона и лайма — яркий, кислый и максимально освежающий вкус.",
"Потужний цитрусовий мікс лимона та лайма — яскравий, кислий і максимально освіжаючий смак.",
"Zitrone und Limette – intensiv, sauer und extrem erfrischend.",
15,"AgACAgIAAxkBAAIF82nOrJ-cfUHnBQwQX7UyAAEDsI4WvAACHBRrG-D0eEq1fNyKEx4PlwEAAwIAA3kAAzoE",1),

("Jasmine Raspberry","Jasmine Raspberry","Jasmine Raspberry",
"Нежная малина с лёгким ароматом жасмина — мягкий, цветочный и необычный вкус.",
"Ніжна малина з легким ароматом жасмину — м’який, квітковий і незвичайний смак.",
"Himbeere mit Jasmin – sanft, blumig und außergewöhnlich.",
15,"AgACAgIAAxkBAAIF9WnOrKpg1rVOUEJhsPHHj8iMVgHCAAIdFGsb4PR4Sth0Ulz6WAEeAQADAgADeAADOgQ",1),

("Grape Cherry","Grape Cherry","Grape Cherry",
"Сладкий виноград и насыщенная вишня — классическое фруктовое сочетание с глубоким вкусом.",
"Солодкий виноград і насичена вишня — класичне фруктове поєднання з глибоким смаком.",
"Traube und Kirsche – klassisch, süß und vollmundig.",
15,"AgACAgIAAxkBAAIF92nOrLWgurOuc_kPQcgdcBDiComTAAIfFGsb4PR4StcMpAc7jGlaAQADAgADeAADOgQ",1),

("Double Apple","Double Apple","Double Apple",
"Два вида яблока создают насыщенный, слегка прохладный и очень узнаваемый классический вкус.",
"Два види яблука створюють насичений, трохи прохолодний класичний смак.",
"Zwei Apfelsorten – klassisch, intensiv und leicht kühl.",
15,"AgACAgIAAxkBAAIF-WnOrMcXM-nibxCoIxbsjEQjQlLQAAIgFGsb4PR4ShybmR_j50sIAQADAgADeAADOgQ",1),

("Blueberry Rose Mint","Blueberry Rose Mint","Blueberry Rose Mint",
"Черника с нотами розы и лёгкой мятной свежестью — сложный и освежающий аромат.",
"Чорниця з нотами троянди та легкою м’ятною свіжістю — складний і освіжаючий аромат.",
"Heidelbeere mit Rose und Minze – komplex und erfrischend.",
15,"AgACAgIAAxkBAAIF-2nOrNMcTfcGrP_w8NCYzp2RL4-XAAIhFGsb4PR4SlfTJjtrD2XqAQADAgADdwADOgQ",1),

("Apple Pear","Apple Pear","Apple Pear",
"Сочное яблоко и сладкая груша — мягкий, натуральный и очень приятный вкус.",
"Соковите яблуко та солодка груша — м’який, натуральний і дуже приємний смак.",
"Apfel und Birne – weich, natürlich und sehr angenehm.",
15,"AgACAgIAAxkBAAIF_WnOrN42fxpqt1KCps8xtAyUlp7AAAIiFGsb4PR4SuIJdiNl-D4cAQADAgADeAADOgQ",1),

("Cherry Cola","Cherry Cola","Cherry Cola",
"Классическая кола с вишнёвой ноткой — сладкий, слегка газированный вкус с приятной глубиной.",
"Класична кола з вишневою ноткою — солодкий, злегка газований смак.",
"Cola mit Kirsche – süß, spritzig und intensiv.",
15,"AgACAgIAAxkBAAIF_2nOrOyI9NUWIuzXxn1m3T1PSEfWAAIjFGsb4PR4Sp14RJFQ7zo6AQADAgADeAADOgQ",1),

("Pink Lemonade","Pink Lemonade","Pink Lemonade",
"Освежающий розовый лимонад — идеальный баланс сладости и кислинки с лёгким летним настроением.",
"Освіжаючий рожевий лимонад — ідеальний баланс солодкого і кислого.",
"Pink Lemonade – perfekte Balance aus süß und sauer mit sommerlichem Gefühl.",
15,"AgACAgIAAxkBAAIGAWnOrPWAFiKrCXUlDsfjTUmG4FeEAAInFGsb4PR4SpGa5bcwZXbHAQADAgADeAADOgQ",1),
]

# ========== ТОВАРЫ: РАЗДЕЛ ELFWORLD (заготовка) ==========
# Структура полностью идентична Elfliq — name_ru/ua/de, desc_ru/ua/de,
# price, image (file_id фото, можно оставить пустой строкой пока фото нет),
# in_stock. Контент (названия, описания, фото) добавляется позже:
# просто впиши реальные значения в каждую строку ниже и перезапусти бота —
# при первом запуске после очистки можно просто отредактировать эти строки.
# in_stock=0 по умолчанию, чтобы пустые карточки без описания/фото не
# показывались покупателям раньше времени — включи через /stock Elfworld all,
# когда наполнишь раздел реальным контентом.

products_elfworld = [
    ("Love 77","Love 77","Love 77",
"Тропический ягодный вкус с сочной сладостью и лёгкой кислинкой, напоминающий экзотические фрукты и спелые лесные ягоды.",
"Тропічно-ягідний смак із соковитою солодкістю та легкою кислинкою, що нагадує екзотичні фрукти й стиглі лісові ягоди.",
"Ein tropischer und beeriger Geschmack mit saftiger Süße und leichter Säure, der an exotische Früchte und reife Waldbeeren erinnert.",
15,"AgACAgIAAxkBAAPZakP4UUHOaXrdZO4FBZUtwJKKyLcAAo0daxtgoSFKkpxqm4BN7ncBAAMCAAN5AAM8BA",1),

    ("Mint Mojito","Mint Mojito","Mint Mojito",
"Классический мохито в парах: ледяная мята, яркий лайм и лёгкая сладость тростникового сахара — освежающий коктейль на каждый день.",
"Класичний мохіто: крижана м'ята, яскравий лайм і легка солодкість тростинного цукру — освіжаючий коктейль на кожен день.",
"Eisige Minze, spritzige Limette und ein Hauch Rohrzucker – der klassische Mojito-Geschmack, der sofort erfrischt.",
15,"AgACAgIAAxkBAAPwakP8gDq-xVJN0bFFDpD9YcazzjwAApYdaxtgoSFKu6u4P0dZOFQBAAMCAAN5AAM8BA",1),

    ("Red Bull","Red Bull","Red Bull",
"Культовый энергетический вкус с кисло-сладкими цитрусовыми нотами и лёгкой газировкой на послевкусии — заряжает с первой затяжки.",
"Культовий енергетичний смак з кисло-солодкими цитрусовими нотами та легкою газованістю на завершенні — заряджає з першої затяжки.",
"Der ikonische Energy-Drink-Geschmack – süß-sauer, leicht spritzig und belebend vom ersten Zug an.",
15,"AgACAgIAAxkBAAPbakP4VVIPy8n9Zt9s6YJidX-vdY4AAo4daxtgoSFKyI0VLqw0d4YBAAMCAAN5AAM8BA",1),

    ("Raspberry Cherry","Raspberry Cherry","Raspberry Cherry",
"Сочная малина встречает насыщенную тёмную вишню — яркое ягодное сочетание с лёгкой кислинкой и сладким финалом.",
"Соковита малина зустрічає насичену темну вишню — яскраве ягідне поєднання з легкою кислинкою та солодким фіналом.",
"Saftige Himbeere trifft dunkle Kirsche – lebhaft, beerig und mit süßem Abgang.",
15,"AgACAgIAAxkBAAPmakP8b4fh2oi3Va5HIZ7NuFHuiNcAApEdaxtgoSFKYemL4dILuZoBAAMCAAN5AAM8BA",1),

    ("Cherry","Cherry","Cherry",
"Чистый, выразительный вкус спелой вишни без лишних примесей — глубокий, насыщенный и узнаваемый с первой секунды.",
"Чистий, виразний смак стиглої вишні без зайвих домішок — глибокий, насичений і впізнаваний з першої секунди.",
"Reife, dunkle Kirsche pur – tief, intensiv und sofort erkennbar.",
15,"AgACAgIAAxkBAAPsakP8eg79wzkpzaVTS0FiYX_2Y7EAApQdaxtgoSFKiSdh7EarHIABAAMCAAN5AAM8BA",1),

    ("Blueberry Watermelon","Blueberry Watermelon","Blueberry Watermelon",
"Сладкая черника и освежающий сочный арбуз — летняя пара, которая дарит лёгкость и яркость вкуса в одном флаконе.",
"Солодка чорниця та освіжаючий соковитий кавун — літня пара, що дарує легкість і яскравість смаку в одному флаконі.",
"Süße Blaubeere und saftige Wassermelone – eine sommerliche Kombination aus Frische und Frucht.",
15,"AgACAgIAAxkBAAPoakP8c3OfWRreyKaGk8WDDK5fregAApIdaxtgoSFKwfX1ASnPKNkBAAMCAAN5AAM8BA",1),

    ("Mint Chill","Mint Chill","Mint Chill",
"Максимально холодная, чистая мята без примесей — для тех, кто ценит экстремальную свежесть и полный кулинг-эффект на каждом выдохе.",
"Максимально холодна, чиста м'ята без домішок — для тих, хто цінує екстремальну свіжість та повний кулінг-ефект.",
"Maximale Kühle, pure Minze ohne Ablenkung – für alle, die ein intensives Cooling-Erlebnis suchen.",
15,"AgACAgIAAxkBAAPqakP8d3vkNUKEHLlg2Sd67ApuPj4AApMdaxtgoSFK-4QgpFuZmQgBAAMCAAN5AAM8BA",1),

    ("Strawberry Kiwi","Strawberry Kiwi","Strawberry Kiwi",
"Сладкая спелая клубника и кисловатый тропический киви — живой, освежающий дуэт с характером и приятным балансом.",
"Солодка стигла полуниця та кислуватий тропічний ківі — живий, освіжаючий дует з характером і приємним балансом.",
"Süße Erdbeere und säuerliche Kiwi – ein lebhaftes, tropisches Duo mit perfekter Balance.",
15,"AgACAgIAAxkBAAPkakP8a3fRGLqxC4lHrMu0wwelMjIAApAdaxtgoSFK2j2zm-QfpNcBAAMCAAN5AAM8BA",1),

    ("Raspberry Lychee","Raspberry Lychee","Raspberry Lychee",
"Сочная малина в сочетании с экзотическим личи — сладкий, лёгкий и слегка тропический вкус с цветочным послевкусием.",
"Соковита малина у поєднанні з екзотичним лічі — солодкий, легкий і трохи тропічний смак з квітковим післясмаком.",
"Himbeere mit exotischer Litschi – süß, leicht und mit einem blumig-tropischen Nachgeschmack.",
15,"AgACAgIAAxkBAAPdakP4WSp_4YB3MB6WIRtTAti8pYYAAo8daxtgoSFKDVk2YgnOXccBAAMCAAN5AAM8BA",1),

    ("Skittles Candy","Skittles Candy","Skittles Candy",
"Взрыв радужных фруктовых конфет прямо во рту — сладкий, яркий и безумно вкусный вкус любимых скиттлов в каждой затяжке.",
"Вибух веселкових фруктових цукерок прямо в роті — солодкий, яскравий і шалено смачний смак улюблених скітлс у кожній затяжці.",
"Regenbogen-Fruchtbonbons pur – süß, bunt und genau so wie die Originalsweets.",
15,"AgACAgIAAxkBAAPuakP8fRZ3kA1u9qI30vN75PCQTHYAApUdaxtgoSFKx_cKNnpc1DwBAAMCAAN5AAM8BA",1),
]


# ========== ЛОКАЛИЗАЦИЯ ==========

TEXTS = {
    "ru": {
        "menu": "📱 Меню",
        "shop": "🛒 Магазин",
        "cart": "🧺 Корзина",
        "language": "🌍 Язык",
        "empty_cart": "Корзина пуста",
        "choose_lang": "Выбери язык",
        "choose_product": "🛒 Выбери товар:",
        "choose_section": "🚐 Выберите раздел",
        "banned_message": "🚫 Ваш аккаунт был заблокирован администрацией.",
        "delivery_mode_on": "🚚 Режим доставки",
        "delivery_mode_off": "🏪 Режим самовывоза",
        "delivery_mode_toggle_on": "✅ Включён режим доставки",
        "delivery_mode_toggle_off": "✅ Включён режим самовывоза",
        "delivery_cart_conflict": "🚚 Очисти корзину, чтобы переключить режим — в ней уже есть товары в другом режиме.",
        "free_delivery_hint": "📦 При заказе от 3 банок доставка бесплатная.",
        "delivery_step_name": "📝 Введи имя и фамилию (как в паспорте):",
        "delivery_step_phone": "📞 Введи номер телефона или нажми кнопку ниже:",
        "delivery_step_phone_btn": "📱 Отправить мой номер",
        "delivery_step_address": "📍 Введи адрес в формате:\n\n<b>Bundesland. Stadt. Straße</b>",
        "delivery_step_tracking": "📦 Выберите вариант доставки\n\n✅ С трек-номером — 7.20€\n\n❌ Без трек-номера — 5.20€",
        "delivery_tracking_yes_btn": "✅ С трек-номером",
        "delivery_tracking_no_btn": "❌ Без трек-номера",
        "gift_delivery_free_hint": "🎁 Для бесплатной банки доставка также полностью бесплатна.",
        "gift_pickup_label": "📍 Самовывоз",
        "gift_delivery_label": "📦 Доставка",
        "delivery_refill_profile_btn": "🔄 Заполнить заново",
        "delivery_tracking_yes": "✅ С трек-номером — 6.20€",
        "delivery_tracking_no": "❌ Без трек-номера — 4.20€",
        "delivery_confirm_title": "📦 Проверьте данные доставки",
        "delivery_field_name": "Имя:",
        "delivery_field_phone": "Телефон:",
        "delivery_field_address": "Адрес:",
        "delivery_field_tracking": "Трек-номер:",
        "delivery_tracking_yes_label": "✅ Да",
        "delivery_tracking_no_label": "❌ Нет",
        "delivery_confirm_btn": "✅ Подтвердить",
        "delivery_refill_btn": "🔄 Заполнить заново",
        "delivery_address_profile_btn": "📦 Адрес доставки",
        "delivery_no_cash": "🚚 Для доставки оплата наличными недоступна.",
        "section_elfliq": "🧪 ELFLIQ",
        "section_elfworld": "🌍 ELFWORLD",
        "section_empty": "Раздел временно пуст",
        "switch_to_elfworld": "🌍 Перейти в ELFWORLD",
        "switch_to_elfliq": "🧪 Перейти в ELFLIQ",
        "total": "Итого",
        "added": "Добавлено",
        "clear": "🗑 Очистить",
        "remove": "↩ Убрать последнее",
        "back_shop": "🛒 Вернуться в магазин",
        "pay": "💳 Оплата",
        "cash": "💵 Наличные",
        "cancel": "❌ Отмена",
        "order_done": "Заказ оформлен. Админ скоро свяжется",
        "no_username_warning": "⚠️ У вас отсутствует Telegram username.\n\nИз-за этого админ не сможет первым написать вам, если потребуется уточнить детали заказа.\n\nПожалуйста, в будущем добавьте username в настройках Telegram — это значительно упростит связь по вашим заказам.",
        "contact_admin_btn": "💬 Написать администратору",
        "confirm_order": "Подтвердить заказ?",
        "confirm": "✅ Подтвердить",
        "paid": "✅ Оплачено",
        "checking_payment": "⏳ Заказ оформлен",
        "fav_added": "Добавлено в избранное",
        "fav_removed": "Убрано из избранного",
        "favorites": "❤️ Избранное",
        "no_favorites": "Нет избранных товаров",
        "fav_removed": "❌ Удалено из избранного",
        "fav_restore": "❤️ Вернуть в избранное",
        "fav_hint": "Нажми на товар чтобы убрать из избранного",
        "back": "⬅️ Назад",
        "profile": "👤 Профиль",
        "profile_title": "👤 Профиль",
        "profile_info": "Твой профиль",
        "history": "📜 История",
        "levels": "🏆 Ранги",
        "roulette": "🎰 Рулетка",
        "streak": "🔥 Стрик",
        "discounts": "💸 Скидки",
        "ref": "🎁 Рефералка",
        "stats": "📊 Статистика",
        "to_shop": "🛒 В магазин",
        "rank": "🏆 Ранг",
        "next_rank": "До следующего ранга:",
        "progress": "Прогресс",
        "to_shop": "🛒 В магазин",
        "new_rank": "🎉 У тебя новый ранг: {rank}!",
        "confirm_admin": "✅ Подтвердить",
        "cancel_admin": "❌ Отменить",
        "savings": "💸 Сэкономлено: {value}€",
        "profile_items": "Банок куплено",
        "profile_orders": "Заказов",
        "profile_saved": "Сэкономлено",
        "profile_discount": "Скидка",
        "history_empty": "История заказов пуста",
        "open_order": "Открыть заказ",
        "order": "Заказ",
        "repeat_order": "🔁 Повторить заказ",
        "discount_all_items": "Скидка действует на все товары",
        "from_jars": "от",
        "roulette_prizes": "🎁 Возможные призы:",
        "free_jar": "🎉 Бесплатная банка",
        "roulette_ready": "✅ Доступен прокрут!",
        "roulette_left": "📦 До следующего прокрута",
        "spin_now": "🎰 Крутить",
        "spin_spinning": "Рулетка крутится",
        "spin_locked": "❌ Пока недоступно",
        "spin_win_free": "🎉 Ты выиграл бесплатную банку!",
        "spin_win_discount": "🎉 Ты выиграл скидку -{value}€ на следующий заказ!",
        "spin_ready_notify": "🎰 Тебе доступен прокрут в колесе удачи!",
        "spin_open": "🎰 Открыть рулетку",
        "active_bonus": "🎁 Активный бонус",
        "wheel_next_order_note": "💸 Скидка применяется ко всему следующему заказу",
        "free_jar_active": "🎁 У тебя есть бесплатная банка",
        "claim_gift": "🎁 Забрать подарок",
        "choose_gift": "🎁 Выбери подарок",
        "gift_already_used": "❌ Подарок уже был получен",
        "select_gift_btn": "🎁 Выбрать подарок",
        "gift_confirm_title": "🎁 Выбран подарок",
        "gift_confirm_question": "Точно выбрать этот товар?",
        "gift_done": "🎁 Подарок оформлен\n\nАдминистратор скоро свяжется с тобой.",
        "gift_cancel": "❌ Отмена",
        "spin_bonus_exists": "У тебя уже есть непотраченный бонус",
        "spin_free_jar_exists": "У тебя уже есть бесплатная банка",
        "streak_current_weeks": "🔥 Текущий стрик: {weeks} недель",
        "streak_discount_value": "💸 Текущая скидка: -{value}€",
        "streak_discount_none": "💸 Текущая скидка: нет",
        "streak_days_left": "⏳ До сброса стрика: {days} дней",
        "streak_inactive_hint": "Сделай заказ, чтобы начать стрик 🔥",
        "streak_max_weeks": "🏆 Максимальный стрик: {weeks} недель",
        "streak_frozen_banner": "❄️ Время стрика временно заморожено из-за задержек магазина.",
        "streak_frozen_footer_days": "❄️ Стрик заморожен (сохранено {days} дн.)",
        "streak_rewards_title": "🎁 Награды за стрик:",
        "streak_week_row": "{weeks} нед. — скидка {value}€",
        "streak_progress_bar_label": "📅 Прогресс до следующего уровня:",
        "history_title": "📜 История заказов",
        "history_order_row": "🧾 Заказ #{id}  |  {date}  |  {total}€",
        "ref_title": "🎁 Реферальная система",
        "ref_your_link": "🔗 Твоя реферальная ссылка:",
        "ref_earned": "💰 Заработано на рефералах: {value}€",
        "stats_title": "📊 Статистика",
        "stats_total_saved": "💸 Всего сэкономлено: {value}€",
        "profile_title_header": "👤 Профиль",
        "profile_rank_row": "🏆 Ранг: {rank}",
        "profile_discount_row": "💸 Скидка с ранга: -{value}€",
        "discounts_title": "💸 Скидки",
        "discounts_total_header": "💰 Общая скидка: {value}€",
        "discount_label_rank": "🏆 Скидка ранга",
        "discount_label_streak": "🔥 Buy Streak",
        "discount_label_ref": "🎁 Реферальная скидка",
        "discount_label_wheel": "🎰 Скидка от рулетки",
        "discounts_total_saved": "💸 Всего сэкономлено: {value}€",
        "discount_label_ref_bonus": "🎉 Скидка новичка",
        "ref_rules": "🎁 Правила:\n• Ты получаешь скидку {inviter}€ после первого заказа приглашённого друга\n• Твой друг получает скидку {new_user}€ на первый заказ",
        "ref_invited_count": "👥 Приглашено пользователей: {count}",
        "ref_share_button": "📤 Поделиться ссылкой",
        "ref_share_text": "Заходи в наш магазин жидкостей по моей ссылке и получи скидку на первый заказ! 🎁",
        "ref_credited_notify": "🎉 Твой друг сделал первый заказ! Тебе начислена реферальная скидка.",
        "stats_total_orders": "🧾 Всего заказов: {count}",
        "stats_first_order": "📅 Дата первого заказа: {date}",
        "stats_rank": "🏆 Текущий ранг: {rank}",
        "stats_top_product": "⭐ Любимый товар: {name} (куплен {count} раз)",
        "stats_top_product_none": "⭐ Любимый товар: пока нет заказов",
        "stats_max_streak": "🔥 Максимальный Buy Streak: {weeks} недель",
        "stats_spins": "🎰 Прокручено колёс: {count}",
        "stats_invited": "👥 Приглашено пользователей: {count}",
        "stats_total_spent": "💰 Всего потрачено: {value}€",
        "stock_out": "❌ К сожалению, товара {name} больше нет в наличии.\n\nУдалите его из корзины или выберите другой вкус.",
        "stock_low": "❌ К сожалению, товара {name} осталось всего {qty} шт.",
        "stock_low_cart": "❌ К сожалению, товара {name} осталось всего {qty} шт.\n\nПожалуйста, уменьшите количество или выберите другой вкус.",
        "stock_changed_retry": "❌ Наличие изменилось пока вы оформляли заказ. Пожалуйста, проверьте корзину и попробуйте снова.",
        "addstock_usage": "Использование:\n/addstock <elfliq|elfworld> <all|N[,N,...]> <кол-во>",
        "removestock_usage": "Использование:\n/removestock <elfliq|elfworld> <all|N[,N,...]> <кол-во>",
        "setstock_usage": "Использование:\n/setstock <elfliq|elfworld> <N[,N,...]> <кол-во>",
        "stock_updated": "✅ Обновлено {count} товар(ов)",
        "promo_activated_discount": "🎉 Промокод активирован!\n\nСкидка -{value}€ добавлена к твоему заказу.",
        "promo_activated_free_jar": "🎉 Промокод активирован!\n\nТебе начислена бесплатная банка — выбери её в магазине.",
        "promo_not_found": "❌ Промокод не найден или уже использован.",
        "promo_already_have": "⚠️ У тебя уже есть активный промокод.",
        "promo_usage": "Использование: /promo КОД",
        "promo_in_shop_label": "🎉 У тебя активирован промокод на {value}€",
        "promo_in_profile_discount": "🎟 Промокод: {code} (-{value}€)",
        "promo_in_profile_free_jar": "🎟 Промокод: {code} (бесплатная банка)",
        "shop_btn": "🛒 Магазин",
        "gift_shop_btn": "🎁 Получить бесплатную банку",
        "createpromo_usage": "Использование:\n/createpromo discount СУММА КОЛИЧЕСТВО\n/createpromo freejar КОЛИЧЕСТВО",
        "createpromo_done": "✅ Создано {count} промокодов:\n{codes}",
        "pay_usdt_btn": "💵 USDT (TRC20)",
        "pay_card_btn": "💳 Банковская карта",
        "pay_currency_title": "💳 Выбери валюту оплаты:",
        "pay_card_uah_btn": "🇺🇦 Оплата в гривне (UAH)",
        "pay_card_eur_btn": "💶 Оплата в евро (EUR)",
        "pay_usdt_screen": "💶 Сумма заказа:\n{eur}€\n\n💲 Курс:\n1 EUR = {rate} USDT\n\n💵 К оплате:\n{usdt} USDT (TRC20)\n\n📥 Адрес:\n`{wallet}`\n\nНажмите на адрес для копирования.",
        "pay_card_eur_screen": "💶 Сумма заказа:\n{eur}€\n\n📥 Карта:\n`{card}`\n\nНажмите на номер карты для копирования.",
        "pay_card_uah_screen": "💶 Сумма заказа:\n{eur}€\n\n💱 Курс:\n1 EUR = {rate} UAH\n\n💳 К оплате:\n{uah} UAH\n\n📥 Карта:\n`{card}`\n\nНажмите на номер карты для копирования.",
        "pay_i_paid_btn": "✅ Я оплатил",
        "pay_pending_user": "⏳ Оплата отправлена на проверку.\n\nАдминистратор свяжется с вами после подтверждения.",
        "rate_unavailable": "⚠️ Не удалось получить курс. Попробуй ещё раз через минуту.",
        # --- Города ---
        "choose_city": "🏙 Выбери город для самовывоза или выбери Доставку:",
        "city_delivery_btn": "🚚 Доставка",
        "city_not_set_reminder": "🏙 Ты ещё не выбрал город.\n\nПожалуйста, выбери город для самовывоза или выбери Доставку:",
        "city_selected": "✅ Город выбран: {city}",
        "city_delivery_selected": "✅ Выбрана Доставка",
        "profile_city_row": "🏙 Город: {city}",
        "profile_city_delivery": "🏙 Режим: Доставка",
        "profile_city_none": "🏙 Город не выбран",
        "profile_change_city": "🏙 Изменить город",
        "admin_order_city": "🏙 Город: {city}",
        "admin_confirmed_notify": "✅ Заказ подтверждён\n\n🏙 Город: {city}\n📦 Товары: {items}\n💰 Сумма: {total}€",
        "stock_city_usage": "Использование:\n/addstock <город|all> <elfliq|elfworld> <all|N[,N,...]> <кол-во>\n\nДоступные города: {cities}",
        "stock_city_invalid": "❌ Неверный город. Доступные: {cities}\nИли используйте 'all' для всех городов.",
    },

    "ua": {
        "menu": "📱 Меню",
        "shop": "🛒 Магазин",
        "cart": "🧺 Кошик",
        "language": "🌍 Мова",
        "empty_cart": "Кошик порожній",
        "choose_lang": "Обери мову",
        "choose_product": "🛒 Обери товар:",
        "choose_section": "🚐 Оберіть розділ",
        "banned_message": "🚫 Ваш акаунт було заблоковано адміністрацією.",
        "delivery_mode_on": "🚚 Режим доставки",
        "delivery_mode_off": "🏪 Режим самовивозу",
        "delivery_mode_toggle_on": "✅ Увімкнено режим доставки",
        "delivery_mode_toggle_off": "✅ Увімкнено режим самовивозу",
        "delivery_cart_conflict": "🚚 Очисти кошик, щоб переключити режим — у ньому вже є товари в іншому режимі.",
        "free_delivery_hint": "📦 При замовленні від 3 банок доставка безкоштовна.",
        "delivery_step_name": "📝 Введи ім'я та прізвище (як у паспорті):",
        "delivery_step_phone": "📞 Введи номер телефону або натисни кнопку нижче:",
        "delivery_step_phone_btn": "📱 Надіслати мій номер",
        "delivery_step_address": "📍 Введи адресу у форматі:\n\n<b>Bundesland. Stadt. Straße</b>",
        "delivery_step_tracking": "📦 Обери варіант доставки\n\n✅ З трек-номером — 7.20€\n\n❌ Без трек-номера — 5.20€",
        "delivery_tracking_yes_btn": "✅ З трек-номером",
        "delivery_tracking_no_btn": "❌ Без трек-номера",
        "gift_delivery_free_hint": "🎁 Для безкоштовної банки доставка також повністю безкоштовна.",
        "gift_pickup_label": "📍 Самовивіз",
        "gift_delivery_label": "📦 Доставка",
        "delivery_refill_profile_btn": "🔄 Заповнити знову",
        "delivery_tracking_yes": "✅ З трек-номером — 6.20€",
        "delivery_tracking_no": "❌ Без трек-номера — 4.20€",
        "delivery_confirm_title": "📦 Перевір дані доставки",
        "delivery_field_name": "Ім'я:",
        "delivery_field_phone": "Телефон:",
        "delivery_field_address": "Адреса:",
        "delivery_field_tracking": "Трек-номер:",
        "delivery_tracking_yes_label": "✅ Так",
        "delivery_tracking_no_label": "❌ Ні",
        "delivery_confirm_btn": "✅ Підтвердити",
        "delivery_refill_btn": "🔄 Заповнити знову",
        "delivery_address_profile_btn": "📦 Адреса доставки",
        "delivery_no_cash": "🚚 Для доставки оплата готівкою недоступна.",
        "section_elfliq": "🧪 ELFLIQ",
        "section_elfworld": "🌍 ELFWORLD",
        "section_empty": "Розділ тимчасово порожній",
        "switch_to_elfworld": "🌍 Перейти до ELFWORLD",
        "switch_to_elfliq": "🧪 Перейти до ELFLIQ",
        "total": "Разом",
        "added": "Додано",
        "clear": "🗑 Очистити",
        "remove": "↩ Прибрати останнє",
        "back_shop": "🛒 Назад до магазину",
        "pay": "💳 Оплата",
        "cash": "💵 Готівка",
        "cancel": "❌ Скасувати",
        "order_done": "Замовлення оформлене. Адмін скоро зв'яжеться",
        "no_username_warning": "⚠️ У вас відсутній Telegram username.\n\nЧерез це адмін не зможе першим написати вам, якщо потрібно буде уточнити деталі замовлення.\n\nБудь ласка, в майбутньому додайте username у налаштуваннях Telegram — це значно спростить зв'язок щодо ваших замовлень.",
        "contact_admin_btn": "💬 Написати адміністратору",
        "confirm_order": "Підтвердити замовлення?",
        "confirm": "✅ Підтвердити",
        "paid": "✅ Оплачено",
        "checking_payment": "⏳ Замовлення оформлене",
        "fav_added": "Додано до обраного",
        "fav_removed": "Прибрано з обраного",
        "favorites": "❤️ Обране",
        "no_favorites": "Немає обраних товарів",
        "fav_removed": "❌ Видалено з обраного",
        "fav_restore": "❤️ Повернути в обране",
        "fav_hint": "Натисни на товар щоб видалити",
        "back": "⬅️ Назад",
        "profile": "👤 Профіль",
        "profile_title": "👤 Профіль",
        "profile_info": "Твій профіль",
        "history": "📜 Історія",
        "levels": "🏆 Ранги",
        "roulette": "🎰 Рулетка",
        "streak": "🔥 Стрік",
        "discounts": "💸 Знижки",
        "ref": "🎁 Рефералка",
        "stats": "📊 Статистика",
        "to_shop": "🛒 До магазину",
        "rank": "🏆 Ранг",
        "next_rank": "До наступного рангу:",
        "progress": "Прогрес",
        "to_shop": "🛒 В магазин",
        "new_rank": "🎉 У тебе новий ранг: {rank}!",
        "confirm_admin": "✅ Підтвердити",
        "cancel_admin": "❌ Скасувати",
        "savings": "💸 Зекономлено: {value}€",
        "profile_items": "Банок куплено",
        "profile_orders": "Замовлень",
        "profile_saved": "Зекономлено",
        "profile_discount": "Знижка",
        "history_empty": "Історія замовлень порожня",
        "open_order": "Відкрити замовлення",
        "order": "Замовлення",
        "repeat_order": "🔁 Повторити замовлення",
        "discount_all_items": "Знижка діє на всі товари",
        "from_jars": "від",
        "roulette_prizes": "🎁 Можливі призи:",
        "free_jar": "🎉 Безкоштовна банка",
        "roulette_ready": "✅ Прокрут доступний!",
        "roulette_left": "📦 До наступного прокруту",
        "spin_now": "🎰 Крутити",
        "spin_spinning": "Рулетка крутиться",
        "spin_locked": "❌ Поки недоступно",
        "spin_win_free": "🎉 Ти виграв безкоштовну банку!",
        "spin_win_discount": "🎉 Ти виграв знижку -{value}€ на наступне замовлення!",
        "spin_ready_notify": "🎰 Тобі доступний прокрут у колесі удачі!",
        "spin_open": "🎰 Відкрити рулетку",
        "active_bonus": "🎁 Активний бонус",
        "wheel_next_order_note": "💸 Знижка застосовується до всього наступного замовлення",
        "free_jar_active": "🎁 У тебе є безкоштовна банка",
        "claim_gift": "🎁 Забрати подарунок",
        "choose_gift": "🎁 Обери подарунок",
        "gift_already_used": "❌ Подарунок вже був отриманий",
        "select_gift_btn": "🎁 Вибрати подарунок",
        "gift_confirm_title": "🎁 Вибраний подарунок",
        "gift_confirm_question": "Точно вибрати цей товар?",
        "gift_done": "🎁 Подарунок оформлений\n\nАдміністратор незабаром зв'яжеться з тобою.",
        "gift_cancel": "❌ Скасувати",
        "spin_bonus_exists": "У тебе вже є невикористаний бонус",
        "spin_free_jar_exists": "У тебе вже є безкоштовна банка",
        "streak_current_weeks": "🔥 Поточний стрик: {weeks} тижнів",
        "streak_discount_value": "💸 Поточна знижка: -{value}€",
        "streak_discount_none": "💸 Поточна знижка: немає",
        "streak_days_left": "⏳ До скасування стрику: {days} днів",
        "streak_inactive_hint": "Зроби замовлення, щоб почати стрик 🔥",
        "streak_max_weeks": "🏆 Максимальний стрик: {weeks} тижнів",
        "streak_frozen_banner": "❄️ Час стрику тимчасово заморожений через затримки магазину.",
        "streak_frozen_footer_days": "❄️ Стрик заморожений (збережено {days} дн.)",
        "streak_rewards_title": "🎁 Нагороди за стрик:",
        "streak_week_row": "{weeks} тиж. — знижка {value}€",
        "streak_progress_bar_label": "📅 Прогрес до наступного рівня:",
        "history_title": "📜 Історія замовлень",
        "history_order_row": "🧾 Замовлення #{id}  |  {date}  |  {total}€",
        "ref_title": "🎁 Реферальна система",
        "ref_your_link": "🔗 Твоє реферальне посилання:",
        "ref_earned": "💰 Зароблено на рефералах: {value}€",
        "stats_title": "📊 Статистика",
        "stats_total_saved": "💸 Всього зекономлено: {value}€",
        "profile_title_header": "👤 Профіль",
        "profile_rank_row": "🏆 Ранг: {rank}",
        "profile_discount_row": "💸 Знижка з рангу: -{value}€",
        "discounts_title": "💸 Знижки",
        "discounts_total_header": "💰 Загальна знижка: {value}€",
        "discount_label_rank": "🏆 Знижка рангу",
        "discount_label_streak": "🔥 Buy Streak",
        "discount_label_ref": "🎁 Реферальна знижка",
        "discount_label_wheel": "🎰 Знижка з рулетки",
        "discounts_total_saved": "💸 Всього зекономлено: {value}€",
        "discount_label_ref_bonus": "🎉 Знижка новачка",
        "ref_rules": "🎁 Правила:\n• Ти отримуєш знижку {inviter}€ після першого замовлення запрошеного друга\n• Твій друг отримує знижку {new_user}€ на перше замовлення",
        "ref_invited_count": "👥 Запрошено користувачів: {count}",
        "ref_share_button": "📤 Поділитися посиланням",
        "ref_share_text": "Заходь у наш магазин рідин за моїм посиланням і отримай знижку на перше замовлення! 🎁",
        "ref_credited_notify": "🎉 Твій друг зробив перше замовлення! Тобі нараховано реферальну знижку.",
        "stats_total_orders": "🧾 Всього замовлень: {count}",
        "stats_first_order": "📅 Дата першого замовлення: {date}",
        "stats_rank": "🏆 Поточний ранг: {rank}",
        "stats_top_product": "⭐ Улюблений товар: {name} (куплено {count} раз)",
        "stats_top_product_none": "⭐ Улюблений товар: ще немає замовлень",
        "stats_max_streak": "🔥 Максимальний Buy Streak: {weeks} тижнів",
        "stats_spins": "🎰 Прокручено коліс: {count}",
        "stats_invited": "👥 Запрошено користувачів: {count}",
        "stats_total_spent": "💰 Всього витрачено: {value}€",
        "stock_out": "❌ На жаль, товару {name} більше немає в наявності.\n\nВидаліть його з кошика або оберіть інший смак.",
        "stock_low": "❌ На жаль, товару {name} залишилось лише {qty} шт.",
        "stock_low_cart": "❌ На жаль, товару {name} залишилось лише {qty} шт.\n\nБудь ласка, зменшіть кількість або оберіть інший смак.",
        "stock_changed_retry": "❌ Наявність змінилась поки ви оформлювали замовлення. Будь ласка, перевірте кошик і спробуйте знову.",
        "addstock_usage": "Використання:\n/addstock <elfliq|elfworld> <all|N[,N,...]> <кількість>",
        "removestock_usage": "Використання:\n/removestock <elfliq|elfworld> <all|N[,N,...]> <кількість>",
        "setstock_usage": "Використання:\n/setstock <elfliq|elfworld> <N[,N,...]> <кількість>",
        "stock_updated": "✅ Оновлено {count} товар(ів)",
        "promo_activated_discount": "🎉 Промокод активовано!\n\nЗнижку -{value}€ додано до твого замовлення.",
        "promo_activated_free_jar": "🎉 Промокод активовано!\n\nТобі нарахована безкоштовна банка — обери її в магазині.",
        "promo_not_found": "❌ Промокод не знайдено або вже використано.",
        "promo_already_have": "⚠️ У тебе вже є активний промокод.",
        "promo_usage": "Використання: /promo КОД",
        "promo_in_shop_label": "🎉 У тебе активовано промокод на {value}€",
        "promo_in_profile_discount": "🎟 Промокод: {code} (-{value}€)",
        "promo_in_profile_free_jar": "🎟 Промокод: {code} (безкоштовна банка)",
        "shop_btn": "🛒 Магазин",
        "gift_shop_btn": "🎁 Отримати безкоштовну банку",
        "createpromo_usage": "Використання:\n/createpromo discount СУМА КІЛЬКІСТЬ\n/createpromo freejar КІЛЬКІСТЬ",
        "createpromo_done": "✅ Створено {count} промокодів:\n{codes}",
        "pay_usdt_btn": "💵 USDT (TRC20)",
        "pay_card_btn": "💳 Банківська карта",
        "pay_currency_title": "💳 Оберіть валюту оплати:",
        "pay_card_uah_btn": "🇺🇦 Оплата в гривні (UAH)",
        "pay_card_eur_btn": "💶 Оплата в євро (EUR)",
        "pay_usdt_screen": "💶 Сума замовлення:\n{eur}€\n\n💲 Курс:\n1 EUR = {rate} USDT\n\n💵 До оплати:\n{usdt} USDT (TRC20)\n\n📥 Адреса:\n`{wallet}`\n\nНатисніть на адресу для копіювання.",
        "pay_card_eur_screen": "💶 Сума замовлення:\n{eur}€\n\n📥 Карта:\n`{card}`\n\nНатисніть на номер картки для копіювання.",
        "pay_card_uah_screen": "💶 Сума замовлення:\n{eur}€\n\n💱 Курс:\n1 EUR = {rate} UAH\n\n💳 До оплати:\n{uah} UAH\n\n📥 Карта:\n`{card}`\n\nНатисніть на номер картки для копіювання.",
        "pay_i_paid_btn": "✅ Я оплатив",
        "pay_pending_user": "⏳ Оплата надіслана на перевірку.\n\nАдміністратор зв'яжеться з вами після підтвердження.",
        "rate_unavailable": "⚠️ Не вдалося отримати курс. Спробуй ще раз за хвилину.",
        # --- Міста ---
        "choose_city": "🏙 Обери місто для самовивозу або обери Доставку:",
        "city_delivery_btn": "🚚 Доставка",
        "city_not_set_reminder": "🏙 Ти ще не обрав місто.\n\nБудь ласка, обери місто для самовивозу або Доставку:",
        "city_selected": "✅ Місто обрано: {city}",
        "city_delivery_selected": "✅ Обрано Доставку",
        "profile_city_row": "🏙 Місто: {city}",
        "profile_city_delivery": "🏙 Режим: Доставка",
        "profile_city_none": "🏙 Місто не обрано",
        "profile_change_city": "🏙 Змінити місто",
        "admin_order_city": "🏙 Місто: {city}",
        "admin_confirmed_notify": "✅ Замовлення підтверджено\n\n🏙 Місто: {city}\n📦 Товари: {items}\n💰 Сума: {total}€",
        "stock_city_usage": "Використання:\n/addstock <місто|all> <elfliq|elfworld> <all|N[,N,...]> <кількість>\n\nДоступні міста: {cities}",
        "stock_city_invalid": "❌ Неправильне місто. Доступні: {cities}\nАбо використовуйте 'all' для всіх міст.",
    },

    "de": {
        "menu": "📱 Menü",
        "shop": "🛒 Shop",
        "cart": "🧺 Warenkorb",
        "language": "🌍 Sprache",
        "empty_cart": "Warenkorb ist leer",
        "choose_lang": "Sprache wählen",
        "choose_product": "🛒 Produkt wählen:",
        "choose_section": "🚐 Wähle einen Bereich",
        "banned_message": "🚫 Dein Konto wurde von der Administration gesperrt.",
        "delivery_mode_on": "🚚 Liefermodus",
        "delivery_mode_off": "🏪 Abholmodus",
        "delivery_mode_toggle_on": "✅ Liefermodus aktiviert",
        "delivery_mode_toggle_off": "✅ Abholmodus aktiviert",
        "delivery_cart_conflict": "🚚 Leere den Warenkorb, um den Modus zu wechseln — er enthält bereits Artikel im anderen Modus.",
        "free_delivery_hint": "📦 Ab 3 Flaschen ist die Lieferung kostenlos.",
        "delivery_step_name": "📝 Gib deinen Vor- und Nachnamen ein (wie im Reisepass):",
        "delivery_step_phone": "📞 Gib deine Telefonnummer ein oder klicke unten:",
        "delivery_step_phone_btn": "📱 Meine Nummer senden",
        "delivery_step_address": "📍 Gib die Adresse im Format ein:\n\n<b>Bundesland. Stadt. Straße</b>",
        "delivery_step_tracking": "📦 Wähle die Versandart\n\n✅ Mit Sendungsverfolgung — 7,20€\n\n❌ Ohne Sendungsverfolgung — 5,20€",
        "delivery_tracking_yes_btn": "✅ Mit Sendungsverfolgung",
        "delivery_tracking_no_btn": "❌ Ohne Sendungsverfolgung",
        "gift_delivery_free_hint": "🎁 Für die Gratis-Dose ist der Versand ebenfalls völlig kostenlos.",
        "gift_pickup_label": "📍 Abholung",
        "gift_delivery_label": "📦 Lieferung",
        "delivery_refill_profile_btn": "🔄 Neu ausfüllen",
        "delivery_tracking_yes": "✅ Mit Sendungsverfolgung — 6,20€",
        "delivery_tracking_no": "❌ Ohne Sendungsverfolgung — 4,20€",
        "delivery_confirm_title": "📦 Lieferdaten prüfen",
        "delivery_field_name": "Name:",
        "delivery_field_phone": "Telefon:",
        "delivery_field_address": "Adresse:",
        "delivery_field_tracking": "Sendungsverfolgung:",
        "delivery_tracking_yes_label": "✅ Ja",
        "delivery_tracking_no_label": "❌ Nein",
        "delivery_confirm_btn": "✅ Bestätigen",
        "delivery_refill_btn": "🔄 Neu ausfüllen",
        "delivery_address_profile_btn": "📦 Lieferadresse",
        "delivery_no_cash": "🚚 Für Lieferungen ist Barzahlung nicht verfügbar.",
        "section_elfliq": "🧪 ELFLIQ",
        "section_elfworld": "🌍 ELFWORLD",
        "section_empty": "Dieser Bereich ist momentan leer",
        "switch_to_elfworld": "🌍 Wechseln zu ELFWORLD",
        "switch_to_elfliq": "🧪 Wechseln zu ELFLIQ",
        "total": "Summe",
        "added": "Hinzugefügt",
        "clear": "🗑 Leeren",
        "remove": "↩ Letztes entfernen",
        "back_shop": "🛒 Zurück zum Shop",
        "pay": "💳 Zahlung",
        "cash": "💵 Bar",
        "cancel": "❌ Abbrechen",
        "order_done": "Bestellung erstellt. Admin meldet sich",
        "no_username_warning": "⚠️ Du hast keinen Telegram-Username.\n\nDadurch kann der Admin dich nicht zuerst kontaktieren, falls Details zur Bestellung geklärt werden müssen.\n\nBitte füge in Zukunft einen Username in den Telegram-Einstellungen hinzu — das erleichtert die Kommunikation zu deinen Bestellungen erheblich.",
        "contact_admin_btn": "💬 Admin schreiben",
        "confirm_order": "Bestellung bestätigen?",
        "confirm": "✅ Bestätigen",
        "paid": "✅ Bezahlt",
        "checking_payment": "Zahlung wird geprüft, Admin meldet sich",
        "fav_added": "Zu Favoriten hinzugefügt",
        "fav_removed": "Aus Favoriten entfernt",
        "favorites": "❤️ Favoriten",
        "no_favorites": "Keine Favoriten",
        "fav_removed": "❌ Entfernt",
        "fav_restore": "❤️ Wieder hinzufügen",
        "fav_hint": "Klicke um zu entfernen",
        "back": "⬅️ Zurück",
        "profile": "👤 Profil",
        "profile_title": "👤 Profil",
        "profile_info": "Dein Profil",
        "history": "📜 Verlauf",
        "levels": "🏆 Level",
        "roulette": "🎰 Roulette",
        "streak": "🔥 Serie",
        "discounts": "💸 Rabatte",
        "ref": "🎁 Referral",
        "stats": "📊 Statistik",
        "to_shop": "🛒 Zum Shop",
        "rank": "🏆 Rang",
        "next_rank": "Bis zum nächsten Rang:",
        "progress": "Fortschritt",
        "to_shop": "🛒 Shop",
        "new_rank": "🎉 Neuer Rang: {rank}!",
        "confirm_admin": "✅ Bestätigen",
        "cancel_admin": "❌ Abbrechen",
        "savings": "💸 Gespart: {value}€",
        "profile_items": "Gekaufte Items",
        "profile_orders": "Bestellungen",
        "profile_saved": "Gespart",
        "profile_discount": "Rabatt",
        "history_empty": "Bestellverlauf ist leer",
        "open_order": "Bestellung öffnen",
        "order": "Bestellung",
        "repeat_order": "🔁 Bestellung wiederholen",
        "discount_all_items": "Rabatt gilt für alle Produkte",
        "from_jars": "ab",
        "roulette_prizes": "🎁 Mögliche Gewinne:",
        "free_jar": "🎉 Gratis Dose",
        "roulette_ready": "✅ Dreh verfügbar!",
        "roulette_left": "📦 Bis zum nächsten Dreh",
        "spin_now": "🎰 Drehen",
        "spin_spinning": "Roulette dreht sich",
        "spin_locked": "❌ Noch nicht verfügbar",
        "spin_win_free": "🎉 Du hast eine Gratis-Dose gewonnen!",
        "spin_win_discount": "🎉 Du hast einen Rabatt von -{value}€ auf die nächste Bestellung gewonnen!",
        "spin_ready_notify": "🎰 Du hast einen verfügbaren Dreh im Glücksrad!",
        "spin_open": "🎰 Glücksrad öffnen",
        "active_bonus": "🎁 Aktiver Bonus",
        "wheel_next_order_note": "💸 Rabatt gilt für die gesamte nächste Bestellung",
        "free_jar_active": "🎁 Du hast eine Gratis-Dose",
        "claim_gift": "🎁 Geschenk abholen",
        "choose_gift": "🎁 Wähle dein Geschenk",
        "gift_already_used": "❌ Geschenk wurde bereits erhalten",
        "select_gift_btn": "🎁 Als Geschenk wählen",
        "gift_confirm_title": "🎁 Geschenk ausgewählt",
        "gift_confirm_question": "Dieses Produkt wirklich wählen?",
        "gift_done": "🎁 Geschenk bestellt\n\nDer Admin meldet sich bald bei dir.",
        "gift_cancel": "❌ Abbrechen",
        "spin_bonus_exists": "Du hast bereits einen ungenutzten Bonus",
        "spin_free_jar_exists": "Du hast bereits eine Gratis-Dose",
        "streak_current_weeks": "🔥 Aktuelle Serie: {weeks} Wochen",
        "streak_discount_value": "💸 Aktueller Rabatt: -{value}€",
        "streak_discount_none": "💸 Aktueller Rabatt: keiner",
        "streak_days_left": "⏳ Bis zum Zurücksetzen der Serie: {days} Tage",
        "streak_inactive_hint": "Bestelle etwas, um eine Serie zu starten 🔥",
        "streak_max_weeks": "🏆 Maximale Serie: {weeks} Wochen",
        "streak_frozen_banner": "❄️ Die Serien-Zeit ist wegen Lieferverzögerungen im Shop vorübergehend eingefroren.",
        "streak_frozen_footer_days": "❄️ Serie eingefroren (gespart: {days} Tage)",
        "streak_rewards_title": "🎁 Belohnungen für Serie:",
        "streak_week_row": "{weeks} Wo. — Rabatt {value}€",
        "streak_progress_bar_label": "📅 Fortschritt zum nächsten Level:",
        "history_title": "📜 Bestellverlauf",
        "history_order_row": "🧾 Bestellung #{id}  |  {date}  |  {total}€",
        "ref_title": "🎁 Empfehlungsprogramm",
        "ref_your_link": "🔗 Dein Empfehlungslink:",
        "ref_earned": "💰 Verdient durch Empfehlungen: {value}€",
        "stats_title": "📊 Statistik",
        "stats_total_saved": "💸 Insgesamt gespart: {value}€",
        "profile_title_header": "👤 Profil",
        "profile_rank_row": "🏆 Rang: {rank}",
        "profile_discount_row": "💸 Rang-Rabatt: -{value}€",
        "discounts_title": "💸 Rabatte",
        "discounts_total_header": "💰 Gesamtrabatt: {value}€",
        "discount_label_rank": "🏆 Rang-Rabatt",
        "discount_label_streak": "🔥 Buy Streak",
        "discount_label_ref": "🎁 Empfehlungsrabatt",
        "discount_label_wheel": "🎰 Roulette-Rabatt",
        "discounts_total_saved": "💸 Insgesamt gespart: {value}€",
        "discount_label_ref_bonus": "🎉 Neukunden-Rabatt",
        "ref_rules": "🎁 Regeln:\n• Du erhältst {inviter}€ Rabatt nach der ersten Bestellung deines eingeladenen Freundes\n• Dein Freund erhält {new_user}€ Rabatt auf die erste Bestellung",
        "ref_invited_count": "👥 Eingeladene Nutzer: {count}",
        "ref_share_button": "📤 Link teilen",
        "ref_share_text": "Komm in unseren Liquid-Shop über meinen Link und erhalte Rabatt auf deine erste Bestellung! 🎁",
        "ref_credited_notify": "🎉 Dein Freund hat seine erste Bestellung aufgegeben! Du hast einen Empfehlungsrabatt erhalten.",
        "stats_total_orders": "🧾 Bestellungen insgesamt: {count}",
        "stats_first_order": "📅 Datum der ersten Bestellung: {date}",
        "stats_rank": "🏆 Aktueller Rang: {rank}",
        "stats_top_product": "⭐ Lieblingsprodukt: {name} ({count}x gekauft)",
        "stats_top_product_none": "⭐ Lieblingsprodukt: noch keine Bestellungen",
        "stats_max_streak": "🔥 Maximale Buy-Streak: {weeks} Wochen",
        "stats_spins": "🎰 Gedrehte Räder: {count}",
        "stats_invited": "👥 Eingeladene Nutzer: {count}",
        "stats_total_spent": "💰 Insgesamt ausgegeben: {value}€",
        "stock_out": "❌ Leider ist {name} nicht mehr vorrätig.\n\nBitte entferne es aus dem Warenkorb oder wähle eine andere Sorte.",
        "stock_low": "❌ Leider sind nur noch {qty} Stück von {name} verfügbar.",
        "stock_low_cart": "❌ Leider sind nur noch {qty} Stück von {name} verfügbar.\n\nBitte reduziere die Menge oder wähle eine andere Sorte.",
        "stock_changed_retry": "❌ Der Bestand hat sich geändert während du bestellt hast. Bitte prüfe den Warenkorb und versuche es erneut.",
        "addstock_usage": "Verwendung:\n/addstock <elfliq|elfworld> <all|N[,N,...]> <Menge>",
        "removestock_usage": "Verwendung:\n/removestock <elfliq|elfworld> <all|N[,N,...]> <Menge>",
        "setstock_usage": "Verwendung:\n/setstock <elfliq|elfworld> <N[,N,...]> <Menge>",
        "stock_updated": "✅ {count} Produkt(e) aktualisiert",
        "promo_activated_discount": "🎉 Promocode aktiviert!\n\nRabatt von -{value}€ wurde deiner Bestellung hinzugefügt.",
        "promo_activated_free_jar": "🎉 Promocode aktiviert!\n\nDu erhältst eine Gratis-Dose — wähle sie im Shop aus.",
        "promo_not_found": "❌ Promocode nicht gefunden oder bereits verwendet.",
        "promo_already_have": "⚠️ Du hast bereits einen aktiven Promocode.",
        "promo_usage": "Verwendung: /promo CODE",
        "promo_in_shop_label": "🎉 Du hast einen aktiven Promocode über {value}€",
        "promo_in_profile_discount": "🎟 Promocode: {code} (-{value}€)",
        "promo_in_profile_free_jar": "🎟 Promocode: {code} (Gratis-Dose)",
        "shop_btn": "🛒 Shop",
        "gift_shop_btn": "🎁 Gratis-Dose erhalten",
        "createpromo_usage": "Verwendung:\n/createpromo discount BETRAG ANZAHL\n/createpromo freejar ANZAHL",
        "createpromo_done": "✅ {count} Promocodes erstellt:\n{codes}",
        "pay_usdt_btn": "💵 USDT (TRC20)",
        "pay_card_btn": "💳 Bankkarte",
        "pay_currency_title": "💳 Zahlungswährung wählen:",
        "pay_card_uah_btn": "🇺🇦 Zahlung in Hrywnja (UAH)",
        "pay_card_eur_btn": "💶 Zahlung in Euro (EUR)",
        "pay_usdt_screen": "💶 Bestellsumme:\n{eur}€\n\n💲 Kurs:\n1 EUR = {rate} USDT\n\n💵 Zu zahlen:\n{usdt} USDT (TRC20)\n\n📥 Adresse:\n`{wallet}`\n\nAdresse antippen zum Kopieren.",
        "pay_card_eur_screen": "💶 Bestellsumme:\n{eur}€\n\n📥 Karte:\n`{card}`\n\nKartennummer antippen zum Kopieren.",
        "pay_card_uah_screen": "💶 Bestellsumme:\n{eur}€\n\n💱 Kurs:\n1 EUR = {rate} UAH\n\n💳 Zu zahlen:\n{uah} UAH\n\n📥 Karte:\n`{card}`\n\nKartennummer antippen zum Kopieren.",
        "pay_i_paid_btn": "✅ Ich habe bezahlt",
        "pay_pending_user": "⏳ Zahlung zur Überprüfung gesendet.\n\nDer Admin meldet sich nach der Bestätigung.",
        "rate_unavailable": "⚠️ Kurs konnte nicht abgerufen werden. Versuche es in einer Minute erneut.",
        # --- Städte ---
        "choose_city": "🏙 Wähle deine Stadt für die Abholung oder wähle Lieferung:",
        "city_delivery_btn": "🚚 Lieferung",
        "city_not_set_reminder": "🏙 Du hast noch keine Stadt gewählt.\n\nBitte wähle deine Abholstadt oder Lieferung:",
        "city_selected": "✅ Stadt gewählt: {city}",
        "city_delivery_selected": "✅ Lieferung gewählt",
        "profile_city_row": "🏙 Stadt: {city}",
        "profile_city_delivery": "🏙 Modus: Lieferung",
        "profile_city_none": "🏙 Keine Stadt gewählt",
        "profile_change_city": "🏙 Stadt ändern",
        "admin_order_city": "🏙 Stadt: {city}",
        "admin_confirmed_notify": "✅ Bestellung bestätigt\n\n🏙 Stadt: {city}\n📦 Artikel: {items}\n💰 Summe: {total}€",
        "stock_city_usage": "Verwendung:\n/addstock <Stadt|all> <elfliq|elfworld> <all|N[,N,...]> <Menge>\n\nVerfügbare Städte: {cities}",
        "stock_city_invalid": "❌ Ungültige Stadt. Verfügbar: {cities}\nOder 'all' für alle Städte verwenden.",
    }
}

DISCOUNTS = {
    "rank": [
        {
            "key": "none",
            "name": {"ru": "⚪ Новичок", "ua": "⚪ Новачок", "de": "⚪ Anfänger"},
            "need": 0,
            "value": 0
        },
        {
            "key": "bronze",
            "name": {"ru": "🥉 Bronze", "ua": "🥉 Bronze", "de": "🥉 Bronze"},
            "need": 5,
            "value": 1.0
        },
        {
            "key": "silver",
            "name": {"ru": "🥈 Silver", "ua": "🥈 Silver", "de": "🥈 Silver"},
            "need": 10,
            "value": 1.5
        },
        {
            "key": "gold",
            "name": {"ru": "🥇 Gold", "ua": "🥇 Gold", "de": "🥇 Gold"},
            "need": 20,
            "value": 2.0
        },
        {
            "key": "diamond",
            "name": {"ru": "💎 Diamond", "ua": "💎 Diamond", "de": "💎 Diamond"},
            "need": 40,
            "value": 3.0
        },
    ],

    "wheel": [
        {"key": "1", "chance": 39, "value": 1.0},
        {"key": "1.5", "chance": 30, "value": 1.5},
        {"key": "2", "chance": 20, "value": 2.0},
        {"key": "3", "chance": 10, "value": 3.0},
        {"key": "free", "chance": 1, "value": 15.0},
    ],

    "streak": [
        {"weeks": 1, "value": 1.0},
        {"weeks": 2, "value": 1.5},
        {"weeks": 3, "value": 2.0},
        {"weeks": 4, "value": 2.5},
        {"weeks": 5, "value": 3.0},
    ],

    "ref": {
        "inviter": 1.5,
        "new_user": 1.0
    }
}

# ========== АНИМАЦИЯ РУЛЕТКИ ==========

ANIMATION_DELAYS = (
    0.075, 0.075, 0.080, 0.085, 0.090,
    0.100, 0.110, 0.125, 0.145, 0.170,
    0.200, 0.240, 0.285, 0.330, 0.380,
    0.430, 0.485, 0.540, 0.595, 0.648,
    0.705, 0.820,
)

# Фиксированный порядок секторов колеса (8 позиций по часовой от верха)
# Позиции: 0=верх, 1=верх-право, 2=право(стрелка), 3=низ-право,
#          4=низ,  5=низ-лево,   6=лево,            7=верх-лево
WHEEL_SECTORS = [
    {"key": "1",    "emoji": "🎁", "label": "-1€"  },
    {"key": "free", "emoji": "🎰", "label": "Банка"},
    {"key": "2",    "emoji": "🥈", "label": "-2€"  },
    {"key": "1.5",  "emoji": "✨", "label": "-1.5€"},
    {"key": "3",    "emoji": "💎", "label": "-3€"  },
    {"key": "1",    "emoji": "🎁", "label": "-1€"  },
    {"key": "2",    "emoji": "🥈", "label": "-2€"  },
    {"key": "1.5",  "emoji": "✨", "label": "-1.5€"},
]
_WN = len(WHEEL_SECTORS)

def _wheel_frame(offset: int, show_arrow: bool) -> str:
    """Строит один кадр кругового колеса по фиксированному offset."""
    def s(pos):
        return WHEEL_SECTORS[((offset + pos) % _WN + _WN) % _WN]
    def cell(pos):
        return f"{s(pos)['emoji']}{s(pos)['label']}"

    arrow = "  ◀━━" if show_arrow else ""
    return (
        f"              {cell(0)}\n"
        f"\n"
        f"    {cell(7)}            {cell(1)}\n"
        f"\n"
        f" {cell(6)}               {cell(2)}{arrow}\n"
        f"\n"
        f"    {cell(5)}            {cell(3)}\n"
        f"\n"
        f"              {cell(4)}"
    )

async def _run_roulette_animation(call, winning_prize: dict, spinning_label: str) -> None:
    """
    Круговое колесо, вращение по часовой стрелке (offset уменьшается).
    Победитель всегда останавливается на позиции 2 (стрелка справа).
    """
    win_key = winning_prize["key"]

    # Ищем все позиции победителя на колесе, берём случайную
    win_positions = [i for i, p in enumerate(WHEEL_SECTORS) if p["key"] == win_key]
    win_pos = random.choice(win_positions)

    # offset такой, чтобы s(2) = WHEEL_SECTORS[win_pos]
    # s(2) = WHEEL[(offset+2) % N]  =>  offset = win_pos - 2
    target_offset = (win_pos - 2) % _WN

    start_offset = random.randint(0, _WN - 1)
    full_spins = 3

    # По часовой: offset уменьшается → путь = (start - target + N*full_spins)
    cw_path = ((start_offset - target_offset) % _WN + _WN) % _WN
    total_steps = full_spins * _WN + cw_path
    total_frames = len(ANIMATION_DELAYS)

    for i in range(total_frames):
        progress = i / (total_frames - 1)
        eased = 1 - (1 - progress) ** 2          # ease-out
        steps = round(eased * total_steps)
        offset = ((start_offset - steps) % _WN + _WN) % _WN
        is_last = i >= total_frames - 4
        dots = "·" * (i % 4 + 1)

        text = f"🎰 {spinning_label}{dots}\n\n{_wheel_frame(offset, is_last)}"
        try:
            await call.message.edit_text(text)
        except Exception:
            pass
        await asyncio.sleep(ANIMATION_DELAYS[i])

    # Финальный кадр со стрелкой точно на победителе
    try:
        await call.message.edit_text(
            f"🎯 {spinning_label}!\n\n{_wheel_frame(target_offset, True)}"
        )
    except Exception:
        pass
    await asyncio.sleep(0.7)

async def _calc_streak_from_row(streak_weeks, last_order_date, city_key) -> int:
    """
    Вычисляет актуальный стрик из уже загруженных данных строки users.
    Не делает дополнительных DB-запросов — используется внутри get_user_discounts.
    """
    if not last_order_date:
        return 0
    is_frozen, freeze_rows, now_dt = await _get_freeze_data(city_key)
    days_since = await _effective_days_since(last_order_date, city_key, _rows=freeze_rows, _now_dt=now_dt)
    if days_since <= 7:
        return streak_weeks or 0
    return 0


async def get_user_discounts(uid):
    # Один SELECT вместо двух (раньше отдельно шёл get_effective_streak → _streak_snapshot)
    async with pool.acquire() as conn:
        user = await conn.fetchrow("""
            SELECT total_items, referrals, current_discount, ref_bonus, promo_discount,
                   streak_weeks, last_order_date, max_streak_weeks, city
            FROM users WHERE user_id=$1
        """, uid)

    if not user:
        return []

    items = user["total_items"]
    refs = user["referrals"]
    wheel_discount = user["current_discount"]
    ref_bonus = user["ref_bonus"] or 0
    promo_discount = user["promo_discount"] or 0

    # Считаем стрик без дополнительного DB-запроса
    streak = await _calc_streak_from_row(
        streak_weeks=user["streak_weeks"],
        last_order_date=user["last_order_date"],
        city_key=user["city"],
    )

    discounts = []

    # RANK
    rank_discount = 0
    for r in DISCOUNTS["rank"]:
        if items >= r["need"]:
            rank_discount = r["value"]

    if rank_discount > 0:
        discounts.append({"type": "rank", "value": rank_discount, "apply_to": "all"})

    # STREAK
    streak_discount = get_streak_discount_value(streak)

    if streak_discount > 0:
        discounts.append({"type": "streak", "value": streak_discount, "apply_to": "all"})

    # WHEEL
    if wheel_discount > 0:
        discounts.append({
            "type": "wheel",
            "value": wheel_discount,
            "apply_to": "all",
            "one_time": True
        })

    # PROMO — скидка от промокода, суммируется с остальными, действует на весь заказ
    if promo_discount > 0:
        discounts.append({
            "type": "promo",
            "value": promo_discount,
            "apply_to": "all",
            "one_time": True
        })

    # REF — скидка ПРИГЛАСИВШЕМУ (начисляется после первого заказа друга)
    if refs > 0:
        discounts.append({
            "type": "ref",
            "value": DISCOUNTS["ref"]["inviter"],
            "apply_to": "one_item",
            "one_time": True
        })

    # REF BONUS — скидка НОВОМУ пользователю, пришедшему по реф. ссылке
    if ref_bonus > 0:
        discounts.append({
            "type": "ref_bonus",
            "value": ref_bonus,
            "apply_to": "one_item",
            "one_time": True
        })

    return discounts

async def calculate_total_discount(uid, quantity):
    discounts = await get_user_discounts(uid)

    discount_all = 0
    discount_one = 0

    for d in discounts:
        if d["apply_to"] == "all":
            discount_all += d["value"]
        elif d["apply_to"] == "one_item":
            discount_one += d["value"]

    total_discount = discount_all * quantity

    if quantity > 0:
        total_discount += discount_one

    return round(total_discount, 2)

async def calculate_final_price(uid, quantity):
    base_total = BASE_PRICE * quantity
    discount = await calculate_total_discount(uid, quantity)

    final = base_total - discount
    if final < 0:
        final = 0

    return round(final, 2), discount


def fmt_amount(value) -> str:
    """
    Округляет до сотых и убирает лишние нули на конце: 15.20 -> '15.2',
    15.00 -> '15', 1.17 -> '1.17'. Используется только в экране/сообщениях
    оплаты USDT, чтобы курс и суммы выглядели одинаково у пользователя и у админов.
    """
    value = round(float(value), 2)
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text or "0"


async def get_cart_mode(uid) -> str:
    """Режим корзины пользователя: 'pickup' или 'delivery'."""
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT cart_mode FROM users WHERE user_id=$1", uid
        ) or "pickup"


async def _fetch_eur_usdt() -> float | None:
    """Получить актуальный курс EUR→USDT с Binance (fallback: CoinGecko)."""
    try:
        async with aiohttp.ClientSession() as session:
            resp = await session.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": "EURUSDT"},
                timeout=aiohttp.ClientTimeout(total=5),
            )
            data = await resp.json()
        return round(float(data["price"]), 4)
    except Exception:
        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.get(
                    "https://api.coingecko.com/api/v3/simple/price",
                    params={"ids": "tether", "vs_currencies": "eur"},
                    timeout=aiohttp.ClientTimeout(total=5),
                )
                data = await resp.json()
            return round(1 / data["tether"]["eur"], 4)
        except Exception:
            return None


async def _fetch_eur_uah() -> float | None:
    """Получить актуальный курс EUR→UAH через exchangerate-api (fallback: НБУ)."""
    try:
        async with aiohttp.ClientSession() as session:
            resp = await session.get(
                "https://api.exchangerate-api.com/v4/latest/EUR",
                timeout=aiohttp.ClientTimeout(total=5),
            )
            data = await resp.json()
        return round(float(data["rates"]["UAH"]), 2)
    except Exception:
        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.get(
                    "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?valcode=EUR&json",
                    timeout=aiohttp.ClientTimeout(total=5),
                )
                data = await resp.json()
            return round(float(data[0]["rate"]), 2)
        except Exception:
            return None


async def get_user_rate(uid: int, currency: str) -> float | None:
    """
    Возвращает зафиксированный курс для пользователя (TTL 30 мин).
    currency: "usdt" | "uah"
    При первом вызове или после истечения TTL — запрашивает свежий курс.
    """
    now = datetime.utcnow().timestamp()
    user_cache = _rate_cache.get(uid, {})
    cached = user_cache.get(currency)
    if cached:
        rate, ts = cached
        if now - ts < _RATE_TTL:
            return rate
    # Получаем свежий курс
    if currency == "usdt":
        rate = await _fetch_eur_usdt()
    else:
        rate = await _fetch_eur_uah()
    if rate:
        if uid not in _rate_cache:
            _rate_cache[uid] = {}
        _rate_cache[uid][currency] = (rate, now)
    return rate




async def _get_freeze_data(city_key: str | None) -> tuple[bool, list]:
    """
    Читает streak_freezes один раз и возвращает (is_frozen, rows).
    Исключает двойной запрос к streak_freezes в _streak_snapshot.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT started_at, ended_at FROM streak_freezes
            WHERE city_key IS NULL OR city_key = $1
        """, city_key)
    now_dt = datetime.now()
    is_frozen = any(r["ended_at"] is None for r in rows)
    return is_frozen, rows, now_dt


async def is_streak_frozen(city_key: str | None = None) -> bool:
    """
    Активна ли сейчас заморозка Buy Streak для данного города.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchval("""
            SELECT 1 FROM streak_freezes
            WHERE ended_at IS NULL
            AND (city_key IS NULL OR city_key = $1)
            LIMIT 1
        """, city_key)
    return row is not None


async def get_frozen_days_since(since_date: date, city_key: str | None = None,
                                 _rows=None, _now_dt=None) -> float:
    """
    Сколько дней заморозки попало в промежуток [since_date, сейчас].
    Принимает опциональные _rows и _now_dt чтобы избежать повторного SELECT.
    """
    since_dt = datetime.combine(since_date, datetime.min.time())
    now_dt = _now_dt or datetime.now()

    if _rows is None:
        async with pool.acquire() as conn:
            _rows = await conn.fetch("""
                SELECT started_at, ended_at FROM streak_freezes
                WHERE city_key IS NULL OR city_key = $1
            """, city_key)

    total = timedelta()
    for r in _rows:
        start = r["started_at"]
        end = r["ended_at"] or now_dt
        overlap_start = max(start, since_dt)
        overlap_end = min(end, now_dt)
        if overlap_end > overlap_start:
            total += overlap_end - overlap_start

    return total.total_seconds() / 86400


async def _effective_days_since(last_date: date, city_key: str | None = None,
                                 _rows=None, _now_dt=None) -> float:
    """Сколько дней реально прошло с last_date до сегодня, не считая дней заморозки города."""
    real_days = (date.today() - last_date).days
    frozen_days = await get_frozen_days_since(last_date, city_key, _rows=_rows, _now_dt=_now_dt)
    return max(real_days - frozen_days, 0)

def get_streak_discount_value(weeks: int) -> float:
    """Скидка за стрик (максимум — последний порог в DISCOUNTS['streak'], сейчас 3€ с 5 недель)."""
    value = 0
    for s in DISCOUNTS["streak"]:
        if weeks >= s["weeks"]:
            value = s["value"]
    return value

async def _calc_next_streak(old_weeks: int, last_date, city_key: str | None = None) -> int:
    """
    Считает новое значение стрика в момент подтверждения нового заказа.
    last_date — дата предыдущего подтверждённого заказа (или None, если заказов не было).
    Стрик НЕ привязан к календарным неделям: каждый заказ, сделанный не
    позднее чем через 7 "неморозных" дней после предыдущего, продолжает
    стрик (+1). Если прошло больше 7 дней — стрик прерывается и начинается
    заново с 1.
    """
    if not last_date:
        return 1

    days_since = await _effective_days_since(last_date, city_key)

    if days_since <= 7:
        return (old_weeks or 0) + 1

    return 1

async def _streak_snapshot(uid):
    """
    Возвращает текущее состояние Buy Streak пользователя:
    (текущий_стрик, дней_до_сброса, максимальный_стрик, заморожен_ли).
    streak_freezes читается ровно один раз.
    """
    async with pool.acquire() as conn:
        user = await conn.fetchrow("""
            SELECT streak_weeks, last_order_date, max_streak_weeks, city
            FROM users WHERE user_id=$1
        """, uid)

    city_key = user["city"] if user else None

    # Читаем freeze-данные один раз
    is_frozen, freeze_rows, now_dt = await _get_freeze_data(city_key)

    if not user:
        return 0, 0, 0, is_frozen

    max_weeks = user["max_streak_weeks"] or 0

    if not user["last_order_date"]:
        return 0, 0, max_weeks, is_frozen

    weeks = user["streak_weeks"] or 0
    days_since = await _effective_days_since(
        user["last_order_date"], city_key, _rows=freeze_rows, _now_dt=now_dt
    )

    if days_since <= 7:
        days_left = max(7 - days_since, 0)
        return weeks, days_left, max_weeks, is_frozen

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET streak_weeks=0 WHERE user_id=$1", uid
        )

    return 0, 0, max_weeks, is_frozen

async def get_effective_streak(uid) -> int:
    """Текущий (актуальный) стрик пользователя — используется в расчёте скидок."""
    weeks, _, _, _ = await _streak_snapshot(uid)
    return weeks

async def get_user_ctx(uid: int) -> dict:
    """
    Один SELECT вместо get_lang() + get_user_city().
    Возвращает dict с ключами: lang, city, delivery_mode, banned.
    Используй везде где нужно 2+ из этих полей в одном хендлере.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT language, city, banned FROM users WHERE user_id=$1", uid
        )
    if not row:
        return {"lang": "ru", "city": None, "banned": False}
    return {
        "lang":   row["language"] or "ru",
        "city":   row["city"],
        "banned": bool(row["banned"]),
    }


async def get_lang(uid: int) -> str:
    """Возвращает язык пользователя. Используй get_user_ctx() если нужно несколько полей."""
    async with pool.acquire() as conn:
        lang = await conn.fetchval(
            "SELECT language FROM users WHERE user_id=$1", uid
        )
    return lang or "ru"

async def check_not_banned(target) -> bool:
    """
    Возвращает True, если пользователь может продолжить (не забанен).
    Если забанен — сама функция отправляет локализованное сообщение о
    блокировке и возвращает False, чтобы вызывающий хендлер мог сразу
    прервать выполнение (return).
    """
    uid = target.from_user.id
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT banned, language FROM users WHERE user_id=$1", uid
        )

    if not row or not row["banned"]:
        return True

    lang = row["language"] or "ru"
    text = TEXTS[lang].get("banned_message", "🚫")

    if isinstance(target, types.CallbackQuery):
        await target.answer(text, show_alert=True)
    else:
        await target.answer(text)

    return False

async def is_fav(uid, pid):
    async with pool.acquire() as conn:
        res = await conn.fetchval("""
            SELECT 1 FROM favorites 
            WHERE user_id=$1 AND product_id=$2
        """, uid, pid)

    return res is not None

async def t(uid, key):
    return TEXTS[await get_lang(uid)][key]

def is_text(message, key):
    if not message.text:
        return False
    return any(message.text == TEXTS[l][key] for l in TEXTS)


# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ГОРОДОВ ==========

async def get_user_city(uid: int) -> str | None:
    """Возвращает city из таблицы users (None если не выбран)."""
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT city FROM users WHERE user_id=$1", uid)


async def set_user_city(uid: int, city: str | None) -> None:
    """Сохраняет город пользователя."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET city=$1 WHERE user_id=$2",
            city, uid
        )


async def show_city_selection(target, uid: int, *, back_cb: str | None = None) -> None:
    """Показывает экран выбора города. Вызывается из /start и профиля."""
    text = await t(uid, "choose_city")
    kb = InlineKeyboardMarkup(row_width=1)
    for city_key, cfg in CITIES.items():
        kb.add(InlineKeyboardButton(
            f"🏙 {cfg['name']}",
            callback_data=f"city_select_{city_key}"
        ))
    if back_cb:
        kb.add(InlineKeyboardButton(await t(uid, "back"), callback_data=back_cb))
    await render(target, text, kb)


async def remind_city_if_needed(target, uid: int) -> bool:
    """
    Если город не выбран — показывает напоминание и возвращает True.
    Если город уже выбран — ничего не делает и возвращает False.
    """
    city = await get_user_city(uid)
    if city:
        return False
    text = await t(uid, "city_not_set_reminder")
    kb = InlineKeyboardMarkup(row_width=1)
    for city_key, cfg in CITIES.items():
        kb.add(InlineKeyboardButton(
            f"🏙 {cfg['name']}",
            callback_data=f"city_select_{city_key}"
        ))
    if isinstance(target, types.Message):
        await target.answer(text, reply_markup=kb)
    else:
        await render(target, text, kb)
    return True


# ========== STOCK: ГОРОД-СПЕЦИФИЧНЫЕ ХЕЛПЕРЫ ==========

async def get_stock_for_city(conn, product_id: int, city_key: str | None) -> int:
    """
    Возвращает остаток товара с учётом пула города.
    delivery → тот же пул, что и buerhausen (default).
    """
    pool_key = get_stock_pool(city_key)
    if pool_key == "default":
        return (await conn.fetchval(
            "SELECT stock FROM products WHERE id=$1", product_id
        )) or 0
    row = await conn.fetchrow(
        "SELECT stock FROM city_stock WHERE city_key=$1 AND product_id=$2",
        pool_key, product_id
    )
    return row["stock"] if row else 0


async def update_stock_for_city(conn, product_id: int, city_key: str | None, delta: int) -> None:
    """Изменяет остаток товара на delta (отрицательный = уменьшить)."""
    pool_key = get_stock_pool(city_key)
    if pool_key == "default":
        await conn.execute(
            "UPDATE products SET stock = GREATEST(stock + $1, 0) WHERE id=$2",
            delta, product_id
        )
    else:
        # upsert city_stock
        await conn.execute("""
            INSERT INTO city_stock (city_key, product_id, stock)
            VALUES ($1, $2, GREATEST($3, 0))
            ON CONFLICT (city_key, product_id) DO UPDATE
            SET stock = GREATEST(city_stock.stock + $3, 0)
        """, pool_key, product_id, delta)


async def set_stock_for_city(conn, product_id: int, city_key: str | None, value: int) -> None:
    """Устанавливает остаток товара явно."""
    pool_key = get_stock_pool(city_key)
    if pool_key == "default":
        await conn.execute(
            "UPDATE products SET stock = $1 WHERE id=$2", value, product_id
        )
    else:
        await conn.execute("""
            INSERT INTO city_stock (city_key, product_id, stock)
            VALUES ($1, $2, $3)
            ON CONFLICT (city_key, product_id) DO UPDATE
            SET stock = $3
        """, pool_key, product_id, value)


def get_rank(total_items):
    ranks = DISCOUNTS.get("rank", [])

    if not ranks:
        return {"key": "none", "name": {}, "need": 0, "value": 0.0}

    current = ranks[0]

    for r in ranks:
        if total_items >= r.get("need", 0):
            current = r

    return current

# Кеш file_id: url/path → telegram file_id.
# После первой отправки фото Telegram выдаёт file_id — последующие отправки
# идут по file_id (~3x быстрее, не нагружает Telegram API).
_photo_file_id_cache: dict[str, str] = {}


async def _send_photo_cached(send_fn, photo: str, **kwargs) -> types.Message:
    """
    Отправляет фото с кешированием file_id.
    send_fn — coroutine function (message.answer_photo, bot.send_photo, etc.)
    photo — URL или путь к файлу.
    """
    cached = _photo_file_id_cache.get(photo)
    sent = await send_fn(cached or photo, **kwargs)
    # Сохраняем file_id при первой отправке
    if not cached and sent and sent.photo:
        file_id = sent.photo[-1].file_id
        _photo_file_id_cache[photo] = file_id
    return sent


async def render(target, text, kb=None, photo=None, parse_mode="HTML"):
    try:
        if isinstance(target, types.Message):
            if photo:
                await _send_photo_cached(
                    target.answer_photo, photo,
                    caption=text, reply_markup=kb, parse_mode=parse_mode
                )
            else:
                await target.answer(text, reply_markup=kb, parse_mode=parse_mode)
            return

        msg = target.message

        if photo:
            await msg.delete()
            await _send_photo_cached(
                msg.answer_photo, photo,
                caption=text, reply_markup=kb, parse_mode=parse_mode
            )
        else:
            await msg.edit_text(text, reply_markup=kb, parse_mode=parse_mode)

    except Exception:
        try:
            await target.message.delete()
        except Exception:
            pass

        if photo:
            await _send_photo_cached(
                target.message.answer_photo, photo,
                caption=text, reply_markup=kb, parse_mode=parse_mode
            )
        else:
            await target.message.answer(text, reply_markup=kb, parse_mode=parse_mode)

async def notify_no_username(call_or_message, uid: int) -> None:
    """
    Если у пользователя нет username — отправляет дополнительное сообщение
    с предупреждением и кнопкой для связи с администратором.
    Вызывается ПОСЛЕ основного confirmation-сообщения.
    """
    username = call_or_message.from_user.username
    if username:
        return  # username есть — ничего не делаем
    warning_text = await t(uid, "no_username_warning")
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(
        await t(uid, "contact_admin_btn"),
        url="https://t.me/bizzshop_admin"
    ))
    try:
        await bot.send_message(uid, warning_text, reply_markup=kb)
    except Exception:
        pass
# ========== МЕНЮ ==========

def main_menu(lang):
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(
        TEXTS[lang]["shop"],
        TEXTS[lang]["cart"]
    )
    kb.add(
        TEXTS[lang]["profile"], 
        TEXTS[lang]["language"]
    )
    return kb
# ========== ПРОФИЛЬ ==========

async def render_profile(target):
    uid = target.from_user.id

    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT total_items, total_orders, total_saved,
                   promo_discount, promo_code, promo_type, city
            FROM users WHERE user_id=$1
        """, uid)

    items = row["total_items"] or 0
    orders = row["total_orders"] or 0
    saved = row["total_saved"] or 0
    promo_discount = row["promo_discount"] or 0
    promo_code = row["promo_code"]
    promo_type = row["promo_type"]
    user_city = row["city"]

    lang = await get_lang(uid)
    rank = get_rank(items)
    rank_name = rank["name"][lang]

    total_discount = await calculate_total_discount(uid, 1)

    # Строка города
    if user_city and user_city in CITIES:
        city_line = (await t(uid, "profile_city_row")).format(city=CITIES[user_city]["name"])
    else:
        city_line = await t(uid, "profile_city_none")

    text = (
        f"{await t(uid,'profile_title')}\n\n"
        f"{rank_name}\n\n"
        f"{city_line}\n\n"
        f"📦 {await t(uid,'profile_items')}: {items}\n"
        f"🧾 {await t(uid,'profile_orders')}: {orders}\n"
        f"💸 {await t(uid,'profile_saved')}: {saved:.2f}€\n"
    )

    # Строка о промокоде в профиле
    if promo_code and promo_type == "discount" and promo_discount > 0:
        text += "\n" + (await t(uid, "promo_in_profile_discount")).format(
            code=promo_code, value=promo_discount
        ) + "\n"
    elif promo_code and promo_type == "free_jar":
        text += "\n" + (await t(uid, "promo_in_profile_free_jar")).format(
            code=promo_code
        ) + "\n"

    text += f"\n💸 {await t(uid,'profile_discount')}: {total_discount}€"

    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton(await t(uid,"history"), callback_data="profile_history"),
        InlineKeyboardButton(await t(uid,"levels"), callback_data="profile_levels"),
    )
    kb.add(
        InlineKeyboardButton(await t(uid,"roulette"), callback_data="profile_roulette"),
        InlineKeyboardButton(await t(uid,"streak"), callback_data="profile_streak"),
    )
    kb.add(
        InlineKeyboardButton(await t(uid,"discounts"), callback_data="profile_discounts"),
        InlineKeyboardButton(await t(uid,"ref"), callback_data="profile_ref"),
    )
    kb.add(
        InlineKeyboardButton(await t(uid,"favorites"), callback_data="profile_fav"),
        InlineKeyboardButton(await t(uid,"stats"), callback_data="profile_stats"),
    )
    kb.add(
        InlineKeyboardButton(
            await t(uid, "profile_change_city"),
            callback_data="profile_change_city"
        )
    )
    kb.add(
        InlineKeyboardButton(await t(uid,"to_shop"), callback_data="back_shop"),
    )

    await render(target, text, kb)

@dp.message_handler(lambda m: is_text(m,"profile"))
async def profile(message: types.Message):
    await render_profile(message)

@dp.callback_query_handler(lambda c: c.data == "profile")
async def profile_cb(call):
    await render_profile(call)


# ========== ИЗБРАННОЕ ==========

@dp.callback_query_handler(lambda c: c.data == "profile_fav")
async def favorites(call):
    uid = call.from_user.id

    async with pool.acquire() as conn:
        favs = await conn.fetch("""
            SELECT p.id, p.name_ru, p.name_ua, p.name_de
            FROM favorites f
            JOIN products p ON f.product_id = p.id
            WHERE f.user_id=$1
            ORDER BY f.position
        """, uid)

    kb = InlineKeyboardMarkup()

    if not favs:
        kb.add(InlineKeyboardButton(await t(uid,"back"), callback_data="profile"))

        await call.message.edit_text(
            await t(uid,"no_favorites"),
            reply_markup=kb
        )
        return

    text = await t(uid, "favorites") + ":\n\n"
    lang = await get_lang(uid)

    for pid, n_ru, n_ua, n_de in favs:
        name = {"ru": n_ru, "ua": n_ua, "de": n_de}[lang]
        text += f"• {name}\n"

        kb.add(InlineKeyboardButton(name, callback_data=f"remove_fav_{pid}"))

    kb.add(InlineKeyboardButton(await t(uid,"back"), callback_data="profile"))

    await call.message.edit_text(
        text + "\n" + await t(uid,"fav_hint"),
        reply_markup=kb
    )

@dp.callback_query_handler(lambda c: c.data.startswith("remove_fav_"))
async def remove_fav(call):
    uid = call.from_user.id
    pid = int(call.data.split("_")[2])

    async with pool.acquire() as conn:
        await conn.execute("""
            DELETE FROM favorites 
            WHERE user_id=$1 AND product_id=$2
        """, uid, pid)

        product = await conn.fetchrow("""
            SELECT name_ru, name_ua, name_de, price 
            FROM products WHERE id=$1
        """, pid)

    name = {
        "ru": product["name_ru"],
        "ua": product["name_ua"],
        "de": product["name_de"]
    }[await get_lang(uid)]

    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton(
            await t(uid,"fav_restore"),
            callback_data=f"restore_fav_{pid}"
        )
    )
    kb.add(
        InlineKeyboardButton(await t(uid,"back"), callback_data="profile_fav")
    )

    await call.message.edit_text(
        f"{await t(uid,'fav_removed')}\n\n{name}",
        reply_markup=kb
    )

@dp.callback_query_handler(lambda c: c.data.startswith("restore_fav_"))
async def restore_fav(call):
    uid = call.from_user.id
    pid = int(call.data.split("_")[2])

    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO favorites (user_id, product_id)
            VALUES ($1, $2)
            ON CONFLICT DO NOTHING
        """, uid, pid)

    await favorites(call)

# ========== ИСТОРИЯ ЗАКАЗОВ ==========
@dp.callback_query_handler(lambda c: c.data == "profile_history")
async def profile_history(call):
    uid = call.from_user.id

    async with pool.acquire() as conn:
        orders = await conn.fetch("""
            SELECT id, total, created_at
            FROM orders
            WHERE user_id=$1
            ORDER BY created_at DESC
            LIMIT 5
        """, uid)

    kb = InlineKeyboardMarkup()

    if not orders:
        text = (
            f"{await t(uid,'history_title')}\n\n"
            f"{await t(uid,'history_empty')}"
        )
        kb.add(InlineKeyboardButton(await t(uid,"back"), callback_data="profile"))
        await render(call, text, kb)
        return

    text = f"{await t(uid,'history_title')}:\n\n"

    for order in orders:
        oid = order["id"]
        total = order["total"]
        date_str = order["created_at"].strftime('%d.%m.%Y')

        row_line = (await t(uid, "history_order_row")).format(
            id=oid, date=date_str, total=total
        )
        text += f"{row_line}\n"

        kb.add(
            InlineKeyboardButton(
                f"{await t(uid,'open_order')} #{oid}",
                callback_data=f"order_{oid}"
            )
        )

    kb.add(
        InlineKeyboardButton(await t(uid,"to_shop"), callback_data="back_shop"),
        InlineKeyboardButton(await t(uid,"back"), callback_data="profile")
    )

    await render(call, text, kb)
    
@dp.callback_query_handler(lambda c: c.data.startswith("order_"))
async def open_order(call):
    uid = call.from_user.id
    oid = int(call.data.split("_")[1])

    async with pool.acquire() as conn:
        order = await conn.fetchrow("""
            SELECT items, total, payment, created_at
            FROM orders
            WHERE id=$1 AND user_id=$2
        """, oid, uid)

    if not order:
        return

    items = order["items"]
    total = order["total"]
    payment = order["payment"]
    date = order["created_at"].strftime('%d.%m.%Y')

    text = f"🧾 {await t(uid,'order')} #{oid}\n"
    text += f"{date}\n\n"

    async with pool.acquire() as conn:
        for item in items.split(","):
            pid, qty = item.split(":")
            pid = int(pid)
            qty = int(qty)

            product = await conn.fetchrow("""
                SELECT name_ru, name_ua, name_de
                FROM products
                WHERE id=$1
            """, pid)

            lang = await get_lang(uid)
            name = product[f"name_{lang}"]

            text += f"• {name} x{qty}\n"

    text += f"\n💰 {total}€\n"
    text += f"💳 {payment}"

    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton(
            await t(uid,"repeat_order"),
            callback_data=f"repeat_{oid}"
        )
    )
    kb.add(
        InlineKeyboardButton(
            await t(uid,"back"),
            callback_data="profile_history"
        )
    )

    await render(call, text, kb)

@dp.callback_query_handler(lambda c: c.data.startswith("repeat_"))
async def repeat_order(call):
    if not await check_not_banned(call):
        return

    uid = call.from_user.id
    oid = int(call.data.split("_")[1])

    async with pool.acquire() as conn:
        order = await conn.fetchrow(
            "SELECT items, is_delivery FROM orders WHERE id=$1", oid
        )

    if not order:
        return

    items_str = order["items"]

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM cart WHERE user_id=$1", uid)
        await conn.execute("UPDATE users SET cart_mode='pickup' WHERE user_id=$1", uid)

        for item in items_str.split(","):
            pid, qty = item.split(":")
            pid, qty = int(pid), int(qty)
            await conn.execute("""
                INSERT INTO cart (user_id, product_id, quantity, cart_mode)
                VALUES ($1, $2, $3, 'pickup')
            """, uid, pid, qty)

    await render_cart(call, uid)

# ========== РАНГИ ==========

@dp.callback_query_handler(lambda c: c.data == "profile_levels")
async def profile_levels(call):
    uid = call.from_user.id
    lang = await get_lang(uid)

    async with pool.acquire() as conn:
        total_items = await conn.fetchval(
            "SELECT total_items FROM users WHERE user_id=$1",
            uid
        )

    current = get_rank(total_items)

    text = f"{await t(uid,'levels')}:\n\n"

    for r in DISCOUNTS["rank"]:
        name = r["name"][lang]
        need = r["need"]
        value = r["value"]

        marker = "👉 " if r["key"] == current["key"] else ""

        if value > 0:
            text += f"{marker}{name}  -{value}€\n"
        else:
            text += f"{marker}{name}\n"

        text += f"{await t(uid,'from_jars')} {need}\n\n"

    next_rank = None
    for r in DISCOUNTS["rank"]:
        if total_items < r["need"]:
            next_rank = r
            break

    text += f"{await t(uid,'discount_all_items')}\n"

    if next_rank:
        need_left = next_rank["need"] - total_items
        progress = int((total_items / next_rank["need"]) * 10)

        bar = "🟩" * progress + "⬜" * (10 - progress)

        text += f"🎯 {bar}\n"
        text += f"{await t(uid,'next_rank')}: {need_left}"
    else:
        text += "🎯 🟩🟩🟩🟩🟩🟩🟩🟩🟩🟩"

    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton(await t(uid,"to_shop"), callback_data="back_shop"),
        InlineKeyboardButton(await t(uid,"back"), callback_data="profile")
    )

    await call.message.edit_text(text, reply_markup=kb)

# ========== BUY STREAK ==========

@dp.callback_query_handler(lambda c: c.data == "profile_streak")
async def profile_streak(call):
    uid = call.from_user.id

    weeks, days_left, max_weeks, frozen = await _streak_snapshot(uid)
    value = get_streak_discount_value(weeks)

    text = f"{await t(uid,'streak')}\n\n"

    if frozen:
        text += await t(uid, "streak_frozen_banner") + "\n\n"

    text += (await t(uid, "streak_current_weeks")).format(weeks=weeks) + "\n\n"

    text += await t(uid, "streak_rewards_title") + "\n"
    for level in DISCOUNTS["streak"]:
        lw = level["weeks"]
        lv = level["value"]
        row = (await t(uid, "streak_week_row")).format(weeks=lw, value=lv)
        if weeks >= lw:
            marker = "✅ "
        elif weeks == lw - 1:
            marker = "👉 "
        else:
            marker = "🔒 "
        text += f"{marker}{row}\n"

    text += "\n"
    text += (await t(uid, "streak_max_weeks")).format(weeks=max_weeks) + "\n"

    if weeks > 0:
        days_left_int = int(round(days_left))
        if frozen:
            text += (await t(uid, "streak_frozen_footer_days")).format(days=days_left_int)
        else:
            text += (await t(uid, "streak_days_left")).format(days=days_left_int)
    else:
        text += await t(uid, "streak_inactive_hint")

    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton(await t(uid,"to_shop"), callback_data="back_shop"),
        InlineKeyboardButton(await t(uid,"back"), callback_data="profile")
    )

    await render(call, text, kb)

# ========== СКИДКИ ==========

@dp.callback_query_handler(lambda c: c.data == "profile_discounts")
async def profile_discounts_screen(call):
    uid = call.from_user.id

    discounts = await get_user_discounts(uid)
    by_type = {d["type"]: d["value"] for d in discounts}

    total_now = await calculate_total_discount(uid, 1)

    async with pool.acquire() as conn:
        total_saved = await conn.fetchval(
            "SELECT total_saved FROM users WHERE user_id=$1", uid
        )
    total_saved = total_saved or 0

    text = f"{await t(uid,'discounts_title')}\n\n"
    text += (await t(uid, "discounts_total_header")).format(value=total_now) + "\n\n"

    rows = [
        ("rank",      "discount_label_rank"),
        ("streak",    "discount_label_streak"),
        ("ref",       "discount_label_ref"),
        ("ref_bonus", "discount_label_ref_bonus"),
        ("wheel",     "discount_label_wheel"),
    ]

    for key, label_key in rows:
        label = await t(uid, label_key)
        if key in by_type and by_type[key] > 0:
            text += f"✅ {label}: -{by_type[key]}€\n"
        else:
            text += f"❌ {label}\n"

    text += "\n" + (await t(uid, "discounts_total_saved")).format(value=round(total_saved, 2))

    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton(await t(uid,"to_shop"), callback_data="back_shop"),
        InlineKeyboardButton(await t(uid,"back"), callback_data="profile")
    )

    await render(call, text, kb)

# ========== РЕФЕРАЛКА ==========

@dp.callback_query_handler(lambda c: c.data == "profile_ref")
async def profile_ref(call):
    if not await check_not_banned(call):
        return

    uid = call.from_user.id

    username = await get_bot_username()
    ref_link = f"https://t.me/{username}?start=ref_{uid}"

    async with pool.acquire() as conn:
        invited_count = await conn.fetchval(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id=$1", uid
        )
        ref_earned = await conn.fetchval(
            "SELECT ref_earned FROM users WHERE user_id=$1", uid
        )
    ref_earned = ref_earned or 0

    rules = (await t(uid, "ref_rules")).format(
        inviter=DISCOUNTS["ref"]["inviter"],
        new_user=DISCOUNTS["ref"]["new_user"]
    )

    text = (
        f"{await t(uid,'ref_title')}\n\n"
        f"{await t(uid,'ref_your_link')}\n"
        f"{ref_link}\n\n"
        f"{rules}\n\n"
        f"{(await t(uid,'ref_invited_count')).format(count=invited_count)}\n"
        f"{(await t(uid,'ref_earned')).format(value=round(ref_earned, 2))}"
    )

    share_text = await t(uid, "ref_share_text")
    share_url = f"https://t.me/share/url?url={quote(ref_link)}&text={quote(share_text)}"

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(await t(uid, "ref_share_button"), url=share_url))
    kb.add(
        InlineKeyboardButton(await t(uid,"to_shop"), callback_data="back_shop"),
        InlineKeyboardButton(await t(uid,"back"), callback_data="profile")
    )

    await render(call, text, kb)

# ========== СТАТИСТИКА ПРОФИЛЯ ==========

@dp.callback_query_handler(lambda c: c.data == "profile_stats")
async def profile_stats(call):
    uid = call.from_user.id
    lang = await get_lang(uid)

    async with pool.acquire() as conn:
        user = await conn.fetchrow("""
            SELECT total_items, total_orders, total_spent, total_saved,
                   max_streak_weeks, spin_count
            FROM users WHERE user_id=$1
        """, uid)

        first_order_date = await conn.fetchval("""
            SELECT MIN(created_at) FROM orders
            WHERE user_id=$1 AND status='confirmed'
        """, uid)

        invited_count = await conn.fetchval(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id=$1", uid
        )

        order_rows = await conn.fetch("""
            SELECT items FROM orders
            WHERE user_id=$1 AND status='confirmed'
        """, uid)

    product_counts = {}
    for row in order_rows:
        for part in row["items"].split(","):
            if not part:
                continue
            pid_str, qty_str = part.split(":")
            pid = int(pid_str)
            qty = int(qty_str)
            product_counts[pid] = product_counts.get(pid, 0) + qty

    top_line = await t(uid, "stats_top_product_none")

    if product_counts:
        top_pid = max(product_counts, key=product_counts.get)
        top_qty = product_counts[top_pid]

        async with pool.acquire() as conn:
            product = await conn.fetchrow("""
                SELECT name_ru, name_ua, name_de FROM products WHERE id=$1
            """, top_pid)

        if product:
            top_name = {
                "ru": product["name_ru"],
                "ua": product["name_ua"],
                "de": product["name_de"]
            }[lang]
            top_line = (await t(uid, "stats_top_product")).format(
                name=top_name, count=top_qty
            )

    rank = get_rank(user["total_items"])
    rank_name = rank["name"][lang]

    first_order_text = (
        first_order_date.strftime("%d.%m.%Y") if first_order_date else "—"
    )

    total_spent = round(user["total_spent"] or 0, 2)
    total_saved = round(user["total_saved"] or 0, 2)
    max_streak = user["max_streak_weeks"] or 0
    spin_count = user["spin_count"] or 0

    text = f"{await t(uid,'stats_title')}\n\n"
    text += (await t(uid,"stats_total_orders")).format(count=user["total_orders"]) + "\n"
    text += (await t(uid,"stats_first_order")).format(date=first_order_text) + "\n"
    text += (await t(uid,"stats_rank")).format(rank=rank_name) + "\n"
    text += top_line + "\n"
    text += (await t(uid,"stats_max_streak")).format(weeks=max_streak) + "\n"
    text += (await t(uid,"stats_spins")).format(count=spin_count) + "\n"
    text += (await t(uid,"stats_invited")).format(count=invited_count) + "\n"
    text += (await t(uid,"stats_total_spent")).format(value=total_spent) + "\n"
    text += (await t(uid,"stats_total_saved")).format(value=total_saved)

    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton(await t(uid,"to_shop"), callback_data="back_shop"),
        InlineKeyboardButton(await t(uid,"back"), callback_data="profile")
    )

    await render(call, text, kb)

# ========== РУЛЕТКА ==========

@dp.callback_query_handler(lambda c: c.data == "profile_roulette")
async def profile_roulette(call):
    if not await check_not_banned(call):
        return

    uid = call.from_user.id

    async with pool.acquire() as conn:
        user = await conn.fetchrow("""
            SELECT spin_progress, current_discount, free_jar_bonus
            FROM users
            WHERE user_id=$1
        """, uid)

    spin_progress = user["spin_progress"]
    current_discount = user["current_discount"]
    free_jar_bonus = user["free_jar_bonus"]

    need = 5
    left = need - spin_progress
    if left < 0:
        left = 0

    progress = int((spin_progress / need) * 10)
    if progress > 10:
        progress = 10

    bar = "🟩" * progress + "⬜" * (10 - progress)

    text = f"{await t(uid,'roulette')}\n\n"
    text += f"{await t(uid,'roulette_prizes')}\n"
    text += "• -1€\n"
    text += "• -1.5€\n"
    text += "• -2€\n"
    text += "• -3€\n"
    text += f"• {await t(uid,'free_jar')}\n\n"

    if free_jar_bonus:
        text += f"{await t(uid,'free_jar_active')}\n"
    elif current_discount and current_discount > 0:
        text += f"{await t(uid,'active_bonus')}: -{current_discount}€\n"

    text += f"{await t(uid,'wheel_next_order_note')}\n\n"

    if spin_progress >= need:
        text += f"{await t(uid,'roulette_ready')}\n"
    else:
        text += f"{await t(uid,'roulette_left')}: {left}\n"

    text += f"🎯 {bar}"

    kb = InlineKeyboardMarkup()

    if free_jar_bonus:
        kb.add(
            InlineKeyboardButton(
                await t(uid, "claim_gift"),
                callback_data="open_gift_shop"
            )
        )
    elif spin_progress >= need:
        kb.add(
            InlineKeyboardButton(
                await t(uid,"spin_now"),
                callback_data="spin_now"
            )
        )
    else:
        kb.add(
            InlineKeyboardButton(
                await t(uid,"spin_locked"),
                callback_data="noop"
            )
        )

    kb.add(
        InlineKeyboardButton(await t(uid,"to_shop"), callback_data="back_shop"),
        InlineKeyboardButton(await t(uid,"back"), callback_data="profile")
    )

    await render(call, text, kb)

@dp.callback_query_handler(lambda c: c.data == "spin_now")
async def spin_now(call):
    if not await check_not_banned(call):
        return

    uid = call.from_user.id

    async with pool.acquire() as conn:
        user = await conn.fetchrow("""
            SELECT current_discount, spin_progress, free_jar_bonus
            FROM users
            WHERE user_id=$1
        """, uid)

        current_discount = user["current_discount"] or 0
        spin_progress = user["spin_progress"] or 0
        free_jar_bonus = user["free_jar_bonus"] or 0

        # ❗ если уже есть денежный бонус — стопаем
        if current_discount > 0:
            await call.answer(await t(uid, "spin_bonus_exists"), show_alert=True)
            return

        # ❗ если уже есть бонус бесплатной банки — стопаем
        if free_jar_bonus:
            await call.answer(await t(uid, "spin_free_jar_exists"), show_alert=True)
            return

        prizes = DISCOUNTS["wheel"]
        weights = [p["chance"] for p in prizes]
        prize = random.choices(prizes, weights=weights, k=1)[0]

        win_value = prize["value"]

    # ── Анимация (до записи в БД — вне транзакции, чтобы не держать соединение) ──
    spinning_label = await t(uid, "spin_spinning")
    await _run_roulette_animation(call, prize, spinning_label)

    async with pool.acquire() as conn:
        if prize["key"] == "free":
            # Бесплатная банка — отдельный бонус, не скидка
            await conn.execute("""
                UPDATE users
                SET free_jar_bonus = 1,
                    spin_progress = GREATEST(spin_progress - 5, 0),
                    spin_count = spin_count + 1
                WHERE user_id=$1
            """, uid)
        else:
            # Обычная скидка на следующий заказ
            await conn.execute("""
                UPDATE users
                SET current_discount=$1,
                    spin_progress=GREATEST(spin_progress - 5, 0),
                    spin_count = spin_count + 1
                WHERE user_id=$2
            """, win_value, uid)

    # текст результата
    if prize["key"] == "free":
        text = await t(uid, "spin_win_free")
    else:
        text = (await t(uid, "spin_win_discount")).format(value=win_value)

    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton(await t(uid, "back"), callback_data="profile_roulette")
    )

    await render(call, text, kb)

# ========== ПОДАРОК (БЕСПЛАТНАЯ БАНКА) ==========

@dp.callback_query_handler(lambda c: c.data == "open_gift_shop")
async def open_gift_shop(call):
    if not await check_not_banned(call):
        return

    uid = call.from_user.id

    async with pool.acquire() as conn:
        bonus = await conn.fetchval(
            "SELECT free_jar_bonus FROM users WHERE user_id=$1", uid
        )

    if not bonus:
        await call.answer(await t(uid, "gift_already_used"), show_alert=True)
        return

    # Сразу показываем выбор категории (самовывоз)
    await render_category_selection_gift(call, uid)



async def render_category_selection_gift(target, uid, is_delivery: bool = False):
    """Выбор раздела каталога для бесплатной банки."""
    text = await t(uid, "choose_section")
    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton(await t(uid, "section_elfliq"),   callback_data="gift_cat_elfliq"),
        InlineKeyboardButton(await t(uid, "section_elfworld"), callback_data="gift_cat_elfworld"),
    )
    kb.add(InlineKeyboardButton(await t(uid, "back"), callback_data="open_gift_shop"))

    await render(target, text, kb)



@dp.callback_query_handler(lambda c: c.data.startswith("gift_cat_"))
async def gift_category(call):
    if not await check_not_banned(call):
        return

    uid = call.from_user.id

    async with pool.acquire() as conn:
        bonus = await conn.fetchval(
            "SELECT free_jar_bonus FROM users WHERE user_id=$1", uid
        )

    if not bonus:
        await call.answer(await t(uid, "gift_already_used"), show_alert=True)
        return

    category = call.data.split("gift_cat_")[1]
    await render_gift_shop(call, uid, category)




async def render_gift_shop(target, uid, category, is_delivery: bool = False):
    async with pool.acquire() as conn:
        products = await conn.fetch(
            "SELECT id, name_ru, name_ua, name_de FROM products WHERE in_stock=1 AND category=$1",
            category
        )

    lang = await get_lang(uid)
    section_key = "section_elfliq" if category == "elfliq" else "section_elfworld"

    text = f"{await t(uid, section_key)}\n\n"
    text += await t(uid, "choose_gift") + "\n\n"


    kb = InlineKeyboardMarkup()

    if not products:
        text += await t(uid, "section_empty") + "\n"

    prefix = "gift_view_"
    for p in products:
        pid = p["id"]
        name = p[f"name_{lang}"]
        text += f"{name}\n"
        kb.add(InlineKeyboardButton(name, callback_data=f"{prefix}{pid}"))

    back_cb = "open_gift_shop"
    kb.add(InlineKeyboardButton(await t(uid, "back"), callback_data=back_cb))

    await render(target, text, kb)

@dp.callback_query_handler(lambda c: c.data.startswith("gift_view_"))
async def gift_view(call):
    if not await check_not_banned(call):
        return

    uid = call.from_user.id
    pid = int(call.data.split("_")[2])

    async with pool.acquire() as conn:
        bonus = await conn.fetchval(
            "SELECT free_jar_bonus FROM users WHERE user_id=$1", uid
        )

    if not bonus:
        await call.answer(await t(uid, "gift_already_used"), show_alert=True)
        return

    async with pool.acquire() as conn:
        p = await conn.fetchrow("SELECT * FROM products WHERE id=$1", pid)

    lang = await get_lang(uid)
    name = p[f"name_{lang}"]
    desc = p[f"desc_{lang}"]
    img = p["image"]
    category = p["category"] or "elfliq"

    text = f"{name}\n\n{desc}"

    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("⬅️", callback_data=f"gift_cat_{category}"),
        InlineKeyboardButton(await t(uid, "select_gift_btn"), callback_data=f"gift_confirm_{pid}")
    )

    await render(call, text, kb, photo=img)


@dp.callback_query_handler(lambda c: c.data.startswith("gift_confirm_"))
async def gift_confirm(call):
    if not await check_not_banned(call):
        return

    uid = call.from_user.id
    pid = int(call.data.split("_")[2])

    async with pool.acquire() as conn:
        bonus = await conn.fetchval(
            "SELECT free_jar_bonus FROM users WHERE user_id=$1", uid
        )

    if not bonus:
        await call.answer(await t(uid, "gift_already_used"), show_alert=True)
        return

    # Проверяем наличие выбранного товара по городу пользователя
    user_city = await get_user_city(uid)
    async with pool.acquire() as conn:
        p = await conn.fetchrow(
            "SELECT name_ru, name_ua, name_de FROM products WHERE id=$1", pid
        )
        stock = await get_stock_for_city(conn, pid, user_city)

    if not p or stock <= 0:
        await call.answer(
            (await t(uid, "stock_out")).format(name=p["name_ru"] if p else f"#{pid}"),
            show_alert=True
        )
        return

    lang = await get_lang(uid)
    name = p[f"name_{lang}"]

    text = (
        f"{await t(uid, 'gift_confirm_title')}\n\n"
        f"{name}\n\n"
        f"{await t(uid, 'gift_confirm_question')}"
    )

    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton(await t(uid, "confirm"), callback_data=f"gift_apply_{pid}"),
        InlineKeyboardButton(await t(uid, "gift_cancel"), callback_data=f"gift_view_{pid}")
    )

    await render(call, text, kb)


@dp.callback_query_handler(lambda c: c.data.startswith("gift_apply_"))
async def gift_apply(call):
    if not await check_not_banned(call):
        return

    uid = call.from_user.id
    pid = int(call.data.split("_")[2])
    username = call.from_user.username or "unknown"

    user_city = await get_user_city(uid)
    resolved_city = get_order_city(user_city)
    pool_key = get_stock_pool(user_city)
    lang = await get_lang(uid)

    async with pool.acquire() as conn:
        p = await conn.fetchrow("SELECT name_ru, name_ua, name_de FROM products WHERE id=$1", pid)
    if not p:
        await call.answer(await t(uid, "gift_already_used"), show_alert=True)
        return

    product_name = p["name_ru"]
    name = p[f"name_{lang}"]

    # Атомарная транзакция: FOR UPDATE на stock + списание бонуса + резервирование
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                # Блокируем строку stock
                if pool_key == "default":
                    avail = await conn.fetchval(
                        "SELECT stock FROM products WHERE id=$1 FOR UPDATE", pid
                    )
                else:
                    avail = await conn.fetchval(
                        "SELECT stock FROM city_stock WHERE city_key=$1 AND product_id=$2 FOR UPDATE",
                        pool_key, pid
                    ) or 0

                if not avail or avail <= 0:
                    raise Exception("stock_out")

                # Атомарно списываем бонус
                updated = await conn.fetchval("""
                    UPDATE users SET free_jar_bonus = 0
                    WHERE user_id=$1 AND free_jar_bonus = 1
                    RETURNING user_id
                """, uid)
                if not updated:
                    raise Exception("bonus_used")

                # Создаём заявку
                request_id = await conn.fetchval("""
                    INSERT INTO gift_requests (user_id, product_id, username)
                    VALUES ($1, $2, $3) RETURNING id
                """, uid, pid, username)

                # Уменьшаем stock
                await update_stock_for_city(conn, pid, user_city, -1)

                # Резерв
                await conn.execute("""
                    INSERT INTO reserved_stock (order_id, product_id, quantity, city_key)
                    VALUES ($1, $2, 1, $3)
                    ON CONFLICT (order_id, product_id) DO UPDATE
                    SET quantity = 1, city_key = EXCLUDED.city_key
                """, -request_id, pid, resolved_city)

        except Exception as e:
            if "stock_out" in str(e):
                await call.answer(
                    (await t(uid, "stock_out")).format(name=product_name), show_alert=True
                )
            elif "bonus_used" in str(e):
                await call.answer(await t(uid, "gift_already_used"), show_alert=True)
            else:
                logger.error("gift_apply error uid=%s pid=%s: %s", uid, pid, e)
                await alert_super_admins(f"gift_apply: ошибка uid={uid} pid={pid}: {e}")
            return

    name_ru = p["name_ru"]

    admin_text = (
        f"🎁 Бесплатная банка\n\n"
        f"🏙 Город: {CITIES.get(resolved_city, {}).get('name', resolved_city)}\n"
        f"Пользователь: @{username}\n\n"
        f"Выбранный товар:\n{name_ru}"
    )

    admin_kb = InlineKeyboardMarkup()
    admin_kb.add(
        InlineKeyboardButton("✅ Выдано", callback_data=f"gift_issued_{request_id}"),
        InlineKeyboardButton("❌ Отменить", callback_data=f"gift_rejected_{request_id}")
    )

    # Рассылаем городским админам и запоминаем message_id
    msg_ids = []
    for admin_id in city_admin_ids:
        try:
            sent = await bot.send_message(admin_id, admin_text, reply_markup=admin_kb)
            msg_ids.append(f"{admin_id}:{sent.message_id}")
        except Exception:
            pass

    # Сохраняем message_ids и city_key в заявке
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE gift_requests SET admin_message_ids=$1, city_key=$2 WHERE id=$3
        """, ",".join(msg_ids), resolved_city, request_id)

    # Подтверждение пользователю
    await render(call, await t(uid, "gift_done"))
    await notify_no_username(call, uid)



@dp.callback_query_handler(lambda c: c.data.startswith("gift_issued_"))
async def gift_issued(call):
    if not is_admin(call.from_user.id):
        await call.answer("❌ Нет доступа", show_alert=True)
        return
    request_id = int(call.data.split("_")[2])
    actor_id = call.from_user.id
    admin_username = call.from_user.username or "admin"

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, admin_message_ids FROM gift_requests WHERE id=$1", request_id
        )

    if not row or row["status"] != "pending":
        await call.answer("Заявка уже обработана", show_alert=True)
        return

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE gift_requests SET status='issued' WHERE id=$1", request_id
        )
        # Финализируем резерв (stock уже уменьшен при оформлении)
        await finalize_reserved_stock(conn, -request_id)

    await call.answer("✅ Выдано")

    await _sync_admin_messages(
        msg_ids_raw=row["admin_message_ids"],
        actor_id=actor_id,
        base_text=call.message.text,
        status_self="\n\n✅ ВЫДАНО",
        status_others=f"\n\n✅ ВЫДАНО @{admin_username}"
    )

@dp.callback_query_handler(lambda c: c.data.startswith("gift_rejected_"))
async def gift_rejected(call):
    if not is_admin(call.from_user.id):
        await call.answer("❌ Нет доступа", show_alert=True)
        return
    request_id = int(call.data.split("_")[2])
    actor_id = call.from_user.id
    admin_username = call.from_user.username or "admin"

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, admin_message_ids FROM gift_requests WHERE id=$1", request_id
        )

    if not row or row["status"] != "pending":
        await call.answer("Заявка уже обработана", show_alert=True)
        return

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE gift_requests SET status='rejected' WHERE id=$1", request_id
        )
        # Возвращаем зарезервированный stock
        await release_reserved_stock(conn, -request_id)

    # Бонус НЕ возвращается — по ТЗ
    await call.answer("❌ Отменено")

    await _sync_admin_messages(
        msg_ids_raw=row["admin_message_ids"],
        actor_id=actor_id,
        base_text=call.message.text,
        status_self="\n\n❌ ОТМЕНЕНО",
        status_others=f"\n\n❌ ОТМЕНЕНО @{admin_username}"
    )

# ========== ОБНОВЛЕНИЕ СТАТИСТИКИ ==========

async def update_user_stats(uid, order_items, total):
    total_count = sum(qty for _, qty in order_items)

    async with pool.acquire() as conn:
        user = await conn.fetchrow("""
            SELECT total_items, total_orders, saved_money
            FROM users WHERE user_id=$1
        """, uid)

        old_items = user["total_items"] or 0
        old_orders = user["total_orders"] or 0
        saved = user["saved_money"] or 0

        new_items = old_items + total_count
        new_orders = old_orders + 1

        rank = get_rank(new_items)

        saved += total * (rank["discount"] / 100)

        await conn.execute("""
            UPDATE users
            SET total_items=$1,
                total_orders=$2,
                level=$3,
                discount=$4,
                saved_money=$5
            WHERE user_id=$6
        """, new_items, new_orders, rank["key"], rank["discount"], saved, uid)

    return rank

# ========== СТАРТ ==========

@dp.message_handler(commands=['start'])
async def start(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    args = message.get_args()  # например "ref_123456"

    async with pool.acquire() as conn:
        result = await conn.execute("""
        INSERT INTO users (user_id)
        VALUES ($1)
        ON CONFLICT (user_id) DO NOTHING
        """, uid)

        is_new_user = result == "INSERT 0 1"

        # Обновляем username при каждом /start — нужен для /ban и /unban
        await conn.execute(
            "UPDATE users SET username=$1 WHERE user_id=$2",
            message.from_user.username, uid
        )

        user_row = await conn.fetchrow(
            "SELECT language, city FROM users WHERE user_id=$1", uid
        )
        has_lang = bool(user_row and user_row["language"])
        has_city = bool(user_row and user_row["city"])

        if is_new_user and args and args.startswith("ref_"):
            try:
                inviter_id = int(args[4:])
            except ValueError:
                inviter_id = None

            if inviter_id and inviter_id != uid:
                # Заблокированный пригласивший не может приводить новых
                # пользователей — реферальная система для него не работает.
                inviter_exists = await conn.fetchval(
                    "SELECT 1 FROM users WHERE user_id=$1 AND NOT COALESCE(banned, false)",
                    inviter_id
                )

                if inviter_exists:
                    await conn.execute("""
                        INSERT INTO referrals (referrer_id, new_user_id, activated)
                        VALUES ($1, $2, 0)
                        ON CONFLICT DO NOTHING
                    """, inviter_id, uid)

                    await conn.execute("""
                        UPDATE users SET ref_bonus=$1 WHERE user_id=$2
                    """, DISCOUNTS["ref"]["new_user"], uid)

    if not has_lang:
        # Новый пользователь — сначала выбор языка, потом город
        kb = ReplyKeyboardMarkup(resize_keyboard=True)
        kb.add("🇷🇺 Русский", "🇺🇦 Українська", "🇩🇪 Deutsch")
        await message.answer("🌍", reply_markup=kb)
        return

    if not has_city:
        # Язык есть, города нет — показываем напоминание и выбор города
        await remind_city_if_needed(message, uid)
        return

    # Всё есть — показываем меню
    lang = await get_lang(uid)
    await message.answer(TEXTS[lang]["menu"], reply_markup=main_menu(lang))


# ========== ЯЗЫК ==========

@dp.message_handler(lambda m: m.text in ["🇷🇺 Русский","🇺🇦 Українська","🇩🇪 Deutsch"])
async def set_lang(message: types.Message):
    uid = message.from_user.id

    if "Рус" in message.text:
        lang = "ru"
    elif "Укра" in message.text:
        lang = "ua"
    else:
        lang = "de"

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET language=$1 WHERE user_id=$2",
            lang, uid
        )

    # После выбора языка — проверяем, выбран ли город
    city = await get_user_city(uid)
    if not city:
        await message.answer("✅", reply_markup=ReplyKeyboardRemove())
        # Показываем выбор города (через inline — не ломает reply-клавиатуру)
        await show_city_selection(message, uid)
    else:
        await message.answer("✅", reply_markup=ReplyKeyboardRemove())
        await message.answer(TEXTS[lang]["menu"], reply_markup=main_menu(lang))

@dp.message_handler(lambda m: is_text(m,"language"))
async def change_lang(message: types.Message):
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("🇷🇺 Русский","🇺🇦 Українська","🇩🇪 Deutsch")
    await message.answer(await t(message.from_user.id,"choose_lang"), reply_markup=kb)


# ========== ВЫБОР ГОРОДА ==========

@dp.callback_query_handler(lambda c: c.data.startswith("city_select_"), state="*")
async def city_select_cb(call: types.CallbackQuery, state: FSMContext):
    """Обрабатывает выбор города или доставки из инлайн-меню."""
    uid = call.from_user.id
    choice = call.data.split("city_select_")[1]  # city_key

    if choice in CITIES:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET city=$1 WHERE user_id=$2", choice, uid
            )
        confirm_text = (await t(uid, "city_selected")).format(city=CITIES[choice]["name"])
    else:
        await call.answer()
        return

    await call.answer(confirm_text)
    lang = await get_lang(uid)

    # Если пришли из профиля — возвращаемся в профиль
    back = (await state.get_data()).get("city_select_back")
    await state.finish()

    if back == "profile":
        try:
            await call.message.delete()
        except Exception:
            pass
        await render_profile(call)
    else:
        # Первичный выбор (после языка или /start) — показываем главное меню
        try:
            await call.message.delete()
        except Exception:
            pass
        await bot.send_message(uid, TEXTS[lang]["menu"], reply_markup=main_menu(lang))


@dp.callback_query_handler(lambda c: c.data == "profile_change_city", state="*")
async def profile_change_city(call: types.CallbackQuery, state: FSMContext):
    """Запускает выбор города из профиля."""
    uid = call.from_user.id
    await state.update_data(city_select_back="profile")
    await show_city_selection(call, uid, back_cb="profile")


# ========== МАГАЗИН ==========

async def render_category_selection(target, uid, mode="shop"):
    """Экран выбора раздела (Elfliq / Elfworld). mode='shop' — обычный
    магазин (с кнопками корзины/профиля), mode='gift' — выбор раздела
    для бесплатной банки (без цен и корзины)."""
    prefix = "shop_cat_" if mode == "shop" else "gift_cat_"

    text = await t(uid, "choose_section")

    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton(await t(uid, "section_elfliq"), callback_data=f"{prefix}elfliq"),
        InlineKeyboardButton(await t(uid, "section_elfworld"), callback_data=f"{prefix}elfworld"),
    )

    if mode == "shop":
        kb.add(
            InlineKeyboardButton(await t(uid,"cart"), callback_data="open_cart"),
            InlineKeyboardButton(await t(uid,"profile"), callback_data="profile")
        )
    else:
        kb.add(InlineKeyboardButton(await t(uid, "back"), callback_data="profile_roulette"))

    await render(target, text, kb)


async def render_category_shop(target, uid, category):
    # Один SELECT для lang + city + delivery_mode (вместо 3 отдельных roundtrip)
    ctx = await get_user_ctx(uid)
    lang = ctx["lang"]
    pool_key = get_stock_pool(ctx["city"])

    # Один SELECT для products + user_discounts + promo (батч)
    async with pool.acquire() as conn:
        products = await conn.fetch(
            "SELECT * FROM products WHERE category=$1 ORDER BY id", category
        )
        pids = [p["id"] for p in products]

        # Избранное
        fav_rows = await conn.fetch(
            "SELECT product_id FROM favorites WHERE user_id=$1 AND product_id = ANY($2::int[])",
            uid, pids
        )
        fav_set = {r["product_id"] for r in fav_rows}

        # Stock (городской пул)
        if pool_key == "default":
            stock_rows = await conn.fetch(
                "SELECT id, stock FROM products WHERE id = ANY($1::int[])", pids
            )
            stock_map = {r["id"]: r["stock"] for r in stock_rows}
        else:
            stock_rows = await conn.fetch(
                "SELECT product_id, stock FROM city_stock WHERE city_key=$1 AND product_id = ANY($2::int[])",
                pool_key, pids
            )
            stock_map = {r["product_id"]: r["stock"] for r in stock_rows}

        # Promo discount (нужен для показа строки скидки)
        promo_discount = await conn.fetchval(
            "SELECT promo_discount FROM users WHERE user_id=$1", uid
        ) or 0

    total_qty = 1
    final_price, discount = await calculate_final_price(uid, total_qty)

    section_key = "section_elfliq" if category == "elfliq" else "section_elfworld"
    text = f"{TEXTS[lang][section_key]}\n\n"
    text += TEXTS[lang]["choose_product"] + "\n\n"

    if discount > 0:
        if promo_discount > 0:
            text += TEXTS[lang]["promo_in_shop_label"].format(value=promo_discount) + "\n"
        text += f"💰 {BASE_PRICE}€ → {final_price}€ (-{discount}€)\n\n"
    else:
        text += f"💰 {BASE_PRICE}€\n\n"

    kb = InlineKeyboardMarkup()

    if not products:
        text += TEXTS[lang]["section_empty"] + "\n"

    for p in products:
        pid = p["id"]
        name = p[f"name_{lang}"]
        stock = stock_map.get(pid, 0)
        heart = "❤️" if pid in fav_set else ""
        status = "✅" if stock > 0 else "❌"
        text += f"{name} {status} {heart}\n"
        if stock > 0:
            kb.add(InlineKeyboardButton(name, callback_data=f"view_{pid}"))

    other_category = "elfworld" if category == "elfliq" else "elfliq"
    switch_key = "switch_to_elfworld" if category == "elfliq" else "switch_to_elfliq"

    kb.add(InlineKeyboardButton(TEXTS[lang][switch_key], callback_data=f"shop_cat_{other_category}"))
    kb.add(
        InlineKeyboardButton(TEXTS[lang]["cart"],    callback_data="open_cart"),
        InlineKeyboardButton(TEXTS[lang]["profile"], callback_data="profile")
    )

    await render(target, text, kb)

@dp.message_handler(lambda m: is_text(m,"shop"))
async def shop(message: types.Message):
    if not await check_not_banned(message):
        return
    uid = message.from_user.id
    if await remind_city_if_needed(message, uid):
        return
    await render_category_selection(message, uid, mode="shop")

@dp.callback_query_handler(lambda c: c.data.startswith("shop_cat_"))
async def shop_category(call):
    if not await check_not_banned(call):
        return

    await call.answer()
    category = call.data.split("shop_cat_")[1]
    await render_category_shop(call, call.from_user.id, category)

@dp.callback_query_handler(lambda c: c.data == "open_cart")
async def open_cart(call):
    await call.answer()
    await render_cart(call, call.from_user.id)
# ========== КАРТОЧКА ТОВАРА ==========

@dp.callback_query_handler(lambda c: c.data.startswith("view_"))
async def view(call: types.CallbackQuery):
    if not await check_not_banned(call):
        return

    uid = call.from_user.id
    pid = int(call.data.split("_")[1])

    async with pool.acquire() as conn:
        p = await conn.fetchrow("SELECT * FROM products WHERE id=$1", pid)

    lang = await get_lang(uid)

    name = p[f"name_{lang}"]
    desc = p[f"desc_{lang}"]
    img = p["image"]
    category = p["category"] or "elfliq"

    fav = await is_fav(uid, pid)
    heart = "❤️" if fav else ""

    text = f"{name} {heart}\n\n{desc}"

    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("⬅️", callback_data=f"shop_cat_{category}"),
        InlineKeyboardButton("❤️", callback_data=f"fav_{pid}"),
        InlineKeyboardButton("🛒", callback_data=f"add_{pid}")
    )

    await render(call, text, kb, photo=img)

@dp.callback_query_handler(lambda c: c.data.startswith("fav_"))
async def toggle_fav(call):
    uid = call.from_user.id
    pid = int(call.data.split("_")[1])

    async with pool.acquire() as conn:
        exists = await conn.fetchval("""
            SELECT 1 FROM favorites 
            WHERE user_id=$1 AND product_id=$2
        """, uid, pid)
    
        if exists:
            await conn.execute("""
                DELETE FROM favorites 
                WHERE user_id=$1 AND product_id=$2
            """, uid, pid)
        else:
            pos = await conn.fetchval("""
                SELECT COALESCE(MAX(position),0)+1 
                FROM favorites WHERE user_id=$1
            """, uid)
    
            await conn.execute("""
                INSERT INTO favorites (user_id, product_id, position)
                VALUES ($1,$2,$3)
            """, uid, pid, pos)

    await view(call)
# ========== ДОБАВЛЕНИЕ В КОРЗИНУ ==========

@dp.callback_query_handler(lambda c: c.data.startswith("add_"))
async def add(call):
    if not await check_not_banned(call):
        return

    uid = call.from_user.id
    pid = int(call.data.split("_")[1])

    async with pool.acquire() as conn:
        exists = await conn.fetchrow("""
            SELECT 1 FROM cart
            WHERE user_id=$1 AND product_id=$2
        """, uid, pid)

        # Проверяем что в наличии хватает на текущее кол-во + 1 (с учётом города)
        product_name = await conn.fetchval("SELECT name_ru FROM products WHERE id=$1", pid) or f"#{pid}"
        user_city = await get_user_city(uid)
        available = await get_stock_for_city(conn, pid, user_city)
        current_in_cart = 0
        if exists:
            current_in_cart = await conn.fetchval(
                "SELECT quantity FROM cart WHERE user_id=$1 AND product_id=$2", uid, pid
            ) or 0
        if current_in_cart + 1 > available:
            if available == 0:
                await call.answer(
                    (await t(uid, "stock_out")).format(name=product_name),
                    show_alert=True
                )
            else:
                await call.answer(
                    (await t(uid, "stock_low")).format(name=product_name, qty=available),
                    show_alert=True
                )
            return

        if exists:
            await conn.execute("""
                UPDATE cart
                SET quantity = quantity + 1
                WHERE user_id=$1 AND product_id=$2
            """, uid, pid)
        else:
            max_pos = await conn.fetchval("""
                SELECT COALESCE(MAX(position), 0)
                FROM cart
                WHERE user_id=$1
            """, uid)

            await conn.execute("""
                INSERT INTO cart (user_id, product_id, quantity, position)
                VALUES ($1, $2, 1, $3)
            """, uid, pid, max_pos + 1)

            # Фиксируем режим корзины при добавлении первого товара
            if cart_count == 0:
                await conn.execute(
                    "UPDATE users SET cart_mode=$1 WHERE user_id=$2", new_mode, uid
                )

    await call.answer(await t(uid,"added"))

# ========== КОРЗИНА ==========

async def render_cart(target, uid):
    async with pool.acquire() as conn:
        items = await conn.fetch("""
            SELECT c.product_id, c.quantity, p.name_ru
            FROM cart c
            JOIN products p ON c.product_id = p.id
            WHERE c.user_id=$1
            ORDER BY c.position ASC
        """, uid)

    if not items:
        await render(target, await t(uid,"empty_cart"))
        return

    total_qty = sum(i["quantity"] for i in items)
    final_total, discount = await calculate_final_price(uid, total_qty)

    text = "🧺\n\n"

    kb = InlineKeyboardMarkup()

    for i in items:
        pid = i["product_id"]
        qty = i["quantity"]
        name_button = i["name_ru"]

        text += f"{i['name_ru']} x{qty}\n"

        kb.row(
            InlineKeyboardButton("➖", callback_data=f"cart_minus_{pid}"),
            InlineKeyboardButton(f"{name_button}", callback_data="noop"),
            InlineKeyboardButton("➕", callback_data=f"cart_plus_{pid}")
        )

    text += f"\n{await t(uid,'total')}: {final_total}€"

    if discount > 0:
        text += "\n" + (await t(uid,"savings")).format(value=discount)

    kb.row(
        InlineKeyboardButton(await t(uid,"clear"), callback_data="clear"),
        InlineKeyboardButton(await t(uid,"back_shop"), callback_data="back_shop"),
        InlineKeyboardButton(await t(uid,"profile"), callback_data="profile"),
        InlineKeyboardButton(await t(uid,"pay"), callback_data="pay")
    )

    await render(target, text, kb)

@dp.message_handler(lambda m: is_text(m,"cart"))
async def cart(message: types.Message):
    await render_cart(message, message.from_user.id)

# ========== ДЕЙСТВИЯ КОРЗИНЫ ==========

@dp.callback_query_handler(lambda c: c.data == "clear")
async def clear(call):
    uid = call.from_user.id

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM cart WHERE user_id=$1", uid)
        await conn.execute("UPDATE users SET cart_mode='pickup' WHERE user_id=$1", uid)

    await call.message.delete()
    await bot.send_message(uid, await t(uid, "empty_cart"))

@dp.callback_query_handler(lambda c: c.data == "back_shop")
async def back_shop(call):
    if not await check_not_banned(call):
        return

    await call.answer()
    await render_category_selection(call, call.from_user.id, mode="shop")

@dp.callback_query_handler(lambda c: c.data.startswith("cart_plus_"))
async def cart_plus(call):
    uid = call.from_user.id
    pid = int(call.data.split("_")[2])

    async with pool.acquire() as conn:
        current_qty = await conn.fetchval(
            "SELECT quantity FROM cart WHERE user_id=$1 AND product_id=$2", uid, pid
        ) or 0
        product_name = await conn.fetchval("SELECT name_ru FROM products WHERE id=$1", pid) or f"#{pid}"
        user_city = await get_user_city(uid)
        available = await get_stock_for_city(conn, pid, user_city)

        if current_qty + 1 > available:
            if available == 0:
                await call.answer(
                    (await t(uid, "stock_out")).format(name=product_name),
                    show_alert=True
                )
            else:
                await call.answer(
                    (await t(uid, "stock_low")).format(name=product_name, qty=available),
                    show_alert=True
                )
            return

        await conn.execute("""
            UPDATE cart
            SET quantity = quantity + 1
            WHERE user_id=$1 AND product_id=$2
        """, uid, pid)

    await call.answer("+1")
    await render_cart(call, uid)

@dp.callback_query_handler(lambda c: c.data.startswith("cart_minus_"))
async def cart_minus(call):
    uid = call.from_user.id
    pid = int(call.data.split("_")[2])

    async with pool.acquire() as conn:
        item = await conn.fetchrow("""
            SELECT quantity
            FROM cart
            WHERE user_id=$1 AND product_id=$2
        """, uid, pid)

        if not item:
            return await call.answer()

        if item["quantity"] > 1:
            await conn.execute("""
                UPDATE cart
                SET quantity = quantity - 1
                WHERE user_id=$1 AND product_id=$2
            """, uid, pid)
        else:
            await conn.execute("""
                DELETE FROM cart
                WHERE user_id=$1 AND product_id=$2
            """, uid, pid)

    await call.answer("-1")
    await render_cart(call, uid)

@dp.callback_query_handler(lambda c: c.data == "noop")
async def noop(call):
    await call.answer()
# ========== ДОСТАВКА: ФОРМА ==========


@dp.callback_query_handler(lambda c: c.data == "cash")
async def cash(call):
    uid = call.from_user.id

    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton(await t(uid,"cancel"), callback_data="open_cart"),
        InlineKeyboardButton(await t(uid,"confirm"), callback_data="confirm_cash")
    )

    await render(call, await t(uid,"confirm_order"), kb)



async def check_cart_stock(uid: int) -> list[dict] | None:
    """
    Проверяет наличие всех товаров в корзине пользователя с учётом города.
    Возвращает список проблем: [{"name": ..., "wanted": N, "available": M}] или None если всё OK.
    Использует батч-запрос к products (нет N+1).
    """
    user_city = await get_user_city(uid)
    pool_key = get_stock_pool(user_city)

    async with pool.acquire() as conn:
        cart_items = await conn.fetch(
            "SELECT product_id, quantity FROM cart WHERE user_id=$1", uid
        )
        if not cart_items:
            return None

        pids = [item["product_id"] for item in cart_items]

        # Батч-запрос имён и stock из products
        products_rows = await conn.fetch(
            "SELECT id, name_ru, stock FROM products WHERE id = ANY($1::int[])", pids
        )
        prod_map = {r["id"]: r for r in products_rows}

        # Батч-запрос city_stock если нужен отдельный пул
        city_stock_map: dict[int, int] = {}
        if pool_key != "default":
            cs_rows = await conn.fetch(
                "SELECT product_id, stock FROM city_stock WHERE city_key=$1 AND product_id = ANY($2::int[])",
                pool_key, pids
            )
            city_stock_map = {r["product_id"]: r["stock"] for r in cs_rows}

        problems = []
        for item in cart_items:
            pid = item["product_id"]
            wanted = item["quantity"]
            prod = prod_map.get(pid)
            name = prod["name_ru"] if prod else f"#{pid}"
            if pool_key == "default":
                available = prod["stock"] if prod else 0
            else:
                available = city_stock_map.get(pid, 0)
            if available < wanted:
                problems.append({"name": name, "wanted": wanted, "available": available})

    return problems if problems else None


async def reserve_stock_for_order(conn, order_id: int, items_str: str, city_key: str | None = None) -> bool:
    """
    Резервирует stock при оформлении заказа.
    Выполняет SELECT FOR UPDATE внутри транзакции — защита от race condition.
    Возвращает True если успешно, False если stock не хватает (заказ не должен создаваться).
    """
    pool_key = get_stock_pool(city_key)
    parts = []
    for part in items_str.split(","):
        pid_str, qty_str = part.split(":")
        parts.append((int(pid_str), int(qty_str)))

    pids = [p[0] for p in parts]

    if pool_key == "default":
        # Блокируем строки товаров для обновления
        rows = await conn.fetch(
            "SELECT id, stock FROM products WHERE id = ANY($1::int[]) FOR UPDATE", pids
        )
        stock_map = {r["id"]: r["stock"] for r in rows}
        for pid, qty in parts:
            if stock_map.get(pid, 0) < qty:
                return False
        for pid, qty in parts:
            await conn.execute(
                "UPDATE products SET stock = stock - $1 WHERE id=$2", qty, pid
            )
    else:
        rows = await conn.fetch(
            "SELECT product_id, stock FROM city_stock WHERE city_key=$1 AND product_id = ANY($2::int[]) FOR UPDATE",
            pool_key, pids
        )
        stock_map = {r["product_id"]: r["stock"] for r in rows}
        for pid, qty in parts:
            if stock_map.get(pid, 0) < qty:
                return False
        for pid, qty in parts:
            await conn.execute(
                "UPDATE city_stock SET stock = GREATEST(stock - $1, 0) WHERE city_key=$2 AND product_id=$3",
                qty, pool_key, pid
            )

    # Записываем резерв (для возврата при отмене)
    for pid, qty in parts:
        await conn.execute("""
            INSERT INTO reserved_stock (order_id, product_id, quantity, city_key)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (order_id, product_id) DO UPDATE
            SET quantity = EXCLUDED.quantity, city_key = EXCLUDED.city_key
        """, order_id, pid, qty, city_key)

    return True


async def release_reserved_stock(conn, order_id: int, city_key: str | None = None) -> None:
    """Возвращает зарезервированный stock при отмене заказа."""
    rows = await conn.fetch(
        "SELECT product_id, quantity, city_key FROM reserved_stock WHERE order_id=$1", order_id
    )
    for row in rows:
        # Используем city_key из записи резерва (он был зафиксирован при оформлении)
        ck = row["city_key"] if row["city_key"] is not None else city_key
        await update_stock_for_city(conn, row["product_id"], ck, row["quantity"])
    await conn.execute("DELETE FROM reserved_stock WHERE order_id=$1", order_id)


async def finalize_reserved_stock(conn, order_id: int) -> None:
    """При подтверждении заказа просто удаляем резерв (stock уже уменьшен)."""
    await conn.execute("DELETE FROM reserved_stock WHERE order_id=$1", order_id)


async def _format_stock_errors(uid: int, problems: list[dict]) -> str:
    """Форматирует список проблем с наличием в текст для пользователя."""
    lines = []
    for p in problems:
        if p["available"] == 0:
            lines.append((await t(uid, "stock_out")).format(name=p["name"]))
        else:
            lines.append((await t(uid, "stock_low_cart")).format(
                name=p["name"], qty=p["available"]
            ))
    return "\n\n".join(lines)


@dp.callback_query_handler(lambda c: c.data == "confirm_cash")
async def confirm_cash(call):
    if not await check_not_banned(call):
        return

    uid = call.from_user.id
    username = call.from_user.username or "нет username"

    # Предварительная проверка (без блокировки — для отображения пользователю)
    problems = await check_cart_stock(uid)
    if problems:
        await call.answer(await _format_stock_errors(uid, problems), show_alert=True)
        return

    async with pool.acquire() as conn:
        cart_items = await conn.fetch(
            "SELECT product_id, quantity FROM cart WHERE user_id=$1", uid
        )

        if not cart_items:
            await render(call, await t(uid,"empty_cart"))
            return

        pids = [r["product_id"] for r in cart_items]
        products_rows = await conn.fetch(
            "SELECT id, name_ru FROM products WHERE id = ANY($1::int[])", pids
        )
        prod_name_map = {r["id"]: r["name_ru"] for r in products_rows}

        items_str = ",".join([f"{r['product_id']}:{r['quantity']}" for r in cart_items])
        total_qty = sum(r["quantity"] for r in cart_items)
        total, discount = await calculate_final_price(uid, total_qty)

        text_admin = "ЗАКАЗ:\n"
        for r in cart_items:
            pid = r["product_id"]
            name = prod_name_map.get(pid, f"#{pid}")
            text_admin += f"{name} x{r['quantity']}\n"

        user_city = await conn.fetchval("SELECT city FROM users WHERE user_id=$1", uid)
        resolved_city = get_order_city(user_city)

        try:
            async with conn.transaction():
                order_id = await conn.fetchval("""
                    INSERT INTO orders (user_id, items, total, payment, discount, is_delivery, city_key)
                    VALUES ($1, $2, $3, $4, $5, false, $6)
                    RETURNING id
                """, uid, items_str, total, "cash", discount, resolved_city)
                ok = await reserve_stock_for_order(conn, order_id, items_str, resolved_city)
                if not ok:
                    raise Exception("stock_unavailable")
        except Exception as e:
            if "stock_unavailable" in str(e):
                await call.answer(await t(uid, "stock_changed_retry"), show_alert=True)
                return
            logger.exception("confirm_cash: неожиданная ошибка uid=%s", uid)
            await alert_super_admins(f"confirm_cash: ошибка создания заказа uid={uid}: {e}")
            raise

        await conn.execute("DELETE FROM cart WHERE user_id=$1", uid)
        await conn.execute("UPDATE users SET cart_mode='pickup' WHERE user_id=$1", uid)

    await render(call, await t(uid,"order_done"))
    await notify_no_username(call, uid)

    payment_line = f"Оплата: Наличные\nИТОГО: {total}€"
    await _send_order_to_admins(
        order_id, uid, username, text_admin, payment_line, city_key=resolved_city
    )

# ========== НОВЫЕ СПОСОБЫ ОПЛАТЫ (DELIVERY) ==========

USDT_WALLET  = "TGZCiwS5fTktQYxeey57KEeSfHXjB1hMQc"
CARD_EUR     = "4400005544191544"
CARD_UAH     = "4400005545864297"


async def _get_cart_totals(uid: int):
    """Вспомогательная: возвращает (cart_items, eur_total, discount, items_str, text_admin) или None если корзина пуста."""
    async with pool.acquire() as conn:
        cart_items = await conn.fetch(
            "SELECT product_id, quantity FROM cart WHERE user_id=$1", uid
        )
    if not cart_items:
        return None
    total_qty = sum(r["quantity"] for r in cart_items)
    eur_total, discount = await calculate_final_price(uid, total_qty)
    pids = [r["product_id"] for r in cart_items]
    async with pool.acquire() as conn:
        products_rows = await conn.fetch(
            "SELECT id, name_ru FROM products WHERE id = ANY($1::int[])", pids
        )
    prod_name_map = {r["id"]: r["name_ru"] for r in products_rows}
    items_str_parts = []
    text_admin = "ЗАКАЗ:\n"
    for r in cart_items:
        pid = r["product_id"]
        text_admin += f"{prod_name_map.get(pid, f'#{pid}')} x{r['quantity']}\n"
        items_str_parts.append(f"{pid}:{r['quantity']}")
    return cart_items, eur_total, discount, ",".join(items_str_parts), text_admin


async def _create_pending_order(uid: int, items_str: str, eur_total: float, discount: float, payment: str) -> tuple[int, str] | None:
    """
    Создаёт заказ со статусом pending, атомарно резервирует stock и очищает корзину.
    Возвращает (order_id, resolved_city_key) или None если stock закончился (race condition).
    """
    try:
        async with pool.acquire() as conn:
            user_city = await conn.fetchval("SELECT city FROM users WHERE user_id=$1", uid)
            resolved_city = get_order_city(user_city)

            async with conn.transaction():
                order_id = await conn.fetchval("""
                    INSERT INTO orders (user_id, items, total, payment, discount, status, is_delivery, city_key)
                    VALUES ($1, $2, $3, $4, $5, 'pending', false, $6)
                    RETURNING id
                """, uid, items_str, eur_total, payment, discount, resolved_city)
                ok = await reserve_stock_for_order(conn, order_id, items_str, resolved_city)
                if not ok:
                    return None   # stock закончился — caller покажет retry

            await conn.execute("DELETE FROM cart WHERE user_id=$1", uid)
            await conn.execute("UPDATE users SET cart_mode='pickup' WHERE user_id=$1", uid)
        return order_id, resolved_city
    except Exception:
        logger.exception("_create_pending_order: неожиданная ошибка uid=%s payment=%s", uid, payment)
        await alert_super_admins(f"_create_pending_order: неожиданная ошибка uid={uid} payment={payment}")
        return None


async def _send_order_to_admins(order_id: int, uid: int, username: str,
                                 text_admin: str, payment_line: str,
                                 city_key: str | None = None) -> None:
    """
    Отправляет заказ городским админам (и только им).
    Высшие админы получают уведомление только ПОСЛЕ подтверждения городским.
    city_key: ключ из CITIES или None (→ fallback на buerhausen).
    """
    resolved_city = get_order_city(city_key)
    city_name = CITIES[resolved_city]["name"]

    order_text = (
        f"🏪 Самовывоз | 🏙 {city_name}\n"
        f"{text_admin}\n"
        f"ID: {order_id}\n"
        f"User: @{username}\n"
        f"{payment_line}"
    )
    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("✅ Подтвердить", callback_data=f"admin_confirm_{order_id}"),
        InlineKeyboardButton("❌ Отменить",    callback_data=f"admin_cancel_{order_id}")
    )

    # Рассылаем только городским админам нужного города
    city_admin_ids = get_city_admins(resolved_city)
    recipients = city_admin_ids if city_admin_ids else ADMIN_IDS  # fallback

    msg_ids = []
    failed_admins = []
    for admin in recipients:
        try:
            sent = await bot.send_message(admin, order_text, reply_markup=kb)
            msg_ids.append(f"{admin}:{sent.message_id}")
        except Exception as e:
            failed_admins.append(str(admin))
            logger.error("Failed to notify admin %s for order %s: %s", admin, order_id, e)
    if failed_admins:
        await alert_super_admins(
            f"Не удалось отправить заказ #{order_id} админам: {', '.join(failed_admins)}. "
            f"Заказ существует в БД, но сообщение не доставлено."
        )
    if msg_ids:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE orders SET admin_message_ids=$1, city_key=$2 WHERE id=$3",
                ",".join(msg_ids), resolved_city, order_id
            )


# --- USDT ---

@dp.callback_query_handler(lambda c: c.data == "pay_usdt_delivery")
async def pay_usdt_delivery(call):
    if not await check_not_banned(call):
        return
    uid = call.from_user.id
    result = await _get_cart_totals(uid)
    if not result:
        await render(call, await t(uid, "empty_cart"))
        return
    _, eur_total, _, _, _ = result

    rate = await get_user_rate(uid, "usdt")
    if not rate:
        await call.answer(await t(uid, "rate_unavailable"), show_alert=True)
        return

    usdt_amount = round(eur_total * rate, 2)
    text = (await t(uid, "pay_usdt_screen")).format(
        eur=fmt_amount(eur_total),
        rate=fmt_amount(rate),
        usdt=fmt_amount(usdt_amount),
        wallet=USDT_WALLET,
    )
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(await t(uid, "pay_i_paid_btn"), callback_data=f"paid_usdt_{fmt_amount(eur_total)}_{fmt_amount(usdt_amount)}_{fmt_amount(rate)}"))
    kb.add(InlineKeyboardButton(await t(uid, "cancel"), callback_data="open_cart"))
    await render(call, text, kb, parse_mode="Markdown")


@dp.callback_query_handler(lambda c: c.data.startswith("paid_usdt_"))
async def paid_usdt(call):
    if not await check_not_banned(call):
        return
    uid = call.from_user.id
    username = call.from_user.username or str(uid)
    # Формат: paid_usdt_{eur}_{usdt}_{rate}
    parts = call.data.split("_")
    eur_str, usdt_str, rate_str = parts[2], parts[3], parts[4]

    problems = await check_cart_stock(uid)
    if problems:
        await call.answer(await _format_stock_errors(uid, problems), show_alert=True)
        return

    result = await _get_cart_totals(uid)
    if not result:
        await render(call, await t(uid, "empty_cart"))
        return
    _, eur_total, discount, items_str, text_admin = result

    result = await _create_pending_order(uid, items_str, eur_total, discount, "usdt_trc20")
    if result is None:
        await call.answer(await t(uid, "stock_changed_retry"), show_alert=True)
        return
    order_id, resolved_city = result
    payment_line = (
        f"Оплата: USDT TRC20 (ожидает проверки)\n"
        f"Сумма EUR: {eur_str}€\n"
        f"Курс: 1 EUR = {rate_str} USDT\n"
        f"К оплате: {usdt_str} USDT\n"
        f"ИТОГО: {eur_str}€"
    )
    await _send_order_to_admins(order_id, uid, username, text_admin, payment_line, city_key=resolved_city)
    await render(call, await t(uid, "pay_pending_user"))
    await notify_no_username(call, uid)


# --- КАРТА: выбор валюты ---

@dp.callback_query_handler(lambda c: c.data == "pay_card_delivery")
async def pay_card_delivery(call):
    if not await check_not_banned(call):
        return
    uid = call.from_user.id
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(await t(uid, "pay_card_uah_btn"), callback_data="pay_card_uah"))
    kb.add(InlineKeyboardButton(await t(uid, "pay_card_eur_btn"), callback_data="pay_card_eur"))
    kb.add(InlineKeyboardButton(await t(uid, "cancel"), callback_data="open_cart"))
    await render(call, await t(uid, "pay_currency_title"), kb)


# --- КАРТА EUR ---

@dp.callback_query_handler(lambda c: c.data == "pay_card_eur")
async def pay_card_eur(call):
    if not await check_not_banned(call):
        return
    uid = call.from_user.id
    result = await _get_cart_totals(uid)
    if not result:
        await render(call, await t(uid, "empty_cart"))
        return
    _, eur_total, _, _, _ = result

    text = (await t(uid, "pay_card_eur_screen")).format(
        eur=fmt_amount(eur_total),
        card=CARD_EUR,
    )
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(await t(uid, "pay_i_paid_btn"), callback_data=f"paid_card_eur_{fmt_amount(eur_total)}"))
    kb.add(InlineKeyboardButton(await t(uid, "cancel"), callback_data="open_cart"))
    await render(call, text, kb, parse_mode="Markdown")


@dp.callback_query_handler(lambda c: c.data.startswith("paid_card_eur_"))
async def paid_card_eur(call):
    if not await check_not_banned(call):
        return
    uid = call.from_user.id
    username = call.from_user.username or str(uid)
    eur_str = call.data.split("paid_card_eur_")[1]

    problems = await check_cart_stock(uid)
    if problems:
        await call.answer(await _format_stock_errors(uid, problems), show_alert=True)
        return

    result = await _get_cart_totals(uid)
    if not result:
        await render(call, await t(uid, "empty_cart"))
        return
    _, eur_total, discount, items_str, text_admin = result

    result = await _create_pending_order(uid, items_str, eur_total, discount, "card_eur")
    if result is None:
        await call.answer(await t(uid, "stock_changed_retry"), show_alert=True)
        return
    order_id, resolved_city = result
    payment_line = (
        f"Оплата: Банковская карта EUR (ожидает проверки)\n"
        f"ИТОГО: {eur_str}€"
    )
    await _send_order_to_admins(order_id, uid, username, text_admin, payment_line, city_key=resolved_city)
    await render(call, await t(uid, "pay_pending_user"))
    await notify_no_username(call, uid)


# --- КАРТА UAH ---

@dp.callback_query_handler(lambda c: c.data == "pay_card_uah")
async def pay_card_uah(call):
    if not await check_not_banned(call):
        return
    uid = call.from_user.id
    result = await _get_cart_totals(uid)
    if not result:
        await render(call, await t(uid, "empty_cart"))
        return
    _, eur_total, _, _, _ = result

    rate = await get_user_rate(uid, "uah")
    if not rate:
        await call.answer(await t(uid, "rate_unavailable"), show_alert=True)
        return

    uah_amount = round(eur_total * rate, 2)
    text = (await t(uid, "pay_card_uah_screen")).format(
        eur=fmt_amount(eur_total),
        rate=fmt_amount(rate),
        uah=fmt_amount(uah_amount),
        card=CARD_UAH,
    )
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(await t(uid, "pay_i_paid_btn"), callback_data=f"paid_card_uah_{fmt_amount(eur_total)}_{fmt_amount(uah_amount)}_{fmt_amount(rate)}"))
    kb.add(InlineKeyboardButton(await t(uid, "cancel"), callback_data="open_cart"))
    await render(call, text, kb, parse_mode="Markdown")


@dp.callback_query_handler(lambda c: c.data.startswith("paid_card_uah_"))
async def paid_card_uah(call):
    if not await check_not_banned(call):
        return
    uid = call.from_user.id
    username = call.from_user.username or str(uid)
    parts = call.data.split("_")
    eur_str, uah_str, rate_str = parts[3], parts[4], parts[5]

    problems = await check_cart_stock(uid)
    if problems:
        await call.answer(await _format_stock_errors(uid, problems), show_alert=True)
        return

    result = await _get_cart_totals(uid)
    if not result:
        await render(call, await t(uid, "empty_cart"))
        return
    _, eur_total, discount, items_str, text_admin = result

    result = await _create_pending_order(uid, items_str, eur_total, discount, "card_uah")
    if result is None:
        await call.answer(await t(uid, "stock_changed_retry"), show_alert=True)
        return
    order_id, resolved_city = result
    payment_line = (
        f"Оплата: Банковская карта UAH (ожидает проверки)\n"
        f"Сумма EUR: {eur_str}€\n"
        f"Курс: 1 EUR = {rate_str} UAH\n"
        f"К оплате: {uah_str} UAH\n"
        f"ИТОГО: {eur_str}€"
    )
    await _send_order_to_admins(order_id, uid, username, text_admin, payment_line, city_key=resolved_city)
    await render(call, await t(uid, "pay_pending_user"))
    await notify_no_username(call, uid)


@dp.callback_query_handler(lambda c: c.data.startswith("admin_confirm_"))
async def admin_confirm(call):
    if not is_admin(call.from_user.id):
        await call.answer("❌ Нет доступа", show_alert=True)
        return
    order_id = int(call.data.split("_")[2])
    admin_username = call.from_user.username or "admin"

    async with pool.acquire() as conn:
        order = await conn.fetchrow("""
            SELECT user_id, items, status, admin_message_ids, discount, total, city_key
            FROM orders 
            WHERE id=$1
        """, order_id)

    if not order or order["status"] != "pending":
        await call.answer("Заказ уже обработан", show_alert=True)
        return

    # Проверка прав: городской админ может подтверждать только свой город
    actor_id = call.from_user.id
    actor_city = get_city_for_admin(actor_id)
    order_city = order["city_key"] or "buerhausen"
    if actor_city is not None and actor_city != order_city:
        await call.answer("❌ Этот заказ относится к другому городу", show_alert=True)
        return

    user_id = order["user_id"]
    items = order["items"]
    msg_ids_raw = order["admin_message_ids"] or ""
    order_discount = order["discount"] or 0
    order_total = order["total"] or 0
    order_city_name = CITIES.get(order_city, {}).get("name", order_city)

    async with pool.acquire() as conn:
        # Атомарно меняем статус: если кто-то успел раньше — RETURNING вернёт пустой результат
        confirmed_id = await conn.fetchval(
            "UPDATE orders SET status='confirmed' WHERE id=$1 AND status='pending' RETURNING id",
            order_id
        )
        if not confirmed_id:
            await call.answer("Заказ уже обработан другим админом", show_alert=True)
            return

        # Финализируем резерв — stock уже уменьшен при оформлении, просто чистим резерв
        await finalize_reserved_stock(conn, order_id)

        old_user = await conn.fetchrow(
            "SELECT total_items, total_orders FROM users WHERE user_id=$1", user_id
        )

        old_total = old_user["total_items"]
        is_first_order = (old_user["total_orders"] or 0) == 0

        old_rank = get_rank(old_total) or 0

        total_items = sum(int(qty) for _, qty in
                          (item.split(":") for item in items.split(",")))

        await conn.execute("""
            UPDATE users 
            SET total_items = total_items + $1,
                total_orders = total_orders + 1,
                spin_progress = spin_progress + $1
            WHERE user_id=$2
        """, total_items, user_id)

        new_total = await conn.fetchval(
            "SELECT total_items FROM users WHERE user_id=$1", user_id
        )
        new_rank = get_rank(new_total)

        if new_rank["key"] != old_rank["key"]:
            await bot.send_message(
                user_id,
                (await t(user_id, "new_rank")).format(
                    rank=new_rank["name"][await get_lang(user_id)]
                )
            )

        spin_progress = await conn.fetchval(
            "SELECT spin_progress FROM users WHERE user_id=$1", user_id
        )

        if spin_progress >= 5:
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton(
                await t(user_id, "spin_open"),
                callback_data="profile_roulette"
            ))
            await bot.send_message(
                user_id,
                await t(user_id, "spin_ready_notify"),
                reply_markup=kb
            )

        streak_row = await conn.fetchrow("""
            SELECT streak_weeks, last_order_date
            FROM users WHERE user_id=$1
        """, user_id)

        new_streak = await _calc_next_streak(
            streak_row["streak_weeks"] or 0,
            streak_row["last_order_date"],
            order_city
        )

        #    пользователя — начисляем скидку пригласившему ──
        if is_first_order:
            ref_row = await conn.fetchrow("""
                SELECT referrer_id FROM referrals
                WHERE new_user_id=$1 AND activated=0
            """, user_id)

            if ref_row:
                inviter_id = ref_row["referrer_id"]

                await conn.execute(
                    "UPDATE referrals SET activated=1 WHERE new_user_id=$1", user_id
                )
                await conn.execute(
                    "UPDATE users SET referrals = referrals + 1 WHERE user_id=$1",
                    inviter_id
                )

                try:
                    await bot.send_message(
                        inviter_id, await t(inviter_id, "ref_credited_notify")
                    )
                except Exception as _e:
                    logger.warning("ref_credited_notify: не удалось уведомить inviter %s: %s", inviter_id, _e)

        #    одноразовые бонусы (скидка с рулетки, реферальная, новичка, промокод) ──
        await conn.execute("""
            UPDATE users 
            SET current_discount = 0,
                referrals = 0,
                ref_bonus = 0,
                promo_discount = 0,
                promo_code = NULL,
                promo_type = NULL,
                total_saved = total_saved + $1,
                total_spent = total_spent + $2,
                streak_weeks = $3,
                last_order_date = $4,
                max_streak_weeks = GREATEST(max_streak_weeks, $3)
            WHERE user_id=$5
        """, order_discount, order_total, new_streak, date.today(), user_id)

    await call.answer("Подтверждено")

    await _sync_admin_messages(
        msg_ids_raw=msg_ids_raw,
        actor_id=actor_id,
        base_text=call.message.text,
        status_self="\n\n✅ ПОДТВЕРЖДЕНО",
        status_others=f"\n\n✅ ПОДТВЕРЖДЕНО @{admin_username}"
    )

    # Уведомляем высших админов о подтверждённом заказе (если подтвердил городской)
    if not is_super_admin(actor_id):
        items_readable = "\n".join(
            f"  • {part.split(':')[0]} x{part.split(':')[1]}"
            for part in (items or "").split(",") if ":" in part
        )
        # Пытаемся подставить имена товаров (best-effort)
        try:
            async with pool.acquire() as conn:
                items_readable_parts = []
                for part in (items or "").split(","):
                    if ":" not in part:
                        continue
                    pid_s, qty_s = part.split(":", 1)
                    name = await conn.fetchval(
                        "SELECT name_ru FROM products WHERE id=$1", int(pid_s)
                    )
                    items_readable_parts.append(f"  • {name or pid_s} x{qty_s}")
                items_readable = "\n".join(items_readable_parts)
        except Exception:
            pass

        super_notify = (
            f"✅ Заказ #{order_id} подтверждён\n\n"
            f"🏙 Город: {order_city_name}\n"
            f"👤 Подтвердил: @{admin_username}\n"
            f"📦 Товары:\n{items_readable}\n"
            f"💰 Итого: {order_total}€"
        )
        for super_id in SUPER_ADMINS:
            try:
                await bot.send_message(super_id, super_notify)
            except Exception:
                pass

@dp.callback_query_handler(lambda c: c.data.startswith("admin_cancel_"))
async def admin_cancel(call):
    if not is_admin(call.from_user.id):
        await call.answer("❌ Нет доступа", show_alert=True)
        return
    order_id = int(call.data.split("_")[2])
    admin_username = call.from_user.username or "admin"

    async with pool.acquire() as conn:
        order = await conn.fetchrow("""
            SELECT status, admin_message_ids
            FROM orders
            WHERE id=$1
        """, order_id)

    if not order or order["status"] != "pending":
        await call.answer("Заказ уже обработан", show_alert=True)
        return

    msg_ids_raw = order["admin_message_ids"] or ""

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE orders SET status='cancelled' WHERE id=$1", order_id
        )
        # Возвращаем зарезервированный stock
        await release_reserved_stock(conn, order_id)

    await call.answer("Отменено")

    await _sync_admin_messages(
        msg_ids_raw=msg_ids_raw,
        actor_id=call.from_user.id,
        base_text=call.message.text,
        status_self="\n\n❌ ОТМЕНЕНО",
        status_others=f"\n\n❌ ОТМЕНЕНО @{admin_username}"
    )

# ========== ПРОМОКОДЫ ==========

def _generate_code(length=8) -> str:
    """Генерирует случайный промокод из заглавных букв и цифр."""
    alphabet = _string.ascii_uppercase + _string.digits
    return "".join(random.choices(alphabet, k=length))


@dp.message_handler(commands=["promo"])
async def promo_cmd(message: types.Message):
    """Пользователь активирует промокод: /promo КОД"""
    uid = message.from_user.id
    args = message.get_args().strip()

    if not args:
        await message.answer(await t(uid, "promo_usage"))
        return

    code = args.upper()

    async with pool.acquire() as conn:
        # Проверяем есть ли у пользователя активный промокод
        user_row = await conn.fetchrow(
            "SELECT promo_code, promo_discount, free_jar_bonus FROM users WHERE user_id=$1", uid
        )
        if user_row and (user_row["promo_code"] or (user_row["free_jar_bonus"] or 0) > 0):
            await message.answer(await t(uid, "promo_already_have"))
            return

        # Ищем промокод
        promo = await conn.fetchrow(
            "SELECT * FROM promocodes WHERE code=$1 AND used=false", code
        )

    if not promo:
        await message.answer(await t(uid, "promo_not_found"))
        return

    # Активируем
    async with pool.acquire() as conn:
        updated = await conn.fetchval("""
            UPDATE promocodes SET used=true, used_by=$1
            WHERE code=$2 AND used=false
            RETURNING code
        """, uid, code)

    if not updated:
        # Гонка — кто-то успел раньше
        await message.answer(await t(uid, "promo_not_found"))
        return

    if promo["type"] == "discount":
        discount_val = promo["discount"]
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE users SET promo_discount=$1, promo_code=$2, promo_type='discount'
                WHERE user_id=$3
            """, discount_val, code, uid)

        text = (await t(uid, "promo_activated_discount")).format(value=discount_val)
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton(await t(uid, "shop_btn"), callback_data="back_shop"))
        await message.answer(text, reply_markup=kb)

    elif promo["type"] == "free_jar":
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE users SET free_jar_bonus=1, promo_code=$1, promo_type='free_jar'
                WHERE user_id=$2
            """, code, uid)

        text = await t(uid, "promo_activated_free_jar")
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton(await t(uid, "gift_shop_btn"), callback_data="open_gift_shop"))
        await message.answer(text, reply_markup=kb)


@dp.message_handler(commands=["createpromo"])
async def createpromo_cmd(message: types.Message):
    """/createpromo discount СУММА КОЛИЧЕСТВО  |  /createpromo freejar КОЛИЧЕСТВО"""
    if not is_super_admin(message.from_user.id):
        return

    uid = message.from_user.id
    args = message.get_args().split()

    if not args:
        await message.answer(await t(uid, "createpromo_usage"))
        return

    promo_type = args[0].lower()

    try:
        if promo_type == "discount":
            if len(args) < 3:
                await message.answer(await t(uid, "createpromo_usage"))
                return
            discount_val = float(args[1])
            count = int(args[2])

        elif promo_type == "freejar":
            if len(args) < 2:
                await message.answer(await t(uid, "createpromo_usage"))
                return
            discount_val = 0
            count = int(args[1])
        else:
            await message.answer(await t(uid, "createpromo_usage"))
            return
    except (ValueError, IndexError):
        await message.answer(await t(uid, "createpromo_usage"))
        return

    db_type = "discount" if promo_type == "discount" else "free_jar"
    codes = []
    async with pool.acquire() as conn:
        for _ in range(count):
            code = _generate_code()
            # Гарантируем уникальность
            while await conn.fetchval("SELECT 1 FROM promocodes WHERE code=$1", code):
                code = _generate_code()
            await conn.execute(
                "INSERT INTO promocodes (code, type, discount) VALUES ($1, $2, $3)",
                code, db_type, discount_val
            )
            codes.append(code)

    codes_text = "\n".join(f"• `{c}`" for c in codes)
    reply = (await t(uid, "createpromo_done")).format(count=count, codes=codes_text)
    await message.answer(reply, parse_mode="HTML")


@dp.message_handler(commands=["freezestreak"])
async def freezestreak(message: types.Message):
    """
    Заморозка Buy Streak по городу.
    Высший админ: /freezestreak <city|all>
    Городской админ: /freezestreak  (город определяется автоматически)
    """
    uid = message.from_user.id
    if not is_admin(uid):
        return

    actor_city = get_city_for_admin(uid)

    if actor_city is not None:
        # Городской админ — замораживает только свой город
        city_keys = [actor_city]
    else:
        # Высший админ — требует аргумент
        arg = message.get_args().strip().lower()
        if not arg:
            city_names = " | ".join(CITIES.keys())
            await message.answer(f"Использование: /freezestreak <{city_names}|all>")
            return
        if arg == "all":
            city_keys = list(CITIES.keys())
        elif arg in CITIES:
            city_keys = [arg]
        else:
            city_names = " | ".join(CITIES.keys())
            await message.answer(f"❌ Неизвестный город. Доступно: {city_names} | all")
            return

    async with pool.acquire() as conn:
        frozen_results = []
        already_frozen = []
        for ck in city_keys:
            active = await conn.fetchval("""
                SELECT 1 FROM streak_freezes
                WHERE ended_at IS NULL AND city_key = $1 LIMIT 1
            """, ck)
            if active:
                already_frozen.append(CITIES[ck]["name"])
            else:
                await conn.execute(
                    "INSERT INTO streak_freezes (city_key) VALUES ($1)", ck
                )
                frozen_results.append(CITIES[ck]["name"])

    parts = []
    if frozen_results:
        parts.append(f"❄️ Заморозка стрика включена: {', '.join(frozen_results)}")
    if already_frozen:
        parts.append(f"⚠️ Уже была заморожена: {', '.join(already_frozen)}")
    await message.answer("\n".join(parts))


@dp.message_handler(commands=["unfreezestreak"])
async def unfreezestreak(message: types.Message):
    """
    Разморозка Buy Streak по городу.
    Высший админ: /unfreezestreak <city|all>
    Городской админ: /unfreezestreak  (город определяется автоматически)
    """
    uid = message.from_user.id
    if not is_admin(uid):
        return

    actor_city = get_city_for_admin(uid)

    if actor_city is not None:
        city_keys = [actor_city]
    else:
        arg = message.get_args().strip().lower()
        if not arg:
            city_names = " | ".join(CITIES.keys())
            await message.answer(f"Использование: /unfreezestreak <{city_names}|all>")
            return
        if arg == "all":
            city_keys = list(CITIES.keys())
        elif arg in CITIES:
            city_keys = [arg]
        else:
            city_names = " | ".join(CITIES.keys())
            await message.answer(f"❌ Неизвестный город. Доступно: {city_names} | all")
            return

    async with pool.acquire() as conn:
        unfrozen = []
        not_frozen = []
        for ck in city_keys:
            row = await conn.fetchrow("""
                SELECT id FROM streak_freezes
                WHERE ended_at IS NULL AND city_key = $1 LIMIT 1
            """, ck)
            if row:
                await conn.execute(
                    "UPDATE streak_freezes SET ended_at = CURRENT_TIMESTAMP WHERE id=$1",
                    row["id"]
                )
                unfrozen.append(CITIES[ck]["name"])
            else:
                not_frozen.append(CITIES[ck]["name"])

    parts = []
    if unfrozen:
        parts.append(f"🔥 Заморозка стрика снята: {', '.join(unfrozen)}")
    if not_frozen:
        parts.append(f"⚠️ Не была заморожена: {', '.join(not_frozen)}")
    await message.answer("\n".join(parts))

# ========== БЛОКИРОВКА ПОЛЬЗОВАТЕЛЕЙ (/ban, /unban) ==========

async def _resolve_user(arg: str):
    """
    Принимает @username, username (без @) или числовой Telegram ID.
    Возвращает user_id или None, если пользователь не найден в базе
    (т.е. ни разу не запускал бота — тогда бан по username невозможен,
    но можно забанить по ID, если он известен).
    """
    arg = arg.strip()

    if arg.startswith("@"):
        arg = arg[1:]

    async with pool.acquire() as conn:
        if arg.isdigit():
            found = await conn.fetchval(
                "SELECT user_id FROM users WHERE user_id=$1", int(arg)
            )
            if found:
                return found

        # Поиск по username без учёта регистра
        found = await conn.fetchval(
            "SELECT user_id FROM users WHERE LOWER(username)=LOWER($1)", arg
        )
        return found


async def _handle_ban_command(message: types.Message, banned: bool):
    if not is_super_admin(message.from_user.id):
        return

    arg = message.get_args().strip()

    if not arg:
        cmd = "/ban" if banned else "/unban"
        await message.answer(f"Использование: {cmd} @username (или {cmd} user_id)")
        return

    target_uid = await _resolve_user(arg)

    if not target_uid:
        await message.answer(
            "❌ Пользователь не найден. Если он ни разу не запускал бота, "
            "поиск по username невозможен — используй его Telegram ID."
        )
        return

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET banned=$1 WHERE user_id=$2", banned, target_uid
        )

    if banned:
        await message.answer(f"🚫 Пользователь {arg} (ID {target_uid}) заблокирован")
    else:
        await message.answer(f"✅ Пользователь {arg} (ID {target_uid}) разблокирован")


@dp.message_handler(commands=["ban"])
async def ban_cmd(message: types.Message):
    # /ban @username  или  /ban username  или  /ban user_id
    await _handle_ban_command(message, banned=True)


@dp.message_handler(commands=["unban"])
async def unban_cmd(message: types.Message):
    # /unban @username  или  /unban username  или  /unban user_id
    await _handle_ban_command(message, banned=False)


# ========== УПРАВЛЕНИЕ НАЛИЧИЕМ ==========

VALID_CATEGORIES = ("elfliq", "elfworld")


async def _resolve_stock_targets(conn, category: str, target: str) -> list[int]:
    """
    Разбирает target ('all' или '1,2,3') и возвращает список реальных product_id.
    Числа трактуются как position внутри категории (1, 2, 3...), а не как id.
    """
    if target == "all":
        rows = await conn.fetch(
            "SELECT id FROM products WHERE category=$1 ORDER BY position", category
        )
        return [r["id"] for r in rows]
    positions = []
    for part in target.split(","):
        part = part.strip()
        if not part.isdigit():
            return []
        positions.append(int(part))
    rows = await conn.fetch(
        "SELECT id FROM products WHERE category=$1 AND position = ANY($2::int[])",
        category, positions
    )
    return [r["id"] for r in rows]


def _stock_city_list() -> str:
    return ", ".join(CITIES.keys())


def _resolve_stock_cities(actor_id: int, city_arg: str) -> list[str] | None:
    """
    Возвращает список city_key для команды наличия.
    - Высший админ: может указать city_key или 'all'
    - Городской админ: всегда только свой город (city_arg игнорируется)
    Возвращает None если указан неверный город.
    """
    admin_city = get_city_for_admin(actor_id)
    if admin_city is not None:
        # Городской админ — только свой город
        return [admin_city]
    # Высший админ
    if city_arg == "all":
        return list(CITIES.keys())
    if city_arg in CITIES:
        return [city_arg]
    return None  # неверный город


async def _apply_stock_change(conn, city_keys: list[str], product_ids: list[int],
                               mode: str, qty: int) -> None:
    """
    mode: 'add' | 'remove' | 'set'
    Для каждого города применяем изменение к нужному пулу.
    """
    for city_key in city_keys:
        for pid in product_ids:
            if mode == "add":
                await update_stock_for_city(conn, pid, city_key, qty)
            elif mode == "remove":
                await update_stock_for_city(conn, pid, city_key, -qty)
            elif mode == "set":
                await set_stock_for_city(conn, pid, city_key, qty)


def _stock_usage_text(uid_lang_hint: str, cmd: str) -> str:
    city_names = " | ".join(CITIES.keys())
    return (
        f"/{cmd} <{city_names}|all> <elfliq|elfworld> <all|N[,N,...]> <кол-во>\n"
        f"Городской админ: город указывать не нужно.\n"
        f"Высший админ: укажи город или 'all'."
    )


async def _parse_stock_command(message: types.Message, mode: str):
    """Общий парсер для /addstock, /removestock, /setstock."""
    if not is_admin(message.from_user.id):
        return
    uid = message.from_user.id
    actor_id = uid
    parts = message.text.split()

    # Определяем: является ли второй аргумент city или category
    # Городской админ не указывает город: /addstock elfliq all 5
    # Высший админ обязан: /addstock buerhausen elfliq all 5
    admin_city = get_city_for_admin(actor_id)

    usage_key = f"{mode}stock_usage"
    city_list_str = _stock_city_list()

    if admin_city is not None:
        # Городской админ — 4 части: /cmd category target qty
        if len(parts) != 4:
            await message.answer(_stock_usage_text(uid, f"{mode}stock"))
            return
        city_arg = None
        category = parts[1].lower()
        target = parts[2].lower()
        qty_str = parts[3]
        city_keys = [admin_city]
    else:
        # Высший админ — 5 частей: /cmd city|all category target qty
        if len(parts) != 5:
            await message.answer(
                (await t(uid, "stock_city_usage")).format(cities=city_list_str)
            )
            return
        city_arg = parts[1].lower()
        category = parts[2].lower()
        target = parts[3].lower()
        qty_str = parts[4]
        city_keys = _resolve_stock_cities(actor_id, city_arg)
        if city_keys is None:
            await message.answer(
                (await t(uid, "stock_city_invalid")).format(cities=city_list_str)
            )
            return

    if category not in VALID_CATEGORIES:
        await message.answer(_stock_usage_text(uid, f"{mode}stock"))
        return

    # setstock теперь поддерживает 'all' так же как addstock/removestock
    try:
        qty = int(qty_str)
        if mode in ("add", "remove") and qty <= 0:
            raise ValueError
        if mode == "set" and qty < 0:
            raise ValueError
    except ValueError:
        await message.answer(_stock_usage_text(uid, f"{mode}stock"))
        return

    async with pool.acquire() as conn:
        ids = await _resolve_stock_targets(conn, category, target)
        if not ids:
            await message.answer(_stock_usage_text(uid, f"{mode}stock"))
            return
        await _apply_stock_change(conn, city_keys, ids, mode, qty)

    await message.answer((await t(uid, "stock_updated")).format(count=len(ids) * len(city_keys)))


@dp.message_handler(commands=["addstock"])
async def addstock_cmd(message: types.Message):
    """/addstock [city|all] <elfliq|elfworld> <all|N[,N,...]> <кол-во>"""
    await _parse_stock_command(message, "add")


@dp.message_handler(commands=["removestock"])
async def removestock_cmd(message: types.Message):
    """/removestock [city|all] <elfliq|elfworld> <all|N[,N,...]> <кол-во>"""
    await _parse_stock_command(message, "remove")


@dp.message_handler(commands=["setstock"])
async def setstock_cmd(message: types.Message):
    """/setstock [city|all] <elfliq|elfworld> <N[,N,...]> <кол-во>"""
    await _parse_stock_command(message, "set")

# ========== СТАТИСТИКА ПРОДАЖ (/sales) ==========

class SalesState(StatesGroup):
    waiting_date_range = State()


def _parse_items_str(items_str: str) -> list[tuple[int, int]]:
    """Разбирает строку 'pid:qty,pid:qty' → [(pid, qty), ...]"""
    result = []
    for part in (items_str or "").split(","):
        part = part.strip()
        if ":" not in part:
            continue
        try:
            pid, qty = part.split(":", 1)
            result.append((int(pid), int(qty)))
        except ValueError:
            continue
    return result


async def _build_sales_report(date_from: date, date_to: date) -> str:
    """
    Считает продажи за период [date_from, date_to] включительно.
    Учитывает только confirmed-заказы (не отменённые и не pending).
    Бесплатные банки (gift_requests) не учитываются — они не проходят через orders.
    """
    async with pool.acquire() as conn:
        orders = await conn.fetch("""
            SELECT items, total, discount
            FROM orders
            WHERE status = 'confirmed'
              AND DATE(created_at) >= $1
              AND DATE(created_at) <= $2
        """, date_from, date_to)

        if not orders:
            return f"📊 Продажи за {date_from.strftime('%d.%m')}–{date_to.strftime('%d.%m.%Y')}\n\nЗаказов не найдено."

        # Собираем агрегацию по product_id
        sales: dict[int, int] = {}  # pid → total_qty
        total_revenue = 0.0

        for order in orders:
            # total в заказе — уже итоговая сумма после скидки, это выручка
            total_revenue += float(order["total"] or 0)
            for pid, qty in _parse_items_str(order["items"]):
                sales[pid] = sales.get(pid, 0) + qty

        if not sales:
            return f"📊 Продажи за {date_from.strftime('%d.%m')}–{date_to.strftime('%d.%m.%Y')}\n\nДанных нет."

        # Загружаем данные о товарах одним запросом
        pids = list(sales.keys())
        products_rows = await conn.fetch("""
            SELECT id, name_ru, price, category
            FROM products
            WHERE id = ANY($1::int[])
        """, pids)

        prod_info: dict[int, dict] = {r["id"]: dict(r) for r in products_rows}

        # Разбивка по категориям
        by_category: dict[str, list[tuple[str, int, float]]] = {"elfliq": [], "elfworld": []}
        total_jars = 0

        for pid, qty in sorted(sales.items(), key=lambda x: -x[1]):
            info = prod_info.get(pid)
            if not info:
                continue
            name = info["name_ru"]
            price = float(info["price"] or 0)
            revenue = round(price * qty, 2)
            cat = info["category"] or "elfliq"
            by_category.setdefault(cat, []).append((name, qty, revenue))
            total_jars += qty

    # Формируем отчёт
    date_label = (
        date_from.strftime("%d.%m")
        if date_from == date_to
        else f"{date_from.strftime('%d.%m')}–{date_to.strftime('%d.%m.%Y')}"
    )
    lines = [f"📊 Продажи за {date_label}\n"]

    for cat_key, cat_label in [("elfliq", "🧪 ELFLIQ"), ("elfworld", "🌍 ELFWORLD")]:
        items_in_cat = by_category.get(cat_key, [])
        if not items_in_cat:
            continue
        lines.append(f"\n{cat_label}:")
        for name, qty, rev in items_in_cat:
            lines.append(f"  {name} — {qty} шт. — {rev:.2f}€")

    lines.append(f"\n────────────────")
    lines.append(f"Всего банок: {total_jars}")
    lines.append(f"Выручка: {total_revenue:.2f}€")

    return "\n".join(lines)


@dp.message_handler(commands=["sales"])
async def sales_cmd(message: types.Message, state: FSMContext):
    if not is_super_admin(message.from_user.id):
        return

    today = date.today()
    yesterday = today - timedelta(days=1)

    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📅 Сегодня",       callback_data="sales_today"),
        InlineKeyboardButton("📅 Вчера",          callback_data="sales_yesterday"),
        InlineKeyboardButton("📅 7 дней",         callback_data="sales_7d"),
        InlineKeyboardButton("📅 30 дней",        callback_data="sales_30d"),
        InlineKeyboardButton("✏️ Произвольный период", callback_data="sales_custom"),
    )
    await message.answer("📊 Выберите период статистики:", reply_markup=kb)


@dp.callback_query_handler(lambda c: c.data.startswith("sales_"), state="*")
async def sales_period(call: types.CallbackQuery, state: FSMContext):
    if not is_super_admin(call.from_user.id):
        return

    await call.answer()
    today = date.today()

    if call.data == "sales_today":
        report = await _build_sales_report(today, today)
        await call.message.edit_text(report, reply_markup=None)

    elif call.data == "sales_yesterday":
        yesterday = today - timedelta(days=1)
        report = await _build_sales_report(yesterday, yesterday)
        await call.message.edit_text(report, reply_markup=None)

    elif call.data == "sales_7d":
        report = await _build_sales_report(today - timedelta(days=6), today)
        await call.message.edit_text(report, reply_markup=None)

    elif call.data == "sales_30d":
        report = await _build_sales_report(today - timedelta(days=29), today)
        await call.message.edit_text(report, reply_markup=None)

    elif call.data == "sales_custom":
        await SalesState.waiting_date_range.set()
        await call.message.edit_text(
            "✏️ Введите диапазон дат в формате:\n<code>ДД.ММ.ГГГГ-ДД.ММ.ГГГГ</code>\n\nНапример: <code>01.07.2025-10.07.2025</code>",
            parse_mode="HTML",
            reply_markup=None,
        )


@dp.message_handler(state=SalesState.waiting_date_range)
async def sales_custom_dates(message: types.Message, state: FSMContext):
    if not is_super_admin(message.from_user.id):
        await state.finish()
        return

    raw = message.text.strip()
    try:
        parts = raw.split("-")
        # Поддерживаем оба формата: "01.07.2025-10.07.2025" или "01.07-10.07.2025"
        if len(parts) == 2:
            date_from = datetime.strptime(parts[0].strip(), "%d.%m.%Y").date()
            date_to   = datetime.strptime(parts[1].strip(), "%d.%m.%Y").date()
        else:
            raise ValueError("bad format")
    except ValueError:
        await message.answer(
            "❌ Неверный формат. Пример: <code>01.07.2025-10.07.2025</code>",
            parse_mode="HTML",
        )
        return

    if date_from > date_to:
        date_from, date_to = date_to, date_from

    await state.finish()
    report = await _build_sales_report(date_from, date_to)
    await message.answer(report)


# ========== ПРОСМОТР НАЛИЧИЯ (/inventory) ==========

@dp.message_handler(commands=["inventory"])
async def inventory_cmd(message: types.Message):
    """
    Показывает актуальный остаток товаров.
    Городской админ видит только свой город.
    Высший админ видит все города.
    Дополнительно: /inventory <city> — только нужный город (для высшего).
    """
    if not is_admin(message.from_user.id):
        return

    uid = message.from_user.id
    actor_city = get_city_for_admin(uid)
    args = message.get_args().strip().lower()

    # Определяем, какие города показывать
    if actor_city is not None:
        # Городской админ — только свой
        city_keys = [actor_city]
    elif args and args in CITIES:
        city_keys = [args]
    elif args == "all" or not args:
        city_keys = list(CITIES.keys())
    else:
        await message.answer(f"❌ Неверный город. Доступные: {_stock_city_list()}")
        return

    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, name_ru, stock, category, in_stock
            FROM products
            ORDER BY category, position, id
        """)
        # Загружаем city_stock для всех нужных городов
        city_stock_rows = await conn.fetch(
            "SELECT city_key, product_id, stock FROM city_stock WHERE city_key = ANY($1::text[])",
            city_keys
        )

    if not rows:
        await message.answer("📦 Товаров в базе нет.")
        return

    # Строим словарь: {(city_key, product_id): stock}
    cs_map: dict[tuple[str, int], int] = {}
    for csr in city_stock_rows:
        cs_map[(csr["city_key"], csr["product_id"])] = csr["stock"]

    cat_labels = {"elfliq": "🧪 ELFLIQ", "elfworld": "🌍 ELFWORLD"}

    lines = ["📦 Текущий остаток товаров\n"]

    for city_key in city_keys:
        pool_key = CITIES[city_key]["stock_pool"]
        city_name = CITIES[city_key]["name"]
        lines.append(f"🏙 {city_name}:")

        by_cat: dict[str, list] = {}
        for r in rows:
            cat = r["category"] or "elfliq"
            by_cat.setdefault(cat, []).append(r)

        total_city = 0
        oos_city = 0

        for cat_key in ("elfliq", "elfworld"):
            items = by_cat.get(cat_key, [])
            if not items:
                continue
            lines.append(f"  {cat_labels.get(cat_key, cat_key.upper())}:")

            for r in items:
                pid = r["id"]
                if pool_key == "default":
                    stock = r["stock"] or 0
                else:
                    stock = cs_map.get((pool_key, pid), 0)

                name = r["name_ru"]
                if stock == 0:
                    status = "❌ нет"
                    oos_city += 1
                elif stock <= 3:
                    status = f"⚠️ {stock} шт."
                else:
                    status = f"✅ {stock} шт."
                total_city += stock
                lines.append(f"    {name}: {status}")

        lines.append(f"  Итого: {total_city} ед. | Нет: {oos_city} поз.")
        lines.append("")

    await message.answer("\n".join(lines))


# ========== НОВЫЕ АДМИН-КОМАНДЫ ==========

# ── /pending ──────────────────────────────────────────────────────────────────

@dp.message_handler(commands=["pending"])
async def pending_cmd(message: types.Message):
    """Список ожидающих подтверждения заказов."""
    uid = message.from_user.id
    if not is_admin(uid):
        return

    actor_city = get_city_for_admin(uid)

    async with pool.acquire() as conn:
        if actor_city:
            rows = await conn.fetch("""
                SELECT id, user_id, total, payment, created_at
                FROM orders WHERE status='pending' AND city_key=$1
                ORDER BY created_at ASC
            """, actor_city)
        else:
            city_arg = message.get_args().strip().lower()
            if city_arg and city_arg in CITIES:
                rows = await conn.fetch("""
                    SELECT id, user_id, total, payment, created_at, city_key
                    FROM orders WHERE status='pending' AND city_key=$1
                    ORDER BY created_at ASC
                """, city_arg)
            else:
                rows = await conn.fetch("""
                    SELECT id, user_id, total, payment, created_at, city_key
                    FROM orders WHERE status='pending'
                    ORDER BY created_at ASC
                """)

    if not rows:
        await message.answer("✅ Нет ожидающих заказов.")
        return

    lines = [f"⏳ Ожидающих заказов: {len(rows)}\n"]
    for r in rows:
        city_label = f" [{CITIES.get(r['city_key'], {}).get('name', r['city_key'])}]" if not actor_city else ""
        dt = r["created_at"].strftime("%d.%m %H:%M") if r["created_at"] else "—"
        lines.append(f"#{r['id']}{city_label} | {r['total']}€ | {r['payment']} | {dt}")

    # Добавляем ожидающие gift-заявки
    async with pool.acquire() as conn:
        if actor_city:
            gift_rows = await conn.fetch("""
                SELECT id, username, created_at FROM gift_requests
                WHERE status='pending' AND city_key=$1
                ORDER BY created_at ASC
            """, actor_city)
        else:
            city_arg = message.get_args().strip().lower()
            if city_arg and city_arg in CITIES:
                gift_rows = await conn.fetch("""
                    SELECT id, username, created_at FROM gift_requests
                    WHERE status='pending' AND city_key=$1 ORDER BY created_at ASC
                """, city_arg)
            else:
                gift_rows = await conn.fetch("""
                    SELECT id, username, city_key, created_at FROM gift_requests
                    WHERE status='pending' ORDER BY created_at ASC
                """)

    if gift_rows:
        lines.append(f"\n🎁 Ожидающих gift-заявок: {len(gift_rows)}")
        for r in gift_rows:
            city_label = f" [{CITIES.get(r['city_key'], {}).get('name', r['city_key'])}]" if not actor_city else ""
            dt = r["created_at"].strftime("%d.%m %H:%M") if r["created_at"] else "—"
            lines.append(f"gift#{r['id']}{city_label} | @{r['username'] or '—'} | {dt}")

    await message.answer("\n".join(lines))


# ── /order ────────────────────────────────────────────────────────────────────

@dp.message_handler(commands=["order"])
async def order_cmd(message: types.Message):
    """Информация о заказе по ID: /order <id>"""
    uid = message.from_user.id
    if not is_admin(uid):
        return

    args = message.get_args().strip()
    if not args or not args.isdigit():
        await message.answer("Использование: /order <id>")
        return

    order_id = int(args)
    async with pool.acquire() as conn:
        order = await conn.fetchrow("""
            SELECT id, user_id, items, total, payment, discount,
                   status, city_key, is_delivery, created_at
            FROM orders WHERE id=$1
        """, order_id)

    if not order:
        await message.answer(f"❌ Заказ #{order_id} не найден.")
        return

    actor_city = get_city_for_admin(uid)
    if actor_city and order["city_key"] != actor_city:
        await message.answer("❌ Этот заказ относится к другому городу.")
        return

    city_name = CITIES.get(order["city_key"] or "", {}).get("name", order["city_key"] or "—")
    delivery_label = "🚚 Доставка" if order["is_delivery"] else "🏪 Самовывоз"
    dt = order["created_at"].strftime("%d.%m.%Y %H:%M") if order["created_at"] else "—"

    # Читаем имена товаров
    lines = [
        f"📦 Заказ #{order_id}",
        f"Статус: {order['status']}",
        f"Город: {city_name} | {delivery_label}",
        f"Оплата: {order['payment']}",
        f"Итого: {order['total']}€ (скидка: {order['discount'] or 0}€)",
        f"Дата: {dt}",
        f"User ID: {order['user_id']}",
        "Товары:",
    ]
    # Батч-запрос имён товаров
    item_parts = [p for p in (order["items"] or "").split(",") if ":" in p]
    pids = [int(p.split(":")[0]) for p in item_parts]
    async with pool.acquire() as conn:
        prod_rows = await conn.fetch(
            "SELECT id, name_ru FROM products WHERE id = ANY($1::int[])", pids
        )
    prod_map = {r["id"]: r["name_ru"] for r in prod_rows}
    for part in item_parts:
        pid_s, qty_s = part.split(":")
        name = prod_map.get(int(pid_s), f"#{pid_s}")
        lines.append(f"  • {name} x{qty_s}")

    await message.answer("\n".join(lines))


# ── /cancelorder ──────────────────────────────────────────────────────────────

@dp.message_handler(commands=["cancelorder"])
async def cancelorder_cmd(message: types.Message):
    """/cancelorder <id> — отменить заказ по ID."""
    uid = message.from_user.id
    if not is_admin(uid):
        return

    args = message.get_args().strip()
    if not args or not args.isdigit():
        await message.answer("Использование: /cancelorder <id>")
        return

    order_id = int(args)
    async with pool.acquire() as conn:
        order = await conn.fetchrow(
            "SELECT status, admin_message_ids, city_key FROM orders WHERE id=$1", order_id
        )

    if not order:
        await message.answer(f"❌ Заказ #{order_id} не найден.")
        return

    actor_city = get_city_for_admin(uid)
    if actor_city and order["city_key"] != actor_city:
        await message.answer("❌ Этот заказ относится к другому городу.")
        return

    if order["status"] != "pending":
        await message.answer(f"⚠️ Заказ #{order_id} уже имеет статус: {order['status']}.")
        return

    async with pool.acquire() as conn:
        await conn.execute("UPDATE orders SET status='cancelled' WHERE id=$1", order_id)
        await release_reserved_stock(conn, order_id)

    await message.answer(f"✅ Заказ #{order_id} отменён, stock возвращён.")

    # Обновляем сообщение у других админов
    msg_ids_raw = order["admin_message_ids"] or ""
    admin_username = message.from_user.username or "admin"
    await _sync_admin_messages(
        msg_ids_raw=msg_ids_raw,
        actor_id=uid,
        base_text=f"Заказ #{order_id}",
        status_self=f"\n\n❌ ОТМЕНЕНО командой /cancelorder",
        status_others=f"\n\n❌ ОТМЕНЕНО @{admin_username} командой /cancelorder"
    )


# ── /stats ────────────────────────────────────────────────────────────────────

@dp.message_handler(commands=["stats"])
async def stats_cmd(message: types.Message):
    """Краткая сводка по магазину (только super_admin)."""
    if not is_super_admin(message.from_user.id):
        return

    async with pool.acquire() as conn:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
        active_7d = await conn.fetchval("""
            SELECT COUNT(DISTINCT user_id) FROM orders
            WHERE created_at >= NOW() - INTERVAL '7 days'
        """)
        active_30d = await conn.fetchval("""
            SELECT COUNT(DISTINCT user_id) FROM orders
            WHERE created_at >= NOW() - INTERVAL '30 days'
        """)
        pending_count = await conn.fetchval(
            "SELECT COUNT(*) FROM orders WHERE status='pending'"
        )
        revenue_month = await conn.fetchval("""
            SELECT COALESCE(SUM(total), 0) FROM orders
            WHERE status='confirmed'
            AND created_at >= DATE_TRUNC('month', NOW())
        """)
        total_orders_month = await conn.fetchval("""
            SELECT COUNT(*) FROM orders
            WHERE status='confirmed'
            AND created_at >= DATE_TRUNC('month', NOW())
        """)

    text = (
        f"📊 Статистика магазина\n\n"
        f"👥 Пользователей всего: {total_users}\n"
        f"🔥 Активных за 7 дней: {active_7d}\n"
        f"📅 Активных за 30 дней: {active_30d}\n\n"
        f"⏳ Ожидают подтверждения: {pending_count}\n\n"
        f"💰 Выручка за текущий месяц: {revenue_month:.2f}€\n"
        f"📦 Заказов за текущий месяц: {total_orders_month}"
    )
    await message.answer(text)


# ── /user ─────────────────────────────────────────────────────────────────────

@dp.message_handler(commands=["user"])
async def user_cmd(message: types.Message):
    """/user <@username|id> — профиль пользователя (только super_admin)."""
    if not is_super_admin(message.from_user.id):
        return

    args = message.get_args().strip().lstrip("@")
    if not args:
        await message.answer("Использование: /user <@username|id>")
        return

    async with pool.acquire() as conn:
        if args.isdigit():
            row = await conn.fetchrow(
                "SELECT * FROM users WHERE user_id=$1", int(args)
            )
        else:
            row = await conn.fetchrow(
                "SELECT * FROM users WHERE username=$1", args
            )
        if not row:
            await message.answer("❌ Пользователь не найден.")
            return

        order_count = await conn.fetchval(
            "SELECT COUNT(*) FROM orders WHERE user_id=$1 AND status='confirmed'", row["user_id"]
        )

    city_name = CITIES.get(row["city"] or "", {}).get("name", row["city"] or "не выбран")
    promo_line = ""
    if row.get("promo_code"):
        promo_line = f"\n🎟 Промокод: {row['promo_code']} ({row.get('promo_type','')})"

    text = (
        f"👤 Пользователь\n\n"
        f"ID: {row['user_id']}\n"
        f"Username: @{row['username'] or '—'}\n"
        f"Язык: {row.get('language', '—')}\n"
        f"Город: {city_name}\n"
        f"Заблокирован: {'да' if row.get('banned') else 'нет'}\n\n"
        f"📦 Товаров куплено: {row.get('total_items', 0)}\n"
        f"🧾 Подтв. заказов: {order_count}\n"
        f"💸 Сэкономлено: {row.get('total_saved', 0):.2f}€\n\n"
        f"🔥 Стрик: {row.get('streak_weeks', 0)} нед.\n"
        f"🎁 Бесплатная банка: {'да' if row.get('free_jar_bonus') else 'нет'}"
        f"{promo_line}"
    )
    await message.answer(text)


# ── /broadcast ────────────────────────────────────────────────────────────────

@dp.message_handler(commands=["broadcast"])
async def broadcast_cmd(message: types.Message):
    """/broadcast <city|all> текст — рассылка сообщения (только super_admin)."""
    if not is_super_admin(message.from_user.id):
        return

    args = message.get_args()
    if not args:
        city_list = " | ".join(CITIES.keys())
        await message.answer(f"Использование: /broadcast <{city_list}|all> текст")
        return

    parts = args.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Нужно указать город/all и текст сообщения.")
        return

    city_arg, text = parts[0].lower(), parts[1]

    async with pool.acquire() as conn:
        if city_arg == "all":
            users = await conn.fetch(
                "SELECT user_id FROM users WHERE banned IS NOT TRUE"
            )
        elif city_arg in CITIES:
            users = await conn.fetch(
                "SELECT user_id FROM users WHERE city=$1 AND banned IS NOT TRUE", city_arg
            )
        else:
            await message.answer(f"❌ Неизвестный город: {city_arg}")
            return

    sent, failed = 0, 0
    total = len(users)
    progress_msg = await message.answer(f"📤 Рассылка запущена... 0/{total}")

    for i, u in enumerate(users, 1):
        try:
            await bot.send_message(u["user_id"], text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # Rate limit ~20 msg/s
        if i % 50 == 0:
            try:
                await progress_msg.edit_text(f"📤 Рассылка... {i}/{total} (✅{sent} ❌{failed})")
            except Exception:
                pass

    summary = f"✅ Рассылка завершена.\nОтправлено: {sent}\nОшибок: {failed}\nВсего: {total}"
    try:
        await progress_msg.edit_text(summary)
    except Exception:
        await message.answer(summary)
    if failed:
        await alert_super_admins(f"broadcast: {failed} ошибок из {total} при рассылке в {city_arg}")


# ── /exportorders ─────────────────────────────────────────────────────────────

@dp.message_handler(commands=["exportorders"])
async def exportorders_cmd(message: types.Message):
    """/exportorders [YYYY-MM-DD] — выгрузка заказов за день (только super_admin)."""
    if not is_super_admin(message.from_user.id):
        return

    args = message.get_args().strip()
    if args:
        try:
            export_date = datetime.strptime(args, "%Y-%m-%d").date()
        except ValueError:
            await message.answer("Формат даты: YYYY-MM-DD, например /exportorders 2025-07-01")
            return
    else:
        export_date = date.today()

    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT o.id, o.user_id, u.username, o.total, o.payment,
                   o.status, o.city_key, o.is_delivery, o.items, o.created_at
            FROM orders o
            LEFT JOIN users u ON u.user_id = o.user_id
            WHERE DATE(o.created_at) = $1
            ORDER BY o.created_at ASC
        """, export_date)

        if not rows:
            await message.answer(f"Нет заказов за {export_date}.")
            return

        # Собираем все pid заранее для батч-запроса
        all_pids = set()
        for r in rows:
            for part in (r["items"] or "").split(","):
                if ":" in part:
                    pid_s, _ = part.split(":")
                    all_pids.add(int(pid_s))

        prod_name_map = {}
        if all_pids:
            prod_rows = await conn.fetch(
                "SELECT id, name_ru FROM products WHERE id = ANY($1::int[])", list(all_pids)
            )
            prod_name_map = {r["id"]: r["name_ru"] for r in prod_rows}

        lines = [f"📋 Заказы за {export_date} ({len(rows)} шт.)\n"]
        for r in rows:
            city_name = CITIES.get(r["city_key"] or "", {}).get("name", r["city_key"] or "—")
            dt = r["created_at"].strftime("%H:%M")
            delivery_label = "доставка" if r["is_delivery"] else "самовывоз"
            items_readable = []
            for part in (r["items"] or "").split(","):
                if ":" in part:
                    pid_s, qty_s = part.split(":")
                    name = prod_name_map.get(int(pid_s), f"#{pid_s}")
                    items_readable.append(f"{name}×{qty_s}")
            lines.append(
                f"#{r['id']} {dt} | @{r['username'] or r['user_id']} | "
                f"{city_name}/{delivery_label} | {r['payment']} | {r['total']}€ | "
                f"{r['status']} | {', '.join(items_readable)}"
            )

    await message.answer("\n".join(lines))


# ========== УПРАВЛЕНИЕ ГОРОДАМИ ==========

class CityManageState(StatesGroup):
    confirm_add    = State()
    confirm_remove = State()
    edit_choose    = State()
    confirm_edit   = State()


@dp.message_handler(commands=["addcity"])
async def addcity_cmd(message: types.Message, state: FSMContext):
    """
    /addcity <city_key> <display_name> <admin_id> [stock_pool]
    stock_pool по умолчанию = city_key (отдельный пул)
    Пример: /addcity hamburg Hamburg 123456789
    """
    if not is_super_admin(message.from_user.id):
        return

    parts = message.text.split(maxsplit=4)
    # /addcity city_key name admin_id [pool]
    if len(parts) < 4:
        await message.answer(
            "Использование:\n"
            "/addcity <city_key> <display_name> <admin_id> [stock_pool]\n\n"
            "Примеры:\n"
            "/addcity hamburg Hamburg 123456789\n"
            "/addcity berlin Berlin 987654321 default"
        )
        return

    city_key = parts[1].lower().strip()
    display_name = parts[2].strip()
    admin_id_str = parts[3].strip()
    stock_pool = parts[4].strip() if len(parts) > 4 else city_key

    if not admin_id_str.isdigit():
        await message.answer("❌ admin_id должен быть числом.")
        return

    admin_id = int(admin_id_str)

    if city_key in CITIES:
        await message.answer(f"❌ Город с ключом '{city_key}' уже существует.")
        return

    if not city_key.isalnum() or len(city_key) > 32:
        await message.answer("❌ city_key должен содержать только буквы и цифры, до 32 символов.")
        return

    confirm_text = (
        f"🏙 Добавить новый город?\n\n"
        f"Ключ: <code>{city_key}</code>\n"
        f"Название: <b>{display_name}</b>\n"
        f"Городской админ ID: <code>{admin_id}</code>\n"
        f"Stock pool: <code>{stock_pool}</code>\n\n"
        f"Подтвердите действие:"
    )
    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("✅ Подтвердить", callback_data="citymanage_add_confirm"),
        InlineKeyboardButton("❌ Отменить",   callback_data="citymanage_cancel")
    )

    await state.update_data(
        city_key=city_key, display_name=display_name,
        admin_id=admin_id, stock_pool=stock_pool
    )
    await CityManageState.confirm_add.set()
    await message.answer(confirm_text, parse_mode="HTML", reply_markup=kb)


@dp.callback_query_handler(lambda c: c.data == "citymanage_add_confirm", state=CityManageState.confirm_add)
async def citymanage_add_confirm(call, state: FSMContext):
    if not is_super_admin(call.from_user.id):
        await call.answer("❌ Нет доступа", show_alert=True)
        return

    data = await state.get_data()
    await state.finish()

    city_key    = data["city_key"]
    display_name = data["display_name"]
    admin_id    = data["admin_id"]
    stock_pool  = data["stock_pool"]

    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO cities (city_key, name, stock_pool, admin_ids)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (city_key) DO UPDATE
            SET name=$2, stock_pool=$3, admin_ids=$4
        """, city_key, display_name, stock_pool, str(admin_id))

    await load_cities_from_db()
    await call.answer("✅ Город добавлен")
    await call.message.edit_text(
        f"✅ Город <b>{display_name}</b> (<code>{city_key}</code>) добавлен.\n"
        f"Городской админ: <code>{admin_id}</code>",
        parse_mode="HTML"
    )


@dp.message_handler(commands=["removecity"])
async def removecity_cmd(message: types.Message, state: FSMContext):
    """/removecity <city_key> — удалить город."""
    if not is_super_admin(message.from_user.id):
        return

    args = message.get_args().strip().lower()
    if not args:
        city_list = ", ".join(CITIES.keys())
        await message.answer(f"Использование: /removecity <city_key>\nГорода: {city_list}")
        return

    if args not in CITIES:
        await message.answer(f"❌ Город '{args}' не найден.")
        return

    city_name = CITIES[args]["name"]
    admins = CITIES[args].get("admins", [])

    # Считаем что будет затронуто
    async with pool.acquire() as conn:
        user_count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE city=$1", args)
        pending_count = await conn.fetchval(
            "SELECT COUNT(*) FROM orders WHERE city_key=$1 AND status='pending'", args
        )

    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("✅ Подтвердить удаление", callback_data="citymanage_remove_confirm"),
        InlineKeyboardButton("❌ Отменить", callback_data="citymanage_cancel")
    )

    warn = ""
    if pending_count:
        warn = f"\n\n⚠️ Есть {pending_count} ожидающих заказов! Они останутся в БД, но потеряют привязку к городу."

    await state.update_data(city_key=args)
    await CityManageState.confirm_remove.set()
    await message.answer(
        f"🗑 Удалить город <b>{city_name}</b> (<code>{args}</code>)?\n\n"
        f"Городские админы: {', '.join(str(a) for a in admins) or '—'}\n"
        f"Пользователей с этим городом: {user_count}"
        f"{warn}",
        parse_mode="HTML", reply_markup=kb
    )


@dp.callback_query_handler(lambda c: c.data == "citymanage_remove_confirm", state=CityManageState.confirm_remove)
async def citymanage_remove_confirm(call, state: FSMContext):
    if not is_super_admin(call.from_user.id):
        await call.answer("❌ Нет доступа", show_alert=True)
        return

    data = await state.get_data()
    await state.finish()
    city_key = data["city_key"]
    city_name = CITIES.get(city_key, {}).get("name", city_key)

    async with pool.acquire() as conn:
        # Обнуляем город у пользователей
        await conn.execute("UPDATE users SET city=NULL WHERE city=$1", city_key)
        # Удаляем city_stock
        await conn.execute("DELETE FROM city_stock WHERE city_key=$1", city_key)
        # Удаляем город
        await conn.execute("DELETE FROM cities WHERE city_key=$1", city_key)

    await load_cities_from_db()
    await call.answer("✅ Город удалён")
    await call.message.edit_text(
        f"✅ Город <b>{city_name}</b> удалён.\n"
        f"Пользователи этого города будут попрошены выбрать город заново.",
        parse_mode="HTML"
    )


@dp.message_handler(commands=["editcity"])
async def editcity_cmd(message: types.Message, state: FSMContext):
    """
    /editcity <city_key> — редактировать данные города.
    Показывает текущие данные и предлагает ввести новые.
    """
    if not is_super_admin(message.from_user.id):
        return

    args = message.get_args().strip().lower()
    if not args:
        city_list = ", ".join(CITIES.keys())
        await message.answer(f"Использование: /editcity <city_key>\nГорода: {city_list}")
        return

    if args not in CITIES:
        await message.answer(f"❌ Город '{args}' не найден.")
        return

    cfg = CITIES[args]
    admins_str = ",".join(str(a) for a in cfg.get("admins", []))

    await state.update_data(city_key=args)
    await CityManageState.edit_choose.set()

    await message.answer(
        f"✏️ Редактирование города <b>{cfg['name']}</b> (<code>{args}</code>)\n\n"
        f"Текущие данные:\n"
        f"• Название: <b>{cfg['name']}</b>\n"
        f"• Stock pool: <code>{cfg['stock_pool']}</code>\n"
        f"• Админы (ID через запятую): <code>{admins_str or '—'}</code>\n\n"
        f"Введите новые данные в формате:\n"
        f"<code>display_name | stock_pool | admin_id1,admin_id2</code>\n\n"
        f"Пример:\n"
        f"<code>Hamburg | hamburg | 123456789,987654321</code>\n\n"
        f"Для отмены: /cancel",
        parse_mode="HTML"
    )


@dp.message_handler(state=CityManageState.edit_choose)
async def citymanage_edit_input(message: types.Message, state: FSMContext):
    if not is_super_admin(message.from_user.id):
        await state.finish()
        return

    if message.text.strip() == "/cancel":
        await state.finish()
        await message.answer("❌ Редактирование отменено.")
        return

    parts = [p.strip() for p in message.text.split("|")]
    if len(parts) != 3:
        await message.answer(
            "❌ Неверный формат. Нужно:\n"
            "<code>display_name | stock_pool | admin_id1,admin_id2</code>",
            parse_mode="HTML"
        )
        return

    display_name, stock_pool, admins_raw = parts
    admin_ids = []
    for a in admins_raw.split(","):
        a = a.strip()
        if a.isdigit():
            admin_ids.append(int(a))
        elif a:
            await message.answer(f"❌ Неверный admin_id: {a}")
            return

    data = await state.get_data()
    city_key = data["city_key"]

    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("✅ Подтвердить", callback_data="citymanage_edit_confirm"),
        InlineKeyboardButton("❌ Отменить",   callback_data="citymanage_cancel")
    )

    admins_preview = ", ".join(str(a) for a in admin_ids) or "—"
    await state.update_data(
        display_name=display_name, stock_pool=stock_pool, admin_ids=admin_ids
    )
    await CityManageState.confirm_edit.set()
    await message.answer(
        f"Сохранить изменения для <b>{city_key}</b>?\n\n"
        f"• Название: <b>{display_name}</b>\n"
        f"• Stock pool: <code>{stock_pool}</code>\n"
        f"• Админы: <code>{admins_preview}</code>",
        parse_mode="HTML", reply_markup=kb
    )


@dp.callback_query_handler(lambda c: c.data == "citymanage_edit_confirm", state=CityManageState.confirm_edit)
async def citymanage_edit_confirm(call, state: FSMContext):
    if not is_super_admin(call.from_user.id):
        await call.answer("❌ Нет доступа", show_alert=True)
        return

    data = await state.get_data()
    await state.finish()

    city_key     = data["city_key"]
    display_name = data["display_name"]
    stock_pool   = data["stock_pool"]
    admin_ids    = data["admin_ids"]
    admin_ids_str = ",".join(str(a) for a in admin_ids)

    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE cities SET name=$1, stock_pool=$2, admin_ids=$3
            WHERE city_key=$4
        """, display_name, stock_pool, admin_ids_str, city_key)

    await load_cities_from_db()
    await call.answer("✅ Данные города обновлены")
    await call.message.edit_text(
        f"✅ Город <b>{display_name}</b> (<code>{city_key}</code>) обновлён.",
        parse_mode="HTML"
    )


@dp.callback_query_handler(lambda c: c.data == "citymanage_cancel",
                            state=[CityManageState.confirm_add,
                                   CityManageState.confirm_remove,
                                   CityManageState.edit_choose,
                                   CityManageState.confirm_edit])
async def citymanage_cancel(call, state: FSMContext):
    await state.finish()
    await call.answer("Отменено")
    await call.message.edit_text("❌ Действие отменено.")


# ========== ЗАПУСК ==========

async def on_startup(dp):
    await init_db()


async def run():
    await on_startup(dp)
    await dp.start_polling(reset_webhook=True)


if __name__ == "__main__":
    asyncio.run(run())
