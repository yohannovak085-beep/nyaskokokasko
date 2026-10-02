"""
Telegram bot with deep-link support, subscription gate (channel & chat), 
dice game, admin manager and AUTO-BROADCAST system.

Environment variables (Replit Secrets):
  BOT_TOKEN = "8991743492:AAGQGctQYsg6jPSG9crrww6AbLQf57foy1s"
  ADMIN_ID   — primary admin Telegram user ID (integer)

IMPORTANT: Add this bot as an Administrator to both @Berlions_mb and @Chats_Berlions
so it can check member status via getChatMember.
"""

import os
import time
import html
import logging
from dotenv import load_dotenv
import telebot
from telebot.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
    Message,
    CallbackQuery,
)
from telebot.apihelper import ApiTelegramException

import database

_dice_cooldown: dict[int, float] = {}
# ── Configuration ─────────────────────────────────────────────────────────────

load_dotenv()

BOT_TOKEN = "8991743492:AAGQGctQYsg6jPSG9crrww6AbLQf57foy1s"
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "")
GROUPS_FILE = "groups.json" # Файл для хранения ID групп

if not BOT_TOKEN:
    raise EnvironmentError("BOT_TOKEN is not set. Add it to Replit Secrets.")

# Ресурсы для подписки
CHANNEL_USERNAME = "@Berlions_mb"
CHANNEL_URL = "https://t.me/Berlions_mb"
CHAT_USERNAME = "@Chats_Berlions" # Исправленный юзернейм чата
CHAT_URL = "https://t.me/Chats_Berlions" # Исправленная ссылка на чат

EXTRA_ADMIN_IDS = [2056454748, 8201074902]
ADMIN_IDS: set[int] = set(EXTRA_ADMIN_IDS)
for _raw in ADMIN_ID_RAW.split(","):
    _raw = _raw.strip()
    if _raw.isdigit():
        ADMIN_IDS.add(int(_raw))

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
logger = logging.getLogger(__name__)
logger.info("Admin IDs: %s", ADMIN_IDS)

# ── Bot setup ─────────────────────────────────────────────────────────────────

# Включаем поддержку Middleware ПЕРЕД инициализацией бота, чтобы не было ошибок
telebot.apihelper.ENABLE_MIDDLEWARE = True

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
database.init_db()

# ── In-memory conversation state ──────────────────────────────────────────────

_pending: dict[int, dict] = {}
_stupid_stats: dict[int, dict] = {} # пока не используется, но оставлено по структуре

# ── Subscription gate (Двойная проверка подписки на канал И чат) ───────────────

def _check_sub_status(chat_username: str, user_id: int) -> bool:
    """Вспомогательная функция для проверки статуса подписки на конкретный ресурс."""
    try:
        member = bot.get_chat_member(chat_username, user_id)
        return member.status not in ("left", "kicked")
    except ApiTelegramException as exc:
        desc = exc.description.lower()
        if "user not found" in desc or "participant not found" in desc:
            return False # Пользователя нет в чате/канале
        logger.warning("get_chat_member failed for %s (user %s): %s", chat_username, user_id, exc)
        return True # Другие ошибки (например, бот не админ) - пропускаем
    except Exception as exc:
        logger.warning("Unexpected error checking sub for %s: %s", chat_username, exc)
        return True # Неизвестные ошибки - пропускаем

def _is_subscribed(user_id: int) -> bool:
    """Возвращает True, если пользователь подписан на ОБА ресурса."""
    if user_id in ADMIN_IDS:
        return True # Админов пропускаем
        
    chan_ok = _check_sub_status(CHANNEL_USERNAME, user_id)
    chat_ok = _check_sub_status(CHAT_USERNAME, user_id)
    
    return chan_ok and chat_ok

def _sub_required_markup(context: str) -> InlineKeyboardMarkup:
    """Генерирует кнопки для подписки."""
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("📢 Подписаться на канал", url=CHANNEL_URL),
        InlineKeyboardButton("💬 Вступить в наш чат", url=CHAT_URL),
        InlineKeyboardButton("✅ Я подписался на всё", callback_data=f"verify:{context}"),
    )
    return markup

def _require_subscription(chat_id: int, user_id: int, context: str) -> bool:
    """Проверяет подписку. Если не подписан, отправляет сообщение-заглушку."""
    if _is_subscribed(user_id):
        return True

    bot.send_message(
        chat_id,
        "🔒 <b>Доступ закрыт</b>\n\n"
        "Чтобы получить контент, нужно подписаться на наш <b>канал</b> и вступить в <b>чат</b>.\n\n"
        "После подписки на оба ресурса нажми кнопку <b>«✅ Я подписался на всё»</b>.",
        reply_markup=_sub_required_markup(context),
    )
    return False

# ── Delivery Helper (Отправка контента) ───────────────────────────────────────

def _deliver_link(chat_id: int, row: database.sqlite3.Row) -> None:
    """Отправляет контент ссылки (файлы, текст или URL) единым блоком."""
    files = database.get_link_files(row["key"])
    caption = row["content_text"]

    # Нет файлов — просто текст или кнопка с URL
    if not files:
        if row["target_url"]:
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("⬇️ Скачать / Перейти", url=row["target_url"]))
            bot.send_message(chat_id, caption, reply_markup=markup)
        else:
            bot.send_message(chat_id, caption)
        return

    # Одиночный файл — отправляем с подписью
    if len(files) == 1:
        _send_file(chat_id, files[0]["file_id"], files[0]["file_type"], caption=caption)
        return

    # Несколько файлов — группируем для медиа-групп
    visuals = [f for f in files if f["file_type"] in ("photo", "video")]
    docs    = [f for f in files if f["file_type"] == "document"]
    others  = [f for f in files if f["file_type"] not in ("photo", "video", "document")]

    caption_used = False

    def _cap() -> str | None:
        nonlocal caption_used
        if caption_used: return None
        caption_used = True
        return caption

    if visuals:
        group = []
        for i, f in enumerate(visuals):
            c = _cap() if i == 0 else None
            if f["file_type"] == "photo": group.append(InputMediaPhoto(f["file_id"], caption=c, parse_mode="HTML" if c else None))
            else: group.append(InputMediaVideo(f["file_id"], caption=c, parse_mode="HTML" if c else None))
        bot.send_media_group(chat_id, group)

    if docs:
        if len(docs) == 1: _send_file(chat_id, docs[0]["file_id"], "document", caption=_cap())
        else:
            group = []
            for i, f in enumerate(docs):
                c = _cap() if i == 0 else None
                group.append(InputMediaDocument(f["file_id"], caption=c, parse_mode="HTML" if c else None))
            bot.send_media_group(chat_id, group)

    for f in others: _send_file(chat_id, f["file_id"], f["file_type"], caption=_cap())
    if not caption_used: bot.send_message(chat_id, caption)

def _send_file(chat_id: int, file_id: str, file_type: str, caption: str | None = None) -> None:
    """Вспомогательная функция для отправки одного файла."""
    senders = {
        "document":  bot.send_document, "photo":     bot.send_photo,
        "video":     bot.send_video,    "audio":     bot.send_audio,
        "voice":     bot.send_voice,    "animation": bot.send_animation,
    }
    fn = senders.get(file_type, bot.send_document)
    kwargs: dict = {}
    if caption: kwargs["caption"] = caption
    fn(chat_id, file_id, **kwargs)

def _extract_file(message: Message) -> tuple[str, str] | tuple[None, None]:
    """Извлекает file_id и file_type из сообщения."""
    if message.document:  return message.document.file_id, "document"
    if message.photo:     return message.photo[-1].file_id, "photo"
    if message.video:     return message.video.file_id, "video"
    if message.audio:     return message.audio.file_id, "audio"
    if message.voice:     return message.voice.file_id, "voice"
    if message.animation: return message.animation.file_id, "animation"
    return None, None

def _files_added_reply(chat_id: int, count: int) -> None:
    bot.send_message(chat_id, f"✅ Файл добавлен (<b>{count}</b> шт.). Отправьте ещё файл или напишите /stop для сохранения.")

# ── Admin helpers ─────────────────────────────────────────────────────────────

def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def _bot_username() -> str:
    try: return bot.get_me().username or "MyBot"
    except Exception: return "MyBot"

def _deep_link(key: str) -> str:
    return f"https://t.me/{_bot_username()}?start={key}"

# ── /start (С новым приветствием) ─────────────────────────────────────────────

@bot.message_handler(commands=["start"])
def handle_start(message: Message) -> None:
    _pending.pop(message.from_user.id, None)

    username = message.from_user.username if message.from_user.username else "Без никнейма"
    first_name = message.from_user.first_name
    logger.info("Пользователь: %s (@%s), ID: %s", first_name, username, message.from_user.id)

    text = message.text.strip()
    parts = text.split(maxsplit=1)
    key = parts[1].strip() if len(parts) > 1 else None

    if not key:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🎲 Сыграть в кости", callback_data="play_dice"))
        
        user_display = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
        
        user_display = html.escape(str(user_display))
        welcome_text = (
            f"👋 <b>Привет! Я Berlions чат-бот.</b>\n\n"
            f"📋 <b>Доступные команды:</b>\n"
            f"• /start — приветствие и главное меню\n"
            f"• /help — список доступных команд\n"
            f"• /search <i>запрос</i> — поиск игры или контента\n"
            f"• /dice — сыграть в кости 🎲\n• /profile — профиль и активность\n• /top — топ активности\n• /topmoney — топ капитала биржи\n\n"
            f"🔍 <b>Поиск:</b>\n"
            f"Например: <code>/search standoff 2</code>\n\n"
            f"🎲 Нажми кнопку ниже, чтобы сразу сыграть."
        )
        
        bot.send_message(message.chat.id, welcome_text, reply_markup=markup)
        return

    if not _require_subscription(message.chat.id, message.from_user.id, f"key:{key}"): return

    row = database.get_link(key)
    if row is None:
        bot.send_message(message.chat.id, "❌ Ссылка не найдена или устарела.")
        logger.warning("Unknown deep-link key: %r", key)
        return

    _deliver_link(message.chat.id, row)
    logger.info("Served key %r to user %s", key, message.from_user.id)

# ── /help ─────────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["help"])
def handle_help(message: Message) -> None:
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🎲 Сыграть в кости", callback_data="play_dice"))
    bot.send_message(
        message.chat.id,
        "📋 <b>Доступные команды:</b>\n\n"
        "• /start — главное меню\n"
        "• /help — список доступных команд\n"
        "• /search <i>запрос</i> — поиск контента\n"
        "• /dice — сыграть в кости 🎲\n\n"
        "Пример поиска: <code>/search minecraft</code>",
        reply_markup=markup,
    )

# ── /search (С анимацией и умным поиском) ─────────────────────────────────────

