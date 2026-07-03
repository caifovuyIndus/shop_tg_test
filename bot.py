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

# ========== КОНФИГ ==========

API_TOKEN = "7686799347:AAFBQFwQAwtm02bsxEReUQPutUcMO58yHxs"
ADMIN_IDS = [7805603791, 8283121468, 5317145892] 
WALLET = "TGZCiwS5fTktQYxeey57KEeSfHXjB1hMQc"

# ── PayPal Sandbox ──────────────────────────────────────────────────────────
PAYPAL_CLIENT_ID     = os.getenv("PAYPAL_CLIENT_ID",
    "Ad7cYKnQJPf3iCtlUKn6ACAgLBctxq96x4Qt-sXvHAZkHCcrmEEB9WIj6HOy4MaHCcaWFb2yON6pLLKO")
PAYPAL_SECRET        = os.getenv("PAYPAL_SECRET",
    "EONipH-lUypcV8ppqZKTr5tnb4xC0svmnuPTAfqGWZais1StpfMIw3vTxKnm4y8vq2j0G64reh5mksNs")
PAYPAL_BASE          = "https://api-m.paypal.com"
PAYPAL_WEBHOOK_PORT  = int(os.getenv("PORT", "8080"))
PAYPAL_WEBHOOK_PATH  = "/paypal/webhook"
# Публичный URL Railway-деплоя (задать в переменных окружения)
PUBLIC_URL           = os.getenv("PUBLIC_URL", "https://shoptgtest-production.up.railway.app")  # напр. https://xxx.up.railway.app
def is_admin(uid):
    return uid in ADMIN_IDS 

logging.basicConfig(level=logging.INFO)

bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)

