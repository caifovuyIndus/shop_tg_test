import logging 
import psycopg2
import os
import asyncio
import asyncpg
import random
import aiohttp
from datetime import date, timedelta, datetime
from urllib.parse import quote

from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from aiogram.utils import executor
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup

# ========== КОНФИГ ==========

BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(token=BOT_TOKEN)
ADMIN_IDS = [7805603791, 8283121468, 5317145892] 
def is_admin(uid):
    return uid in ADMIN_IDS 

logging.basicConfig(level=logging.INFO)


storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

_bot_username = None

# ========== FSM: ФОРМА ДОСТАВКИ ==========

class DeliveryForm(StatesGroup):
    name     = State()   # Шаг 1 — Имя Фамилия
    phone    = State()   # Шаг 2 — Телефон
    address  = State()   # Шаг 3 — Bundesland. Stadt. Straße
    tracking = State()   # Шаг 4 — трек-номер или без

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
            started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            ended_at TIMESTAMP
        )
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

        # ========== ПРОМОКОДЫ ==========
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
        "givefreejar_done": "✅ Бесплатная банка выдана",
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
        "givefreejar_done": "✅ Безкоштовну банку видано",
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
        "givefreejar_done": "✅ Gratis-Dose wurde vergeben",
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

async def get_user_discounts(uid):
    async with pool.acquire() as conn:
        user = await conn.fetchrow("""
            SELECT total_items, referrals, current_discount, ref_bonus, promo_discount
            FROM users WHERE user_id=$1
        """, uid)

    if not user:
        return []

    items = user["total_items"]
    streak = await get_effective_streak(uid)
    refs = user["referrals"]
    wheel_discount = user["current_discount"]
    ref_bonus = user["ref_bonus"] or 0
    promo_discount = user["promo_discount"] or 0

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
    # Проверяем тестовый override (только для админов через /testprice)
    try:
        async with pool.acquire() as conn:
            override = await conn.fetchval(
                "SELECT override_price FROM cart_price_override WHERE user_id=$1", uid
            )
    except Exception:
        override = None

    if override is not None:
        # Тестовый режим: фиксированная цена за штуку, скидки не применяем
        return round(override * quantity, 2), 0.0

    base_total = 15 * quantity
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


async def get_user_delivery_mode(uid) -> bool:
    """Текущий режим пользователя: True = доставка, False = самовывоз."""
    async with pool.acquire() as conn:
        return bool(await conn.fetchval(
            "SELECT delivery_mode FROM users WHERE user_id=$1", uid
        ))


async def get_cart_mode(uid) -> str:
    """Режим корзины пользователя: 'pickup' или 'delivery'."""
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT cart_mode FROM users WHERE user_id=$1", uid
        ) or "pickup"


async def get_delivery_data(uid) -> dict:
    """Возвращает сохранённые данные доставки пользователя или None если не заполнены."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT delivery_saved, delivery_name, delivery_phone,
                   delivery_address, delivery_tracking
            FROM users WHERE user_id=$1
        """, uid)
    if not row or not row["delivery_saved"]:
        return None
    return {
        "name": row["delivery_name"],
        "phone": row["delivery_phone"],
        "address": row["delivery_address"],
        "tracking": row["delivery_tracking"],
    }


async def format_delivery_block(uid, data: dict) -> str:
    """Форматирует блок данных доставки для показа пользователю."""
    addr_parts = (data["address"] or "").split("|")
    bundesland = addr_parts[0] if len(addr_parts) > 0 else ""
    stadt = addr_parts[1] if len(addr_parts) > 1 else ""
    strasse = addr_parts[2] if len(addr_parts) > 2 else ""

    tracking_label = await t(uid, "delivery_tracking_yes_label" if data["tracking"] else "delivery_tracking_no_label")

    text = (
        f"{await t(uid,'delivery_confirm_title')}\n\n"
        f"{await t(uid,'delivery_field_name')}\n{data['name']}\n\n"
        f"{await t(uid,'delivery_field_phone')}\n{data['phone']}\n\n"
        f"{await t(uid,'delivery_field_address')}\n{bundesland}\n{stadt}\n{strasse}\n\n"
        f"{await t(uid,'delivery_field_tracking')} {tracking_label}"
    )
    return text

# ========== КУРСЫ ВАЛЮТ ==========

# Кэш курсов для каждого пользователя: {uid: {"usdt": (rate, ts), "uah": (rate, ts)}}
_rate_cache: dict = {}
_RATE_TTL = 1800  # 30 минут


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




async def is_streak_frozen() -> bool:
    """Активна ли сейчас глобальная заморозка Buy Streak."""
    async with pool.acquire() as conn:
        row = await conn.fetchval(
            "SELECT 1 FROM streak_freezes WHERE ended_at IS NULL LIMIT 1"
        )
    return row is not None

async def get_frozen_days_since(since_date: date) -> float:
    """
    Сколько дней заморозки попало в промежуток [since_date, сейчас].
    Учитывает все периоды заморозки (в т.ч. текущий незавершённый),
    но только ту их часть, что приходится после since_date — чтобы
    заморозка, случившаяся ДО последнего заказа пользователя, не
    давала ему лишние дни.
    """
    since_dt = datetime.combine(since_date, datetime.min.time())
    now_dt = datetime.now()

    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT started_at, ended_at FROM streak_freezes")

    total = timedelta()
    for r in rows:
        start = r["started_at"]
        end = r["ended_at"] or now_dt

        overlap_start = max(start, since_dt)
        overlap_end = min(end, now_dt)

        if overlap_end > overlap_start:
            total += overlap_end - overlap_start

    return total.total_seconds() / 86400

async def _effective_days_since(last_date: date) -> float:
    """Сколько дней реально прошло с last_date до сегодня, не считая дней заморозки."""
    real_days = (date.today() - last_date).days
    frozen_days = await get_frozen_days_since(last_date)
    return max(real_days - frozen_days, 0)

def get_streak_discount_value(weeks: int) -> float:
    """Скидка за стрик (максимум — последний порог в DISCOUNTS['streak'], сейчас 3€ с 5 недель)."""
    value = 0
    for s in DISCOUNTS["streak"]:
        if weeks >= s["weeks"]:
            value = s["value"]
    return value

async def _calc_next_streak(old_weeks: int, last_date) -> int:
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

    days_since = await _effective_days_since(last_date)

    if days_since <= 7:
        return (old_weeks or 0) + 1

    return 1

async def _streak_snapshot(uid):
    """
    Возвращает текущее состояние Buy Streak пользователя:
    (текущий_стрик, дней_до_сброса, максимальный_стрик, заморожен_ли).

    Если с последнего заказа прошло больше 7 "неморозных" дней — стрик
    считается прерванным и сбрасывается в БД (чтобы не давать неактуальную
    скидку дальше).
    """
    async with pool.acquire() as conn:
        user = await conn.fetchrow("""
            SELECT streak_weeks, last_order_date, max_streak_weeks
            FROM users WHERE user_id=$1
        """, uid)

    frozen = await is_streak_frozen()

    if not user:
        return 0, 0, 0, frozen

    max_weeks = user["max_streak_weeks"] or 0

    if not user["last_order_date"]:
        return 0, 0, max_weeks, frozen

    weeks = user["streak_weeks"] or 0
    days_since = await _effective_days_since(user["last_order_date"])

    if days_since <= 7:
        days_left = max(7 - days_since, 0)
        return weeks, days_left, max_weeks, frozen

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET streak_weeks=0 WHERE user_id=$1", uid
        )

    return 0, 0, max_weeks, frozen