@bot.message_handler(commands=["search"])
def handle_search(message: Message) -> None:
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        bot.send_message(
            message.chat.id,
            "🔍 <b>Поиск по играм</b>\n\n"
            "Использование: <code>/search название игры</code>\n\n"
            "Пример: <code>/search minecraft</code>",
        )
        return

    query = parts[1].strip()
    query_lower = query.lower()
    
    # === ХАКЕРСКАЯ АНИМАЦИЯ ИНИЦИАЛИЗАЦИИ ===
    loader_msg = bot.send_message(
        message.chat.id,
        f"<code>> CONNECTING TO BERLIONS_DB...</code>\n"
        f"<code>[██████░░░░░░░░] 45%</code>\n"
        f"<i>Ага, ищу таккк, таккк...</i>"
    )
    
    time.sleep(1.0)  # Небольшая пауза для эффекта загрузки

    rows = database.search_links(query) # Получаем все потенциальные результаты
    
    # === УМНЫЙ ПОИСК (ФИЛЬТРАЦИЯ) ===
    filtered_rows = []
    if len(query) <= 2:
        # Для коротких запросов ищем только те, что НАЧИНАЮТСЯ с этой буквы/символа
        filtered_rows = [r for r in rows if r["content_text"].lower().startswith(query_lower)]
    else:
        # Для длинных: сначала те, что начинаются на этот текст, потом все остальные совпадения
        starts = [r for r in rows if r["content_text"].lower().startswith(query_lower)]
        contains = [r for r in rows if query_lower in r["content_text"].lower() and not r["content_text"].lower().startswith(query_lower)]
        filtered_rows = starts + contains # Объединяем, сначала "начинающиеся", потом "содержащие"

    if not filtered_rows:
        bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=loader_msg.message_id,
            text=f"<code>> SEARCH FAILED</code>\n\n"
                 f"😔 По запросу <b>«{html.escape(query)}»</b> ничего не найдено.\n\n"
                 f"Попробуй другое название."
        )
        return

    markup = InlineKeyboardMarkup(row_width=1)
    is_private = message.chat.type == "private"

    for row in filtered_rows[:15]:  # максимум 15 результатов
        title = row["content_text"]
        btn_label = title if len(title) <= 60 else title[:57] + "…"
        
        if is_private:
            markup.add(
                InlineKeyboardButton(
                    f"🎮 {btn_label}",
                    callback_data=f"search_pick:{row['key']}",
                )
            )
        else:
            markup.add(
                InlineKeyboardButton(
                    f"🎮 {btn_label}",
                    url=_deep_link(row["key"]),
                )
            )

    count = len(filtered_rows)
    
    # === НАШЁЛ И ВЫДАЧА ===
    bot.edit_message_text(
        chat_id=message.chat.id,
        message_id=loader_msg.message_id,
        text=f"<code>> DATABASE UNLOCKED</code>\n"
             f"<code>[██████████████] 100%</code>\n\n"
             f"🔑 <b>НАШЁЛ!</b>\n\n"
             f"🔍 Результатов по запросу «{html.escape(query)}»: <b>{count}</b>\n"
             f"Выбери что тебя интересует 👇",
        reply_markup=markup
    )

# ── BERLIONS EXCHANGE SYSTEM ───────────────────────────────────────────────────
# Отдельный функциональный блок. Старые функции бота не изменяет.

import sqlite3
import random
import struct
import binascii
import zlib
from datetime import datetime, date

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError:
    matplotlib = None
    plt = None

EXCHANGE_DB = "exchange.db"
EXCHANGE_GRAPH_DIR = "exchange_graphs"
EXCHANGE_START_BALANCE = 10_000.0
EXCHANGE_DAILY_BONUS = 1_000.0
EXCHANGE_DEFAULT_PRICE = 100.0
EXCHANGE_DEFAULT_TICKER = "BERL"
EXCHANGE_MAX_TRADE = 100_000