_bot_username = None

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

        # Фиксация курса EUR -> USDT на 30 минут (временное хранилище —
        # пока пользователь смотрит экран оплаты, заказ ещё не создан).
        # Переносится в orders в момент нажатия "Я оплатил" и очищается.
        await conn.execute("""
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS usdt_lock_eur REAL,
            ADD COLUMN IF NOT EXISTS usdt_lock_discount REAL,
            ADD COLUMN IF NOT EXISTS usdt_lock_rate REAL,
            ADD COLUMN IF NOT EXISTS usdt_lock_usdt REAL,
            ADD COLUMN IF NOT EXISTS usdt_lock_created_at TIMESTAMP,
            ADD COLUMN IF NOT EXISTS usdt_lock_expires_at TIMESTAMP
        """)

        # Постоянная запись зафиксированного курса USDT в самом заказе
        # (order_total_eur не дублируем — это уже существующее поле orders.total).
        await conn.execute("""
            ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS eur_usdt_rate REAL,
            ADD COLUMN IF NOT EXISTS order_total_usdt REAL,
            ADD COLUMN IF NOT EXISTS payment_created_at TIMESTAMP,
            ADD COLUMN IF NOT EXISTS payment_expires_at TIMESTAMP
        """)

        # PayPal order ID для сверки при подтверждении оплаты
        await conn.execute("""
            ALTER TABLE orders
            ADD COLUMN IF NOT EXISTS paypal_order_id TEXT
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
        "choose_section": "📦 Выберите раздел",
        "banned_message": "🚫 Ваш аккаунт был заблокирован администрацией.",
        "section_elfliq": "🧪 Elfliq",
        "section_elfworld": "🌍 Elfworld",
        "section_empty": "Раздел временно пуст",
        "switch_to_elfworld": "🌍 Перейти в Elfworld",
        "switch_to_elfliq": "🧪 Перейти в Elfliq",
        "total": "Итого",
        "added": "Добавлено",
        "clear": "🗑 Очистить",
        "remove": "↩ Убрать последнее",
        "back_shop": "🛒 Вернуться в магазин",
        "pay": "💳 Оплата",
        "cash": "💵 Наличные",
        "usdt": "💳 USDT",
        "paypal": "🅿️ PayPal",
        "paypal_pay_btn": "💳 Оплатить через PayPal",
        "paypal_waiting": "⏳ Ожидаем оплату через PayPal...\n\nПосле оплаты нажмите кнопку ниже.",
        "paypal_paid_btn": "✅ Я оплатил",
        "paypal_success": "✅ Оплата успешно получена.\n\nВаш заказ передан в обработку.\n\nАдминистратор скоро свяжется с вами.",
        "paypal_error": "❌ Не удалось создать платёж PayPal. Попробуйте другой способ оплаты.",
        "paypal_not_paid": "❌ Оплата ещё не подтверждена PayPal. Попробуйте через несколько секунд.",
        "cancel": "❌ Отмена",
        "order_done": "Заказ оформлен. Админ скоро свяжется",
        "confirm_order": "Подтвердить заказ?",
        "confirm": "✅ Подтвердить",
        "paid": "✅ Оплачено",
        "checking_payment": "Проверяем оплату, админ скоро свяжется",
        "wallet_text": "Оплати USDT\n\n{wallet}\n\n(нажми чтобы скопировать)",
        "usdt_payment_screen": "💶 Сумма заказа: {eur}€\n\n💲 Курс:\n1 EUR = {rate} USDT\n\n💵 К оплате:\n{usdt} USDT (TRC20)\n\n📥 Адрес:\n{wallet}\n\nНажмите на адрес для копирования.",
        "rate_unavailable": "⚠️ Не удалось получить курс USDT. Попробуй ещё раз через минуту.",
        "rate_expired_retry": "⏳ Курс устарел, открой оплату USDT заново.",
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
    },

    "ua": {
        "menu": "📱 Меню",
        "shop": "🛒 Магазин",
        "cart": "🧺 Кошик",
        "language": "🌍 Мова",
        "empty_cart": "Кошик порожній",
        "choose_lang": "Обери мову",
        "choose_product": "🛒 Обери товар:",
        "choose_section": "📦 Оберіть розділ",
        "banned_message": "🚫 Ваш акаунт було заблоковано адміністрацією.",
        "section_elfliq": "🧪 Elfliq",
        "section_elfworld": "🌍 Elfworld",
        "section_empty": "Розділ тимчасово порожній",
        "switch_to_elfworld": "🌍 Перейти до Elfworld",
        "switch_to_elfliq": "🧪 Перейти до Elfliq",
        "total": "Разом",
        "added": "Додано",
        "clear": "🗑 Очистити",
        "remove": "↩ Прибрати останнє",
        "back_shop": "🛒 Назад до магазину",
        "pay": "💳 Оплата",
        "cash": "💵 Готівка",
        "usdt": "💳 USDT",
        "paypal": "🅿️ PayPal",
        "paypal_pay_btn": "💳 Сплатити через PayPal",
        "paypal_waiting": "⏳ Очікуємо оплату через PayPal...\n\nПісля оплати натисни кнопку нижче.",
        "paypal_paid_btn": "✅ Я оплатив",
        "paypal_success": "✅ Оплата успішно отримана.\n\nВаше замовлення передано в обробку.\n\nАдміністратор незабаром зв'яжеться з вами.",
        "paypal_error": "❌ Не вдалося створити платіж PayPal. Спробуй інший спосіб оплати.",
        "paypal_not_paid": "❌ Оплату ще не підтверджено PayPal. Спробуй через кілька секунд.",
        "cancel": "❌ Скасувати",
        "order_done": "Замовлення оформлене. Адмін скоро зв'яжеться",
        "confirm_order": "Підтвердити замовлення?",
        "confirm": "✅ Підтвердити",
        "paid": "✅ Оплачено",
        "checking_payment": "Перевіряємо оплату, адмін скоро зв’яжеться",
        "wallet_text": "Оплати USDT\n\n{wallet}\n\n(натисни щоб скопіювати)",
        "usdt_payment_screen": "💶 Сума замовлення: {eur}€\n\n💲 Курс:\n1 EUR = {rate} USDT\n\n💵 До оплати:\n{usdt} USDT (TRC20)\n\n📥 Адреса:\n{wallet}\n\nНатисни на адресу, щоб скопіювати.",
        "rate_unavailable": "⚠️ Не вдалося отримати курс USDT. Спробуй ще раз за хвилину.",
        "rate_expired_retry": "⏳ Курс застарів, відкрий оплату USDT знову.",
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
    },

    "de": {
        "menu": "📱 Menü",
        "shop": "🛒 Shop",
        "cart": "🧺 Warenkorb",
        "language": "🌍 Sprache",
        "empty_cart": "Warenkorb ist leer",
        "choose_lang": "Sprache wählen",
        "choose_product": "🛒 Produkt wählen:",
        "choose_section": "📦 Wähle einen Bereich",
        "banned_message": "🚫 Dein Konto wurde von der Administration gesperrt.",
        "section_elfliq": "🧪 Elfliq",
        "section_elfworld": "🌍 Elfworld",
        "section_empty": "Dieser Bereich ist momentan leer",
        "switch_to_elfworld": "🌍 Wechseln zu Elfworld",
        "switch_to_elfliq": "🧪 Wechseln zu Elfliq",
        "total": "Summe",
        "added": "Hinzugefügt",
        "clear": "🗑 Leeren",
        "remove": "↩ Letztes entfernen",
        "back_shop": "🛒 Zurück zum Shop",
        "pay": "💳 Zahlung",
        "cash": "💵 Bar",
        "usdt": "💳 USDT",
        "paypal": "🅿️ PayPal",
        "paypal_pay_btn": "💳 Per PayPal bezahlen",
        "paypal_waiting": "⏳ Warte auf PayPal-Zahlung...\n\nNach der Zahlung bitte unten klicken.",
        "paypal_paid_btn": "✅ Ich habe gezahlt",
        "paypal_success": "✅ Zahlung erfolgreich erhalten.\n\nIhre Bestellung wird bearbeitet.\n\nDer Admin meldet sich bald.",
        "paypal_error": "❌ PayPal-Zahlung konnte nicht erstellt werden. Bitte andere Zahlungsart wählen.",
        "paypal_not_paid": "❌ Zahlung noch nicht von PayPal bestätigt. Bitte in einigen Sekunden erneut versuchen.",
        "cancel": "❌ Abbrechen",
        "order_done": "Bestellung erstellt. Admin meldet sich",
        "confirm_order": "Bestellung bestätigen?",
        "confirm": "✅ Bestätigen",
        "paid": "✅ Bezahlt",
        "checking_payment": "Zahlung wird geprüft, Admin meldet sich",
        "wallet_text": "Zahle USDT\n\n{wallet}\n\n(klicken zum Kopieren)",
        "usdt_payment_screen": "💶 Bestellsumme: {eur}€\n\n💲 Kurs:\n1 EUR = {rate} USDT\n\n💵 Zu zahlen:\n{usdt} USDT (TRC20)\n\n📥 Adresse:\n{wallet}\n\nTippe auf die Adresse, um sie zu kopieren.",
        "rate_unavailable": "⚠️ Der USDT-Kurs konnte nicht abgerufen werden. Versuche es in einer Minute erneut.",
        "rate_expired_retry": "⏳ Der Kurs ist abgelaufen, öffne die USDT-Zahlung erneut.",
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
            SELECT total_items, referrals, current_discount, ref_bonus
            FROM users WHERE user_id=$1
        """, uid)

    if not user:
        return []

    items = user["total_items"]
    streak = await get_effective_streak(uid)
    refs = user["referrals"]
    wheel_discount = user["current_discount"]
    ref_bonus = user["ref_bonus"] or 0

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