async def get_effective_streak(uid) -> int:
    """Текущий (актуальный) стрик пользователя — используется в расчёте скидок."""
    weeks, _, _, _ = await _streak_snapshot(uid)
    return weeks

async def get_lang(uid):
    async with pool.acquire() as conn:
        lang = await conn.fetchval(
            "SELECT language FROM users WHERE user_id=$1",
            uid
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
        banned = await conn.fetchval(
            "SELECT banned FROM users WHERE user_id=$1", uid
        )

    if not banned:
        return True

    text = await t(uid, "banned_message")

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

def get_rank(total_items):
    ranks = DISCOUNTS.get("rank", [])

    if not ranks:
        return {"key": "none", "name": {}, "need": 0, "value": 0.0}

    current = ranks[0]

    for r in ranks:
        if total_items >= r.get("need", 0):
            current = r

    return current

async def render(target, text, kb=None, photo=None, parse_mode="HTML"):
    try:
        if isinstance(target, types.Message):
            if photo:
                await target.answer_photo(
                    photo,
                    caption=text,
                    reply_markup=kb,
                    parse_mode=parse_mode
                )
            else:
                await target.answer(
                    text,
                    reply_markup=kb,
                    parse_mode=parse_mode
                )
            return

        msg = target.message

        if photo:
            await msg.delete()
            await msg.answer_photo(
                photo,
                caption=text,
                reply_markup=kb,
                parse_mode=parse_mode
            )
        else:
            await msg.edit_text(
                text,
                reply_markup=kb,
                parse_mode=parse_mode
            )

    except Exception:
        try:
            await target.message.delete()
        except:
            pass

        if photo:
            await target.message.answer_photo(
                photo,
                caption=text,
                reply_markup=kb,
                parse_mode=parse_mode
            )
        else:
            await target.message.answer(
                text,
                reply_markup=kb,
                parse_mode=parse_mode
            )

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
            SELECT total_items, total_orders, total_saved, delivery_saved,
                   promo_discount, promo_code, promo_type
            FROM users WHERE user_id=$1
        """, uid)

    items = row["total_items"] or 0
    orders = row["total_orders"] or 0
    saved = row["total_saved"] or 0
    has_delivery = bool(row["delivery_saved"])
    promo_discount = row["promo_discount"] or 0
    promo_code = row["promo_code"]
    promo_type = row["promo_type"]

    lang = await get_lang(uid)
    rank = get_rank(items)
    rank_name = rank["name"][lang]

    total_discount = await calculate_total_discount(uid, 1)

    text = (
        f"{await t(uid,'profile_title')}\n\n"
        f"{rank_name}\n\n"
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
    if has_delivery:
        kb.add(
            InlineKeyboardButton(
                await t(uid, "delivery_address_profile_btn"),
                callback_data="profile_delivery"
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

  
@dp.callback_query_handler(lambda c: c.data == "profile_delivery")
async def profile_delivery(call):
    uid = call.from_user.id
    delivery_data = await get_delivery_data(uid)

    if not delivery_data:
        await call.answer()
        return

    text = await format_delivery_block(uid, delivery_data)

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(await t(uid, "delivery_refill_profile_btn"), callback_data="profile_delivery_refill"))
    kb.add(InlineKeyboardButton(await t(uid, "back"), callback_data="profile"))

    await render(call, text, kb)


@dp.callback_query_handler(lambda c: c.data == "profile_delivery_refill")
async def profile_delivery_refill(call):
    """Перезаполнение данных доставки из профиля."""
    uid = call.from_user.id
    await call.answer()
    try:
        await call.message.delete()
    except Exception:
        pass
    await start_delivery_form(call, uid)

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
    is_delivery = bool(order["is_delivery"])
    cart_mode = "delivery" if is_delivery else "pickup"

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM cart WHERE user_id=$1", uid)
        # Восстанавливаем режим из оригинального заказа
        await conn.execute(
            "UPDATE users SET delivery_mode=$1, cart_mode=$2 WHERE user_id=$3",
            1 if is_delivery else 0, cart_mode, uid
        )

        for item in items_str.split(","):
            pid, qty = item.split(":")
            pid, qty = int(pid), int(qty)
            await conn.execute("""
                INSERT INTO cart (user_id, product_id, quantity, cart_mode)
                VALUES ($1, $2, $3, $4)
            """, uid, pid, qty, cart_mode)

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

    # Показываем выбор: доставка или самовывоз
    text = await t(uid, "choose_section") + "\n\n" + await t(uid, "gift_delivery_free_hint")

    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton(await t(uid, "gift_delivery_label"), callback_data="gift_mode_delivery"),
        InlineKeyboardButton(await t(uid, "gift_pickup_label"),   callback_data="gift_mode_pickup"),
    )
    kb.add(InlineKeyboardButton(await t(uid, "back"), callback_data="profile_roulette"))

    await render(call, text, kb)


@dp.callback_query_handler(lambda c: c.data in ("gift_mode_delivery", "gift_mode_pickup"))
async def gift_mode_select(call):
    if not await check_not_banned(call):
        return

    uid = call.from_user.id
    is_delivery = (call.data == "gift_mode_delivery")

    async with pool.acquire() as conn:
        bonus = await conn.fetchval(
            "SELECT free_jar_bonus FROM users WHERE user_id=$1", uid
        )

    if not bonus:
        await call.answer(await t(uid, "gift_already_used"), show_alert=True)
        return

    # Если выбрана доставка — проверяем, есть ли сохранённые данные
    if is_delivery:
        delivery_data = await get_delivery_data(uid)
        if not delivery_data:
            # Сохраняем флаг «доставка для подарка» и запускаем форму
            await call.answer()
            # Используем delivery_confirmed_save-сценарий: после заполнения
            # пользователь подтверждает → его ведут обратно в gift-магазин
            # Для этого передаём контекст через callback_data после подтверждения
            await _gift_start_delivery_form(call, uid)
            return

    # Запоминаем выбранный режим в state через пустой FSM (не нужен),
    # просто передаём через callback_data в каталог
    cat_prefix = "gift_delivery_cat_" if is_delivery else "gift_cat_"
    await render_category_selection_gift(call, uid, is_delivery=is_delivery)


async def render_category_selection_gift(target, uid, is_delivery: bool):
    """Выбор раздела каталога для бесплатной банки (с учётом режима доставки)."""
    text = await t(uid, "choose_section")
    if is_delivery:
        text += "\n\n" + await t(uid, "gift_delivery_free_hint")

    prefix = "gift_delivery_cat_" if is_delivery else "gift_cat_"
    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton(await t(uid, "section_elfliq"),   callback_data=f"{prefix}elfliq"),
        InlineKeyboardButton(await t(uid, "section_elfworld"), callback_data=f"{prefix}elfworld"),
    )
    kb.add(InlineKeyboardButton(await t(uid, "back"), callback_data="open_gift_shop"))

    await render(target, text, kb)


async def _gift_start_delivery_form(call, uid):
    """Запускает форму доставки из режима подарка."""
    await call.answer()
    try:
        await call.message.delete()
    except Exception:
        pass
    await DeliveryForm.name.set()
    sent = await bot.send_message(uid, await t(uid, "delivery_step_name"), reply_markup=ReplyKeyboardRemove())
    # from_pay=False — после подтверждения адреса открываем профиль, не оплату
    await dp.storage.update_data(chat=uid, user=uid, data={"last_bot_msg": sent.message_id, "from_pay": False, "gift_flow": True})


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
    await render_gift_shop(call, uid, category, is_delivery=False)


@dp.callback_query_handler(lambda c: c.data.startswith("gift_delivery_cat_"))
async def gift_delivery_category(call):
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

    category = call.data.split("gift_delivery_cat_")[1]
    await render_gift_shop(call, uid, category, is_delivery=True)


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

    if is_delivery:
        text += await t(uid, "gift_delivery_free_hint") + "\n\n"

    kb = InlineKeyboardMarkup()

    if not products:
        text += await t(uid, "section_empty") + "\n"

    prefix = "gift_delivery_view_" if is_delivery else "gift_view_"
    for p in products:
        pid = p["id"]
        name = p[f"name_{lang}"]
        text += f"{name}\n"
        kb.add(InlineKeyboardButton(name, callback_data=f"{prefix}{pid}"))

    back_cb = "gift_delivery_cat_" + category if is_delivery else "open_gift_shop"
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


@dp.callback_query_handler(lambda c: c.data.startswith("gift_delivery_view_"))
async def gift_delivery_view(call):
    """Просмотр товара для бесплатной банки в режиме доставки."""
    if not await check_not_banned(call):
        return

    uid = call.from_user.id
    pid = int(call.data.split("gift_delivery_view_")[1])

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
        InlineKeyboardButton("⬅️", callback_data=f"gift_delivery_cat_{category}"),
        InlineKeyboardButton(await t(uid, "select_gift_btn"), callback_data=f"gift_delivery_confirm_{pid}")
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

    async with pool.acquire() as conn:
        p = await conn.fetchrow(
            "SELECT name_ru, name_ua, name_de FROM products WHERE id=$1", pid
        )

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


@dp.callback_query_handler(lambda c: c.data.startswith("gift_delivery_confirm_"))
async def gift_delivery_confirm(call):
    """Экран подтверждения выбора товара для доставки."""
    if not await check_not_banned(call):
        return

    uid = call.from_user.id
    pid = int(call.data.split("gift_delivery_confirm_")[1])

    async with pool.acquire() as conn:
        bonus = await conn.fetchval(
            "SELECT free_jar_bonus FROM users WHERE user_id=$1", uid
        )

    if not bonus:
        await call.answer(await t(uid, "gift_already_used"), show_alert=True)
        return

    async with pool.acquire() as conn:
        p = await conn.fetchrow(
            "SELECT name_ru, name_ua, name_de FROM products WHERE id=$1", pid
        )

    lang = await get_lang(uid)
    name = p[f"name_{lang}"]

    text = (
        f"{await t(uid, 'gift_confirm_title')}\n\n"
        f"{name}\n\n"
        f"{await t(uid, 'gift_confirm_question')}"
    )

    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton(await t(uid, "confirm"), callback_data=f"gift_delivery_apply_{pid}"),
        InlineKeyboardButton(await t(uid, "gift_cancel"), callback_data=f"gift_delivery_view_{pid}")
    )

    await render(call, text, kb)

@dp.callback_query_handler(lambda c: c.data.startswith("gift_apply_"))
async def gift_apply(call):
    if not await check_not_banned(call):
        return

    uid = call.from_user.id
    pid = int(call.data.split("_")[2])
    username = call.from_user.username or "unknown"

    async with pool.acquire() as conn:
        # Атомарное списание
        updated = await conn.fetchval("""
            UPDATE users
            SET free_jar_bonus = 0
            WHERE user_id=$1 AND free_jar_bonus = 1
            RETURNING user_id
        """, uid)

    if not updated:
        await call.answer(await t(uid, "gift_already_used"), show_alert=True)
        return

    async with pool.acquire() as conn:
        p = await conn.fetchrow(
            "SELECT name_ru, name_ua, name_de FROM products WHERE id=$1", pid
        )

    name_ru = p["name_ru"]
    lang = await get_lang(uid)
    name = p[f"name_{lang}"]

    # Создаём запись заявки (без request_id пока)
    async with pool.acquire() as conn:
        request_id = await conn.fetchval("""
            INSERT INTO gift_requests (user_id, product_id, username)
            VALUES ($1, $2, $3)
            RETURNING id
        """, uid, pid, username)

    admin_text = (
        f"🎁 Бесплатная банка\n\n"
        f"Пользователь: @{username}\n\n"
        f"Выбранный товар:\n{name_ru}"
    )

    admin_kb = InlineKeyboardMarkup()
    admin_kb.add(
        InlineKeyboardButton("✅ Выдано", callback_data=f"gift_issued_{request_id}"),
        InlineKeyboardButton("❌ Отменить", callback_data=f"gift_rejected_{request_id}")
    )

    # Рассылаем всем админам и запоминаем message_id
    msg_ids = []
    for admin_id in ADMIN_IDS:
        try:
            sent = await bot.send_message(admin_id, admin_text, reply_markup=admin_kb)
            msg_ids.append(f"{admin_id}:{sent.message_id}")
        except Exception:
            pass

    # Сохраняем message_ids в заявке
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE gift_requests SET admin_message_ids=$1 WHERE id=$2
        """, ",".join(msg_ids), request_id)

    # Подтверждение пользователю
    await render(call, await t(uid, "gift_done"))
    await notify_no_username(call, uid)