def _ex_db():
    conn = sqlite3.connect(EXCHANGE_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _ex_init_db():
    conn = _ex_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS ex_traders (
        user_id INTEGER PRIMARY KEY,
        balance REAL NOT NULL DEFAULT 10000,
        last_bonus TEXT,
        market_message_id INTEGER,
        market_chat_id INTEGER
    );

    CREATE TABLE IF NOT EXISTS ex_exchanges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        ticker TEXT NOT NULL UNIQUE,
        price REAL NOT NULL DEFAULT 100,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS ex_holdings (
        user_id INTEGER NOT NULL,
        exchange_id INTEGER NOT NULL,
        shares INTEGER NOT NULL DEFAULT 0,
        avg_price REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, exchange_id)
    );

    CREATE TABLE IF NOT EXISTS ex_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        exchange_id INTEGER NOT NULL,
        price REAL NOT NULL,
        ts TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS ex_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        exchange_id INTEGER NOT NULL,
        action TEXT NOT NULL,
        shares INTEGER NOT NULL,
        price REAL NOT NULL,
        total REAL NOT NULL,
        ts TEXT NOT NULL
    );
    """)

    # Базовая биржа Berlions.
    row = conn.execute("SELECT id FROM ex_exchanges WHERE ticker=?", (EXCHANGE_DEFAULT_TICKER,)).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO ex_exchanges(owner_id,name,ticker,price,created_at) VALUES(?,?,?,?,?)",
            (0, "Berlions", EXCHANGE_DEFAULT_TICKER, EXCHANGE_DEFAULT_PRICE, datetime.now().isoformat(timespec="seconds")),
        )
        exchange_id = cur.lastrowid
        conn.execute(
            "INSERT INTO ex_history(exchange_id,price,ts) VALUES(?,?,?)",
            (exchange_id, EXCHANGE_DEFAULT_PRICE, datetime.now().isoformat(timespec="seconds")),
        )
    conn.commit()
    conn.close()


_ex_init_db()


def _ex_ensure_trader(user_id: int):
    conn = _ex_db()
    conn.execute("INSERT OR IGNORE INTO ex_traders(user_id,balance) VALUES(?,?)", (user_id, EXCHANGE_START_BALANCE))
    conn.commit()
    row = conn.execute("SELECT * FROM ex_traders WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row


def _ex_escape(value):
    return html.escape(str(value))


def _ex_change_price(conn, exchange_id: int, action_bias: float = 0.0):
    row = conn.execute("SELECT price FROM ex_exchanges WHERE id=?", (exchange_id,)).fetchone()
    if not row:
        return None, 0.0

    old_price = float(row["price"])
    shock = random.gauss(0, 0.035) + action_bias
    shock = max(-0.12, min(0.12, shock))
    new_price = max(1.0, old_price * (1.0 + shock))

    conn.execute("UPDATE ex_exchanges SET price=? WHERE id=?", (new_price, exchange_id))
    conn.execute(
        "INSERT INTO ex_history(exchange_id,price,ts) VALUES(?,?,?)",
        (exchange_id, new_price, datetime.now().isoformat(timespec="seconds")),
    )
    return new_price, ((new_price / old_price) - 1.0) * 100.0


def _ex_chart(exchange_id: int):
    """
    Создаёт PNG-график без matplotlib/Pillow.
    Использует только стандартную библиотеку Python, поэтому /market
    работает даже если Bothost не установил дополнительные библиотеки.
    """
    import os
    os.makedirs(EXCHANGE_GRAPH_DIR, exist_ok=True)

    conn = _ex_db()
    ex = conn.execute("SELECT * FROM ex_exchanges WHERE id=?", (exchange_id,)).fetchone()
    rows = conn.execute(
        "SELECT price,ts FROM ex_history WHERE exchange_id=? ORDER BY id DESC LIMIT 40",
        (exchange_id,),
    ).fetchall()
    conn.close()

    if not ex:
        raise RuntimeError("Биржа не найдена")

    rows = list(reversed(rows))
    prices = [float(r["price"]) for r in rows] or [float(ex["price"])]
    change = ((prices[-1] / prices[0]) - 1.0) * 100.0 if len(prices) > 1 and prices[0] else 0.0
    path = os.path.join(EXCHANGE_GRAPH_DIR, f"market_{exchange_id}.png")

    # --- PNG рисуем вручную: никаких внешних библиотек не требуется. ---
    width, height = 1200, 650
    bg = (17, 24, 39)
    grid = (55, 65, 81)
    line_color = (34, 197, 94) if change >= 0 else (239, 68, 68)
    area_color = (31, 65, 48) if change >= 0 else (70, 35, 42)

    pixels = bytearray(bg * (width * height))

    def set_px(x, y, color):
        if 0 <= x < width and 0 <= y < height:
            i = (y * width + x) * 3
            pixels[i:i + 3] = bytes(color)

    def rect(x1, y1, x2, y2, color):
        x1, x2 = max(0, int(x1)), min(width - 1, int(x2))
        y1, y2 = max(0, int(y1)), min(height - 1, int(y2))
        if x1 > x2 or y1 > y2:
            return
        row = bytes(color) * (x2 - x1 + 1)
        for yy in range(y1, y2 + 1):
            i = (yy * width + x1) * 3
            pixels[i:i + len(row)] = row

    def line(x1, y1, x2, y2, color, thickness=3):
        # DDA — достаточно для простого биржевого графика.
        steps = max(abs(int(x2) - int(x1)), abs(int(y2) - int(y1)), 1)
        for s in range(steps + 1):
            t = s / steps
            x = round(x1 + (x2 - x1) * t)
            y = round(y1 + (y2 - y1) * t)
            r = max(0, thickness // 2)
            rect(x - r, y - r, x + r, y + r, color)

    margin_left, margin_right = 70, 45
    margin_top, margin_bottom = 55, 55
    chart_left = margin_left
    chart_right = width - margin_right
    chart_top = margin_top
    chart_bottom = height - margin_bottom

    # Рамка и сетка.
    rect(chart_left, chart_top, chart_right, chart_top + 2, grid)
    rect(chart_left, chart_bottom - 2, chart_right, chart_bottom, grid)
    rect(chart_left, chart_top, chart_left + 2, chart_bottom, grid)
    rect(chart_right - 2, chart_top, chart_right, chart_bottom, grid)

    for j in range(1, 5):
        y = chart_top + (chart_bottom - chart_top) * j / 5
        rect(chart_left, y, chart_right, y + 1, grid)

    for j in range(1, 6):
        x = chart_left + (chart_right - chart_left) * j / 6
        rect(x, chart_top, x + 1, chart_bottom, grid)

    low, high = min(prices), max(prices)
    if high == low:
        pad = max(1.0, abs(high) * 0.02)
        low -= pad
        high += pad
    else:
        pad = (high - low) * 0.08
        low -= pad
        high += pad

    points = []
    denom = max(1, len(prices) - 1)
    for i, price in enumerate(prices):
        x = chart_left + (chart_right - chart_left) * i / denom
        y = chart_bottom - (price - low) / (high - low) * (chart_bottom - chart_top)
        points.append((x, y))

    # Лёгкая заливка под линией.
    if points:
        for i in range(len(points) - 1):
            x1, y1 = points[i]
            x2, y2 = points[i + 1]
            steps = max(1, int(abs(x2 - x1)))
            for s in range(steps + 1):
                t = s / steps
                x = round(x1 + (x2 - x1) * t)
                y = round(y1 + (y2 - y1) * t)
                rect(x, min(y, chart_bottom), x + 1, chart_bottom, area_color)

    # Линия курса.
    for i in range(len(points) - 1):
        line(*points[i], *points[i + 1], line_color, thickness=7)

    # Последняя точка.
    if points:
        x, y = points[-1]
        rect(x - 7, y - 7, x + 7, y + 7, line_color)
        rect(x - 3, y - 3, x + 3, y + 3, (255, 255, 255))

    # Заголовок/цифры находятся в caption Telegram, поэтому шрифты не нужны.

    def png_chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", binascii.crc32(kind + data) & 0xffffffff)
        )

    raw = bytearray()
    row_bytes = width * 3
    for y in range(height):
        raw.append(0)  # filter type
        start = y * row_bytes
        raw.extend(pixels[start:start + row_bytes])

    png = (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + png_chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + png_chunk(b"IEND", b"")
    )

    with open(path, "wb") as f:
        f.write(png)

    return path

def _ex_market_text(exchange_id: int):
    conn = _ex_db()
    ex = conn.execute("SELECT * FROM ex_exchanges WHERE id=?", (exchange_id,)).fetchone()
    owner = conn.execute("SELECT user_id FROM ex_traders WHERE user_id=?", (ex["owner_id"],)).fetchone()
    hist = conn.execute(
        "SELECT price FROM ex_history WHERE exchange_id=? ORDER BY id DESC LIMIT 2",
        (exchange_id,),
    ).fetchall()
    conn.close()

    current = float(ex["price"])
    previous = float(hist[1]["price"]) if len(hist) > 1 else current
    change = ((current / previous) - 1.0) * 100 if previous else 0.0
    arrow = "📈" if change > 0 else "📉" if change < 0 else "➖"

    return (
        f"🏦 <b>{_ex_escape(ex['name'])}</b>  <code>${_ex_escape(ex['ticker'])}</code>\n"
        f"💰 Цена: <b>{current:,.2f} ₽</b>\n"
        f"{arrow} Изменение: <b>{change:+.2f}%</b>\n"
        f"👤 Владелец: <code>{ex['owner_id']}</code>\n\n"
        f"/buy {ex['ticker']} 5 — купить\n"
        f"/sell {ex['ticker']} 5 — продать\n"
        f"/portfolio — мой портфель"
    )


def _ex_markup():
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("🔄 Обновить рынок", callback_data="ex_refresh"),
        InlineKeyboardButton("💼 Портфель", callback_data="ex_portfolio"),
        InlineKeyboardButton("💰 Бонус", callback_data="ex_bonus"),
    )
    return markup


def _ex_replace_market(chat_id: int, user_id: int, exchange_id: int = 1):
    _ex_ensure_trader(user_id)
    conn = _ex_db()
    ex = conn.execute("SELECT id FROM ex_exchanges WHERE id=?", (exchange_id,)).fetchone()
    if not ex:
        exchange_id = 1
    old = conn.execute(
        "SELECT market_message_id,market_chat_id FROM ex_traders WHERE user_id=?",
        (user_id,),
    ).fetchone()
    conn.close()

    if old and old["market_message_id"] and old["market_chat_id"]:
        try:
            bot.delete_message(old["market_chat_id"], old["market_message_id"])
        except Exception:
            pass

    path = _ex_chart(exchange_id)
    with open(path, "rb") as photo:
        msg = bot.send_photo(
            chat_id,
            photo,
            caption=_ex_market_text(exchange_id),
            reply_markup=_ex_markup(),
        )

    conn = _ex_db()
    conn.execute(
        "UPDATE ex_traders SET market_message_id=?,market_chat_id=? WHERE user_id=?",
        (msg.message_id, chat_id, user_id),
    )
    conn.commit()
    conn.close()


def _ex_require_user(message):
    # Биржа не меняет существующую подписочную логику старых команд.
    _ex_ensure_trader(message.from_user.id)
    return True


@bot.message_handler(commands=["exchange", "биржа"])
def handle_exchange(message: Message) -> None:
    if not _ex_require_user(message):
        return
    bot.send_message(
        message.chat.id,
        "🏦 <b>BERLIONS STOCK EXCHANGE</b>\n\n"
        "💰 /bonus — ежедневный бонус 1 000 ₽\n"
        "💵 /balance — баланс\n"
        "📈 /market — открыть рынок и график\n"
        "💼 /portfolio — мои акции\n"
        "🛒 /buy TICKER 5 — купить акции\n"
        "💸 /sell TICKER 5 — продать акции\n"
        "🏭 /create_exchange Название TICKER — создать свою биржу\n"
        "📋 /exchanges — список бирж\n"
        "🏆 /top — рейтинг игроков\n\n"
        "Курс меняется после сделок, а график рынка обновляется автоматически."
    )


@bot.message_handler(commands=["bonus"])
def handle_exchange_bonus(message: Message) -> None:
    user_id = message.from_user.id
    _ex_ensure_trader(user_id)
    today = date.today().isoformat()
    conn = _ex_db()
    row = conn.execute("SELECT balance,last_bonus FROM ex_traders WHERE user_id=?", (user_id,)).fetchone()
    if row["last_bonus"] == today:
        conn.close()
        bot.send_message(message.chat.id, "⏳ Ты уже получил ежедневный бонус сегодня. Возвращайся завтра!")
        return
    new_balance = float(row["balance"]) + EXCHANGE_DAILY_BONUS
    conn.execute("UPDATE ex_traders SET balance=?,last_bonus=? WHERE user_id=?", (new_balance, today, user_id))
    conn.commit()
    conn.close()
    bot.send_message(message.chat.id, f"🎁 <b>Ежедневный бонус!</b>\n\n+{EXCHANGE_DAILY_BONUS:,.0f} ₽\n💰 Баланс: <b>{new_balance:,.2f} ₽</b>")


@bot.message_handler(commands=["balance"])
def handle_exchange_balance(message: Message) -> None:
    row = _ex_ensure_trader(message.from_user.id)
    bot.send_message(message.chat.id, f"💰 Твой баланс: <b>{float(row['balance']):,.2f} ₽</b>\n\n🎁 Ежедневный бонус: /bonus")


@bot.message_handler(commands=["market"])
def handle_exchange_market(message: Message) -> None:
    try:
        _ex_require_user(message)
        _ex_replace_market(message.chat.id, message.from_user.id, 1)
    except Exception as exc:
        logger.exception("/market error: %s", exc)
        bot.send_message(
            message.chat.id,
            "❌ Не удалось открыть рынок.\n\n"
            f"Техническая ошибка: <code>{html.escape(str(exc))}</code>"
        )


@bot.message_handler(commands=["portfolio"])
def handle_exchange_portfolio(message: Message) -> None:
    user_id = message.from_user.id
    _ex_ensure_trader(user_id)
    conn = _ex_db()
    rows = conn.execute("""
        SELECT h.shares,h.avg_price,e.name,e.ticker,e.price
        FROM ex_holdings h JOIN ex_exchanges e ON e.id=h.exchange_id
        WHERE h.user_id=? AND h.shares>0 ORDER BY e.ticker
    """, (user_id,)).fetchall()
    balance = conn.execute("SELECT balance FROM ex_traders WHERE user_id=?", (user_id,)).fetchone()["balance"]
    conn.close()

    if not rows:
        bot.send_message(message.chat.id, f"💼 <b>Портфель пуст</b>\n\n💰 Баланс: <b>{float(balance):,.2f} ₽</b>\n\nПопробуй /buy BERL 5")
        return

    lines = [f"💼 <b>Твой портфель</b>\n💰 Баланс: <b>{float(balance):,.2f} ₽</b>\n"]
    for r in rows:
        value = r["shares"] * r["price"]
        pnl = (r["price"] - r["avg_price"]) * r["shares"]
        lines.append(f"• <b>{r['ticker']}</b> — {r['shares']} шт. × {r['price']:,.2f} ₽ = {value:,.2f} ₽\n  P/L: {pnl:+,.2f} ₽")
    bot.send_message(message.chat.id, "\n".join(lines))


def _ex_parse_trade(message):
    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[2].isdigit():
        bot.send_message(message.chat.id, "❓ Формат: <code>/buy BERL 5</code>")
        return None
    ticker = parts[1].upper()
    shares = int(parts[2])
    if shares <= 0 or shares > EXCHANGE_MAX_TRADE:
        bot.send_message(message.chat.id, f"❌ Количество должно быть от 1 до {EXCHANGE_MAX_TRADE}.")
        return None
    return ticker, shares


@bot.message_handler(commands=["buy"])
def handle_exchange_buy(message: Message) -> None:
    parsed = _ex_parse_trade(message)
    if not parsed:
        return
    ticker, shares = parsed
    user_id = message.from_user.id
    _ex_ensure_trader(user_id)
    conn = _ex_db()
    ex = conn.execute("SELECT * FROM ex_exchanges WHERE ticker=?", (ticker,)).fetchone()
    if not ex:
        conn.close()
        bot.send_message(message.chat.id, "❌ Такой акции нет. Посмотри /exchanges")
        return

    price = float(ex["price"])
    total = price * shares
    trader = conn.execute("SELECT balance FROM ex_traders WHERE user_id=?", (user_id,)).fetchone()
    if float(trader["balance"]) < total:
        conn.close()
        bot.send_message(message.chat.id, f"❌ Недостаточно денег. Нужно <b>{total:,.2f} ₽</b>.")
        return

    holding = conn.execute("SELECT shares,avg_price FROM ex_holdings WHERE user_id=? AND exchange_id=?", (user_id, ex["id"])).fetchone()
    old_shares = int(holding["shares"]) if holding else 0
    old_avg = float(holding["avg_price"]) if holding else 0.0
    new_shares = old_shares + shares
    new_avg = ((old_shares * old_avg) + total) / new_shares

    conn.execute("UPDATE ex_traders SET balance=balance-? WHERE user_id=?", (total, user_id))
    conn.execute(
        "INSERT INTO ex_holdings(user_id,exchange_id,shares,avg_price) VALUES(?,?,?,?) "
        "ON CONFLICT(user_id,exchange_id) DO UPDATE SET shares=excluded.shares,avg_price=excluded.avg_price",
        (user_id, ex["id"], new_shares, new_avg),
    )
    new_price, change = _ex_change_price(conn, ex["id"], min(0.012, shares / 10000.0))
    conn.execute(
        "INSERT INTO ex_transactions(user_id,exchange_id,action,shares,price,total,ts) VALUES(?,?,?,?,?,?,?)",
        (user_id, ex["id"], "BUY", shares, price, total, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()

    bot.send_message(message.chat.id, f"📈 <b>Покупка исполнена</b>\n\n🪙 {shares} × {ticker}\n💵 Цена сделки: {price:,.2f} ₽\n💸 Сумма: {total:,.2f} ₽\n📊 Новый курс: {new_price:,.2f} ₽ ({change:+.2f}%)")
    try:
        _ex_replace_market(message.chat.id, user_id, ex["id"])
    except Exception as exc:
        logger.exception("Exchange chart error: %s", exc)


@bot.message_handler(commands=["sell"])
def handle_exchange_sell(message: Message) -> None:
    parsed = _ex_parse_trade(message)
    if not parsed:
        return
    ticker, shares = parsed
    user_id = message.from_user.id
    _ex_ensure_trader(user_id)
    conn = _ex_db()
    ex = conn.execute("SELECT * FROM ex_exchanges WHERE ticker=?", (ticker,)).fetchone()
    if not ex:
        conn.close()
        bot.send_message(message.chat.id, "❌ Такой акции нет. Посмотри /exchanges")
        return

    holding = conn.execute("SELECT shares,avg_price FROM ex_holdings WHERE user_id=? AND exchange_id=?", (user_id, ex["id"])).fetchone()
    if not holding or int(holding["shares"]) < shares:
        conn.close()
        bot.send_message(message.chat.id, "❌ У тебя недостаточно этих акций.")
        return

    price = float(ex["price"])
    total = price * shares
    remaining = int(holding["shares"]) - shares
    conn.execute("UPDATE ex_traders SET balance=balance+? WHERE user_id=?", (total, user_id))
    if remaining:
        conn.execute("UPDATE ex_holdings SET shares=? WHERE user_id=? AND exchange_id=?", (remaining, user_id, ex["id"]))
    else:
        conn.execute("DELETE FROM ex_holdings WHERE user_id=? AND exchange_id=?", (user_id, ex["id"]))

    new_price, change = _ex_change_price(conn, ex["id"], -min(0.012, shares / 10000.0))
    conn.execute(
        "INSERT INTO ex_transactions(user_id,exchange_id,action,shares,price,total,ts) VALUES(?,?,?,?,?,?,?)",
        (user_id, ex["id"], "SELL", shares, price, total, datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()

    bot.send_message(message.chat.id, f"📉 <b>Продажа исполнена</b>\n\n🪙 {shares} × {ticker}\n💵 Цена сделки: {price:,.2f} ₽\n💰 Получено: {total:,.2f} ₽\n📊 Новый курс: {new_price:,.2f} ₽ ({change:+.2f}%)")
    try:
        _ex_replace_market(message.chat.id, user_id, ex["id"])
    except Exception as exc:
        logger.exception("Exchange chart error: %s", exc)


@bot.message_handler(commands=["create_exchange"])
def handle_create_exchange(message: Message) -> None:
    parts = (message.text or "").split()
    if len(parts) < 3:
        bot.send_message(message.chat.id, "❓ Формат: <code>/create_exchange MyCompany MYC</code>")
        return
    ticker = parts[-1].upper()
    name = " ".join(parts[1:-1]).strip()
    if not name or not ticker.isalnum() or not 2 <= len(ticker) <= 8:
        bot.send_message(message.chat.id, "❌ Название или тикер указаны неправильно. Тикер: 2–8 латинских символов/цифр.")
        return

    conn = _ex_db()
    exists = conn.execute("SELECT id FROM ex_exchanges WHERE ticker=?", (ticker,)).fetchone()
    if exists:
        conn.close()
        bot.send_message(message.chat.id, "❌ Такой тикер уже занят.")
        return
    cur = conn.execute(
        "INSERT INTO ex_exchanges(owner_id,name,ticker,price,created_at) VALUES(?,?,?,?,?)",
        (message.from_user.id, name, ticker, EXCHANGE_DEFAULT_PRICE, datetime.now().isoformat(timespec="seconds")),
    )
    exchange_id = cur.lastrowid
    conn.execute("INSERT INTO ex_history(exchange_id,price,ts) VALUES(?,?,?)", (exchange_id, EXCHANGE_DEFAULT_PRICE, datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()
    bot.send_message(message.chat.id, f"🏭 <b>Биржа создана!</b>\n\n🏦 {html.escape(name)}\n📈 Тикер: <code>{ticker}</code>\n💰 Стартовая цена: <b>100 ₽</b>\n\nТеперь игроки могут покупать и продавать <code>{ticker}</code> через /buy и /sell.")


@bot.message_handler(commands=["exchanges"])
def handle_exchanges(message: Message) -> None:
    conn = _ex_db()
    rows = conn.execute("SELECT * FROM ex_exchanges ORDER BY id DESC LIMIT 30").fetchall()
    conn.close()
    lines = ["🏦 <b>Все биржи</b>\n"]
    for r in rows:
        lines.append(f"• <code>{r['ticker']}</code> — {html.escape(r['name'])}: <b>{r['price']:,.2f} ₽</b>\n  Владелец: <code>{r['owner_id']}</code>")
    bot.send_message(message.chat.id, "\n".join(lines))


@bot.message_handler(commands=["topmoney", "toptraders"])
def handle_exchange_top(message: Message) -> None:
    conn = _ex_db()
    traders = conn.execute("SELECT user_id,balance FROM ex_traders").fetchall()
    result = []
    for trader in traders:
        holdings = conn.execute(
            "SELECT h.shares,e.price FROM ex_holdings h JOIN ex_exchanges e ON e.id=h.exchange_id WHERE h.user_id=?",
            (trader["user_id"],),
        ).fetchall()
        total = float(trader["balance"]) + sum(int(h["shares"]) * float(h["price"]) for h in holdings)
        result.append((total, trader["user_id"]))
    conn.close()
    result.sort(reverse=True)
    lines = ["🏆 <b>TOP TRADERS</b>\n"]
    for i, (total, uid) in enumerate(result[:20], 1):
        lines.append(f"{i}. <code>{uid}</code> — <b>{total:,.2f} ₽</b>")
    bot.send_message(message.chat.id, "\n".join(lines) if len(lines) > 1 else "🏆 Пока рейтинг пуст.")


# ВАЖНО: этот callback зарегистрирован ДО старого общего callback-хендлера.
@bot.callback_query_handler(func=lambda call: call.data.startswith("ex_"))
def handle_exchange_callback(call: CallbackQuery) -> None:
    user_id = call.from_user.id
    chat_id = call.message.chat.id if call.message else user_id
    try:
        if call.data == "ex_refresh":
            bot.answer_callback_query(call.id, "🔄 Обновляю рынок...")
            _ex_replace_market(chat_id, user_id, 1)
        elif call.data == "ex_portfolio":
            bot.answer_callback_query(call.id)
            handle_exchange_portfolio(call.message)
        elif call.data == "ex_bonus":
            bot.answer_callback_query(call.id)
            handle_exchange_bonus(call.message)
    except Exception as exc:
        logger.exception("Exchange callback error: %s", exc)
        try:
            bot.answer_callback_query(call.id, "❌ Ошибка биржи", show_alert=True)
        except Exception:
            pass

# ── END BERLIONS EXCHANGE SYSTEM ───────────────────────────────────────────────


# ── Dice game logic ───────────────────────────────────────────────────────────

@bot.message_handler(commands=["dice"])
def handle_dice_command(message: Message) -> None:
    user_id = message.from_user.id
    now = time.time()
    last_roll = _dice_cooldown.get(user_id, 0)
    
    # КД 60 секунд
    if now - last_roll < 60:
        bot.send_message(
            message.chat.id,
            f"⏳ Подожди {int(60 - (now - last_roll))} секунд перед следующим броском!"
        )
        return
    
    if not _require_subscription(message.chat.id, user_id, "dice"):
        return
    
    _play_dice(message.chat.id, user_id)

def _play_dice(chat_id: int, user_id: int) -> None:
    """Запускает бросок кубика и применяет cooldown."""
    now = time.time()
    last_roll = _dice_cooldown.get(user_id, 0)

    # КД проверяется здесь, чтобы команда и кнопка использовали одну логику.
    if now - last_roll < 60:
        bot.send_message(
            chat_id,
            f"⏳ Подожди {int(60 - (now - last_roll))} секунд перед следующим броском!"
        )
        return

    # Ставим cooldown только непосредственно перед реальным броском.
    _dice_cooldown[user_id] = now

    try:
        dice_msg = bot.send_dice(chat_id, emoji="🎲")
        value = dice_msg.dice.value
    except Exception:
        # Если Telegram не принял бросок, не наказываем пользователя cooldown'ом.
        _dice_cooldown.pop(user_id, None)
        logger.exception("Не удалось отправить кубик в чат %s", chat_id)
        bot.send_message(chat_id, "❌ Не удалось бросить кубик. Попробуй ещё раз.")
        return

    if value < 3:
        result = (
            f"😢 <b>Выпало {value}</b> — ты проиграл!\n\n"
            f"Меньше 3 — не повезло. Попробуй ещё раз!"
        )
    elif value > 5:
        result = (
            f"🏆 <b>Выпало {value}</b> — ты победил!\n\n"
            f"Максимум! Ты настоящий везунчик 🎉"
        )
    else:
        result = (
            f"😐 <b>Выпало {value}</b> — ничья!\n\n"
            f"Не выиграл, но и не проиграл. Попробуй снова?"
        )

    markup = InlineKeyboardMarkup().add(
        InlineKeyboardButton("🎲 Играть ещё", callback_data="play_dice")
    )
    bot.send_message(chat_id, result, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: bool(call.data and call.data.startswith("marry_")))
def handle_marriage_callback_direct(call: CallbackQuery) -> None:
    """Dedicated marriage callback handler. Registered before the catch-all handler."""
    logger.info("Marriage button pressed: data=%r user=%s", call.data, call.from_user.id)
    handle_social_marriage_callback(call)


@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call: CallbackQuery) -> None:
    user_id = call.from_user.id
    chat_id = call.message.chat.id

    logger.info("Клик от пользователя: %s (@%s), ID: %s", call.from_user.first_name, call.from_user.username, user_id)

    if call.data.startswith("verify:"):
        context = call.data[len("verify:"):]
        if not _is_subscribed(user_id):
            bot.answer_callback_query(call.id, "❌ Ты ещё не подписался на оба ресурса! Подпишись и попробуй снова.", show_alert=True)
            return
        bot.answer_callback_query(call.id, "✅ Подписка подтверждена!")
        try: bot.delete_message(chat_id, call.message.message_id)
        except Exception: pass
        if context == "dice": _play_dice(chat_id, user_id)
        elif context.startswith("key:"):
            key = context[len("key:"):]
            row = database.get_link(key)
            if row: _deliver_link(chat_id, row)
            else: bot.send_message(chat_id, "❌ Ссылка не найдена.")
        return

    if call.data.startswith("search_pick:"):
        key = call.data[len("search_pick:"):]
        bot.answer_callback_query(call.id)
        try: bot.delete_message(chat_id, call.message.message_id)
        except Exception as e: logger.warning("Failed to delete search message: %s", e)
        row = database.get_link(key)
        if row: _deliver_link(chat_id, row)
        else: bot.send_message(chat_id, "❌ Контент не найден.")
        return

    if call.data == "play_dice":
        bot.answer_callback_query(call.id)
        
        # Проверка КД для кнопки
        now = time.time()
        last_roll = _dice_cooldown.get(user_id, 0)
        if now - last_roll < 60:
            bot.send_message(
                chat_id,
                f"⏳ Подожди {int(60 - (now - last_roll))} секунд перед следующим броском!"
            )
            return
        
        if not _require_subscription(chat_id, user_id, "dice"):
            return
        
        _play_dice(chat_id, user_id)
        return
  
# ── /stop — завершение сбора файлов для админов ───────────────────────────────

@bot.message_handler(commands=["stop"])
def handle_stop(message: Message) -> None:
    if not _is_admin(message.from_user.id): return
    state = _pending.get(message.from_user.id)
    if state is None or state["step"] not in ("collecting_files", "edit_collecting_files"):
        bot.send_message(message.chat.id, "ℹ️ /stop используется только во время добавления файлов.")
        return

    files: list[tuple[str, str]] = state.get("files", [])
    step = state["step"]

    if step == "collecting_files":
        if not files:
            bot.send_message(message.chat.id, "⚠️ Ты не добавил ни одного файла. Отправь хотя бы один файл или URL.")
            return
        content_text = state["content_text"]
        del _pending[message.from_user.id]
        try:
            key = database.create_link(content_text=content_text, files=files)
            bot.send_message(message.chat.id, f"✅ <b>Ссылка создана!</b> ({len(files)} файл(ов))\n\n🔑 Ключ: <code>{key}</code>\n\n📎 Вставь эту ссылку в канал:\n<code>{_deep_link(key)}</code>", disable_web_page_preview=True)
            logger.info("Admin created key %r with %d file(s)", key, len(files))
        except Exception as exc:
            logger.exception("create_link failed: %s", exc)
            bot.send_message(message.chat.id, f"❌ Ошибка при сохранении: {exc}")

    elif step == "edit_collecting_files":
        key = state["key"]
        new_text = state.get("new_text")
        if not files and new_text is None:
            del _pending[message.from_user.id]
            bot.send_message(message.chat.id, f"ℹ️ Редактирование <code>{key}</code> отменено.")
            return
        del _pending[message.from_user.id]
        try:
            database.update_link(key, content_text=new_text, files=files if files else None, clear_url=bool(files))
            bot.send_message(message.chat.id, f"✅ Ключ <code>{key}</code> обновлён" + (f" — {len(files)} файл(ов) сохранено." if files else " (текст обновлён)."))
            logger.info("Admin edited key %r → %d file(s)", key, len(files))
        except Exception as exc:
            bot.send_message(message.chat.id, f"❌ Ошибка: {exc}")

# ── Admin: /add ───────────────────────────────────────────────────────────────

@bot.message_handler(commands=["add"])
def handle_add(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "⛔ У тебя нет доступа к этой команде.")
        return
    _pending[message.from_user.id] = {"step": "awaiting_text"}
    bot.send_message(message.chat.id, "📝 <b>Шаг 1/2</b> — Отправь описание / текст, который увидит пользователь:")

# ── Admin: /list ──────────────────────────────────────────────────────────────

@bot.message_handler(commands=["list"])
def handle_list(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "⛔ У тебя нет доступа к этой команде.")
        return
    rows = database.list_links()
    if not rows:
        bot.send_message(message.chat.id, "📭 Пока не создано ни одной ссылки.")
        return
    lines = ["<b>📋 Все созданные ссылки:</b>\n"]
    for row in rows:
        created = str(row["created_at"])[:16]
        files = database.get_link_files(row["key"])
        content_type = f"📎 {len(files)} файл(ов)" if files else ("🔗 " + row["target_url"] if row["target_url"] else "📄 текст")
        lines.append(f"🔑 <code>{row['key']}</code>  ({created})  [{content_type}]\n   📄 {row['content_text'][:60]}{'…' if len(row['content_text']) > 60 else ''}\n   👉 <a href='{_deep_link(row['key'])}'>Ссылка для канала</a>\n")
    bot.send_message(message.chat.id, "\n".join(lines), disable_web_page_preview=True)

# ── Admin: /delete ────────────────────────────────────────────────────────────

@bot.message_handler(commands=["delete"])
def handle_delete(message: Message) -> None:
    if not _is_admin(message.from_user.id): return
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        bot.send_message(message.chat.id, "❓ Укажи ключ: <code>/delete ключ</code>\nКлючи можно посмотреть командой /list")
        return
    key = parts[1].strip()
    row = database.get_link(key)
    if row is None:
        bot.send_message(message.chat.id, f"❌ Ключ <code>{key}</code> не найден.")
        return
    database.delete_link(key)
    bot.send_message(message.chat.id, f"🗑 Ссылка <code>{key}</code> удалена.\n📄 Текст был: {row['content_text'][:80]}")
    logger.info("Admin deleted key %r", key)

# ── Admin: /edit ──────────────────────────────────────────────────────────────

@bot.message_handler(commands=["edit"])
def handle_edit(message: Message) -> None:
    if not _is_admin(message.from_user.id): return
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        bot.send_message(message.chat.id, "❓ Укажи ключ: <code>/edit ключ</code>\nКлючи можно посмотреть командой /list")
        return
    key = parts[1].strip()
    row = database.get_link(key)
    if row is None:
        bot.send_message(message.chat.id, f"❌ Ключ <code>{key}</code> не найден.")
        return
    files = database.get_link_files(key)
    content_type = f"📎 {len(files)} файл(ов)" if files else ("🔗 " + row["target_url"] if row["target_url"] else "📄 только текст")
    bot.send_message(
        message.chat.id,
        f"✏️ <b>Редактируем</b> <code>{key}</code>\n\n📄 Текст: {row['content_text']}\nКонтент: {content_type}\n\n<b>Шаг 1/2</b> — Отправь новый текст описания.\nЧтобы оставить текущий — отправь <code>-</code>",
    )
    _pending[message.from_user.id] = {"step": "edit_text", "key": key}

# ── Admin conversation (логика диалога для /add и /edit) ───────────────────────

@bot.message_handler(
    func=lambda m: (m.from_user.id in _pending and not (m.text and m.text.startswith("/"))),
    content_types=[ "text", "document", "photo", "video", "audio", "voice", "animation", ],
)
def handle_conversation(message: Message) -> None:
    state = _pending.get(message.from_user.id)
    if state is None: return
    step = state["step"]

    if step == "awaiting_text":
        if message.content_type != "text":
            bot.send_message(message.chat.id, "⚠️ На этом шаге нужен текст:")
            return
        state["content_text"] = message.text.strip()
        state["step"] = "collecting_files"
        state["files"] = []
        bot.send_message(message.chat.id, "📎 <b>Шаг 2/2</b> — Отправь файл(ы) (фото, video, документ…).\n\n• Добавляй по одному — бот подтвердит каждый.\n• Когда всё загружено — напиши /stop для сохранения.\n• Или отправь текстом <b>ссылку</b> (http://…) вместо файла.")

    elif step == "collecting_files":
        file_id, file_type = _extract_file(message)
        if file_id:
            state["files"].append((file_id, file_type))
            _files_added_reply(message.chat.id, len(state["files"]))
        elif message.content_type == "text":
            url = message.text.strip()
            if not (url.startswith("http://") or url.startswith("https://")):
                bot.send_message(message.chat.id, "⚠️ Ссылка должна начинаться с <code>http://</code> или <code>https://</code>. Попробуй ещё раз или отправь файл.")
                return
            content_text = state["content_text"]
            del _pending[message.from_user.id]
            try:
                key = database.create_link(content_text=content_text, target_url=url)
                bot.send_message(message.chat.id, f"✅ <b>Ссылка создана!</b>\n\n🔑 Ключ: <code>{key}</code>\n\n📎 Вставь эту ссылку в канал:\n<code>{_deep_link(key)}</code>", disable_web_page_preview=True)
                logger.info("Admin created url-link key %r", key)
            except Exception as exc:
                logger.exception("create_link failed: %s", exc)
                bot.send_message(message.chat.id, f"❌ Ошибка при сохранении: {exc}")
        else: bot.send_message(message.chat.id, "⚠️ Отправь файл, ссылку, или /stop чтобы сохранить добавленное.")

    elif step == "edit_text":
        if message.content_type != "text":
            bot.send_message(message.chat.id, "⚠️ На этом шаге нужен текст. Отправь новый текст или <code>-</code>:")
            return
        text = message.text.strip()
        state["new_text"] = None if text == "-" else text
        state["step"] = "edit_collecting_files"
        state["files"] = []
        bot.send_message(message.chat.id, "📎 <b>Шаг 2/2</b> — Отправь новые файлы (старые заменятся).\n\n• Отправляй по одному — бот подтвердит каждый.\n• Когда всё готово — напиши /stop для сохранения.\n• Или отправь текстом <b>ссылку</b> (http://…) вместо файлов.\n• Чтобы оставить текущий контент — напиши /stop сразу.")

    elif step == "edit_collecting_files":
        file_id, file_type = _extract_file(message)
        if file_id:
            state["files"].append((file_id, file_type))
            _files_added_reply(message.chat.id, len(state["files"]))
        elif message.content_type == "text":
            url = message.text.strip()
            if url == "-":
                key = state["key"]
                new_text = state.get("new_text")
                del _pending[message.from_user.id]
                if new_text is not None: database.update_link(key, content_text=new_text)
                bot.send_message(message.chat.id, f"✅ Ключ <code>{key}</code> обновлён (контент не изменён).")
                logger.info("Admin edited key %r (text only)", key)
                return
            if not (url.startswith("http://") or url.startswith("https://")):
                bot.send_message(message.chat.id, "⚠️ Ссылка должна начинаться с <code>http://</code> или <code>https://</code>. Попробуй ещё раз, или отправь файл, или <code>-</code> чтобы оставить текущее.")
                return
            key = state["key"]
            new_text = state.get("new_text")
            del _pending[message.from_user.id]
            try:
                database.update_link(key, content_text=new_text, target_url=url, clear_files=True)
                bot.send_message(message.chat.id, f"✅ Ключ <code>{key}</code> обновлён — новая ссылка сохранена.")
                logger.info("Admin edited key %r → new url", key)
            except Exception as exc: bot.send_message(message.chat.id, f"❌ Ошибка: {exc}")
        else: bot.send_message(message.chat.id, "⚠️ Отправь файл, ссылку, <code>-</code> чтобы оставить текущее, или /stop чтобы сохранить уже добавленные файлы.")

# ── Chat Admin System (Iris-like) - ПЛЕЙСХОЛДЕРЫ ───────────────────────────────
# Эти функции требуют соответствующей реализации в database.py для работы с рангами.

RANK_NAMES = {1: "Младший модератор", 2: "Модератор", 3: "Старший модератор", 4: "Администратор", 5: "Создатель"}

# Функция _get_user_rank в текущем виде не взаимодействует с реальной БД для рангов.
# Она использует `hasattr(database, "get_chat_admins")` как проверку, но `database.py`
# не содержит этих функций. Это плейсхолдер.
def _get_user_rank(chat_id: int, user_id: int) -> int:
    """Возвращает ранг пользователя в чате. 0, если не админ."""
    # ВНИМАНИЕ: Эта часть требует, чтобы 'database.py' имел функции для управления админами чата.
    # Если их нет, эта функция всегда будет возвращать 0.
    if hasattr(database, "get_chat_admins"):
        try:
            admins = database.get_chat_admins(chat_id)
            for row in admins:
                if isinstance(row, (list, tuple)):
                    uid, rank = row[0], row[1]
                else:
                    uid, rank = row["user_id"], row["rank"]
                if int(uid) == int(user_id):
                    return int(rank)
        except Exception as e:
            logger.debug(f"Ошибка при получении ранга из БД: {e}")
    return 0

def _can_manage(chat_id: int, actor_id: int, target_id: int, required_rank: int) -> bool:
    """Проверяет, может ли actor_id управлять target_id."""
    if _is_admin(actor_id): return True # Глобальный админ может всё
    if _is_admin(target_id): return False # Нельзя управлять глобальным админом
    actor_rank = _get_user_rank(chat_id, actor_id)
    target_rank = _get_user_rank(chat_id, target_id)
    return actor_rank >= required_rank and actor_rank > target_rank

def _parse_target(message: Message) -> int | None:
    """Извлекает ID целевого пользователя из ответа или текста сообщения."""
    if message.reply_to_message: return message.reply_to_message.from_user.id
    parts = (message.text or "").split()
    if len(parts) > 1 and parts[1].isdigit(): return int(parts[1])
    return None

@bot.message_handler(commands=["rank", "ranks"])
def handle_rank(message: Message) -> None:
    if not _is_admin(message.from_user.id) and _get_user_rank(message.chat.id, message.from_user.id) < 3:
        bot.send_message(message.chat.id, "⛔ Недостаточно прав.")
        return
    chat_id = message.chat.id
    admins = [] # Здесь должен быть вызов database.get_chat_admins(chat_id)
    if not hasattr(database, "get_chat_admins"):
        bot.send_message(chat_id, "⚠️ Система рангов не настроена в database.py.")
        return
    
    try:
        admins = database.get_chat_admins(chat_id)
    except Exception as e:
        bot.send_message(chat_id, f"❌ Ошибка получения админов: {e}")
        return

    if not admins:
        bot.send_message(chat_id, "📋 В этом чате нет назначенных админов.")
        return
    lines = ["<b>👮 Админы чата:</b>\n"]
    for row in admins:
        if isinstance(row, (list, tuple)):
            uid, rank = row[0], row[1]
        else:
            uid, rank = row["user_id"], row["rank"]
        lines.append(f"• User_{uid} — {RANK_NAMES.get(rank, f'Ранг {rank}')}")
    bot.send_message(chat_id, "\n".join(lines))

@bot.message_handler(commands=["setrank"])
def handle_setrank(message: Message) -> None:
    if not _is_admin(message.from_user.id) and _get_user_rank(message.chat.id, message.from_user.id) < 4:
        bot.send_message(message.chat.id, "⛔ Только от 4 ранга или глобальный админ.")
        return
    target_id = _parse_target(message)
    if not target_id: bot.send_message(message.chat.id, "❓ /setrank ID или ответь на сообщение"); return
    parts = message.text.strip().split()
    if len(parts) < 3 or not parts[2].isdigit(): bot.send_message(message.chat.id, "❓ /setrank <ID> <1-5>"); return
    new_rank = int(parts[2])
    if not 1 <= new_rank <= 5: bot.send_message(message.chat.id, "❌ Ранг от 1 до 5."); return
    if not _can_manage(message.chat.id, message.from_user.id, target_id, 4): bot.send_message(message.chat.id, "⛔ Недостаточно прав."); return
    # Здесь должен быть вызов database.set_admin_rank
    if hasattr(database, "set_admin_rank"):
        database.set_admin_rank(message.chat.id, target_id, new_rank)
        bot.send_message(message.chat.id, f"✅ Пользователь {target_id} получил ранг {new_rank} — {RANK_NAMES.get(new_rank)}")
    else:
        bot.send_message(message.chat.id, "⚠️ Функция set_admin_rank не найдена в database.py.")

@bot.message_handler(commands=["demote"])
def handle_demote(message: Message) -> None:
    if not _is_admin(message.from_user.id) and _get_user_rank(message.chat.id, message.from_user.id) < 4:
        bot.send_message(message.chat.id, "⛔ Недостаточно прав.")
        return
    target_id = _parse_target(message)
    if not target_id: bot.send_message(message.chat.id, "❓ Ответь на сообщение."); return
    if not _can_manage(message.chat.id, message.from_user.id, target_id, 4): bot.send_message(message.chat.id, "⛔ Не можешь понизить."); return
    # Здесь должен быть вызов database.remove_admin
    if hasattr(database, "remove_admin"):
        database.remove_admin(message.chat.id, target_id)
        bot.send_message(message.chat.id, f"✅ Пользователь {target_id} разжалован.")
    else:
        bot.send_message(message.chat.id, "⚠️ Функция remove_admin не найдена в database.py.")


@bot.message_handler(commands=["kick"])
def handle_kick(message: Message) -> None:
    if _get_user_rank(message.chat.id, message.from_user.id) < 2: bot.send_message(message.chat.id, "⛔ Требуется минимум 2 ранг."); return
    target_id = _parse_target(message)
    if not target_id: bot.send_message(message.chat.id, "❓ /kick (ответ)"); return
    if not _can_manage(message.chat.id, message.from_user.id, target_id, 2): bot.send_message(message.chat.id, "⛔ Недостаточно прав."); return
    try:
        bot.kick_chat_member(message.chat.id, target_id)
        bot.unban_chat_member(message.chat.id, target_id)
        bot.send_message(message.chat.id, f"👢 Пользователь {target_id} кикнут.")
    except Exception as e: bot.send_message(message.chat.id, f"❌ Ошибка: {e}")

@bot.message_handler(commands=["warn"])
def handle_warn(message: Message) -> None:
    if _get_user_rank(message.chat.id, message.from_user.id) < 2: bot.send_message(message.chat.id, "⛔ Требуется минимум 2 ранг."); return
    target_id = _parse_target(message)
    if not target_id: bot.send_message(message.chat.id, "❓ /warn (ответ) [причина]"); return
    if not _can_manage(message.chat.id, message.from_user.id, target_id, 2): bot.send_message(message.chat.id, "⛔ Недостаточно прав."); return
    reason = " ".join(message.text.split()[2:]) if len(message.text.split()) > 2 else "Без причины"
    # Здесь должен быть вызов database.add_warning
    if hasattr(database, "add_warning"):
        database.add_warning(message.chat.id, target_id, message.from_user.id, reason)
        warnings = len(database.get_user_warnings(message.chat.id, target_id)) # Здесь должен быть вызов database.get_user_warnings
        bot.send_message(message.chat.id, f"⚠️ Предупреждение {target_id} ({warnings}/3)\nПричина: {reason}")
        if warnings >= 3:
            try: bot.ban_chat_member(message.chat.id, target_id); bot.send_message(message.chat.id, "🚫 Автобан после 3 варнов.")
            except: pass
    else: bot.send_message(message.chat.id, "⚠️ Функции предупреждений не найдены в database.py.")


# ── BERLIONS SOCIAL SYSTEM ────────────────────────────────────────────────────
# Профили, активность, награды, браки, кланы и безопасные ролевые команды.

import re
from datetime import timedelta

SOCIAL_DB = "social.db"
SOCIAL_GRAPH_DIR = "social_graphs"


def _social_db():
    conn = sqlite3.connect(SOCIAL_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _social_init_db():
    conn = _social_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS social_users (
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        PRIMARY KEY (chat_id, user_id)
    );

    CREATE INDEX IF NOT EXISTS idx_social_users_username
        ON social_users(chat_id, username);

    CREATE TABLE IF NOT EXISTS social_activity (
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        day TEXT NOT NULL,
        messages INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (chat_id, user_id, day)
    );

    CREATE TABLE IF NOT EXISTS social_awards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        from_user INTEGER NOT NULL,
        to_user INTEGER NOT NULL,
        reason TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS social_marriage_proposals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        from_user INTEGER NOT NULL,
        to_user INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
    );

    CREATE TABLE IF NOT EXISTS social_marriages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        user1 INTEGER NOT NULL,
        user2 INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1
    );

    CREATE INDEX IF NOT EXISTS idx_social_marriages_chat
        ON social_marriages(chat_id, active);

    CREATE TABLE IF NOT EXISTS social_clans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        name_key TEXT NOT NULL,
        owner_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(chat_id, name_key)
    );

    CREATE TABLE IF NOT EXISTS social_clan_members (
        chat_id INTEGER NOT NULL,
        clan_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        joined_at TEXT NOT NULL,
        PRIMARY KEY(chat_id, user_id)
    );
    """)
    conn.commit()
    conn.close()