# ========== USDT: КУРС И ФИКСАЦИЯ ==========

USDT_RATE_LOCK_MINUTES = 30


async def get_eur_usdt_rate():
    """
    Текущий курс 1 EUR -> USDT.
    Основной источник — Binance (пара EURUSDT), т.к. кошелёк для приёма
    платежей именно на Binance. Если Binance недоступен — берём курс
    EUR->USD с ECB (через Frankfurter, без ключа) как практический эквивалент
    (USDT исторически близок к 1 USD). Возвращает None, если оба источника
    недоступны.
    """
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": "EURUSDT"}
            ) as resp:
                data = await resp.json()
                return round(float(data["price"]), 2)
    except Exception:
        logging.exception("Binance недоступен, пробую резервный источник курса EUR/USDT")

    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                "https://api.frankfurter.dev/v1/latest",
                params={"from": "EUR", "to": "USD"}
            ) as resp:
                data = await resp.json()
                return round(float(data["rates"]["USD"]), 2)
    except Exception:
        logging.exception("Резервный источник курса EUR/USDT тоже недоступен")
        return None


async def get_or_create_usdt_lock(uid, eur_total, discount):
    """
    Возвращает зафиксированные значения оплаты USDT для этого пользователя:
    {"eur", "discount", "rate", "usdt", "created_at", "expires_at"}.

    Если есть ещё действующая (не истёкшая) фиксация — возвращает её БЕЗ
    изменений, даже если переданные eur_total/discount отличаются от того,
    что было зафиксировано изначально (курс и сумма не должны меняться
    "за спиной" пользователя в течение 30 минут).

    Если фиксации нет или она истекла — запрашивает новый курс и создаёт
    новую фиксацию на основе переданной суммы.

    Возвращает None, если курс получить не удалось (оба источника недоступны).
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT usdt_lock_eur, usdt_lock_discount, usdt_lock_rate,
                   usdt_lock_usdt, usdt_lock_created_at, usdt_lock_expires_at
            FROM users WHERE user_id=$1
        """, uid)

    now = datetime.now()

    if row and row["usdt_lock_expires_at"] and row["usdt_lock_expires_at"] > now:
        return {
            "eur": row["usdt_lock_eur"],
            "discount": row["usdt_lock_discount"] or 0,
            "rate": row["usdt_lock_rate"],
            "usdt": row["usdt_lock_usdt"],
            "created_at": row["usdt_lock_created_at"],
            "expires_at": row["usdt_lock_expires_at"],
        }

    rate = await get_eur_usdt_rate()

    if rate is None:
        return None

    usdt_amount = round(eur_total * rate, 2)
    created_at = now
    expires_at = now + timedelta(minutes=USDT_RATE_LOCK_MINUTES)

    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE users SET
                usdt_lock_eur=$1,
                usdt_lock_discount=$2,
                usdt_lock_rate=$3,
                usdt_lock_usdt=$4,
                usdt_lock_created_at=$5,
                usdt_lock_expires_at=$6
            WHERE user_id=$7
        """, eur_total, discount, rate, usdt_amount, created_at, expires_at, uid)

    return {
        "eur": eur_total,
        "discount": discount,
        "rate": rate,
        "usdt": usdt_amount,
        "created_at": created_at,
        "expires_at": expires_at,
    }


async def clear_usdt_lock(uid):
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE users SET
                usdt_lock_eur=NULL,
                usdt_lock_discount=NULL,
                usdt_lock_rate=NULL,
                usdt_lock_usdt=NULL,
                usdt_lock_created_at=NULL,
                usdt_lock_expires_at=NULL
            WHERE user_id=$1
        """, uid)