@dp.callback_query_handler(lambda c: c.data.startswith("gift_delivery_apply_"))
async def gift_delivery_apply(call):
    """Финальное подтверждение бесплатной банки с доставкой."""
    if not await check_not_banned(call):
        return

    uid = call.from_user.id
    pid = int(call.data.split("gift_delivery_apply_")[1])
    username = call.from_user.username or "unknown"

    async with pool.acquire() as conn:
        updated = await conn.fetchval("""
            UPDATE users
            SET free_jar_bonus = 0
            WHERE user_id=$1 AND free_jar_bonus = 1
            RETURNING user_id
        """, uid)

    if not updated:
        await call.answer(await t(uid, "gift_already_used"), show_alert=True)
        return

    async with pool.acquire() as conn:
        p = await conn.fetchrow(
            "SELECT name_ru, name_ua, name_de FROM products WHERE id=$1", pid
        )

    name_ru = p["name_ru"]
    lang = await get_lang(uid)
    name = p[f"name_{lang}"]

    async with pool.acquire() as conn:
        request_id = await conn.fetchval("""
            INSERT INTO gift_requests (user_id, product_id, username)
            VALUES ($1, $2, $3)
            RETURNING id
        """, uid, pid, username)

    # Блок данных доставки для администратора
    delivery_block = await _build_delivery_admin_block(uid)

    admin_text = (
        f"🎁 Бесплатная банка (ДОСТАВКА)\n\n"
        f"Пользователь: @{username}\n\n"
        f"Выбранный товар:\n{name_ru}"
        f"{delivery_block}"
    )

    admin_kb = InlineKeyboardMarkup()
    admin_kb.add(
        InlineKeyboardButton("✅ Выдано", callback_data=f"gift_issued_{request_id}"),
        InlineKeyboardButton("❌ Отменить", callback_data=f"gift_rejected_{request_id}")
    )

    msg_ids = []
    for admin_id in ADMIN_IDS:
        try:
            sent = await bot.send_message(admin_id, admin_text, reply_markup=admin_kb)
            msg_ids.append(f"{admin_id}:{sent.message_id}")
        except Exception:
            pass

    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE gift_requests SET admin_message_ids=$1 WHERE id=$2
        """, ",".join(msg_ids), request_id)

    await render(call, await t(uid, "gift_done"))
    await notify_no_username(call, uid)


async def _sync_gift_admins(request_id: int, status_line: str):
    """Обновляет или пересылает сообщение всем админам по заявке на подарок."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT admin_message_ids FROM gift_requests WHERE id=$1", request_id
        )
    if not row or not row["admin_message_ids"]:
        return

    for entry in row["admin_message_ids"].split(","):
        if ":" not in entry:
            continue
        admin_id, msg_id = int(entry.split(":")[0]), int(entry.split(":")[1])
        try:
            await bot.edit_message_text(
                chat_id=admin_id, message_id=msg_id, text=status_line
            )
        except Exception:
            try:
                await bot.delete_message(chat_id=admin_id, message_id=msg_id)
                await bot.send_message(admin_id, status_line)
            except Exception:
                pass