_social_init_db()


def _social_now():
    return datetime.now().replace(microsecond=0)


def _social_user_label(chat_id: int, user_id: int) -> str:
    conn = _social_db()
    row = conn.execute(
        "SELECT username,first_name,last_name FROM social_users WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    ).fetchone()
    conn.close()
    if row:
        if row["username"]:
            return "@" + html.escape(row["username"])
        name = " ".join(x for x in (row["first_name"], row["last_name"]) if x).strip()
        if name:
            return html.escape(name)
    return f"<code>{user_id}</code>"


def _social_register(message: Message) -> None:
    if not message.from_user or message.chat.type == "private":
        return
    now = _social_now().isoformat(sep=" ")
    u = message.from_user
    username = (u.username or "").lower() or None
    first_name = u.first_name or ""
    last_name = u.last_name or ""
    conn = _social_db()
    conn.execute("""
        INSERT INTO social_users(chat_id,user_id,username,first_name,last_name,first_seen,last_seen)
        VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(chat_id,user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_name=excluded.last_name,
            last_seen=excluded.last_seen
    """, (message.chat.id, u.id, username, first_name, last_name, now, now))
    day = now[:10]
    conn.execute("""
        INSERT INTO social_activity(chat_id,user_id,day,messages)
        VALUES(?,?,?,1)
        ON CONFLICT(chat_id,user_id,day) DO UPDATE SET messages=messages+1
    """, (message.chat.id, u.id, day))
    conn.commit()
    conn.close()


def _social_target(message: Message, arg_text: str = "") -> int | None:
    # Ответ на сообщение всегда имеет приоритет: это самый надёжный способ
    # определить пользователя в Telegram.
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id
    parts = (arg_text or "").split()
    if not parts:
        return None
    token = parts[0].strip().rstrip(",.!?")
    if token.isdigit():
        return int(token)
    if token.startswith("@"):
        username = token[1:].lower()
        conn = _social_db()
        row = conn.execute(
            "SELECT user_id FROM social_users WHERE chat_id=? AND username=? ORDER BY last_seen DESC LIMIT 1",
            (message.chat.id, username),
        ).fetchone()
        conn.close()
        return int(row["user_id"]) if row else None
    return None


def _social_name_from_message(message: Message) -> str:
    u = message.from_user
    if u.username:
        return "@" + u.username
    return u.first_name or str(u.id)


def _social_period_start(period: str):
    now = _social_now()
    if period == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        base = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return base - timedelta(days=base.weekday())
    if period == "month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return datetime.min


def _social_activity_for_user(chat_id: int, user_id: int, period: str = "all") -> int:
    conn = _social_db()
    if period == "all":
        row = conn.execute("SELECT COALESCE(SUM(messages),0) n FROM social_activity WHERE chat_id=? AND user_id=?", (chat_id, user_id)).fetchone()
    else:
        start = _social_period_start(period).date().isoformat()
        row = conn.execute("SELECT COALESCE(SUM(messages),0) n FROM social_activity WHERE chat_id=? AND user_id=? AND day>=?", (chat_id, user_id, start)).fetchone()
    conn.close()
    return int(row["n"] or 0)


def _social_top_rows(chat_id: int, period: str, limit: int = 10):
    conn = _social_db()
    if period == "all":
        rows = conn.execute("""
            SELECT u.user_id,u.username,u.first_name,u.last_name,COALESCE(SUM(a.messages),0) messages
            FROM social_users u LEFT JOIN social_activity a
              ON a.chat_id=u.chat_id AND a.user_id=u.user_id
            WHERE u.chat_id=?
            GROUP BY u.user_id
            ORDER BY messages DESC, u.last_seen ASC
            LIMIT ?
        """, (chat_id, limit)).fetchall()
    else:
        start = _social_period_start(period).date().isoformat()
        rows = conn.execute("""
            SELECT u.user_id,u.username,u.first_name,u.last_name,COALESCE(SUM(a.messages),0) messages
            FROM social_users u LEFT JOIN social_activity a
              ON a.chat_id=u.chat_id AND a.user_id=u.user_id AND a.day>=?
            WHERE u.chat_id=?
            GROUP BY u.user_id
            HAVING messages > 0
            ORDER BY messages DESC, u.last_seen ASC
            LIMIT ?
        """, (start, chat_id, limit)).fetchall()
    conn.close()
    return rows


# Мини-шрифт 5x7 для PNG. Только ASCII, чтобы не зависеть от системных шрифтов.
_SOCIAL_FONT = {
    "A":"01110 10001 10001 11111 10001 10001 10001", "B":"11110 10001 10001 11110 10001 10001 11110",
    "C":"01111 10000 10000 10000 10000 10000 01111", "D":"11110 10001 10001 10001 10001 10001 11110",
    "E":"11111 10000 10000 11110 10000 10000 11111", "F":"11111 10000 10000 11110 10000 10000 10000",
    "G":"01111 10000 10000 10111 10001 10001 01111", "H":"10001 10001 10001 11111 10001 10001 10001",
    "I":"11111 00100 00100 00100 00100 00100 11111", "J":"00111 00010 00010 00010 10010 10010 01100",
    "K":"10001 10010 10100 11000 10100 10010 10001", "L":"10000 10000 10000 10000 10000 10000 11111",
    "M":"10001 11011 10101 10101 10001 10001 10001", "N":"10001 11001 10101 10011 10001 10001 10001",
    "O":"01110 10001 10001 10001 10001 10001 01110", "P":"11110 10001 10001 11110 10000 10000 10000",
    "Q":"01110 10001 10001 10001 10101 10010 01101", "R":"11110 10001 10001 11110 10100 10010 10001",
    "S":"01111 10000 10000 01110 00001 00001 11110", "T":"11111 00100 00100 00100 00100 00100 00100",
    "U":"10001 10001 10001 10001 10001 10001 01110", "V":"10001 10001 10001 10001 10001 01010 00100",
    "W":"10001 10001 10001 10101 10101 11011 10001", "X":"10001 10001 01010 00100 01010 10001 10001",
    "Y":"10001 10001 01010 00100 00100 00100 00100", "Z":"11111 00001 00010 00100 01000 10000 11111",
    "0":"01110 10001 10011 10101 11001 10001 01110", "1":"00100 01100 00100 00100 00100 00100 01110",
    "2":"01110 10001 00001 00010 00100 01000 11111", "3":"11110 00001 00001 01110 00001 00001 11110",
    "4":"00010 00110 01010 10010 11111 00010 00010", "5":"11111 10000 10000 11110 00001 00001 11110",
    "6":"01110 10000 10000 11110 10001 10001 01110", "7":"11111 00001 00010 00100 01000 01000 01000",
    "8":"01110 10001 10001 01110 10001 10001 01110", "9":"01110 10001 10001 01111 00001 00001 01110",
    ":":"00000 00100 00100 00000 00100 00100 00000", "-":"00000 00000 00000 11111 00000 00000 00000",
    "%":"11001 11010 00010 00100 01000 01011 10011", " ":"00000 00000 00000 00000 00000 00000 00000"
}


def _social_profile_png(path: str, name: str, day: int, week: int, month: int, total: int):
    import os
    os.makedirs(SOCIAL_GRAPH_DIR, exist_ok=True)
    width, height = 1200, 760
    bg=(7,8,12); card=(18,18,25); card2=(24,24,33); grid=(47,48,60)
    white=(242,242,247); muted=(160,163,175); accent=(184,130,255); accent2=(113,76,170)
    pixels=bytearray(bg*(width*height))
    def rect(x1,y1,x2,y2,color):
        x1,x2=max(0,int(x1)),min(width-1,int(x2)); y1,y2=max(0,int(y1)),min(height-1,int(y2))
        if x1>x2 or y1>y2:return
        row=bytes(color)*(x2-x1+1)
        for yy in range(y1,y2+1):
            i=(yy*width+x1)*3; pixels[i:i+len(row)]=row
    def draw_text(x,y,s,scale=4,color=white):
        x0=x
        for ch in str(s).upper()[:28]:
            glyph=_SOCIAL_FONT.get(ch,_SOCIAL_FONT[" "]).split()
            for gy,row in enumerate(glyph):
                for gx,bit in enumerate(row):
                    if bit=="1": rect(x+gx*scale,y+gy*scale,x+(gx+1)*scale-1,y+(gy+1)*scale-1,color)
            x+=6*scale
            if x>width-100: x=x0; y+=9*scale
    def line(x1,y1,x2,y2,color,thickness=4):
        steps=max(abs(int(x2-x1)),abs(int(y2-y1)),1)
        for i in range(steps+1):
            t=i/steps; x=x1+(x2-x1)*t; y=y1+(y2-y1)*t
            rect(x-thickness//2,y-thickness//2,x+thickness//2,y+thickness//2,color)
    # dark Iris-like card
    rect(28,28,width-28,height-28,card)
    rect(55,55,width-55,170,card2)
    # avatar placeholder / initials
    rect(78,78,170,150,accent2)
    initials=''.join([p[0] for p in str(name).replace('@',' ').split() if p])[:2] or 'U'
    draw_text(91,92,initials,4,white)
    draw_text(205,78,str(name),5,white)
    draw_text(205,125,"BERLIONS PROFILE",3,muted)
    # stats cards
    stats=[("СЕГОДНЯ",day),("НЕДЕЛЯ",week),("МЕСЯЦ",month),("ВСЕГО",total)]
    for i,(label,val) in enumerate(stats):
        x=70+(i%2)*535; y=205+(i//2)*125
        rect(x,y,x+495,y+100,card2)
        draw_text(x+24,y+20,label,3,muted)
        draw_text(x+24,y+54,str(val),5,accent if i<3 else white)
    # activity chart
    rect(70,470,width-70,700,card2)
    draw_text(95,492,"АКТИВНОСТЬ",4,white)
    vals=[day,week,month]; labels=["DAY","WEEK","MONTH"]; maxv=max(1,*vals)
    for i,(lab,val) in enumerate(zip(labels,vals)):
        y=550+i*43; draw_text(95,y,lab,3,muted); rect(220,y+1,1080,y+25,grid)
        bw=max(5,int(860*val/maxv)); rect(220,y+1,220+bw,y+25,accent)
        draw_text(1100,y,str(val),3,white)
    raw=bytes(pixels)
    def png_chunk(kind,data): return struct.pack(">I",len(data))+kind+data+struct.pack(">I",binascii.crc32(kind+data)&0xffffffff)
    scan=b"".join(b"\x00"+raw[y*width*3:(y+1)*width*3] for y in range(height))
    png=b"\x89PNG\r\n\x1a\n"+png_chunk(b"IHDR",struct.pack(">IIBBBBB",width,height,8,2,0,0,0))+png_chunk(b"IDAT",zlib.compress(scan,6))+png_chunk(b"IEND",b"")
    with open(path,"wb") as f:f.write(png)


def _social_marriage_stage(days: int) -> str:
    if days < 7: return "💚 Зелёные"
    if days < 30: return "🌱 Молодые"
    if days < 90: return "💍 Серьёзные"
    if days < 180: return "💎 Опытные"
    return "👑 Легендарные"


def _social_duration_text(start: str) -> str:
    try:
        days = max(0, (_social_now() - datetime.fromisoformat(start)).days)
    except Exception:
        days = 0
    if days < 1: return "меньше дня"
    if days % 10 == 1 and days % 100 != 11: word="день"
    elif days % 10 in (2,3,4) and days % 100 not in (12,13,14): word="дня"
    else: word="дней"
    return f"{days} {word}"


def _social_active_marriage(conn, chat_id: int, user_id: int):
    return conn.execute("""
        SELECT * FROM social_marriages
        WHERE chat_id=? AND active=1 AND (user1=? OR user2=?) LIMIT 1
    """, (chat_id,user_id,user_id)).fetchone()


@bot.message_handler(commands=["profile", "профиль"])
def handle_social_profile(message: Message) -> None:
    args = " ".join((message.text or "").split()[1:])
    target = _social_target(message, args) or message.from_user.id
    conn = _social_db()
    row = conn.execute("SELECT first_name,username FROM social_users WHERE chat_id=? AND user_id=?", (message.chat.id,target)).fetchone()
    conn.close()
    day = _social_activity_for_user(message.chat.id,target,"day")
    week = _social_activity_for_user(message.chat.id,target,"week")
    month = _social_activity_for_user(message.chat.id,target,"month")
    total = _social_activity_for_user(message.chat.id,target,"all")
    name = row["username"] if row and row["username"] else (row["first_name"] if row else f"User {target}")
    display_name = ("@" + name) if row and row["username"] else name
    path=os.path.join(SOCIAL_GRAPH_DIR,f"profile_{message.chat.id}_{target}.png")
    _social_profile_png(path,display_name,day,week,month,total)
    caption=(f"👤 <b>Профиль {html.escape(str(display_name))}</b>\n\n"
             f"💬 Сегодня: <b>{day}</b> сообщений\n"
             f"📅 За неделю: <b>{week}</b>\n"
             f"🗓 За месяц: <b>{month}</b>\n"
             f"📊 Всего: <b>{total}</b>")
    with open(path,"rb") as photo: bot.send_photo(message.chat.id,photo,caption=caption)


@bot.message_handler(commands=["awards", "награды"])
def handle_social_awards(message: Message) -> None:
    args = " ".join((message.text or "").split()[1:])
    target = _social_target(message, args) or message.from_user.id
    conn=_social_db()
    rows=conn.execute("SELECT reason,created_at,from_user FROM social_awards WHERE chat_id=? AND to_user=? ORDER BY id DESC LIMIT 20",(message.chat.id,target)).fetchall()
    conn.close()
    label = _social_user_label(message.chat.id,target)
    if not rows:
        bot.send_message(message.chat.id,f"🏅 <b>Награды {label}</b>\n\nПока наград нет.")
        return
    lines=[f"🏅 <b>Награды {label}</b>\n"]
    for i,r in enumerate(rows,1):
        lines.append(f"🏆 <b>#{i}</b>  {html.escape(r['reason'])}\n   📅 {r['created_at'][:10]}")
    bot.send_message(message.chat.id,"\n".join(lines))


@bot.message_handler(commands=["marriages", "браки"])
def handle_social_marriages(message: Message) -> None:
    conn=_social_db()
    rows=conn.execute("SELECT * FROM social_marriages WHERE chat_id=? AND active=1 ORDER BY created_at ASC",(message.chat.id,)).fetchall()
    conn.close()
    if not rows:
        bot.send_message(message.chat.id,"💍 В этом чате пока нет активных браков.")
        return
    counts={}
    for r in rows:
        d=max(0,(_social_now()-datetime.fromisoformat(r['created_at'])).days)
        counts[_social_marriage_stage(d)]=counts.get(_social_marriage_stage(d),0)+1
    lines=[f"💍 <b>Браки чата</b>\nВсего активных: <b>{len(rows)}</b>\n"]
    for stage,n in counts.items(): lines.append(f"{stage}: <b>{n}</b>")
    lines.append("\n<b>Самые долгие:</b>")
    for r in sorted(rows,key=lambda x:x['created_at'])[:10]:
        d=max(0,(_social_now()-datetime.fromisoformat(r['created_at'])).days)
        lines.append(f"• {_social_user_label(message.chat.id,r['user1'])} ❤️ {_social_user_label(message.chat.id,r['user2'])} — <b>{_social_duration_text(r['created_at'])}</b>")
    bot.send_message(message.chat.id,"\n".join(lines))


@bot.message_handler(commands=["clans", "кланы"])
def handle_social_clans(message: Message) -> None:
    conn=_social_db()
    rows=conn.execute("""
        SELECT c.name,c.owner_id,COUNT(m.user_id) members
        FROM social_clans c LEFT JOIN social_clan_members m ON m.clan_id=c.id
        WHERE c.chat_id=? GROUP BY c.id ORDER BY members DESC,c.name LIMIT 20
    """,(message.chat.id,)).fetchall()
    conn.close()
    if not rows:
        bot.send_message(message.chat.id,"🏰 Кланов пока нет. Создай первый: <code>создать клан название</code>")
        return
    lines=["🏰 <b>Кланы Berlions</b>\n"]
    for i,r in enumerate(rows,1): lines.append(f"{i}. <b>{html.escape(r['name'])}</b> — 👥 {r['members']}")
    bot.send_message(message.chat.id,"\n".join(lines))


@bot.message_handler(commands=["top"])
def handle_social_top_command(message: Message) -> None:
    handle_social_top(message,"all")
    # Старый рейтинг биржи не теряем: он доступен отдельно через /topmoney.


def handle_social_top(message: Message, period: str = "all") -> None:
    rows=_social_top_rows(message.chat.id,period,10)
    title={"all":"🏆 ТОП АКТИВНОСТИ","day":"🔥 ТОП ДНЯ","week":"📅 ТОП НЕДЕЛИ","month":"🗓 ТОП МЕСЯЦА"}[period]
    if not rows:
        bot.send_message(message.chat.id,"📊 Пока нет статистики активности.")
        return
    lines=[f"<b>{title}</b>\n"]
    medals=["🥇","🥈","🥉"]
    for i,r in enumerate(rows,1):
        label=("@"+r['username']) if r['username'] else (r['first_name'] or f"User {r['user_id']}")
        prefix=medals[i-1] if i<=3 else f"{i}."
        lines.append(f"{prefix} <b>{html.escape(str(label))}</b> — <b>{r['messages']}</b> сообщ.")
    bot.send_message(message.chat.id,"\n".join(lines))


@bot.message_handler(commands=["topday", "topweek", "topmonth"])
def handle_social_top_period_command(message: Message) -> None:
    cmd=(message.text or "").split()[0].lower().lstrip("/")
    handle_social_top(message,{"topday":"day","topweek":"week","topmonth":"month"}[cmd])


@bot.message_handler(commands=["commands", "команды"])
def handle_social_commands(message: Message) -> None:
    text = (
        "📚 <b>Команды Berlions</b>\n\n"
        "👤 <b>Профиль и общение</b>\n"
        "• <code>профиль</code> — твой профиль\n"
        "• <code>профиль @user</code> — профиль пользователя\n"
        "• <code>награды</code> — твои награды\n"
        "• <code>награды @user</code> — награды пользователя\n"
        "• <code>наградить @user причина</code> — выдать награду\n"
        "• <code>поцеловать @user</code> / ответом — поцеловать\n"
        "• <code>обнять @user</code> / ответом — обнять\n"
        "• <code>дай пять @user</code> / ответом — дать пять\n\n"
        "💍 <b>Отношения</b>\n"
        "• <code>брак @user</code> / ответом — предложение брака\n"
        "• <code>браки</code> — статистика браков\n\n"
        "🏰 <b>Кланы</b>\n"
        "• <code>создать клан Название</code>\n"
        "• <code>+клан Название</code>\n"
        "• <code>кланы</code> — список кланов\n\n"
        "🏆 <b>Активность</b>\n"
        "• <code>топ</code> — общий топ\n"
        "• <code>топ дня</code>\n"
        "• <code>топ неделя</code>\n"
        "• <code>топ месяц</code>\n\n"
        "💰 <b>Биржа</b>\n"
        "• <code>/market</code> • <code>/balance</code> • <code>/bonus</code>\n"
        "• <code>/portfolio</code> • <code>/buy</code> • <code>/sell</code>\n"
        "• <code>/exchange</code> • <code>/exchanges</code>\n\n"
        "🎲 <code>/dice</code> — кости\n"
        "🔎 <code>/search запрос</code> — поиск"
    )
    bot.send_message(message.chat.id,text)


@bot.message_handler(func=lambda m: bool(m.text and re.fullmatch(r"команды", m.text.strip(), re.I)), content_types=["text"])
def handle_social_commands_plain(message: Message) -> None:
    handle_social_commands(message)


@bot.message_handler(func=lambda m: bool(m.text and re.fullmatch(r"профиль(?:\s+@\w+)?", m.text.strip(), re.I)), content_types=["text"])
def handle_social_profile_plain(message: Message) -> None:
    handle_social_profile(message)


@bot.message_handler(func=lambda m: bool(m.text and re.fullmatch(r"награды(?:\s+@\w+)?", m.text.strip(), re.I)), content_types=["text"])
def handle_social_awards_plain(message: Message) -> None:
    handle_social_awards(message)


@bot.message_handler(func=lambda m: bool(m.text and re.fullmatch(r"браки", m.text.strip(), re.I)), content_types=["text"])
def handle_social_marriages_plain(message: Message) -> None:
    handle_social_marriages(message)


@bot.message_handler(func=lambda m: bool(m.text and re.fullmatch(r"кланы", m.text.strip(), re.I)), content_types=["text"])
def handle_social_clans_plain(message: Message) -> None:
    handle_social_clans(message)


@bot.message_handler(func=lambda m: bool(m.text and re.fullmatch(r"топ", m.text.strip(), re.I)), content_types=["text"])
def handle_social_top_plain(message: Message) -> None:
    handle_social_top(message,"all")


@bot.message_handler(func=lambda m: bool(m.text and re.fullmatch(r"топ\s+дня", m.text.strip(), re.I)), content_types=["text"])
def handle_social_top_day_plain(message: Message) -> None:
    handle_social_top(message,"day")


@bot.message_handler(func=lambda m: bool(m.text and re.fullmatch(r"топ\s+неделя", m.text.strip(), re.I)), content_types=["text"])
def handle_social_top_week_plain(message: Message) -> None:
    handle_social_top(message,"week")


@bot.message_handler(func=lambda m: bool(m.text and re.fullmatch(r"топ\s+месяц", m.text.strip(), re.I)), content_types=["text"])
def handle_social_top_month_plain(message: Message) -> None:
    handle_social_top(message,"month")


@bot.message_handler(func=lambda m: bool(m.text and re.match(r"^брак(?:\s+.+)?$", m.text.strip(), re.I)), content_types=["text"])
def handle_social_marry_plain(message: Message) -> None:
    handle_social_marry_command(message)


@bot.message_handler(func=lambda m: bool(m.text and re.match(r"^наградить(?:\s+.+)?$",m.text.strip(),re.I)), content_types=["text"])
def handle_social_award(message: Message) -> None:
    parts=message.text.strip().split(maxsplit=2)
    if message.reply_to_message and message.reply_to_message.from_user:
        target=_social_target(message,"")
        reason=parts[1].strip() if len(parts)>1 else "За вклад в чат"
    else:
        arg=parts[1] if len(parts)>1 else ""
        reason=parts[2].strip() if len(parts)>2 else "За вклад в чат"
        target=_social_target(message,arg)
    if not target:
        bot.send_message(message.chat.id,"🏅 Формат: <code>наградить @user причина</code> или ответом на сообщение: <code>наградить причина</code>")
        return
    if target==message.from_user.id:
        bot.send_message(message.chat.id,"😄 Себя наградить нельзя."); return
    now=_social_now().isoformat(sep=" ")
    conn=_social_db(); conn.execute("INSERT INTO social_awards(chat_id,from_user,to_user,reason,created_at) VALUES(?,?,?,?,?)",(message.chat.id,message.from_user.id,target,reason,now)); conn.commit(); conn.close()
    bot.send_message(message.chat.id,f"🏆 {_social_user_label(message.chat.id,target)} <b>награждён!</b>\n🎖 Причина: <i>{html.escape(reason)}</i>")


@bot.message_handler(func=lambda m: bool(m.text and re.match(r"^создать\s+клан\s+.+$",m.text.strip(),re.I)), content_types=["text"])
def handle_social_create_clan(message: Message) -> None:
    name=message.text.strip().split(None,2)[2].strip()
    if len(name)<2 or len(name)>32:
        bot.send_message(message.chat.id,"🏰 Название клана должно быть от 2 до 32 символов."); return
    key=name.casefold()
    conn=_social_db()
    try:
        existing=conn.execute("SELECT id FROM social_clans WHERE chat_id=? AND name_key=?",(message.chat.id,key)).fetchone()
        if existing:
            bot.send_message(message.chat.id,"❌ Такой клан уже существует."); conn.close(); return
        cur=conn.execute("INSERT INTO social_clans(chat_id,name,name_key,owner_id,created_at) VALUES(?,?,?,?,?)",(message.chat.id,name,key,message.from_user.id,_social_now().isoformat(sep=" ")))
        clan_id=cur.lastrowid
        conn.execute("INSERT OR REPLACE INTO social_clan_members(chat_id,clan_id,user_id,joined_at) VALUES(?,?,?,?)",(message.chat.id,clan_id,message.from_user.id,_social_now().isoformat(sep=" ")))
        conn.commit()
    finally: conn.close()
    bot.send_message(message.chat.id,f"🏰 Клан <b>{html.escape(name)}</b> создан!\nТы автоматически вступил в него.\nВступить другим: <code>+клан {html.escape(name)}</code>")


@bot.message_handler(func=lambda m: bool(m.text and re.match(r"^\+клан\s+.+$",m.text.strip(),re.I)), content_types=["text"])
def handle_social_join_clan(message: Message) -> None:
    name=message.text.strip().split(None,1)[1].strip(); key=name.casefold()
    conn=_social_db(); clan=conn.execute("SELECT id,name FROM social_clans WHERE chat_id=? AND name_key=?",(message.chat.id,key)).fetchone()
    if not clan:
        conn.close(); bot.send_message(message.chat.id,"❌ Такой клан не найден. Посмотри список: <code>кланы</code>"); return
    old=conn.execute("SELECT clan_id FROM social_clan_members WHERE chat_id=? AND user_id=?",(message.chat.id,message.from_user.id)).fetchone()
    if old and old['clan_id']==clan['id']:
        conn.close(); bot.send_message(message.chat.id,"🏰 Ты уже в этом клане!"); return
    conn.execute("DELETE FROM social_clan_members WHERE chat_id=? AND user_id=?",(message.chat.id,message.from_user.id))
    conn.execute("INSERT INTO social_clan_members(chat_id,clan_id,user_id,joined_at) VALUES(?,?,?,?)",(message.chat.id,clan['id'],message.from_user.id,_social_now().isoformat(sep=" ")))
    conn.commit(); conn.close()
    bot.send_message(message.chat.id,f"🏰 Добро пожаловать в клан <b>{html.escape(clan['name'])}</b>!")


@bot.message_handler(commands=["marry", "брак"])
def handle_social_marry_command(message: Message) -> None:
    arg=" ".join((message.text or "").split()[1:])
    target=_social_target(message,arg)
    if not target:
        bot.send_message(message.chat.id,"💍 Формат: <code>брак @user</code> или ответом на сообщение пользователя.")
        return
    if target==message.from_user.id:
        bot.send_message(message.chat.id,"💍 Сам с собой брак не заключается 😄"); return
    conn=_social_db()
    if _social_active_marriage(conn,message.chat.id,message.from_user.id) or _social_active_marriage(conn,message.chat.id,target):
        conn.close(); bot.send_message(message.chat.id,"💍 У одного из пользователей уже есть активный брак."); return
    pending=conn.execute("SELECT id FROM social_marriage_proposals WHERE chat_id=? AND from_user=? AND to_user=? AND status='pending'",(message.chat.id,message.from_user.id,target)).fetchone()
    if pending:
        conn.close(); bot.send_message(message.chat.id,"⏳ Предложение уже отправлено."); return
    cur=conn.execute("INSERT INTO social_marriage_proposals(chat_id,from_user,to_user,created_at,status) VALUES(?,?,?,?, 'pending')",(message.chat.id,message.from_user.id,target,_social_now().isoformat(sep=" ")))
    proposal_id=cur.lastrowid; conn.commit(); conn.close()
    markup=InlineKeyboardMarkup(row_width=2).add(InlineKeyboardButton("💍 Согласиться",callback_data=f"marry_yes:{proposal_id}"),InlineKeyboardButton("❌ Отказаться",callback_data=f"marry_no:{proposal_id}"))
    bot.send_message(message.chat.id,f"💍 {_social_user_label(message.chat.id,message.from_user.id)} предлагает брак {_social_user_label(message.chat.id,target)}!\n\nСогласие второго пользователя обязательно.",reply_markup=markup)


def handle_social_marriage_callback(call: CallbackQuery) -> None:
    try:
        action,pid=call.data.split(":",1); pid=int(pid)
        conn=_social_db(); p=conn.execute("SELECT * FROM social_marriage_proposals WHERE id=?",(pid,)).fetchone()
        if not p or p['status']!='pending':
            conn.close(); bot.answer_callback_query(call.id,"Предложение уже обработано.",show_alert=True); return
        if call.from_user.id != p['to_user']:
            conn.close(); bot.answer_callback_query(call.id,"Это предложение не для тебя.",show_alert=True); return
        if action=="marry_no":
            conn.execute("UPDATE social_marriage_proposals SET status='rejected' WHERE id=?",(pid,)); conn.commit(); conn.close(); bot.answer_callback_query(call.id,"Отказ принят."); bot.edit_message_text("❌ Предложение отклонено.",call.message.chat.id,call.message.message_id); return
        if _social_active_marriage(conn,p['chat_id'],p['from_user']) or _social_active_marriage(conn,p['chat_id'],p['to_user']):
            conn.execute("UPDATE social_marriage_proposals SET status='rejected' WHERE id=?",(pid,)); conn.commit(); conn.close(); bot.answer_callback_query(call.id,"У одного из вас уже есть брак.",show_alert=True); return
        now=_social_now().isoformat(sep=" ")
        conn.execute("UPDATE social_marriage_proposals SET status='accepted' WHERE id=?",(pid,))
        conn.execute("INSERT INTO social_marriages(chat_id,user1,user2,created_at,active) VALUES(?,?,?,?,1)",(p['chat_id'],p['from_user'],p['to_user'],now))
        conn.commit(); conn.close(); bot.answer_callback_query(call.id,"💍 Брак создан!"); bot.edit_message_text(f"💍 <b>Брак заключён!</b>\n\n{_social_user_label(p['chat_id'],p['from_user'])} ❤️ {_social_user_label(p['chat_id'],p['to_user'])}\n🌱 Стаж: <b>0 дней</b> — 💚 Зелёные",call.message.chat.id,call.message.message_id)
    except Exception:
        logger.exception("Marriage callback error")
        try: bot.answer_callback_query(call.id,"❌ Ошибка",show_alert=True)
        except Exception: pass


@bot.message_handler(func=lambda m: bool(m.text and re.match(r"^(поцеловать|поцеловал|поцеловала|обнять|обнял|обняла|дай пять|дать пять|погладить|пожать руку)\b",m.text.strip(),re.I)), content_types=["text"])
def handle_social_roleplay(message: Message) -> None:
    parts=message.text.strip().split(maxsplit=1)
    action=parts[0].lower()
    target=_social_target(message,parts[1] if len(parts)>1 else "")
    if not target:
        bot.send_message(message.chat.id,f"💫 Формат: <code>{html.escape(action)} @user</code> или просто ответь этой командой на сообщение."); return
    target_label=_social_user_label(message.chat.id,target)
    replies={
        "поцеловать": f"💋 {target_label}, тебя только что поцеловали!",
        "поцеловал": f"💋 {target_label}, тебя поцеловали!",
        "поцеловала": f"💋 {target_label}, тебя поцеловали!",
        "обнять": f"🤗 {target_label}, тебя крепко обняли!",
        "обнял": f"🤗 {target_label}, тебя крепко обняли!",
        "обняла": f"🤗 {target_label}, тебя крепко обняли!",
        "дай пять": f"🖐 {target_label}, вам дали пять!",
        "дать пять": f"🖐 {target_label}, вам дали пять!",
        "погладить": f"🥰 {target_label}, тебя нежно погладили!",
        "пожать руку": f"🤝 {target_label}, вам пожали руку!",
    }
    bot.send_message(message.chat.id,replies.get(action,f"✨ {target_label} — действие выполнено!"))


@bot.message_handler(commands=["learn"])
def handle_learn(message: Message) -> None:
    if not _is_admin(message.from_user.id) and _get_user_rank(message.chat.id, message.from_user.id) < 5:
        bot.send_message(message.chat.id, "⛔ Только глава или глобальный.")
        return
    bot.send_message(message.chat.id, "✅ /learn активирован. Полная история чата пока не поддерживается (ограничения Telegram).")

# Последний обработчик сообщений: собирает активность, не перехватывая уже обработанные команды.
@bot.message_handler(content_types=["text"])
def handle_social_activity_tracker(message: Message) -> None:
    try:
        _social_register(message)
    except Exception:
        logger.exception("Social activity tracker error")



# ── Advertising/autopost ───────────────────────────────────────────────────────
# Автопостинг рекламы удалён по запросу. Остальная логика бота не зависит от него.

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    bot.remove_webhook()
    logger.info("Bot started. Polling…")
    bot.polling(non_stop=True, interval=0)