# ========== PAYPAL ==========

_paypal_token_cache: dict = {"token": None, "expires_at": 0}


async def paypal_get_access_token() -> str | None:
    """Получить Access Token PayPal (кэшируется до истечения срока)."""
    import time
    now = time.time()
    if _paypal_token_cache["token"] and now < _paypal_token_cache["expires_at"] - 30:
        return _paypal_token_cache["token"]

    try:
        async with aiohttp.ClientSession() as session:
            resp = await session.post(
                f"{PAYPAL_BASE}/v1/oauth2/token",
                data={"grant_type": "client_credentials"},
                auth=aiohttp.BasicAuth(PAYPAL_CLIENT_ID, PAYPAL_SECRET),
                headers={"Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10),
            )
            data = await resp.json()

        if "access_token" not in data:
            logging.error(f"PayPal token error: {data}")
            return None

        _paypal_token_cache["token"] = data["access_token"]
        _paypal_token_cache["expires_at"] = now + data.get("expires_in", 3600)
        return _paypal_token_cache["token"]
    except Exception as e:
        logging.error(f"PayPal token exception: {e}")
        return None


async def paypal_create_order(amount_eur: float, order_id: int) -> dict | None:
    """
    Создать PayPal Order.
    Возвращает {"paypal_order_id": ..., "approve_url": ...} или None при ошибке.
    """
    token = await paypal_get_access_token()
    if not token:
        return None

    return_url = f"{PUBLIC_URL}{PAYPAL_WEBHOOK_PATH}/return?bot_order_id={order_id}"
    cancel_url = f"{PUBLIC_URL}{PAYPAL_WEBHOOK_PATH}/cancel?bot_order_id={order_id}"

    payload = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": str(order_id),
            "amount": {
                "currency_code": "EUR",
                "value": f"{amount_eur:.2f}",
            },
            "description": f"Order #{order_id}",
        }],
        "application_context": {
            "return_url": return_url,
            "cancel_url": cancel_url,
            "brand_name": "BIZZ Shop",
            "user_action": "PAY_NOW",
        },
    }

    try:
        async with aiohttp.ClientSession() as session:
            resp = await session.post(
                f"{PAYPAL_BASE}/v2/checkout/orders",
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=15),
            )
            data = await resp.json()

        if data.get("status") != "CREATED":
            logging.error(f"PayPal create order error: {data}")
            return None

        approve_url = next(
            (l["href"] for l in data.get("links", []) if l["rel"] == "approve"),
            None,
        )
        return {"paypal_order_id": data["id"], "approve_url": approve_url}
    except Exception as e:
        logging.error(f"PayPal create order exception: {e}")
        return None