async def _sync_admin_messages(msg_ids_raw: str, actor_id: int,
                                base_text: str, status_self: str, status_others: str):
    """
    Обновляет сообщения у всех админов с учётом того, кто нажал кнопку.
    actor_id    — Telegram ID администратора, который нажал кнопку
    status_self — строка-суффикс для нажавшего (без username)
    status_others — строка-суффикс для остальных (с @username)
    """
    for entry in (msg_ids_raw or "").split(","):
        if ":" not in entry:
            continue
        a_id, m_id = int(entry.split(":")[0]), int(entry.split(":")[1])
        suffix = status_self if a_id == actor_id else status_others
        text = base_text + suffix
        try:
            await bot.edit_message_text(chat_id=a_id, message_id=m_id, text=text)
        except Exception:
            try:
                await bot.delete_message(chat_id=a_id, message_id=m_id)
                await bot.send_message(a_id, text)
            except Exception:
                pass

@dp.callback_query_handler(lambda c: c.data.startswith("gift_issued_"))
async def gift_issued(call):
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
async def start(message: types.Message):
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

    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("🇷🇺 Русский", "🇺🇦 Українська", "🇩🇪 Deutsch")

    await message.answer("🌍", reply_markup=kb)

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

    await message.answer("✅", reply_markup=ReplyKeyboardRemove())
    await message.answer(TEXTS[lang]["menu"], reply_markup=main_menu(lang))

@dp.message_handler(lambda m: is_text(m,"language"))
async def change_lang(message: types.Message):
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("🇷🇺 Русский","🇺🇦 Українська","🇩🇪 Deutsch")
    await message.answer(await t(message.from_user.id,"choose_lang"), reply_markup=kb)

# ========== МАГАЗИН ==========

async def render_category_selection(target, uid, mode="shop"):
    """Экран выбора раздела (Elfliq / Elfworld). mode='shop' — обычный
    магазин (с кнопками корзины/профиля), mode='gift' — выбор раздела
    для бесплатной банки (без цен и корзины)."""
    prefix = "shop_cat_" if mode == "shop" else "gift_cat_"

    delivery = await get_user_delivery_mode(uid) if mode == "shop" else False

    text = await t(uid, "choose_section")

    if mode == "shop":
        text += "\n\n" + await t(uid, "free_delivery_hint")

    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton(await t(uid, "section_elfliq"), callback_data=f"{prefix}elfliq"),
        InlineKeyboardButton(await t(uid, "section_elfworld"), callback_data=f"{prefix}elfworld"),
    )

    if mode == "shop":
        # Кнопка переключения режима — показывает, что сейчас активно,
        # и предлагает переключиться на противоположное.
        toggle_label = await t(uid, "delivery_mode_off" if delivery else "delivery_mode_on")
        kb.add(InlineKeyboardButton(toggle_label, callback_data="toggle_delivery_mode"))
        kb.add(
            InlineKeyboardButton(await t(uid,"cart"), callback_data="open_cart"),
            InlineKeyboardButton(await t(uid,"profile"), callback_data="profile")
        )
    else:
        kb.add(InlineKeyboardButton(await t(uid, "back"), callback_data="profile_roulette"))

    await render(target, text, kb)


async def render_category_shop(target, uid, category):
    async with pool.acquire() as conn:
        products = await conn.fetch(
            "SELECT * FROM products WHERE category=$1 ORDER BY id", category
        )

    total_qty = 1

    final_price, discount = await calculate_final_price(uid, total_qty)
    base_price = 15

    delivery = await get_user_delivery_mode(uid)
    section_key = "section_elfliq" if category == "elfliq" else "section_elfworld"

    mode_label = await t(uid, "delivery_mode_on" if delivery else "delivery_mode_off")
    text = f"{await t(uid, section_key)} | {mode_label}\n\n"
    text += await t(uid, "choose_product") + "\n\n"

    if discount > 0:
        # Строка о промокоде — выше строки скидки, только если есть promo_discount
        async with pool.acquire() as conn:
            promo_discount = await conn.fetchval(
                "SELECT promo_discount FROM users WHERE user_id=$1", uid
            ) or 0
        if promo_discount > 0:
            text += (await t(uid, "promo_in_shop_label")).format(value=promo_discount) + "\n"
        text += f"💰 {base_price}€ → {final_price}€ (-{discount}€)\n\n"
    else:
        text += f"💰 {base_price}€\n\n"

    if delivery == True:
        text += await t(uid, "free_delivery_hint") + "\n\n"

    kb = InlineKeyboardMarkup()

    lang = await get_lang(uid)

    if not products:
        text += await t(uid, "section_empty") + "\n"

    for p in products:
        pid = p["id"]
        name = p[f"name_{lang}"]
        stock = p["in_stock"]

        fav = await is_fav(uid, pid)
        heart = "❤️" if fav else ""

        status = "✅" if stock else "❌"
        text += f"{name} {status} {heart}\n"

        if stock:
            kb.add(InlineKeyboardButton(name, callback_data=f"view_{pid}"))

    other_category = "elfworld" if category == "elfliq" else "elfliq"
    switch_key = "switch_to_elfworld" if category == "elfliq" else "switch_to_elfliq"

    kb.add(InlineKeyboardButton(await t(uid, switch_key), callback_data=f"shop_cat_{other_category}"))

    toggle_label = await t(uid, "delivery_mode_off" if delivery else "delivery_mode_on")
    kb.add(InlineKeyboardButton(toggle_label, callback_data=f"toggle_delivery_mode_cat_{category}"))

    kb.add(
        InlineKeyboardButton(await t(uid,"cart"), callback_data="open_cart"),
        InlineKeyboardButton(await t(uid,"profile"), callback_data="profile")
    )

    await render(target, text, kb)

@dp.message_handler(lambda m: is_text(m,"shop"))
async def shop(message: types.Message):
    if not await check_not_banned(message):
        return

    await render_category_selection(message, message.from_user.id, mode="shop")

@dp.callback_query_handler(lambda c: c.data.startswith("shop_cat_"))
async def shop_category(call):
    if not await check_not_banned(call):
        return

    await call.answer()
    category = call.data.split("shop_cat_")[1]
    await render_category_shop(call, call.from_user.id, category)

@dp.callback_query_handler(lambda c: c.data == "toggle_delivery_mode" or c.data.startswith("toggle_delivery_mode_cat_"))
async def toggle_delivery_mode(call):
    if not await check_not_banned(call):
        return

    uid = call.from_user.id

    # Определяем контекст: находимся ли мы внутри конкретного раздела
    category = None
    if call.data.startswith("toggle_delivery_mode_cat_"):
        category = call.data.split("toggle_delivery_mode_cat_")[1]

    async with pool.acquire() as conn:
        user = await conn.fetchrow("""
            SELECT delivery_mode, cart_mode FROM users WHERE user_id=$1
        """, uid)

        cart_count = await conn.fetchval(
            "SELECT COUNT(*) FROM cart WHERE user_id=$1", uid
        )

    current_delivery = bool(user["delivery_mode"])
    new_delivery = not current_delivery
    new_mode_str = "delivery" if new_delivery else "pickup"

    # Если корзина не пуста и её режим не совпадает — блокируем переключение
    if cart_count > 0:
        cart_mode = user["cart_mode"] or "pickup"
        if cart_mode != new_mode_str:
            await call.answer(await t(uid, "delivery_cart_conflict"), show_alert=True)
            return

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET delivery_mode=$1 WHERE user_id=$2", new_delivery, uid
        )

    toggle_key = "delivery_mode_toggle_on" if new_delivery else "delivery_mode_toggle_off"
    await call.answer(await t(uid, toggle_key))

    # Редактируем текущее сообщение: если были в разделе — остаёмся в нём,
    # если на экране выбора раздела — обновляем его (аналогично elfliq/elfworld)
    if category:
        await render_category_shop(call, uid, category)
    else:
        await render_category_selection(call, uid, mode="shop")

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

    delivery = await get_user_delivery_mode(uid)
    new_mode = "delivery" if delivery else "pickup"

    async with pool.acquire() as conn:
        cart_count = await conn.fetchval(
            "SELECT COUNT(*) FROM cart WHERE user_id=$1", uid
        )

        # Если корзина не пуста — проверяем совместимость режимов
        if cart_count > 0:
            cart_mode = await conn.fetchval(
                "SELECT cart_mode FROM users WHERE user_id=$1", uid
            ) or "pickup"

            if cart_mode != new_mode:
                await call.answer(await t(uid, "delivery_cart_conflict"), show_alert=True)
                return

        exists = await conn.fetchrow("""
            SELECT 1 FROM cart
            WHERE user_id=$1 AND product_id=$2
        """, uid, pid)

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

    # Подсказка про бесплатную доставку — только в режиме доставки
    cart_mode = await get_cart_mode(uid)
    if cart_mode == "delivery":
        text += "\n\n" + await t(uid, "free_delivery_hint")

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

async def start_delivery_form(target, uid, from_pay: bool = False):
    """Запускает форму с шага 1 — запрос имени."""
    await DeliveryForm.name.set()
    sent = await bot.send_message(uid, await t(uid, "delivery_step_name"), reply_markup=ReplyKeyboardRemove())
    # Сохраняем message_id чтобы удалить после ответа пользователя, и контекст запуска
    await dp.storage.update_data(chat=uid, user=uid, data={"last_bot_msg": sent.message_id, "from_pay": from_pay})