async def paypal_capture_order(paypal_order_id: str) -> bool:
    """
    Capture (списать деньги) по PayPal Order.
    Возвращает True если платёж успешно захвачен (status == COMPLETED).
    """
    token = await paypal_get_access_token()
    if not token:
        return False

    try:
        async with aiohttp.ClientSession() as session:
            resp = await session.post(
                f"{PAYPAL_BASE}/v2/checkout/orders/{paypal_order_id}/capture",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=15),
            )
            data = await resp.json()

        return data.get("status") == "COMPLETED"
    except Exception as e:
        logging.error(f"PayPal capture exception: {e}")
        return False

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

async def render(target, text, kb=None, photo=None):
    try:
        if isinstance(target, types.Message):
            if photo:
                await target.answer_photo(
                    photo,
                    caption=text,
                    reply_markup=kb,
                    parse_mode="HTML"
                )
            else:
                await target.answer(
                    text,
                    reply_markup=kb,
                    parse_mode="HTML"
                )
            return
            
        msg = target.message

        if photo:
            await msg.delete()
            await msg.answer_photo(
                photo,
                caption=text,
                reply_markup=kb,
                parse_mode="HTML"
            )
        else:
            await msg.edit_text(
                text,
                reply_markup=kb,
                parse_mode="HTML"
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
                parse_mode="HTML"
            )
        else:
            await target.message.answer(
                text,
                reply_markup=kb,
                parse_mode="HTML"
            )
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
            SELECT total_items, total_orders, total_saved
            FROM users WHERE user_id=$1
        """, uid)

    items = row["total_items"] or 0
    orders = row["total_orders"] or 0
    saved = row["total_saved"] or 0

    lang = await get_lang(uid)
    rank = get_rank(items)
    rank_name = rank["name"][lang]

    # Общая скидка — та же величина, что и на экране "Скидки"
    # (ранг + стрик + рулетка + рефералка + любые другие активные скидки)
    total_discount = await calculate_total_discount(uid, 1)

    text = (
        f"{await t(uid,'profile_title')}\n\n"
        f"{rank_name}\n\n"
        f"📦 {await t(uid,'profile_items')}: {items}\n"
        f"🧾 {await t(uid,'profile_orders')}: {orders}\n"
        f"💸 {await t(uid,'profile_saved')}: {saved:.2f}€\n"
        f"\n💸 {await t(uid,'profile_discount')}: {total_discount}€"
    )

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
        items = await conn.fetchval(
            "SELECT items FROM orders WHERE id=$1",
            oid
        )

        await conn.execute("DELETE FROM cart WHERE user_id=$1", uid)

        for item in items.split(","):
            pid, qty = item.split(":")
            pid = int(pid)
            qty = int(qty)

            await conn.execute("""
                INSERT INTO cart (user_id, product_id, quantity)
                VALUES ($1,$2,$3)
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

    await render_category_selection(call, uid, mode="gift")

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

async def render_gift_shop(target, uid, category):
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

    for p in products:
        pid = p["id"]
        name = p[f"name_{lang}"]
        text += f"{name}\n"
        kb.add(InlineKeyboardButton(name, callback_data=f"gift_view_{pid}"))

    kb.add(InlineKeyboardButton(await t(uid, "back"), callback_data="open_gift_shop"))

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
    async with pool.acquire() as conn:
        products = await conn.fetch(
            "SELECT * FROM products WHERE category=$1 ORDER BY id", category
        )

    total_qty = 1

    final_price, discount = await calculate_final_price(uid, total_qty)
    base_price = 15

    section_key = "section_elfliq" if category == "elfliq" else "section_elfworld"

    text = f"{await t(uid, section_key)}\n\n"
    text += await t(uid, "choose_product") + "\n\n"

    if discount > 0:
        text += f"💰 {base_price}€ → {final_price}€ (-{discount}€)\n\n"
    else:
        text += f"💰 {base_price}€\n\n"

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
# ========== ОПЛАТА ==========

@dp.callback_query_handler(lambda c: c.data == "pay")
async def pay(call):
    uid = call.from_user.id

    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton(await t(uid,"cash"), callback_data="cash"),
        InlineKeyboardButton(await t(uid,"usdt"), callback_data="usdt"),
    )
    kb.add(
        InlineKeyboardButton(await t(uid,"paypal"), callback_data="paypal"),
    )
    kb.add(
        InlineKeyboardButton(await t(uid,"cancel"), callback_data="open_cart")
    )

    await render(call, await t(uid,"pay"), kb)