async def show_delivery_confirm(target, uid, data: dict, from_pay: bool = False, gift_flow: bool = False):
    """Показывает блок данных доставки с кнопками Подтвердить / Заново.
    Редактирует существующее сообщение target вместо отправки новых."""
    text = await format_delivery_block(uid, data)

    kb = InlineKeyboardMarkup()
    if gift_flow:
        confirm_cb = "delivery_confirmed_gift"
        refill_cb  = "delivery_refill_gift"
    elif from_pay:
        confirm_cb = "delivery_confirmed_pay"
        refill_cb  = "delivery_refill_pay"
    else:
        confirm_cb = "delivery_confirmed_save"
        refill_cb  = "delivery_refill_save"

    kb.add(
        InlineKeyboardButton(await t(uid, "delivery_confirm_btn"), callback_data=confirm_cb),
        InlineKeyboardButton(await t(uid, "delivery_refill_btn"),  callback_data=refill_cb),
    )

    # Редактируем существующее сообщение (если target — callback) или отправляем новое
    try:
        if isinstance(target, types.CallbackQuery):
            await target.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        else:
            await bot.send_message(uid, text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        # Если edit_text не сработал (сообщение удалено или не изменилось) — шлём новое
        await bot.send_message(uid, text, reply_markup=kb, parse_mode="HTML")


@dp.message_handler(state=DeliveryForm.name)
async def delivery_got_name(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    name = message.text.strip()

    # Удаляем вопрос бота (сообщение с запросом имени) и ответ пользователя
    data = await state.get_data()
    try:
        await bot.delete_message(uid, data.get("last_bot_msg"))
    except Exception:
        pass
    try:
        await message.delete()
    except Exception:
        pass

    await state.update_data(name=name)
    await DeliveryForm.phone.set()

    kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(types.KeyboardButton(await t(uid, "delivery_step_phone_btn"), request_contact=True))
    sent = await bot.send_message(uid, await t(uid, "delivery_step_phone"), reply_markup=kb)
    await state.update_data(last_bot_msg=sent.message_id)


@dp.message_handler(state=DeliveryForm.phone, content_types=[types.ContentType.CONTACT])
async def delivery_got_phone_contact(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    phone = message.contact.phone_number
    await state.update_data(phone=phone)
    await DeliveryForm.address.set()

    data = await state.get_data()
    try:
        await bot.delete_message(uid, data.get("last_bot_msg"))
        await message.delete()
    except Exception:
        pass

    sent = await bot.send_message(
        uid, await t(uid, "delivery_step_address"),
        reply_markup=ReplyKeyboardRemove(), parse_mode="HTML"
    )
    await state.update_data(last_bot_msg=sent.message_id)


@dp.message_handler(state=DeliveryForm.phone, content_types=[types.ContentType.TEXT])
async def delivery_got_phone_text(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    phone = message.text.strip()
    await state.update_data(phone=phone)
    await DeliveryForm.address.set()

    data = await state.get_data()
    try:
        await bot.delete_message(uid, data.get("last_bot_msg"))
        await message.delete()
    except Exception:
        pass

    sent = await bot.send_message(
        uid, await t(uid, "delivery_step_address"),
        reply_markup=ReplyKeyboardRemove(), parse_mode="HTML"
    )
    await state.update_data(last_bot_msg=sent.message_id)


@dp.message_handler(state=DeliveryForm.address)
async def delivery_got_address(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    raw = message.text.strip().replace(". ", "|").replace(".", "|")
    await state.update_data(address=raw)
    await DeliveryForm.tracking.set()

    data = await state.get_data()
    try:
        await bot.delete_message(uid, data.get("last_bot_msg"))
        await message.delete()
    except Exception:
        pass

    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton(await t(uid, "delivery_tracking_yes_btn"), callback_data="dtracking_yes"),
        InlineKeyboardButton(await t(uid, "delivery_tracking_no_btn"), callback_data="dtracking_no"),
    )
    sent = await bot.send_message(uid, await t(uid, "delivery_step_tracking"), reply_markup=kb)
    await state.update_data(last_bot_msg=sent.message_id)


@dp.callback_query_handler(lambda c: c.data in ("dtracking_yes", "dtracking_no"),
                            state=DeliveryForm.tracking)
async def delivery_got_tracking(call: types.CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    tracking = (call.data == "dtracking_yes")
    await state.update_data(tracking=tracking)

    data = await state.get_data()
    await state.finish()

    delivery_data = {
        "name":     data["name"],
        "phone":    data["phone"],
        "address":  data["address"],
        "tracking": tracking,
    }

    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE users SET
                delivery_name=$1, delivery_phone=$2,
                delivery_address=$3, delivery_tracking=$4
            WHERE user_id=$5
        """, delivery_data["name"], delivery_data["phone"],
             delivery_data["address"], delivery_data["tracking"], uid)

    # Удаляем шаг с выбором трека
    try:
        await call.message.delete()
    except Exception:
        pass

    from_pay  = data.get("from_pay", False)
    gift_flow = data.get("gift_flow", False)

    # Если форма заполнялась не из потока оплаты — восстанавливаем главную клавиатуру.
    # При from_pay форма была запущена поверх inline-сообщения и клавиатура уже на месте.
    if not from_pay and not gift_flow:
        lang = await get_lang(uid)
        mk = main_menu(lang)
        await bot.send_message(uid, "✅", reply_markup=mk)

    await show_delivery_confirm(call, uid, delivery_data, from_pay=from_pay, gift_flow=gift_flow)


@dp.callback_query_handler(lambda c: c.data in ("delivery_refill", "delivery_refill_pay", "delivery_refill_save"))
async def delivery_refill(call):
    uid = call.from_user.id
    from_pay = (call.data == "delivery_refill_pay")
    await call.answer()
    try:
        await call.message.delete()
    except Exception:
        pass
    await start_delivery_form(call, uid, from_pay=from_pay)


@dp.callback_query_handler(lambda c: c.data == "delivery_confirmed_pay")
async def delivery_confirmed_pay(call):
    uid = call.from_user.id

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET delivery_saved=true WHERE user_id=$1", uid
        )

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(await t(uid, "pay_usdt_btn"), callback_data="pay_usdt_delivery"))
    kb.add(InlineKeyboardButton(await t(uid, "pay_card_btn"), callback_data="pay_card_delivery"))
    kb.add(InlineKeyboardButton(await t(uid, "cancel"), callback_data="open_cart"))

    # Редактируем то же сообщение с адресом → экран выбора оплаты
    try:
        await call.message.edit_text(await t(uid, "pay"), reply_markup=kb)
    except Exception:
        await render(call, await t(uid, "pay"), kb)


@dp.callback_query_handler(lambda c: c.data == "delivery_confirmed_save")
async def delivery_confirmed_save(call):
    uid = call.from_user.id

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET delivery_saved=true WHERE user_id=$1", uid
        )

    await call.answer("✅")
    await render_profile(call)


@dp.callback_query_handler(lambda c: c.data == "delivery_confirmed_gift")
async def delivery_confirmed_gift(call):
    """Адрес подтверждён из gift-потока — возвращаем в gift-магазин."""
    uid = call.from_user.id

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET delivery_saved=true WHERE user_id=$1", uid
        )

    await call.answer("✅")
    # Возвращаем в выбор режима доставки для подарка
    await open_gift_shop(call)


@dp.callback_query_handler(lambda c: c.data == "delivery_refill_gift")
async def delivery_refill_gift(call):
    """Перезаполнение адреса из gift-потока."""
    uid = call.from_user.id
    await call.answer()
    try:
        await call.message.delete()
    except Exception:
        pass
    await _gift_start_delivery_form(call, uid)

# ========== ОПЛАТА ==========

@dp.callback_query_handler(lambda c: c.data == "pay")
async def pay(call):
    uid = call.from_user.id

    cart_mode = await get_cart_mode(uid)
    is_delivery = (cart_mode == "delivery")

    if is_delivery:
        delivery_data = await get_delivery_data(uid)
        if not delivery_data:
            await start_delivery_form(call, uid, from_pay=True)
            return
        else:
            await show_delivery_confirm(call, uid, delivery_data, from_pay=True)
            return

    # Самовывоз — наличные + USDT + карта
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(await t(uid, "cash"), callback_data="cash"))
    kb.add(InlineKeyboardButton(await t(uid, "pay_usdt_btn"), callback_data="pay_usdt_delivery"))
    kb.add(InlineKeyboardButton(await t(uid, "pay_card_btn"), callback_data="pay_card_delivery"))
    kb.add(InlineKeyboardButton(await t(uid, "cancel"), callback_data="open_cart"))
    await render(call, await t(uid, "pay"), kb)

@dp.callback_query_handler(lambda c: c.data == "cash")
async def cash(call):
    uid = call.from_user.id

    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton(await t(uid,"cancel"), callback_data="pay"),
        InlineKeyboardButton(await t(uid,"confirm"), callback_data="confirm_cash")
    )

    await render(call, await t(uid,"confirm_order"), kb)

async def _build_delivery_admin_block(uid: int) -> str:
    """Блок данных доставки для сообщения админам (по-русски, без локализации)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT delivery_name, delivery_phone, delivery_address, delivery_tracking
            FROM users WHERE user_id=$1
        """, uid)

    if not row or not row["delivery_name"]:
        return ""

    addr_parts = (row["delivery_address"] or "").split("|")
    bundesland = addr_parts[0] if len(addr_parts) > 0 else ""
    stadt      = addr_parts[1] if len(addr_parts) > 1 else ""
    strasse    = addr_parts[2] if len(addr_parts) > 2 else ""
    tracking   = "✅ Да" if row["delivery_tracking"] else "❌ Нет"

    return (
        f"\n\n📦 ДОСТАВКА:\n"
        f"Имя: {row['delivery_name']}\n"
        f"Телефон: {row['delivery_phone']}\n"
        f"Bundesland: {bundesland}\n"
        f"Stadt: {stadt}\n"
        f"Straße: {strasse}\n"
        f"Трек-номер: {tracking}"
    )





@dp.callback_query_handler(lambda c: c.data == "confirm_cash")
async def confirm_cash(call):
    if not await check_not_banned(call):
        return

    uid = call.from_user.id
    username = call.from_user.username or "нет username"

    async with pool.acquire() as conn:
        cart_items = await conn.fetch("""
            SELECT product_id, quantity 
            FROM cart 
            WHERE user_id=$1
        """, uid)

        if not cart_items:
            await render(call, await t(uid,"empty_cart"))
            return

        items_str = ",".join([f"{r['product_id']}:{r['quantity']}" for r in cart_items])

        total_qty = sum(r["quantity"] for r in cart_items)
        total, discount = await calculate_final_price(uid, total_qty)

        text_admin = "ЗАКАЗ:\n"

        for r in cart_items:
            product = await conn.fetchrow("""
                SELECT name_ru 
                FROM products 
                WHERE id=$1
            """, r["product_id"])

            text_admin += f"{product['name_ru']} x{r['quantity']}\n"

        order_id = await conn.fetchval("""
            INSERT INTO orders (user_id, items, total, payment, discount, is_delivery)
            VALUES ($1, $2, $3, $4, $5, false)
            RETURNING id
        """, uid, items_str, total, "cash", discount)

        await conn.execute("""
            DELETE FROM cart WHERE user_id=$1
        """, uid)
        await conn.execute("UPDATE users SET cart_mode='pickup' WHERE user_id=$1", uid)

    await render(call, await t(uid,"order_done"))
    await notify_no_username(call, uid)

    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("✅ Подтвердить", callback_data=f"admin_confirm_{order_id}"),
        InlineKeyboardButton("❌ Отменить", callback_data=f"admin_cancel_{order_id}")
    )

    order_text = f"🏪 Самовывоз | {text_admin}\n\nID: {order_id}\nUser: @{username}\nОплата: Наличные\nИТОГО: {total}€"
    msg_ids = []
    for admin in ADMIN_IDS:
        try:
            sent = await bot.send_message(admin, order_text, reply_markup=kb)
            msg_ids.append(f"{admin}:{sent.message_id}")
        except Exception:
            pass

    if msg_ids:
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE orders SET admin_message_ids=$1 WHERE id=$2
            """, ",".join(msg_ids), order_id)

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
    async with pool.acquire() as conn:
        items_str_parts = []
        text_admin = "ЗАКАЗ:\n"
        for r in cart_items:
            product = await conn.fetchrow("SELECT name_ru FROM products WHERE id=$1", r["product_id"])
            text_admin += f"{product['name_ru']} x{r['quantity']}\n"
            items_str_parts.append(f"{r['product_id']}:{r['quantity']}")
    return cart_items, eur_total, discount, ",".join(items_str_parts), text_admin


async def _create_pending_order(uid: int, items_str: str, eur_total: float, discount: float, payment: str) -> int:
    """Создаёт заказ со статусом pending и очищает корзину."""
    cart_mode = await get_cart_mode(uid)
    is_delivery = (cart_mode == "delivery")

    async with pool.acquire() as conn:
        order_id = await conn.fetchval("""
            INSERT INTO orders (user_id, items, total, payment, discount, status, is_delivery)
            VALUES ($1, $2, $3, $4, $5, 'pending', $6)
            RETURNING id
        """, uid, items_str, eur_total, payment, discount, is_delivery)
        await conn.execute("DELETE FROM cart WHERE user_id=$1", uid)
        await conn.execute("UPDATE users SET cart_mode='pickup' WHERE user_id=$1", uid)
    return order_id


async def _send_order_to_admins(order_id: int, uid: int, username: str,
                                 text_admin: str, payment_line: str,
                                 is_delivery: bool = True) -> None:
    """Отправляет заказ всем администраторам и сохраняет message_ids."""
    order_type = "🚚 Доставка" if is_delivery else "🏪 Самовывоз"
    delivery_block = await _build_delivery_admin_block(uid) if is_delivery else ""
    order_text = (
        f"{order_type} | {text_admin}\n"
        f"ID: {order_id}\n"
        f"User: @{username}\n"
        f"{payment_line}"
        f"{delivery_block}"
    )
    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("✅ Подтвердить", callback_data=f"admin_confirm_{order_id}"),
        InlineKeyboardButton("❌ Отменить",    callback_data=f"admin_cancel_{order_id}")
    )
    msg_ids = []
    for admin in ADMIN_IDS:
        try:
            sent = await bot.send_message(admin, order_text, reply_markup=kb)
            msg_ids.append(f"{admin}:{sent.message_id}")
        except Exception:
            pass
    if msg_ids:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE orders SET admin_message_ids=$1 WHERE id=$2",
                ",".join(msg_ids), order_id
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

    cart_mode = await get_cart_mode(uid)
    is_delivery = (cart_mode == "delivery")

    result = await _get_cart_totals(uid)
    if not result:
        await render(call, await t(uid, "empty_cart"))
        return
    _, eur_total, discount, items_str, text_admin = result

    order_id = await _create_pending_order(uid, items_str, eur_total, discount, "usdt_trc20")
    payment_line = (
        f"Оплата: USDT TRC20 (ожидает проверки)\n"
        f"Сумма EUR: {eur_str}€\n"
        f"Курс: 1 EUR = {rate_str} USDT\n"
        f"К оплате: {usdt_str} USDT\n"
        f"ИТОГО: {eur_str}€"
    )
    await _send_order_to_admins(order_id, uid, username, text_admin, payment_line, is_delivery=is_delivery)
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

    cart_mode = await get_cart_mode(uid)
    is_delivery = (cart_mode == "delivery")

    result = await _get_cart_totals(uid)
    if not result:
        await render(call, await t(uid, "empty_cart"))
        return
    _, eur_total, discount, items_str, text_admin = result

    order_id = await _create_pending_order(uid, items_str, eur_total, discount, "card_eur")
    payment_line = (
        f"Оплата: Банковская карта EUR (ожидает проверки)\n"
        f"ИТОГО: {eur_str}€"
    )
    await _send_order_to_admins(order_id, uid, username, text_admin, payment_line, is_delivery=is_delivery)
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

    cart_mode = await get_cart_mode(uid)
    is_delivery = (cart_mode == "delivery")

    result = await _get_cart_totals(uid)
    if not result:
        await render(call, await t(uid, "empty_cart"))
        return
    _, eur_total, discount, items_str, text_admin = result

    order_id = await _create_pending_order(uid, items_str, eur_total, discount, "card_uah")
    payment_line = (
        f"Оплата: Банковская карта UAH (ожидает проверки)\n"
        f"Сумма EUR: {eur_str}€\n"
        f"Курс: 1 EUR = {rate_str} UAH\n"
        f"К оплате: {uah_str} UAH\n"
        f"ИТОГО: {eur_str}€"
    )
    await _send_order_to_admins(order_id, uid, username, text_admin, payment_line, is_delivery=is_delivery)
    await render(call, await t(uid, "pay_pending_user"))
    await notify_no_username(call, uid)


@dp.callback_query_handler(lambda c: c.data.startswith("admin_confirm_"))
async def admin_confirm(call):
    order_id = int(call.data.split("_")[2])
    admin_username = call.from_user.username or "admin"

    async with pool.acquire() as conn:
        order = await conn.fetchrow("""
            SELECT user_id, items, status, admin_message_ids, discount, total
            FROM orders 
            WHERE id=$1
        """, order_id)

    if not order or order["status"] != "pending":
        await call.answer("Заказ уже обработан", show_alert=True)
        return

    user_id = order["user_id"]
    items = order["items"]
    msg_ids_raw = order["admin_message_ids"] or ""
    order_discount = order["discount"] or 0
    order_total = order["total"] or 0

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE orders SET status='confirmed' WHERE id=$1", order_id
        )

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
            streak_row["last_order_date"]
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
                except Exception:
                    pass

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
        actor_id=call.from_user.id,
        base_text=call.message.text,
        status_self="\n\n✅ ПОДТВЕРЖДЕНО",
        status_others=f"\n\n✅ ПОДТВЕРЖДЕНО @{admin_username}"
    )

@dp.callback_query_handler(lambda c: c.data.startswith("admin_cancel_"))
async def admin_cancel(call):
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

    await call.answer("Отменено")

    await _sync_admin_messages(
        msg_ids_raw=msg_ids_raw,
        actor_id=call.from_user.id,
        base_text=call.message.text,
        status_self="\n\n❌ ОТМЕНЕНО",
        status_others=f"\n\n❌ ОТМЕНЕНО @{admin_username}"
    )

# ========== ПРОМОКОДЫ ==========

import string as _string

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
    if not is_admin(message.from_user.id):
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


@dp.message_handler(commands=["testprice"])
async def testprice(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    uid = message.from_user.id
    args = message.get_args().strip().lower()

    # Гарантируем существование таблицы при любом вызове команды
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS cart_price_override (
                user_id BIGINT PRIMARY KEY,
                override_price REAL
            )
        """)

    if args == "reset":
        async with pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM cart_price_override WHERE user_id=$1", uid
            )
        await message.answer("✅ Тестовая цена сброшена. В корзине снова реальные цены.")
        return

    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO cart_price_override (user_id, override_price)
            VALUES ($1, 1.0)
            ON CONFLICT (user_id) DO UPDATE SET override_price = 1.0
        """, uid)

    await message.answer(
        "✅ Тестовая цена активна.\n\n"
        "Все товары в корзине будут считаться по 1€ за штуку.\n"
        "Оформи заказ через PayPal и проверь.\n\n"
        "Для сброса: /testprice reset"
    )
async def freezestreak(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    async with pool.acquire() as conn:
        active = await conn.fetchval(
            "SELECT 1 FROM streak_freezes WHERE ended_at IS NULL LIMIT 1"
        )

        if active:
            await message.answer("❄️ Заморозка уже активна")
            return

        await conn.execute(
            "INSERT INTO streak_freezes (started_at) VALUES (CURRENT_TIMESTAMP)"
        )

    await message.answer("❄️ Глобальная заморозка Buy Streak включена. "
                          "Отсчёт времени у всех пользователей остановлен.")

@dp.message_handler(commands=["unfreezestreak"])
async def unfreezestreak(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM streak_freezes WHERE ended_at IS NULL LIMIT 1"
        )

        if not row:
            await message.answer("❄️ Заморозка не была активна")
            return

        await conn.execute(
            "UPDATE streak_freezes SET ended_at = CURRENT_TIMESTAMP WHERE id=$1",
            row["id"]
        )

    await message.answer("🔥 Глобальная заморозка Buy Streak выключена. "
                          "Отсчёт времени продолжается с того места, где остановился.")

@dp.message_handler(commands=["givefreejar"])
async def givefreejar(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    uid = message.from_user.id

    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET free_jar_bonus = 1 WHERE user_id=$1", uid
        )

    await message.answer(await t(uid, "givefreejar_done"))


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
    if not is_admin(message.from_user.id):
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


# ========== УПРАВЛЕНИЕ НАЛИЧИЕМ (/stock, /unstock) ==========

VALID_CATEGORIES = ("elfliq", "elfworld")


async def _handle_stock_command(message: types.Message, in_stock: int):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split()

    if len(parts) != 3:
        cmd = parts[0] if parts else "/stock"
        await message.answer(
            f"Использование: {cmd} <Elfliq|Elfworld> <product_id|all>"
        )
        return

    category = parts[1].lower()
    target = parts[2].lower()

    if category not in VALID_CATEGORIES:
        await message.answer("❌ Неизвестный раздел. Используй Elfliq или Elfworld")
        return

    section_label = category.capitalize()
    action_label = "убраны из наличия" if in_stock == 0 else "возвращены в наличие"
    action_label_single = "убран из наличия" if in_stock == 0 else "возвращён в наличие"

    async with pool.acquire() as conn:
        if target == "all":
            await conn.execute(
                "UPDATE products SET in_stock=$1 WHERE category=$2",
                in_stock, category
            )
            await message.answer(f"✅ Все товары раздела {section_label} {action_label}")
            return

        try:
            pid = int(target)
        except ValueError:
            await message.answer("❌ ID товара должен быть числом или 'all'")
            return

        result = await conn.execute(
            "UPDATE products SET in_stock=$1 WHERE id=$2 AND category=$3",
            in_stock, pid, category
        )

    rows_affected = int(result.split()[-1]) if result else 0

    if rows_affected == 0:
        await message.answer(f"❌ Товар #{pid} не найден в разделе {section_label}")
    else:
        await message.answer(f"✅ Товар #{pid} {action_label_single} в разделе {section_label}")


@dp.message_handler(commands=["stock"])
async def stock_cmd(message: types.Message):
    # /stock <Elfliq|Elfworld> <product_id|all> — вернуть товар(ы) в наличие
    await _handle_stock_command(message, in_stock=1)


@dp.message_handler(commands=["unstock"])
async def unstock_cmd(message: types.Message):
    # /unstock <Elfliq|Elfworld> <product_id|all> — убрать товар(ы) из наличия
    await _handle_stock_command(message, in_stock=0)

# ========== ЗАПУСК ==========

async def on_startup(dp):
    await init_db()


async def run():
    await on_startup(dp)
    await dp.start_polling(reset_webhook=True)


if __name__ == "__main__":
    asyncio.run(run())