@dp.callback_query_handler(lambda c: c.data == "cash")
async def cash(call):
    uid = call.from_user.id

    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton(await t(uid,"cancel"), callback_data="pay"),
        InlineKeyboardButton(await t(uid,"confirm"), callback_data="confirm_cash")
    )

    await render(call, await t(uid,"confirm_order"), kb)

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
            INSERT INTO orders (user_id, items, total, payment, discount)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
        """, uid, items_str, total, "cash", discount)

        await conn.execute("""
            DELETE FROM cart WHERE user_id=$1
        """, uid)

    await render(call, await t(uid,"order_done"))

    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("✅ Подтвердить", callback_data=f"admin_confirm_{order_id}"),
        InlineKeyboardButton("❌ Отменить", callback_data=f"admin_cancel_{order_id}")
    )

    order_text = f"{text_admin}\n\nID: {order_id}\nUser: @{username}\nОплата: Наличные\n ИТОГО: {total}€"
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

@dp.callback_query_handler(lambda c: c.data == "usdt")
async def usdt(call):
    if not await check_not_banned(call):
        return

    uid = call.from_user.id

    async with pool.acquire() as conn:
        cart_items = await conn.fetch(
            "SELECT quantity FROM cart WHERE user_id=$1", uid
        )

    if not cart_items:
        await render(call, await t(uid, "empty_cart"))
        return

    total_qty = sum(r["quantity"] for r in cart_items)
    eur_total, discount = await calculate_final_price(uid, total_qty)

    lock = await get_or_create_usdt_lock(uid, eur_total, discount)

    if lock is None:
        await call.answer(await t(uid, "rate_unavailable"), show_alert=True)
        return

    text = (await t(uid, "usdt_payment_screen")).format(
        eur=fmt_amount(lock["eur"]),
        rate=fmt_amount(lock["rate"]),
        usdt=fmt_amount(lock["usdt"]),
        wallet=f"<code>{WALLET}</code>"
    )

    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton(await t(uid,"cancel"), callback_data="pay"),
        InlineKeyboardButton(await t(uid,"paid"), callback_data="paid")
    )

    await render(call, text, kb)

@dp.callback_query_handler(lambda c: c.data == "paid")
async def paid(call):
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

        lock_row = await conn.fetchrow("""
            SELECT usdt_lock_eur, usdt_lock_discount, usdt_lock_rate,
                   usdt_lock_usdt, usdt_lock_created_at, usdt_lock_expires_at
            FROM users WHERE user_id=$1
        """, uid)

    # Зафиксированных значений нет или они истекли — заказ создавать нельзя,
    # пользователь должен сначала заново открыть экран оплаты USDT и увидеть
    # актуальные цифры, прежде чем они лягут в заказ.
    if (not lock_row or not lock_row["usdt_lock_expires_at"]
            or lock_row["usdt_lock_expires_at"] <= datetime.now()):
        await call.answer(await t(uid, "rate_expired_retry"), show_alert=True)
        return

    items_str = ",".join([f"{r['product_id']}:{r['quantity']}" for r in cart_items])

    # Берём СТРОГО зафиксированные значения — то, что пользователь видел на
    # экране оплаты. Ничего не пересчитываем, даже если корзина изменилась.
    total = lock_row["usdt_lock_eur"]
    discount = lock_row["usdt_lock_discount"] or 0
    rate = lock_row["usdt_lock_rate"]
    usdt_amount = lock_row["usdt_lock_usdt"]
    payment_created_at = lock_row["usdt_lock_created_at"]
    payment_expires_at = lock_row["usdt_lock_expires_at"]

    async with pool.acquire() as conn:
        text_admin = "ЗАКАЗ:\n"

        for r in cart_items:
            product = await conn.fetchrow("""
                SELECT name_ru 
                FROM products 
                WHERE id=$1
            """, r["product_id"])

            text_admin += f"{product['name_ru']} x{r['quantity']}\n"

        order_id = await conn.fetchval("""
            INSERT INTO orders
                (user_id, items, total, payment, discount,
                 eur_usdt_rate, order_total_usdt, payment_created_at, payment_expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING id
        """, uid, items_str, total, "usdt", discount,
             rate, usdt_amount, payment_created_at, payment_expires_at)

        await conn.execute("DELETE FROM cart WHERE user_id=$1", uid)

    await clear_usdt_lock(uid)

    await render(call, await t(uid,"checking_payment"))

    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("✅ Подтвердить", callback_data=f"admin_confirm_{order_id}"),
        InlineKeyboardButton("❌ Отменить", callback_data=f"admin_cancel_{order_id}")
    )

    # Админы видят и сумму в EUR (которую видел пользователь), и точную
    # сумму в USDT + курс, по которому она была зафиксирована.
    order_text = (
        f"{text_admin}\n\nID: {order_id}\nUser: @{username}\nОплата: USDT\n"
        f"ИТОГО: {fmt_amount(total)}€ = {fmt_amount(usdt_amount)} USDT (курс 1 EUR = {fmt_amount(rate)} USDT)"
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
            await conn.execute("""
                UPDATE orders SET admin_message_ids=$1 WHERE id=$2
            """, ",".join(msg_ids), order_id)

# ========== PAYPAL ОПЛАТА ==========

@dp.callback_query_handler(lambda c: c.data == "paypal")
async def paypal_pay(call):
    if not await check_not_banned(call):
        return

    uid = call.from_user.id

    async with pool.acquire() as conn:
        cart_items = await conn.fetch("""
            SELECT product_id, quantity FROM cart WHERE user_id=$1
        """, uid)

    if not cart_items:
        await render(call, await t(uid, "empty_cart"))
        return

    total_qty = sum(r["quantity"] for r in cart_items)
    eur_total, discount = await calculate_final_price(uid, total_qty)
    username = call.from_user.username or "нет username"

    async with pool.acquire() as conn:
        text_admin = "ЗАКАЗ:\n"
        items_str_parts = []
        for r in cart_items:
            product = await conn.fetchrow(
                "SELECT name_ru FROM products WHERE id=$1", r["product_id"]
            )
            text_admin += f"{product['name_ru']} x{r['quantity']}\n"
            items_str_parts.append(f"{r['product_id']}:{r['quantity']}")

        items_str = ",".join(items_str_parts)

        # Создаём заказ в БД со статусом 'paypal_pending'
        order_id = await conn.fetchval("""
            INSERT INTO orders (user_id, items, total, payment, discount, status)
            VALUES ($1, $2, $3, 'paypal', $4, 'paypal_pending')
            RETURNING id
        """, uid, items_str, eur_total, discount)

        await conn.execute("DELETE FROM cart WHERE user_id=$1", uid)

    # Создаём PayPal Order
    result = await paypal_create_order(eur_total, order_id)

    if not result:
        # Откатываем заказ если PayPal недоступен
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE orders SET status='cancelled' WHERE id=$1", order_id
            )
        await render(call, await t(uid, "paypal_error"))
        return

    paypal_order_id = result["paypal_order_id"]
    approve_url = result["approve_url"]

    # Сохраняем paypal_order_id
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE orders SET paypal_order_id=$1 WHERE id=$2",
            paypal_order_id, order_id
        )

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(
        await t(uid, "paypal_pay_btn"), url=approve_url
    ))
    kb.add(InlineKeyboardButton(
        await t(uid, "paypal_paid_btn"),
        callback_data=f"paypal_check_{order_id}"
    ))

    await render(call, await t(uid, "paypal_waiting"), kb)


@dp.callback_query_handler(lambda c: c.data.startswith("paypal_check_"))
async def paypal_check(call):
    """Пользователь нажал 'Я оплатил' — проверяем и capture'им PayPal Order."""
    if not await check_not_banned(call):
        return

    uid = call.from_user.id
    order_id = int(call.data.split("_")[2])
    username = call.from_user.username or "нет username"

    async with pool.acquire() as conn:
        order = await conn.fetchrow("""
            SELECT status, paypal_order_id, total, discount, items
            FROM orders WHERE id=$1 AND user_id=$2
        """, order_id, uid)

    if not order:
        await call.answer("Заказ не найден", show_alert=True)
        return

    if order["status"] == "pending":
        # Уже подтверждён через webhook — показываем сообщение об успехе
        await render(call, await t(uid, "paypal_success"))
        return

    if order["status"] != "paypal_pending":
        await render(call, await t(uid, "paypal_success"))
        return

    # Пытаемся capture (списать деньги у PayPal)
    captured = await paypal_capture_order(order["paypal_order_id"])

    if not captured:
        await call.answer(await t(uid, "paypal_not_paid"), show_alert=True)
        return

    await _finalize_paypal_order(order_id, uid, order)


async def _finalize_paypal_order(order_id: int, uid: int, order):
    """
    Финализация PayPal заказа: обновить статус → уведомить пользователя и админов.
    Идемпотентна: повторный вызов безопасен благодаря UPDATE WHERE status='paypal_pending'.
    """
    async with pool.acquire() as conn:
        updated = await conn.fetchval("""
            UPDATE orders SET status='pending'
            WHERE id=$1 AND status='paypal_pending'
            RETURNING id
        """, order_id)

    if not updated:
        return

    # Берём реальный username пользователя из БД
    async with pool.acquire() as conn:
        username = await conn.fetchval(
            "SELECT username FROM users WHERE user_id=$1", uid
        ) or "нет username"

    # Уведомление пользователю
    try:
        await bot.send_message(uid, await t(uid, "paypal_success"))
    except Exception:
        pass

    # Собираем текст для админов
    async with pool.acquire() as conn:
        items_str = order["items"]
        text_admin = "ЗАКАЗ (оплачен через PayPal ✅):\n"
        for part in items_str.split(","):
            pid, qty = part.split(":")
            product = await conn.fetchrow(
                "SELECT name_ru FROM products WHERE id=$1", int(pid)
            )
            if product:
                text_admin += f"{product['name_ru']} x{qty}\n"

    total = order["total"]
    order_text = (
        f"{text_admin}\n"
        f"ID: {order_id}\n"
        f"User: @{username}\n"
        f"Оплата: PayPal ✅ (уже оплачено)\n"
        f"ИТОГО: {total}€"
    )

    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("✅ Подтвердить", callback_data=f"admin_confirm_{order_id}"),
        InlineKeyboardButton("❌ Отменить", callback_data=f"admin_cancel_{order_id}")
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

        #    одноразовые бонусы (скидка с рулетки, реферальная, новичка) ──
        await conn.execute("""
            UPDATE users 
            SET current_discount = 0,
                referrals = 0,
                ref_bonus = 0,
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

# ========== АДМИН-КОМАНДЫ ==========


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

async def paypal_webhook_handler(request: aiohttp.web.Request) -> aiohttp.web.Response:
    """
    PayPal шлёт IPN/Webhook на этот endpoint при изменении статуса платежа.
    Также принимает return_url редирект после оплаты (пользователь возвращается
    с PayPal). Обрабатываем оба сценария в одном хендлере.
    """
    # Return URL: пользователь вернулся после оплаты
    if request.method == "GET":
        bot_order_id = int(request.rel_url.query.get("bot_order_id", 0))
        token = request.rel_url.query.get("token", "")  # PayPal Order ID

        if bot_order_id and token:
            async with pool.acquire() as conn:
                order = await conn.fetchrow("""
                    SELECT id, user_id, status, paypal_order_id, total, discount, items
                    FROM orders WHERE id=$1
                """, bot_order_id)

            if order and order["status"] == "paypal_pending":
                captured = await paypal_capture_order(order["paypal_order_id"])
                if captured:
                    uid = order["user_id"]
                    await _finalize_paypal_order(bot_order_id, uid, order)

        return aiohttp.web.Response(
            text="<html><body><h2>✅ Оплата получена. Вернитесь в Telegram.</h2></body></html>",
            content_type="text/html"
        )

    # Webhook от PayPal (POST)
    try:
        data = await request.json()
    except Exception:
        return aiohttp.web.Response(status=400)

    event_type = data.get("event_type", "")

    if event_type == "CHECKOUT.ORDER.APPROVED":
        resource = data.get("resource", {})
        paypal_order_id = resource.get("id")

        if paypal_order_id:
            async with pool.acquire() as conn:
                order = await conn.fetchrow("""
                    SELECT id, user_id, status, paypal_order_id, total, discount, items
                    FROM orders WHERE paypal_order_id=$1
                """, paypal_order_id)

            if order and order["status"] == "paypal_pending":
                captured = await paypal_capture_order(paypal_order_id)
                if captured:
                    uid = order["user_id"]
                    await _finalize_paypal_order(order["id"], uid, order)

    return aiohttp.web.Response(text="ok")


async def on_startup(dp):
    await init_db()


async def run():
    await on_startup(dp)

    # aiohttp-сервер для PayPal webhook и return URL
    app = aiohttp.web.Application()
    app.router.add_get(f"{PAYPAL_WEBHOOK_PATH}/return", paypal_webhook_handler)
    app.router.add_get(f"{PAYPAL_WEBHOOK_PATH}/cancel", lambda r: aiohttp.web.Response(
        text="<html><body><h2>Оплата отменена. Вернитесь в Telegram.</h2></body></html>",
        content_type="text/html"
    ))
    app.router.add_post(PAYPAL_WEBHOOK_PATH, paypal_webhook_handler)

    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "0.0.0.0", PAYPAL_WEBHOOK_PORT)
    await site.start()
    logging.info(f"PayPal webhook listening on :{PAYPAL_WEBHOOK_PORT}{PAYPAL_WEBHOOK_PATH}")

    # Запускаем polling рядом с aiohttp
    await dp.start_polling(reset_webhook=True)


if __name__ == "__main__":
    asyncio.run(run())
