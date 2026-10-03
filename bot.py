"""
Telegram bot with deep-link support, subscription gate (channel & chat), 
dice game, admin manager and AUTO-BROADCAST system.

Environment variables (Replit Secrets):
  BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
  ADMIN_ID   — primary admin Telegram user ID (integer)

IMPORTANT: Add this bot as an Administrator to both @Berlions_mb and @Chats_Berlions
so it can check member status via getChatMember.
"""

import os
import time
import html
import io
import random
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

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
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
            f"• /помощь — список доступных команд\n"
            f"• /поиск <i>запрос</i> — поиск игры или контента\n"
            f"• /dice — сыграть в кости 🎲\n• /profile — профиль и активность\n• /top — топ активности\n• /topmoney — топ капитала биржи\n\n"
            f"🔍 <b>Поиск:</b>\n"
            f"Например: <code>/поиск standoff 2</code>\n\n"
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

@bot.message_handler(commands=["help", "помощь"])
def handle_help(message: Message) -> None:
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🎲 Сыграть в кости", callback_data="play_dice"))
    bot.send_message(
        message.chat.id,
        "📋 <b>Доступные команды:</b>\n\n"
        "• /start — главное меню\n"
        "• /помощь — список доступных команд\n"
        "• /поиск <i>запрос</i> — поиск контента\n"
        "• /кости — сыграть в кости 🎲\n\n"
        "Пример поиска: <code>/поиск minecraft</code>",
        reply_markup=markup,
    )

# ── /search (С анимацией и умным поиском) ─────────────────────────────────────

@bot.message_handler(commands=["search", "поиск"])
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
# Компании, акции, владельцы, спрос/предложение и графики.
# Сохраняет старые команды /buy, /sell, /market, /portfolio и т.д.

import sqlite3
import math
import struct
import binascii
import zlib
from datetime import datetime, date

EXCHANGE_DB = "exchange.db"
EXCHANGE_GRAPH_DIR = "exchange_graphs"
EXCHANGE_START_BALANCE = 10_000.0
EXCHANGE_DAILY_BONUS = 1_000.0
EXCHANGE_DEFAULT_PRICE = 100.0
EXCHANGE_DEFAULT_TICKER = "BERL"
EXCHANGE_MAX_TRADE = 100_000
EXCHANGE_DEFAULT_TOTAL_SHARES = 1_000_000
EXCHANGE_OWNER_SHARES = 700_000
EXCHANGE_FLOAT_SHARES = 300_000
EXCHANGE_TRADE_COOLDOWN = 5
EXCHANGE_MARKET_COOLDOWN = 10
EXCHANGE_TRADE_FEE = 0.015
EXCHANGE_OWNER_FEE_SHARE = 0.35
EXCHANGE_MIN_DEPOSIT = 5_000
EXCHANGE_DEPOSIT_RATE = 0.02
EXCHANGE_LOAN_LIMIT = 2_000_000
EXCHANGE_LOAN_RATE = 0.12
EXCHANGE_CREATE_BASE_COST = 1_000_000.0
EXCHANGE_MAX_TOTAL_SHARES = 10_000_000
EXCHANGE_MIN_CONTROL_PCT = 50.0
EXCHANGE_INACTIVE_DAYS = 30
_ex_trade_cooldown: dict[int, float] = {}
_ex_market_cooldown: dict[int, float] = {}


def _ex_db():
    conn = sqlite3.connect(EXCHANGE_DB, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ex_has_column(conn, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})").fetchall())


def _ex_add_column(conn, table: str, column: str, definition: str) -> None:
    if not _ex_has_column(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


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
    CREATE TABLE IF NOT EXISTS ex_deposits (user_id INTEGER PRIMARY KEY, principal REAL NOT NULL DEFAULT 0, started_at TEXT NOT NULL, last_claim TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS ex_loans (user_id INTEGER PRIMARY KEY, principal REAL NOT NULL DEFAULT 0, debt REAL NOT NULL DEFAULT 0, started_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS ex_takeover_offers (id INTEGER PRIMARY KEY AUTOINCREMENT, buyer_company_id INTEGER NOT NULL, target_company_id INTEGER NOT NULL, from_user INTEGER NOT NULL, to_user INTEGER NOT NULL, price REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL);
    """)

    # Миграция старой базы: существующие таблицы не удаляем.
    _ex_add_column(conn, "ex_exchanges", "total_shares", "INTEGER NOT NULL DEFAULT 1000000")
    _ex_add_column(conn, "ex_exchanges", "treasury_shares", "INTEGER NOT NULL DEFAULT 1000000")
    _ex_add_column(conn, "ex_exchanges", "description", "TEXT NOT NULL DEFAULT ''")
    _ex_add_column(conn, "ex_exchanges", "active", "INTEGER NOT NULL DEFAULT 1")
    _ex_add_column(conn, "ex_exchanges", "treasury", "REAL NOT NULL DEFAULT 0")
    _ex_add_column(conn, "ex_exchanges", "level", "INTEGER NOT NULL DEFAULT 1")
    _ex_add_column(conn, "ex_exchanges", "popularity", "INTEGER NOT NULL DEFAULT 50")
    _ex_add_column(conn, "ex_exchanges", "trade_volume", "REAL NOT NULL DEFAULT 0")
    _ex_add_column(conn, "ex_exchanges", "dividends_paid", "REAL NOT NULL DEFAULT 0")
    _ex_add_column(conn, "ex_exchanges", "market_cash", "REAL NOT NULL DEFAULT 0")
    _ex_add_column(conn, "ex_exchanges", "founder_id", "INTEGER NOT NULL DEFAULT 0")
    _ex_add_column(conn, "ex_exchanges", "control_changed_at", "TEXT")
    _ex_add_column(conn, "ex_exchanges", "acquisitions", "INTEGER NOT NULL DEFAULT 0")
    _ex_add_column(conn, "ex_exchanges", "parent_company_id", "INTEGER")
    _ex_add_column(conn, "ex_exchanges", "last_activity", "TEXT")
    _ex_add_column(conn, "ex_holdings", "locked_shares", "INTEGER NOT NULL DEFAULT 0")
    _ex_add_column(conn, "ex_traders", "reputation", "INTEGER NOT NULL DEFAULT 50")
    _ex_add_column(conn, "ex_traders", "last_trade", "TEXT")

    # Shares are real equity. The market-maker never creates money:
    # every company has a bounded cash reserve used to settle sales.
    # Old founder locks are removed because the reserve now prevents money creation.
    conn.execute("UPDATE ex_holdings SET locked_shares=0 WHERE locked_shares IS NOT NULL")
    conn.execute("UPDATE ex_exchanges SET founder_id=CASE WHEN founder_id=0 THEN owner_id ELSE founder_id END")
    conn.execute("UPDATE ex_exchanges SET market_cash=CASE WHEN market_cash>0 THEN market_cash ELSE MAX(0,treasury) END")
    conn.execute("UPDATE ex_exchanges SET last_activity=COALESCE(last_activity,created_at)")

    row = conn.execute("SELECT id,total_shares,treasury_shares FROM ex_exchanges WHERE ticker=?", (EXCHANGE_DEFAULT_TICKER,)).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO ex_exchanges(owner_id,name,ticker,price,created_at,total_shares,treasury_shares,description,active) VALUES(?,?,?,?,?,?,?,?,1)",
            (0, "Berlions", EXCHANGE_DEFAULT_TICKER, EXCHANGE_DEFAULT_PRICE,
             datetime.now().isoformat(timespec="seconds"), EXCHANGE_DEFAULT_TOTAL_SHARES,
             EXCHANGE_DEFAULT_TOTAL_SHARES, "Главная акция экосистемы Berlions.")
        )
        exchange_id = cur.lastrowid
        conn.execute(
            "INSERT INTO ex_history(exchange_id,price,ts) VALUES(?,?,?)",
            (exchange_id, EXCHANGE_DEFAULT_PRICE, datetime.now().isoformat(timespec="seconds")),
        )
    else:
        # Старую BERL оставляем полностью ликвидной: никто не должен внезапно получить чужие акции.
        conn.execute("UPDATE ex_exchanges SET total_shares=COALESCE(total_shares,?), treasury_shares=COALESCE(treasury_shares,total_shares,?), active=COALESCE(active,1) WHERE id=?",
                     (EXCHANGE_DEFAULT_TOTAL_SHARES, EXCHANGE_DEFAULT_TOTAL_SHARES, row["id"]))
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


def _ex_add_work_column():
    conn = _ex_db()
    _ex_add_column(conn, "ex_traders", "last_work", "TEXT")
    conn.commit(); conn.close()

_ex_add_work_column()


def _ex_escape(value):
    return html.escape(str(value))


def _ex_user_mention(chat_id: int, user_id: int, fallback: str = "Пользователь") -> str:
    """Настоящее Telegram-упоминание по ID с приоритетом данным текущего чата."""
    display = None
    try:
        conn = _ex_db()
        # social_users уже создаётся при старте до обработки сообщений.
        row = conn.execute("SELECT username,first_name,last_name FROM social_users WHERE chat_id=? AND user_id=?", (chat_id,user_id)).fetchone()
        conn.close()
        if row:
            if row["username"]: display = "@" + row["username"]
            else: display = " ".join(x for x in (row["first_name"],row["last_name"]) if x).strip() or None
    except Exception:
        pass
    if not display:
        try:
            member = bot.get_chat_member(chat_id, user_id)
            user = member.user
            display = f"@{user.username}" if user.username else (user.first_name or None)
        except Exception:
            pass
    if not display:
        display = fallback
    return f'<a href="tg://user?id={int(user_id)}">{html.escape(str(display))}</a>'


def _ex_change_price(conn, exchange_id: int, action_bias: float = 0.0):
    """Цена двигается от спроса/предложения, а не случайно.

    action_bias > 0: покупка, action_bias < 0: продажа.
    Величина эффекта растёт примерно как sqrt(объёма/свободных акций),
    поэтому маленькая сделка почти незаметна, а крупный слив уже влияет на рынок.
    """
    row = conn.execute("SELECT price,treasury_shares,active FROM ex_exchanges WHERE id=?", (exchange_id,)).fetchone()
    if not row:
        return None, 0.0
    old_price = max(0.01, float(row["price"]))
    if not row["active"]:
        return old_price, 0.0

    effect = max(-0.30, min(0.30, float(action_bias)))
    new_price = max(1.0, old_price * (1.0 + effect))
    conn.execute("UPDATE ex_exchanges SET price=? WHERE id=?", (new_price, exchange_id))
    conn.execute(
        "INSERT INTO ex_history(exchange_id,price,ts) VALUES(?,?,?)",
        (exchange_id, new_price, datetime.now().isoformat(timespec="seconds")),
    )
    return new_price, ((new_price / old_price) - 1.0) * 100.0


def _ex_trade_impact(shares: int, available_before: int) -> float:
    """Процентный impact одной сделки. Покупка +, продажа -."""
    base = max(10_000, int(available_before or 10_000))
    magnitude = 0.08 * math.sqrt(max(1, shares) / base)
    return min(0.25, magnitude)


def _ex_chart(exchange_id: int):
    """PNG-график только стандартной библиотекой Python."""
    import os
    os.makedirs(EXCHANGE_GRAPH_DIR, exist_ok=True)
    conn = _ex_db()
    ex = conn.execute("SELECT * FROM ex_exchanges WHERE id=?", (exchange_id,)).fetchone()
    rows = conn.execute(
        "SELECT price,ts FROM ex_history WHERE exchange_id=? ORDER BY id DESC LIMIT 80",
        (exchange_id,),
    ).fetchall()
    conn.close()
    if not ex:
        raise RuntimeError("Компания не найдена")
    rows = list(reversed(rows))
    prices = [float(r["price"]) for r in rows] or [float(ex["price"])]
    change = ((prices[-1] / prices[0]) - 1.0) * 100.0 if len(prices) > 1 and prices[0] else 0.0
    path = os.path.join(EXCHANGE_GRAPH_DIR, f"market_{exchange_id}.png")

    width, height = 1200, 650
    bg = (17, 24, 39); grid = (55, 65, 81)
    line_color = (34, 197, 94) if change >= 0 else (239, 68, 68)
    area_color = (31, 65, 48) if change >= 0 else (70, 35, 42)
    pixels = bytearray(bg * (width * height))

    def rect(x1, y1, x2, y2, color):
        x1, x2 = max(0, int(x1)), min(width - 1, int(x2))
        y1, y2 = max(0, int(y1)), min(height - 1, int(y2))
        if x2 < x1 or y2 < y1: return
        for yy in range(y1, y2 + 1):
            start = (yy * width + x1) * 3
            pixels[start:start + (x2 - x1 + 1) * 3] = bytes(color) * (x2 - x1 + 1)

    def line(x1, y1, x2, y2, color, thickness=4):
        steps = max(1, int(max(abs(x2-x1), abs(y2-y1))))
        for i in range(steps + 1):
            t = i / steps
            x = round(x1 + (x2-x1)*t); y = round(y1 + (y2-y1)*t)
            r = max(0, thickness // 2)
            rect(x-r, y-r, x+r, y+r, color)

    ml, mr, mt, mb = 70, 45, 55, 55
    left, right, top, bottom = ml, width-mr, mt, height-mb
    rect(left, top, right, top+2, grid); rect(left, bottom-2, right, bottom, grid)
    rect(left, top, left+2, bottom, grid); rect(right-2, top, right, bottom, grid)
    for j in range(1, 5):
        y = top + (bottom-top)*j/5; rect(left, y, right, y+1, grid)
    for j in range(1, 6):
        x = left + (right-left)*j/6; rect(x, top, x+1, bottom, grid)
    low, high = min(prices), max(prices)
    if high == low:
        pad = max(1.0, abs(high)*0.02); low -= pad; high += pad
    else:
        pad = (high-low)*0.08; low -= pad; high += pad
    points = []
    denom = max(1, len(prices)-1)
    for i, price in enumerate(prices):
        x = left + (right-left)*i/denom
        y = bottom - (price-low)/(high-low)*(bottom-top)
        points.append((x,y))
    for i in range(len(points)-1):
        x1,y1=points[i]; x2,y2=points[i+1]
        steps=max(1,int(abs(x2-x1)))
        for s in range(steps+1):
            t=s/steps; x=round(x1+(x2-x1)*t); y=round(y1+(y2-y1)*t)
            rect(x, min(y,bottom), x+1, bottom, area_color)
    for i in range(len(points)-1): line(*points[i], *points[i+1], line_color, thickness=7)
    if points:
        x,y=points[-1]; rect(x-7,y-7,x+7,y+7,line_color); rect(x-3,y-3,x+3,y+3,(255,255,255))

    def png_chunk(kind, data):
        return struct.pack(">I",len(data))+kind+data+struct.pack(">I",binascii.crc32(kind+data)&0xffffffff)
    raw=bytearray(); row_bytes=width*3
    for y in range(height):
        raw.append(0); raw.extend(pixels[y*row_bytes:(y+1)*row_bytes])
    png=(b"\x89PNG\r\n\x1a\n"+
         png_chunk(b"IHDR",struct.pack(">IIBBBBB",width,height,8,2,0,0,0))+
         png_chunk(b"IDAT",zlib.compress(bytes(raw),6))+png_chunk(b"IEND",b""))
    with open(path,"wb") as f: f.write(png)
    return path


def _ex_company_row(conn, ticker_or_id):
    if isinstance(ticker_or_id, int):
        return conn.execute("SELECT * FROM ex_exchanges WHERE id=?", (ticker_or_id,)).fetchone()
    return conn.execute("SELECT * FROM ex_exchanges WHERE ticker=?", (str(ticker_or_id).upper(),)).fetchone()


def _ex_market_text(exchange_id: int, chat_id: int = 0):
    conn=_ex_db(); ex=conn.execute("SELECT * FROM ex_exchanges WHERE id=?",(exchange_id,)).fetchone(); hist=conn.execute("SELECT price FROM ex_history WHERE exchange_id=? ORDER BY id DESC LIMIT 2",(exchange_id,)).fetchall()
    top=conn.execute("SELECT user_id,shares FROM ex_holdings WHERE exchange_id=? AND shares>0 ORDER BY shares DESC LIMIT 1",(exchange_id,)).fetchone(); conn.close()
    if not ex: return "❌ Компания не найдена."
    current=float(ex["price"]); previous=float(hist[1]["price"]) if len(hist)>1 else current; change=((current/previous)-1)*100 if previous else 0
    arrow="📈" if change>0 else "📉" if change<0 else "➖"; status="🟢 Торги открыты" if ex["active"] else "🔴 Компания закрыта"
    top_pct=(int(top['shares'])/max(1,int(ex['total_shares']))*100) if top else 0
    top_label=_ex_user_mention(chat_id,int(top['user_id'])) if top else "Никто"
    return (f"🏭 <b>{_ex_escape(ex['name'])}</b>  <code>${_ex_escape(ex['ticker'])}</code>\n{status}\n\n"
            f"💰 Цена: <b>{current:,.2f} ₽</b>\n{arrow} Последняя сделка: <b>{change:+.2f}%</b>\n"
            f"📦 Всего акций: <b>{int(ex['total_shares']):,}</b>\n🛒 Свободно: <b>{int(ex['treasury_shares']):,}</b> ({int(ex['treasury_shares'])/max(1,int(ex['total_shares']))*100:.2f}%)\n"
            f"💎 Капитализация: <b>{current*int(ex['total_shares']):,.0f} ₽</b>\n🏦 Казна: <b>{float(ex['treasury']):,.0f} ₽</b>\n💧 Ликвидность: <b>{float(ex['market_cash'] or 0):,.0f} ₽</b>\n"
            f"🎯 Крупнейший пакет: {top_label} · <b>{top_pct:.2f}%</b>\n\n"
            f"/компания {ex['ticker']} — профиль\n/купить {ex['ticker']} 5 — купить\n/продать {ex['ticker']} 5 — продать\n/держатели {ex['ticker']} — акционеры")

def _ex_markup():
    markup=InlineKeyboardMarkup(row_width=2)
    markup.add(InlineKeyboardButton("🔄 Обновить рынок",callback_data="ex_refresh"),InlineKeyboardButton("💼 Портфель",callback_data="ex_portfolio"),InlineKeyboardButton("💰 Бонус",callback_data="ex_bonus"))
    return markup


def _ex_cd(bucket: dict[int,float], user_id: int, seconds: int) -> int:
    now=time.time(); left=seconds-(now-bucket.get(user_id,0))
    if left>0: return max(1,int(left+0.999))
    bucket[user_id]=now; return 0


def _ex_replace_market(chat_id: int, user_id: int, exchange_id: int = 1):
    _ex_ensure_trader(user_id)
    conn=_ex_db(); ex=conn.execute("SELECT id FROM ex_exchanges WHERE id=?",(exchange_id,)).fetchone()
    if not ex: exchange_id=1
    old=conn.execute("SELECT market_message_id,market_chat_id FROM ex_traders WHERE user_id=?",(user_id,)).fetchone(); conn.close()
    if old and old["market_message_id"] and old["market_chat_id"]:
        try: bot.delete_message(old["market_chat_id"],old["market_message_id"])
        except Exception: pass
    path=_ex_chart(exchange_id)
    with open(path,"rb") as photo:
        msg=bot.send_photo(chat_id,photo,caption=_ex_market_text(exchange_id, chat_id),reply_markup=_ex_markup())
    conn=_ex_db(); conn.execute("UPDATE ex_traders SET market_message_id=?,market_chat_id=? WHERE user_id=?",(msg.message_id,chat_id,user_id)); conn.commit(); conn.close()


def _ex_require_user(message):
    _ex_ensure_trader(message.from_user.id); return True


@bot.message_handler(commands=["exchange", "биржа"])
def handle_exchange(message: Message) -> None:
    _ex_require_user(message)
    bot.send_message(message.chat.id,
        "🏦 <b>BERLIONS STOCK MARKET 2.0</b>\n\n"
        "💰 <b>Деньги</b>\n/бонус — ежедневные 1 000 ₽\n/баланс — баланс\n/работа — заработать раз в 30 мин.\n/банк — вклад и кредит\n/репутация — рейтинг трейдера\n\n"
        "📈 <b>Торговля</b>\n/рынок — рынок и график\n/портфель — мои акции\n/купить TICKER 100 — купить\n/продать TICKER 100 — продать\n/держатели TICKER — доли всех крупных акционеров\n\n"
        "🏢 <b>Компании</b>\n/создать_компанию Название TICKER — зарегистрировать бизнес\n/компания TICKER — профиль\n/компании — все активные компании\n/топкомпаний — рейтинг по капитализации\n/топоборота — рейтинг по обороту\n/статистика_компании TICKER — подробная статистика\n/управление_компанией TICKER ... — управление, выпуск акций и расходы\n\n"
        "👑 <b>Контроль и война компаний</b>\n/контроль TICKER — кто контролирует компанию\n/поглощение BUYER TARGET — предложить поглощение\n/дочерние — мои дочерние компании\n\n"
        "💎 <b>Доход компании</b>\n/дивиденды TICKER 100000 — выплатить дивиденды\n/улучшить TICKER маркетинг — развитие компании\n/топакций — крупнейшие акционеры по стоимости\n\n"
        "🛡 <b>Главное правило экономики:</b> деньги за покупку уходят в ликвидность компании, а продажи оплачиваются только из этой ликвидности. Система не должна создавать деньги из воздуха.")


@bot.message_handler(commands=["bonus", "бонус"])
def handle_exchange_bonus(message: Message) -> None:
    user_id=message.from_user.id; _ex_ensure_trader(user_id); today=date.today().isoformat(); conn=_ex_db()
    row=conn.execute("SELECT balance,last_bonus FROM ex_traders WHERE user_id=?",(user_id,)).fetchone()
    if row["last_bonus"]==today:
        conn.close(); bot.send_message(message.chat.id,"⏳ Ты уже получил ежедневный бонус сегодня. Возвращайся завтра!"); return
    new_balance=float(row["balance"])+EXCHANGE_DAILY_BONUS
    conn.execute("UPDATE ex_traders SET balance=?,last_bonus=? WHERE user_id=?",(new_balance,today,user_id)); conn.commit(); conn.close()
    bot.send_message(message.chat.id,f"🎁 <b>Ежедневный бонус!</b>\n\n+{EXCHANGE_DAILY_BONUS:,.0f} ₽\n💰 Баланс: <b>{new_balance:,.2f} ₽</b>")


@bot.message_handler(commands=["balance", "баланс"])
def handle_exchange_balance(message: Message) -> None:
    row=_ex_ensure_trader(message.from_user.id); bot.send_message(message.chat.id,f"💰 Твой баланс: <b>{float(row['balance']):,.2f} ₽</b>\n\n🎁 Ежедневный бонус: /bonus")


@bot.message_handler(commands=["market", "рынок"])
def handle_exchange_market(message: Message) -> None:
    try:
        wait=_ex_cd(_ex_market_cooldown,message.from_user.id,EXCHANGE_MARKET_COOLDOWN)
        if wait:
            bot.send_message(message.chat.id,f"⏳ Рынок можно обновлять раз в {EXCHANGE_MARKET_COOLDOWN} сек. Ещё <b>{wait} сек.</b>"); return
        _ex_require_user(message); _ex_replace_market(message.chat.id,message.from_user.id,1)
    except Exception as exc:
        logger.exception("/market error: %s",exc); bot.send_message(message.chat.id,"❌ Не удалось открыть рынок.\n\nТехническая ошибка: <code>"+html.escape(str(exc))+"</code>")


@bot.message_handler(commands=["portfolio", "портфель"])
def handle_exchange_portfolio(message: Message) -> None:
    user_id=message.from_user.id; _ex_ensure_trader(user_id); conn=_ex_db()
    rows=conn.execute("SELECT h.shares,h.avg_price,e.name,e.ticker,e.price FROM ex_holdings h JOIN ex_exchanges e ON e.id=h.exchange_id WHERE h.user_id=? AND h.shares>0 ORDER BY e.ticker",(user_id,)).fetchall()
    balance=conn.execute("SELECT balance FROM ex_traders WHERE user_id=?",(user_id,)).fetchone()["balance"]; conn.close()
    if not rows:
        bot.send_message(message.chat.id,f"💼 <b>Портфель пуст</b>\n\n💰 Баланс: <b>{float(balance):,.2f} ₽</b>\n\nПопробуй /buy BERL 5"); return
    lines=[f"💼 <b>Твой портфель</b>\n💰 Баланс: <b>{float(balance):,.2f} ₽</b>\n"]
    for r in rows:
        value=r["shares"]*r["price"]; pnl=(r["price"]-r["avg_price"])*r["shares"]
        lines.append(f"• <b>{r['ticker']}</b> — {r['shares']:,} шт. × {r['price']:,.2f} ₽ = {value:,.2f} ₽\n  P/L: {pnl:+,.2f} ₽")
    bot.send_message(message.chat.id,"\n".join(lines))


def _ex_parse_trade(message):
    parts=(message.text or "").split()
    if len(parts)!=3 or not parts[2].isdigit(): bot.send_message(message.chat.id,"❓ Формат: <code>/buy BERL 5</code>"); return None
    ticker=parts[1].upper(); shares=int(parts[2])
    if shares<=0 or shares>EXCHANGE_MAX_TRADE: bot.send_message(message.chat.id,f"❌ Количество должно быть от 1 до {EXCHANGE_MAX_TRADE}."); return None
    return ticker,shares


def _ex_sync_control(conn, exchange_id: int) -> tuple[int, float, int | None, float]:
    """Пересчитывает контроль компании атомарно. >50% = контролирующий акционер."""
    ex = conn.execute("SELECT owner_id,total_shares FROM ex_exchanges WHERE id=?", (exchange_id,)).fetchone()
    if not ex:
        return 0, 0.0, None, 0.0
    top = conn.execute(
        "SELECT user_id,shares FROM ex_holdings WHERE exchange_id=? AND shares>0 ORDER BY shares DESC,user_id ASC LIMIT 1",
        (exchange_id,),
    ).fetchone()
    old_owner = int(ex["owner_id"] or 0)
    if not top:
        new_owner, pct = 0, 0.0
    else:
        pct = int(top["shares"]) / max(1, int(ex["total_shares"])) * 100.0
        new_owner = int(top["user_id"]) if pct > EXCHANGE_MIN_CONTROL_PCT else 0
    if new_owner != old_owner:
        conn.execute(
            "UPDATE ex_exchanges SET owner_id=?,control_changed_at=?,acquisitions=acquisitions+? WHERE id=?",
            (new_owner, datetime.now().isoformat(timespec="seconds"), 1 if new_owner and old_owner and new_owner != old_owner else 0, exchange_id),
        )
    return new_owner, pct, int(top["user_id"]) if top else None, float(pct)


def _ex_control_state(conn, exchange_id: int, chat_id: int) -> tuple[int, float, str]:
    owner, pct, top_uid, _ = _ex_sync_control(conn, exchange_id)
    uid = owner or (top_uid or 0)
    label = _ex_user_mention(chat_id, uid) if uid else "Никто"
    return owner, pct, label


def _ex_reconcile_all_controls() -> None:
    conn=_ex_db()
    conn.execute("BEGIN IMMEDIATE")
    rows=conn.execute("SELECT id FROM ex_exchanges WHERE owner_id!=0 OR founder_id!=0").fetchall()
    for r in rows:
        _ex_sync_control(conn,int(r["id"]))
    conn.commit(); conn.close()


_ex_reconcile_all_controls()


@bot.message_handler(commands=["buy", "купить"])
def handle_exchange_buy(message: Message) -> None:
    parsed = _ex_parse_trade(message)
    if not parsed: return
    ticker, shares = parsed; user_id = message.from_user.id
    wait = _ex_cd(_ex_trade_cooldown, user_id, EXCHANGE_TRADE_COOLDOWN)
    if wait:
        bot.send_message(message.chat.id, f"⏳ Слишком быстро. Следующая сделка через <b>{wait} сек.</b>"); return
    _ex_ensure_trader(user_id)
    conn = _ex_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ex = conn.execute("SELECT * FROM ex_exchanges WHERE ticker=?", (ticker,)).fetchone()
        if not ex: raise ValueError("❌ Такой акции нет. Посмотри /компании")
        if not ex["active"]: raise ValueError("🔴 Эта компания закрыта, новые сделки недоступны.")
        available = int(ex["treasury_shares"])
        if available < shares: raise ValueError(f"❌ На рынке только <b>{available:,}</b> свободных акций <code>{ticker}</code>.")
        price = float(ex["price"])
        impact = _ex_trade_impact(shares, available)
        execution_price = max(1.0, price * (1.0 + impact))
        total = execution_price * shares
        fee = total * EXCHANGE_TRADE_FEE
        need = total + fee
        trader = conn.execute("SELECT balance FROM ex_traders WHERE user_id=?", (user_id,)).fetchone()
        if not trader or float(trader["balance"]) < need:
            raise ValueError(f"❌ Недостаточно денег. Нужно <b>{need:,.2f} ₽</b> с учётом комиссии <b>{fee:,.2f} ₽</b>.")
        holding = conn.execute("SELECT shares,avg_price FROM ex_holdings WHERE user_id=? AND exchange_id=?", (user_id, ex["id"])).fetchone()
        old_shares = int(holding["shares"]) if holding else 0
        old_avg = float(holding["avg_price"]) if holding else 0.0
        new_shares = old_shares + shares
        new_avg = ((old_shares * old_avg) + (execution_price * shares)) / new_shares
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute("UPDATE ex_traders SET balance=balance-?,reputation=MIN(100,reputation+1),last_trade=? WHERE user_id=?", (need, now, user_id))
        # Главный анти-дюп: стоимость купленных акций уходит в резерв компании.
        owner_share = fee * EXCHANGE_OWNER_FEE_SHARE
        company_share = fee - owner_share
        old_controller = int(ex["owner_id"] or 0)
        if old_controller:
            conn.execute("UPDATE ex_traders SET balance=balance+? WHERE user_id=?", (owner_share, old_controller))
        conn.execute("UPDATE ex_exchanges SET market_cash=market_cash+?,treasury=treasury+?,trade_volume=trade_volume+?,last_activity=? WHERE id=?", (total, company_share, total, now, ex["id"]))
        conn.execute("INSERT INTO ex_holdings(user_id,exchange_id,shares,avg_price,locked_shares) VALUES(?,?,?,?,0) ON CONFLICT(user_id,exchange_id) DO UPDATE SET shares=excluded.shares,avg_price=excluded.avg_price", (user_id, ex["id"], new_shares, new_avg))
        conn.execute("UPDATE ex_exchanges SET treasury_shares=treasury_shares-? WHERE id=?", (shares, ex["id"]))
        new_price, change = _ex_change_price(conn, ex["id"], impact)
        conn.execute("INSERT INTO ex_transactions(user_id,exchange_id,action,shares,price,total,ts) VALUES(?,?,?,?,?,?,?)", (user_id, ex["id"], "BUY", shares, execution_price, total, now))
        new_owner, pct, control_label = _ex_control_state(conn, ex["id"], message.chat.id)
        conn.commit()
    except ValueError as exc:
        conn.rollback(); conn.close(); bot.send_message(message.chat.id, str(exc)); return
    except Exception:
        conn.rollback(); conn.close(); logger.exception("Exchange buy error"); bot.send_message(message.chat.id, "❌ Сделка отменена из-за технической ошибки."); return
    conn.close()
    control_note = ""
    if new_owner and new_owner == user_id and int(ex["owner_id"] or 0) != user_id:
        control_note = f"\n\n👑 <b>Ты получил контроль над {html.escape(ticker)}!</b> Доля: <b>{pct:.2f}%</b>"
    bot.send_message(message.chat.id, f"📈 <b>Покупка исполнена</b>\n\n🪙 {shares:,} × {ticker}\n💵 Цена исполнения: {execution_price:,.2f} ₽\n💸 Сумма: {total:,.2f} ₽\n📊 Новый курс: <b>{new_price:,.2f} ₽</b> ({change:+.2f}%)\n🎯 Контроль: {new_owner if new_owner else 'нет'} — {pct:.2f}%{control_note}")
    try: _ex_replace_market(message.chat.id, user_id, ex["id"])
    except Exception as exc: logger.exception("Exchange chart error: %s", exc)


@bot.message_handler(commands=["sell", "продать"])
def handle_exchange_sell(message: Message) -> None:
    parsed = _ex_parse_trade(message)
    if not parsed: return
    ticker, shares = parsed; user_id = message.from_user.id
    wait = _ex_cd(_ex_trade_cooldown, user_id, EXCHANGE_TRADE_COOLDOWN)
    if wait:
        bot.send_message(message.chat.id, f"⏳ Слишком быстро. Следующая сделка через <b>{wait} сек.</b>"); return
    _ex_ensure_trader(user_id)
    conn = _ex_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ex = conn.execute("SELECT * FROM ex_exchanges WHERE ticker=?", (ticker,)).fetchone()
        if not ex: raise ValueError("❌ Такой акции нет. Посмотри /компании")
        if not ex["active"]: raise ValueError("🔴 Эта компания закрыта, новые сделки недоступны.")
        holding = conn.execute("SELECT shares,avg_price FROM ex_holdings WHERE user_id=? AND exchange_id=?", (user_id, ex["id"])).fetchone()
        if not holding or int(holding["shares"]) < shares: raise ValueError("❌ У тебя недостаточно этих акций.")
        price = float(ex["price"])
        treasury_before = int(ex["treasury_shares"])
        impact = _ex_trade_impact(shares, max(10_000, treasury_before + shares))
        execution_price = max(1.0, price * (1.0 - impact))
        gross = execution_price * shares
        fee = gross * EXCHANGE_TRADE_FEE
        payout = gross - fee
        market_cash = float(ex["market_cash"] or 0.0)
        if market_cash + 1e-9 < gross:
            raise ValueError(f"🏦 <b>Недостаточно ликвидности.</b> Компания может выкупить сейчас только на <b>{market_cash:,.2f} ₽</b>. Попробуй меньший пакет или дождись покупок других игроков.")
        remaining = int(holding["shares"]) - shares
        now = datetime.now().isoformat(timespec="seconds")
        owner_share = fee * EXCHANGE_OWNER_FEE_SHARE
        company_share = fee - owner_share
        controller = int(ex["owner_id"] or 0)
        if controller:
            conn.execute("UPDATE ex_traders SET balance=balance+? WHERE user_id=?", (owner_share, controller))
        conn.execute("UPDATE ex_traders SET balance=balance+?,reputation=MAX(0,reputation-1),last_trade=? WHERE user_id=?", (payout, now, user_id))
        conn.execute("UPDATE ex_exchanges SET market_cash=market_cash-?,treasury=treasury+?,trade_volume=trade_volume+?,treasury_shares=treasury_shares+?,last_activity=? WHERE id=?", (gross, company_share, gross, shares, now, ex["id"]))
        if remaining:
            conn.execute("UPDATE ex_holdings SET shares=? WHERE user_id=? AND exchange_id=?", (remaining, user_id, ex["id"]))
        else:
            conn.execute("DELETE FROM ex_holdings WHERE user_id=? AND exchange_id=?", (user_id, ex["id"]))
        new_price, change = _ex_change_price(conn, ex["id"], -impact)
        conn.execute("INSERT INTO ex_transactions(user_id,exchange_id,action,shares,price,total,ts) VALUES(?,?,?,?,?,?,?)", (user_id, ex["id"], "SELL", shares, execution_price, payout, now))
        old_controller = int(ex["owner_id"] or 0)
        new_owner, pct, control_label = _ex_control_state(conn, ex["id"], message.chat.id)
        conn.commit()
    except ValueError as exc:
        conn.rollback(); conn.close(); bot.send_message(message.chat.id, str(exc)); return
    except Exception:
        conn.rollback(); conn.close(); logger.exception("Exchange sell error"); bot.send_message(message.chat.id, "❌ Сделка отменена из-за технической ошибки."); return
    conn.close()
    control_note = ""
    if old_controller != new_owner:
        if new_owner:
            control_note = f"\n\n👑 <b>Контроль перешёл к новому акционеру.</b> Доля: <b>{pct:.2f}%</b>"
        else:
            control_note = "\n\n⚖️ <b>Контроль потерян:</b> ни у кого нет пакета больше 50%."
    bot.send_message(message.chat.id, f"📉 <b>Продажа исполнена</b>\n\n🪙 {shares:,} × {ticker}\n💵 Цена исполнения: {execution_price:,.2f} ₽\n💰 Получено: {payout:,.2f} ₽\n🏦 Остаток ликвидности компании: <b>{market_cash-gross:,.2f} ₽</b>\n📊 Новый курс: <b>{new_price:,.2f} ₽</b> ({change:+.2f}%){control_note}")
    try: _ex_replace_market(message.chat.id, user_id, ex["id"])
    except Exception as exc: logger.exception("Exchange chart error: %s", exc)


@bot.message_handler(commands=["create_exchange", "создать_компанию"])
def handle_create_exchange(message: Message) -> None:
    parts=(message.text or "").split()
    if len(parts)<3:
        bot.send_message(message.chat.id,"❓ Формат: <code>/создать_компанию Название TICKER</code>"); return
    ticker=parts[-1].upper(); name=" ".join(parts[1:-1]).strip()
    if not name or not ticker.isalnum() or not 2<=len(ticker)<=8 or not ticker.isascii():
        bot.send_message(message.chat.id,"❌ Название или тикер указаны неправильно. Тикер: 2–8 латинских символов/цифр."); return
    conn=_ex_db()
    exists=conn.execute("SELECT id FROM ex_exchanges WHERE ticker=?",(ticker,)).fetchone()
    if exists:
        conn.close(); bot.send_message(message.chat.id,"❌ Такой тикер уже занят."); return
    company_count=int(conn.execute("SELECT COUNT(*) FROM ex_exchanges WHERE founder_id=? AND active=1",(message.from_user.id,)).fetchone()[0])
    if company_count>=3:
        conn.close(); bot.send_message(message.chat.id,"❌ У тебя уже 3 активные компании. Сначала закрой одну из них."); return
    create_cost=EXCHANGE_CREATE_BASE_COST*(5**company_count)
    trader=conn.execute("SELECT balance FROM ex_traders WHERE user_id=?",(message.from_user.id,)).fetchone()
    if not trader:
        conn.execute("INSERT INTO ex_traders(user_id,balance) VALUES(?,?)",(message.from_user.id,EXCHANGE_START_BALANCE)); balance=EXCHANGE_START_BALANCE
    else: balance=float(trader["balance"])
    if balance<create_cost:
        conn.close(); bot.send_message(message.chat.id,f"💸 <b>Недостаточно капитала для запуска компании.</b>\n\nСтоимость запуска: <b>{create_cost:,.0f} ₽</b>\nТвой баланс: <b>{balance:,.0f} ₽</b>\n\nЭти деньги не исчезают: они становятся стартовой ликвидностью компании."); return
    now=datetime.now().isoformat(timespec="seconds")
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Защита от гонки при двух одновременных командах.
        if conn.execute("SELECT 1 FROM ex_exchanges WHERE ticker=?",(ticker,)).fetchone():
            raise ValueError("❌ Такой тикер уже занят.")
        inside_count=int(conn.execute("SELECT COUNT(*) FROM ex_exchanges WHERE founder_id=? AND active=1",(message.from_user.id,)).fetchone()[0])
        if inside_count>=3:
            raise ValueError("❌ У тебя уже 3 активные компании.")
        create_cost=EXCHANGE_CREATE_BASE_COST*(5**inside_count)
        cur_debit=conn.execute("UPDATE ex_traders SET balance=balance-? WHERE user_id=? AND balance>=?",(create_cost,message.from_user.id,create_cost))
        if cur_debit.rowcount!=1:
            raise ValueError("❌ Недостаточно денег для создания компании.")
        cur=conn.execute("INSERT INTO ex_exchanges(owner_id,founder_id,name,ticker,price,created_at,total_shares,treasury_shares,description,active,treasury,market_cash,last_activity) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(message.from_user.id,message.from_user.id,name,ticker,EXCHANGE_DEFAULT_PRICE,now,EXCHANGE_DEFAULT_TOTAL_SHARES,EXCHANGE_FLOAT_SHARES,f"Компания {name}.",1,0.0,create_cost,now))
        exchange_id=cur.lastrowid
        conn.execute("INSERT INTO ex_holdings(user_id,exchange_id,shares,avg_price,locked_shares) VALUES(?,?,?,?,0)",(message.from_user.id,exchange_id,EXCHANGE_OWNER_SHARES,EXCHANGE_DEFAULT_PRICE))
        conn.execute("INSERT INTO ex_history(exchange_id,price,ts) VALUES(?,?,?)",(exchange_id,EXCHANGE_DEFAULT_PRICE,now))
        conn.commit()
    except ValueError as exc:
        conn.rollback(); conn.close(); bot.send_message(message.chat.id,str(exc)); return
    except Exception:
        conn.rollback(); conn.close(); logger.exception("Company creation error"); bot.send_message(message.chat.id,"❌ Компания не создана из-за технической ошибки."); return
    conn.close()
    bot.send_message(message.chat.id,f"🏭 <b>Компания зарегистрирована!</b>\n\n🏢 {html.escape(name)}\n📈 Тикер: <code>{ticker}</code>\n\n💸 Стоимость запуска: <b>{create_cost:,.0f} ₽</b>\n🏦 Стартовая ликвидность: <b>{create_cost:,.0f} ₽</b>\n📦 Акций: <b>1 000 000</b>\n👑 Основатель: <b>700 000 — 70.00%</b>\n🛒 Свободный рынок: <b>300 000 — 30.00%</b>\n\n💡 Ликвидность компании оплачивает будущие продажи акций — деньги из воздуха больше не создаются.\n\nОткрыть профиль: /компания {ticker}")


@bot.message_handler(commands=["company", "компания"])
def handle_company(message: Message) -> None:
    parts=(message.text or "").split(maxsplit=1)
    if len(parts)<2: bot.send_message(message.chat.id,"❓ Формат: <code>/company BERL</code>"); return
    conn=_ex_db(); ex=_ex_company_row(conn,parts[1].strip())
    if not ex: conn.close(); bot.send_message(message.chat.id,"❌ Компания не найдена."); return
    top=conn.execute("SELECT user_id,shares FROM ex_holdings WHERE exchange_id=? AND shares>0 ORDER BY shares DESC LIMIT 1",(ex["id"],)).fetchone(); conn.close()
    path=_ex_chart(ex["id"])
    owner=_ex_user_mention(message.chat.id,int(ex["owner_id"]),"Рынок") if int(ex["owner_id"]) else "Berlions"
    control=_ex_user_mention(message.chat.id,int(top["user_id"]),"Никто") if top else "Никто"
    control_pct=(int(top["shares"])/max(1,int(ex["total_shares"]))*100) if top else 0
    founder=_ex_user_mention(message.chat.id,int(ex['founder_id'] or ex['owner_id']),"Основатель") if int(ex['founder_id'] or ex['owner_id']) else "Berlions"
    text=(f"🏢 <b>{html.escape(ex['name'])}</b>  <code>${html.escape(ex['ticker'])}</code>\n\n"
          f"📝 {html.escape(ex['description'] or 'Описание отсутствует.')}\n\n"
          f"💰 Цена: <b>{float(ex['price']):,.2f} ₽</b>\n📊 Капитализация: <b>{float(ex['price'])*int(ex['total_shares']):,.2f} ₽</b>\n"
          f"📦 Акций: <b>{int(ex['total_shares']):,}</b>\n🛒 В продаже: <b>{int(ex['treasury_shares']):,}</b> · {int(ex['treasury_shares'])/max(1,int(ex['total_shares']))*100:.2f}%\n🏦 Казна: <b>{float(ex['treasury']):,.0f} ₽</b>\n💧 Ликвидность: <b>{float(ex['market_cash'] or 0):,.0f} ₽</b>\n⭐ Уровень: <b>{int(ex['level'])}</b> · 🔥 Популярность: <b>{int(ex['popularity'])}/100</b>\n👑 Контролирующий акционер: {owner}\n🎯 Крупнейший пакет: {control} — <b>{control_pct:.2f}%</b>\n👤 Основатель: {founder}\n"
          f"{'🟢 Торги открыты' if ex['active'] else '🔴 Торги закрыты'}\n\n"
          f"Покупка: <code>/buy {ex['ticker']} 100</code>\nПродажа: <code>/sell {ex['ticker']} 100</code>\nДержатели: <code>/holders {ex['ticker']}</code>")
    with open(path,"rb") as photo: bot.send_photo(message.chat.id,photo,caption=text)


@bot.message_handler(commands=["companies", "exchanges", "компании"])
def handle_exchanges(message: Message) -> None:
    conn=_ex_db(); rows=conn.execute("SELECT * FROM ex_exchanges WHERE active=1 ORDER BY id DESC LIMIT 30").fetchall(); conn.close()
    if not rows: bot.send_message(message.chat.id,"🏢 Активных компаний пока нет."); return
    lines=["🏢 <b>Все компании</b>\n"]
    for r in rows:
        lines.append(f"• <code>{r['ticker']}</code> — <b>{html.escape(r['name'])}</b> · {float(r['price']):,.2f} ₽\n  Акций в рынке: {int(r['treasury_shares']):,}")
    lines.append("\nОткрыть компанию: <code>/company TICKER</code>")
    bot.send_message(message.chat.id,"\n".join(lines))


@bot.message_handler(commands=["holders", "держатели"])
def handle_holders(message: Message) -> None:
    parts=(message.text or "").split(maxsplit=1)
    if len(parts)<2: bot.send_message(message.chat.id,"❓ Формат: <code>/holders BERL</code>"); return
    conn=_ex_db(); ex=_ex_company_row(conn,parts[1].strip())
    if not ex: conn.close(); bot.send_message(message.chat.id,"❌ Компания не найдена."); return
    holders=conn.execute("SELECT user_id,shares,avg_price FROM ex_holdings WHERE exchange_id=? AND shares>0 ORDER BY shares DESC LIMIT 10",(ex["id"],)).fetchall(); conn.close()
    total_shares=int(ex['total_shares'])
    lines=[f"👥 <b>Крупнейшие держатели {html.escape(ex['ticker'])}</b>",f"📦 Всего акций: <b>{total_shares:,}</b>",""]
    for i,h in enumerate(holders,1):
        shares=int(h['shares']); pct=shares/max(1,total_shares)*100
        value=shares*float(ex['price'])
        lines.append(f"{i}. {_ex_user_mention(message.chat.id,int(h['user_id']))} — <b>{shares:,}</b> акций · <b>{pct:.2f}%</b>\n   💰 Рыночная стоимость: {value:,.0f} ₽")
    lines.append(f"\n🛒 Свободно на рынке: <b>{int(ex['treasury_shares']):,}</b> · {int(ex['treasury_shares'])/max(1,total_shares)*100:.2f}%")
    bot.send_message(message.chat.id,"\n".join(lines))


@bot.message_handler(commands=["удалить_компанию"])
def handle_delete_company(message: Message) -> None:
    parts=(message.text or "").split(maxsplit=1)
    if len(parts)<2:
        bot.send_message(message.chat.id,"❓ Формат: <code>/удалить_компанию TICKER</code>"); return
    ticker=parts[1].strip().upper(); conn=_ex_db(); ex=conn.execute("SELECT * FROM ex_exchanges WHERE ticker=?",(ticker,)).fetchone()
    if not ex: conn.close(); bot.send_message(message.chat.id,"❌ Компания не найдена."); return
    if int(ex["owner_id"])!=message.from_user.id and not _is_admin(message.from_user.id):
        conn.close(); bot.send_message(message.chat.id,"⛔ Удалить компанию может только её владелец или администратор."); return
    conn.execute("UPDATE ex_exchanges SET active=0 WHERE id=?",(ex["id"],)); conn.commit(); conn.close()
    bot.send_message(message.chat.id,f"🗑 Компания <code>{ticker}</code> закрыта и убрана из активного списка. История и акции сохранены.")


@bot.message_handler(commands=["company_manage", "управление_компанией"])
def handle_company_manage(message: Message) -> None:
    parts=(message.text or "").split(maxsplit=2)
    if len(parts)<3:
        bot.send_message(message.chat.id,"⚙️ <b>Управление компанией</b>\n\n<code>/управление_компанией TICKER описание Новый текст</code>\n<code>/управление_компанией TICKER выпуск 100000</code>\n<code>/управление_компанией TICKER закрыть</code>\n<code>/управление_компанией TICKER бонус 100000</code>")
        return
    ticker,action=parts[1].upper(),parts[2].strip(); conn=_ex_db(); ex=conn.execute("SELECT * FROM ex_exchanges WHERE ticker=?",(ticker,)).fetchone()
    if not ex: conn.close(); bot.send_message(message.chat.id,"❌ Компания не найдена."); return
    if int(ex["owner_id"] or 0)!=message.from_user.id:
        conn.close(); bot.send_message(message.chat.id,"⛔ Управлять компанией может только контролирующий акционер с долей больше 50%."); return
    low=action.lower()
    if low.startswith("описание "):
        desc=action.split(" ",1)[1].strip()[:500]; conn.execute("UPDATE ex_exchanges SET description=? WHERE id=?",(desc,ex["id"])); conn.commit(); conn.close(); bot.send_message(message.chat.id,"✅ Описание компании обновлено."); return
    if low in ("close","закрыть"):
        conn.execute("UPDATE ex_exchanges SET active=0 WHERE id=?",(ex["id"],)); conn.commit(); conn.close(); bot.send_message(message.chat.id,f"🔴 Компания <code>{ticker}</code> закрыта. История и акции сохранены."); return
    if low.startswith(("issue ","выпуск ")):
        raw=low.split(" ",1)[1]
        if not raw.isdigit(): conn.close(); bot.send_message(message.chat.id,"❌ Укажи целое количество акций."); return
        amount=int(raw); current_total=int(ex['total_shares'])
        if amount<=0 or amount>1_000_000 or current_total+amount>EXCHANGE_MAX_TOTAL_SHARES:
            conn.close(); bot.send_message(message.chat.id,f"❌ Выпуск: 1–1 000 000 акций за раз, общий лимит — {EXCHANGE_MAX_TOTAL_SHARES:,}."); return
        fee=max(10_000.0,float(ex['price'])*amount*0.005)
        if float(ex['treasury'])<fee:
            conn.close(); bot.send_message(message.chat.id,f"❌ Выпуск требует {fee:,.0f} ₽ из казны компании."); return
        conn.execute("UPDATE ex_exchanges SET total_shares=total_shares+?,treasury_shares=treasury_shares+?,treasury=treasury-?,last_activity=? WHERE id=?",(amount,amount,fee,datetime.now().isoformat(timespec='seconds'),ex['id'])); conn.commit(); conn.close(); bot.send_message(message.chat.id,f"📦 Выпущено <b>{amount:,}</b> акций <code>{ticker}</code>.\n💸 Комиссия из казны: <b>{fee:,.0f} ₽</b>\n📊 Новое количество: <b>{current_total+amount:,}</b>. Доли старых акционеров уменьшились пропорционально."); return
    if low.startswith(("бонус ","премия ")):
        raw=low.split(" ",1)[1]
        if not raw.isdigit() or int(raw)<=0: conn.close(); bot.send_message(message.chat.id,"❌ Укажи сумму бонуса."); return
        amount=float(raw)
        if float(ex['treasury'])<amount: conn.close(); bot.send_message(message.chat.id,"❌ В казне недостаточно денег."); return
        conn.execute("UPDATE ex_exchanges SET treasury=treasury-?,last_activity=? WHERE id=?",(amount,datetime.now().isoformat(timespec='seconds'),ex['id'])); conn.execute("UPDATE ex_traders SET balance=balance+? WHERE user_id=?",(amount,message.from_user.id)); conn.commit(); conn.close(); bot.send_message(message.chat.id,f"💰 Контролирующий акционер получил из казны компании <b>{amount:,.0f} ₽</b>. Это расход компании, а не создание денег."); return
    conn.close(); bot.send_message(message.chat.id,"❓ Доступно: <code>описание</code>, <code>выпуск</code>, <code>закрыть</code>, <code>бонус</code>.")


@bot.message_handler(commands=["topmoney", "toptraders", "топденьги", "топтрейдеров"])
def handle_exchange_top(message: Message) -> None:
    conn=_ex_db(); traders=conn.execute("""
        SELECT t.user_id,t.balance FROM ex_traders t
        WHERE EXISTS (SELECT 1 FROM social_users su WHERE su.chat_id=? AND su.user_id=t.user_id)
    """, (message.chat.id,)).fetchall(); result=[]
    for trader in traders:
        holdings=conn.execute("SELECT h.shares,e.price FROM ex_holdings h JOIN ex_exchanges e ON e.id=h.exchange_id WHERE h.user_id=?",(trader["user_id"],)).fetchall()
        total=float(trader["balance"])+sum(int(h["shares"])*float(h["price"]) for h in holdings); result.append((total,int(trader["user_id"])))
    conn.close(); result.sort(reverse=True); lines=["🏆 <b>TOP TRADERS</b>\n"]
    for i,(total,uid) in enumerate(result[:20],1): lines.append(f"{i}. {_ex_user_mention(message.chat.id,uid)} — <b>{total:,.2f} ₽</b>")
    bot.send_message(message.chat.id,"\n".join(lines) if len(lines)>1 else "🏆 Пока рейтинг пуст.")


@bot.message_handler(commands=["работа", "work"])
def handle_exchange_work(message: Message) -> None:
    user_id = message.from_user.id
    _ex_ensure_trader(user_id)
    conn = _ex_db()
    row = conn.execute("SELECT balance,last_work FROM ex_traders WHERE user_id=?", (user_id,)).fetchone()
    now = datetime.now()
    if row["last_work"]:
        try:
            elapsed = (now - datetime.fromisoformat(row["last_work"])).total_seconds()
            if elapsed < 1800:
                mins = max(1, int((1800-elapsed + 59)//60))
                conn.close()
                bot.send_message(message.chat.id, f"⏳ Ты уже работал. Следующая смена через <b>{mins} мин.</b>")
                return
        except Exception:
            pass
    reward = random.randint(500, 3000)
    new_balance = float(row["balance"]) + reward
    conn.execute("UPDATE ex_traders SET balance=?,last_work=? WHERE user_id=?", (new_balance, now.isoformat(timespec="seconds"), user_id))
    conn.commit(); conn.close()
    jobs = ["отработал смену", "выполнил заказ", "подзаработал", "закрыл рабочий день"]
    bot.send_message(message.chat.id, f"💼 <b>{random.choice(jobs).capitalize()}!</b>\n\n💰 Получено: <b>+{reward:,} ₽</b>\n💳 Баланс: <b>{new_balance:,.2f} ₽</b>\n\n⏱ Следующая работа — через 30 минут.")


def _money_target(message: Message, arg: str = "") -> int | None:
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id
    target = _social_target(message, arg) if "_social_target" in globals() else None
    if target:
        return target
    token = (arg or "").split()[0] if (arg or "").split() else ""
    if token.startswith("@"):
        try:
            member = bot.get_chat_member(message.chat.id, token)
            return member.user.id
        except Exception:
            return None
    return None


@bot.message_handler(commands=["забрать"])
def handle_admin_take_money(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "⛔ Только администратор может использовать эту команду.")
        return
    parts = (message.text or "").split(maxsplit=2)
    if message.reply_to_message:
        target = message.reply_to_message.from_user.id
        amount_raw = parts[1] if len(parts) > 1 else ""
    else:
        target = _social_target(message, parts[1]) if len(parts) > 1 and "_social_target" in globals() else None
        amount_raw = parts[2] if len(parts) > 2 else ""
    if not target or not amount_raw.isdigit():
        bot.send_message(message.chat.id, "❓ Формат: <code>/забрать @user 1000</code> или ответом: <code>/забрать 1000</code>")
        return
    amount = int(amount_raw)
    if amount <= 0:
        bot.send_message(message.chat.id, "❌ Сумма должна быть больше нуля.")
        return
    _ex_ensure_trader(target)
    conn = _ex_db()
    row = conn.execute("SELECT balance FROM ex_traders WHERE user_id=?", (target,)).fetchone()
    old = float(row["balance"])
    taken = min(old, float(amount))
    new = old - taken
    conn.execute("UPDATE ex_traders SET balance=? WHERE user_id=?", (new, target))
    conn.commit(); conn.close()
    bot.send_message(message.chat.id, f"💸 У {_ex_user_mention(message.chat.id,target)} забрано <b>{taken:,.2f} ₽</b>.\n💰 Новый баланс: <b>{new:,.2f} ₽</b>")


@bot.message_handler(commands=["выдать"])
def handle_admin_give_money(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "⛔ Только администратор может использовать эту команду.")
        return
    parts=(message.text or "").split()
    if len(parts)!=2 or not parts[1].isdigit() or int(parts[1])<=0:
        bot.send_message(message.chat.id, "❓ Формат: <code>/выдать 100000</code>")
        return
    amount=int(parts[1]); uid=message.from_user.id; _ex_ensure_trader(uid)
    conn=_ex_db(); row=conn.execute("SELECT balance FROM ex_traders WHERE user_id=?",(uid,)).fetchone(); new=float(row["balance"])+amount
    conn.execute("UPDATE ex_traders SET balance=? WHERE user_id=?",(new,uid)); conn.commit(); conn.close()
    bot.send_message(message.chat.id,f"💰 Тебе выдано <b>+{amount:,} ₽</b>.\nБаланс: <b>{new:,.2f} ₽</b>")


# Этот callback расположен ДО общего callback-хендлера ниже.
@bot.callback_query_handler(func=lambda call: bool(call.data and call.data.startswith("ex_")))
def handle_exchange_callback(call: CallbackQuery) -> None:
    user_id=call.from_user.id; chat_id=call.message.chat.id if call.message else user_id
    try:
        bot.answer_callback_query(call.id)
        if call.data=="ex_refresh": _ex_replace_market(chat_id,user_id,1)
        elif call.data=="ex_portfolio": handle_exchange_portfolio(call.message)
        elif call.data=="ex_bonus": handle_exchange_bonus(call.message)
    except Exception as exc:
        logger.exception("Exchange callback error: %s",exc)
        try: bot.answer_callback_query(call.id,"❌ Ошибка биржи",show_alert=True)
        except Exception: pass

# ── END BERLIONS EXCHANGE SYSTEM ───────────────────────────────────────────────


# ── ADVANCED ECONOMY ───────────────────────────────────────────────────────────

def _ex_money_fmt(v: float) -> str: return f"{v:,.0f} ₽"

def _ex_holder_control(conn, exchange_id: int):
    return conn.execute("SELECT user_id,shares FROM ex_holdings WHERE exchange_id=? AND shares>0 ORDER BY shares DESC LIMIT 1",(exchange_id,)).fetchone()

@bot.message_handler(commands=["банк"])
def handle_ex_bank(message: Message) -> None:
    uid=message.from_user.id; _ex_ensure_trader(uid); conn=_ex_db(); dep=conn.execute("SELECT * FROM ex_deposits WHERE user_id=?",(uid,)).fetchone(); loan=conn.execute("SELECT * FROM ex_loans WHERE user_id=?",(uid,)).fetchone(); conn.close()
    d="нет" if not dep else f"{_ex_money_fmt(dep['principal'])} · 2%/день"; l="нет" if not loan else f"{_ex_money_fmt(loan['debt'])}"
    bot.send_message(message.chat.id,f"🏦 <b>БЕРЛИОНС БАНК</b>\n\n💰 Вклад: <b>{d}</b>\n💳 Долг: <b>{l}</b>\n\n/вклад 50000\n/забрать_вклад\n/кредит 100000\n/погасить_кредит 100000")

@bot.message_handler(commands=["вклад"])
def handle_ex_deposit(message: Message) -> None:
    p=(message.text or '').split(); uid=message.from_user.id; _ex_ensure_trader(uid)
    if len(p)!=2 or not p[1].isdigit() or int(p[1])<EXCHANGE_MIN_DEPOSIT: bot.send_message(message.chat.id,f"❓ Формат: <code>/вклад 50000</code>. Минимум {EXCHANGE_MIN_DEPOSIT:,} ₽"); return
    amount=float(p[1]); conn=_ex_db(); dep=conn.execute("SELECT * FROM ex_deposits WHERE user_id=?",(uid,)).fetchone(); bal=conn.execute("SELECT balance FROM ex_traders WHERE user_id=?",(uid,))["balance"]
    if dep: conn.close(); bot.send_message(message.chat.id,"❌ У тебя уже есть вклад."); return
    if bal<amount: conn.close(); bot.send_message(message.chat.id,"❌ Недостаточно денег."); return
    now=datetime.now().isoformat(timespec='seconds'); conn.execute("UPDATE ex_traders SET balance=balance-? WHERE user_id=?",(amount,uid)); conn.execute("INSERT INTO ex_deposits VALUES(?,?,?,?)",(uid,amount,now,now)); conn.commit(); conn.close(); bot.send_message(message.chat.id,f"🏦 Вклад открыт: <b>{_ex_money_fmt(amount)}</b> · 2% в день.")

@bot.message_handler(commands=["забрать_вклад"])
def handle_ex_deposit_withdraw(message: Message) -> None:
    uid=message.from_user.id; _ex_ensure_trader(uid); conn=_ex_db(); d=conn.execute("SELECT * FROM ex_deposits WHERE user_id=?",(uid,)).fetchone()
    if not d: conn.close(); bot.send_message(message.chat.id,"❌ Вклада нет."); return
    days=max(0,(datetime.now()-datetime.fromisoformat(d['started_at'])).total_seconds()/86400); profit=float(d['principal'])*EXCHANGE_DEPOSIT_RATE*days; total=float(d['principal'])+profit
    conn.execute("UPDATE ex_traders SET balance=balance+? WHERE user_id=?",(total,uid)); conn.execute("DELETE FROM ex_deposits WHERE user_id=?",(uid,)); conn.commit(); conn.close(); bot.send_message(message.chat.id,f"🏦 Вклад закрыт.\n💰 Возвращено: <b>{_ex_money_fmt(total)}</b>\n📈 Доход: <b>+{_ex_money_fmt(profit)}</b>")

@bot.message_handler(commands=["кредит"])
def handle_ex_loan(message: Message) -> None:
    p=(message.text or '').split(); uid=message.from_user.id; _ex_ensure_trader(uid)
    if len(p)!=2 or not p[1].isdigit() or int(p[1])<=0: bot.send_message(message.chat.id,"❓ Формат: <code>/кредит 100000</code>"); return
    amount=float(p[1]); conn=_ex_db(); loan=conn.execute("SELECT * FROM ex_loans WHERE user_id=?",(uid,)).fetchone()
    if loan: conn.close(); bot.send_message(message.chat.id,"❌ Сначала погаси текущий кредит."); return
    if amount>EXCHANGE_LOAN_LIMIT: conn.close(); bot.send_message(message.chat.id,f"❌ Лимит: {EXCHANGE_LOAN_LIMIT:,} ₽."); return
    debt=amount*(1+EXCHANGE_LOAN_RATE); conn.execute("INSERT INTO ex_loans VALUES(?,?,?,?)",(uid,amount,debt,datetime.now().isoformat(timespec='seconds'))); conn.execute("UPDATE ex_traders SET balance=balance+?,reputation=MAX(0,reputation-1) WHERE user_id=?",(amount,uid)); conn.commit(); conn.close(); bot.send_message(message.chat.id,f"💳 Получено: <b>{_ex_money_fmt(amount)}</b>\n📌 Вернуть: <b>{_ex_money_fmt(debt)}</b>")

@bot.message_handler(commands=["погасить_кредит"])
def handle_ex_loan_pay(message: Message) -> None:
    uid=message.from_user.id; _ex_ensure_trader(uid); conn=_ex_db(); loan=conn.execute("SELECT * FROM ex_loans WHERE user_id=?",(uid,)).fetchone(); bal=conn.execute("SELECT balance FROM ex_traders WHERE user_id=?",(uid,))["balance"]
    if not loan: conn.close(); bot.send_message(message.chat.id,"❌ Кредита нет."); return
    p=(message.text or '').split(); req=float(loan['debt']) if len(p)==1 else (float(p[1]) if p[1].replace('.','',1).isdigit() else 0); pay=min(req,float(bal),float(loan['debt']))
    if pay<=0: conn.close(); bot.send_message(message.chat.id,"❌ Недостаточно денег."); return
    debt=float(loan['debt'])-pay; conn.execute("UPDATE ex_traders SET balance=balance-? WHERE user_id=?",(pay,uid))
    if debt<=0.01: conn.execute("DELETE FROM ex_loans WHERE user_id=?",(uid,)); tail="🎉 Кредит погашен полностью!"
    else: conn.execute("UPDATE ex_loans SET debt=? WHERE user_id=?",(debt,uid)); tail=f"💳 Остаток: <b>{_ex_money_fmt(debt)}</b>"
    conn.commit(); conn.close(); bot.send_message(message.chat.id,f"💸 Платёж: <b>{_ex_money_fmt(pay)}</b>\n{tail}")

@bot.message_handler(commands=["репутация"])
def handle_ex_rep(message: Message) -> None:
    r=_ex_ensure_trader(message.from_user.id); bot.send_message(message.chat.id,f"🧠 <b>Репутация трейдера</b>\n\n⭐ <b>{int(r['reputation'])}/100</b>\n\nТорговая активность повышает рейтинг.")

@bot.message_handler(commands=["дивиденды"])
def handle_ex_dividends(message: Message) -> None:
    p=(message.text or '').split(); uid=message.from_user.id
    if len(p)!=3 or not p[2].isdigit() or int(p[2])<=0: bot.send_message(message.chat.id,"❓ Формат: <code>/дивиденды TICKER 100000</code>"); return
    ticker=p[1].upper(); amount=float(p[2]); conn=_ex_db(); ex=conn.execute("SELECT * FROM ex_exchanges WHERE ticker=?",(ticker,)).fetchone()
    if not ex: conn.close(); bot.send_message(message.chat.id,"❌ Компания не найдена."); return
    if int(ex['owner_id'])!=uid: conn.close(); bot.send_message(message.chat.id,"⛔ Только владелец."); return
    if float(ex['treasury'])<amount: conn.close(); bot.send_message(message.chat.id,"❌ В казне недостаточно денег."); return
    hs=conn.execute("SELECT user_id,shares FROM ex_holdings WHERE exchange_id=? AND shares>0",(ex['id'],)).fetchall(); total=sum(int(x['shares']) for x in hs)
    for h in hs:
        pay=amount*int(h['shares'])/total; conn.execute("INSERT OR IGNORE INTO ex_traders(user_id,balance) VALUES(?,?)",(int(h['user_id']),EXCHANGE_START_BALANCE)); conn.execute("UPDATE ex_traders SET balance=balance+? WHERE user_id=?",(pay,int(h['user_id'])))
    conn.execute("UPDATE ex_exchanges SET treasury=treasury-?,dividends_paid=dividends_paid+? WHERE id=?",(amount,amount,ex['id'])); conn.commit(); conn.close(); bot.send_message(message.chat.id,f"💎 <b>{ticker}</b> выплатила акционерам <b>{_ex_money_fmt(amount)}</b> дивидендов.")

@bot.message_handler(commands=["улучшить"])
def handle_ex_upgrade(message: Message) -> None:
    p=(message.text or '').split(maxsplit=2); uid=message.from_user.id
    if len(p)!=3: bot.send_message(message.chat.id,"❓ <code>/улучшить TICKER маркетинг</code>\nВарианты: маркетинг, разработка, производство"); return
    ticker,kind=p[1].upper(),p[2].lower(); costs={'маркетинг':100000,'разработка':250000,'производство':500000}
    if kind not in costs: bot.send_message(message.chat.id,"❌ Варианты: маркетинг, разработка, производство."); return
    conn=_ex_db(); ex=conn.execute("SELECT * FROM ex_exchanges WHERE ticker=?",(ticker,)).fetchone()
    if not ex: conn.close(); bot.send_message(message.chat.id,"❌ Компания не найдена."); return
    if int(ex['owner_id'])!=uid: conn.close(); bot.send_message(message.chat.id,"⛔ Только владелец."); return
    cost=costs[kind]*int(ex['level'])
    if float(ex['treasury'])<cost: conn.close(); bot.send_message(message.chat.id,f"❌ В казне нужно {_ex_money_fmt(cost)}."); return
    pop=min(100,int(ex['popularity'])+(8 if kind=='маркетинг' else 4)); lvl=int(ex['level'])+1; conn.execute("UPDATE ex_exchanges SET treasury=treasury-?,level=?,popularity=? WHERE id=?",(cost,lvl,pop,ex['id'])); conn.commit(); conn.close(); bot.send_message(message.chat.id,f"🚀 <b>{ticker}</b> улучшена!\n\n⚙️ {kind}\n⭐ Уровень: <b>{lvl}</b>\n🔥 Популярность: <b>{pop}/100</b>\n💸 Потрачено: <b>{cost:,} ₽</b>")

@bot.message_handler(commands=["топакций"])
def handle_ex_top_shares(message: Message) -> None:
    conn=_ex_db(); rows=conn.execute("""
        SELECT h.user_id, SUM(h.shares) AS shares, SUM(h.shares*e.price) AS value
        FROM ex_holdings h JOIN ex_exchanges e ON e.id=h.exchange_id
        WHERE h.shares>0 AND e.active=1
        GROUP BY h.user_id ORDER BY value DESC LIMIT 20
    """).fetchall(); conn.close()
    if not rows: bot.send_message(message.chat.id,"🏆 Пока никто не владеет акциями."); return
    lines=["🏆 <b>ТОП АКЦИОНЕРОВ</b>","<i>По текущей стоимости всех пакетов</i>",""]
    for i,r in enumerate(rows,1):
        lines.append(f"{i}. {_ex_user_mention(message.chat.id,int(r['user_id']))} — <b>{float(r['value']):,.0f} ₽</b> · {int(r['shares']):,} акций")
    bot.send_message(message.chat.id,"\n".join(lines))


@bot.message_handler(commands=["контроль"])
def handle_ex_control(message: Message) -> None:
    p=(message.text or '').split();
    if len(p)!=2: bot.send_message(message.chat.id,"❓ <code>/контроль TICKER</code>"); return
    conn=_ex_db(); ex=conn.execute("SELECT * FROM ex_exchanges WHERE ticker=?",(p[1].upper(),)).fetchone()
    if not ex: conn.close(); bot.send_message(message.chat.id,"❌ Компания не найдена."); return
    h=_ex_holder_control(conn,ex['id']); conn.close()
    if not h: bot.send_message(message.chat.id,"👥 Акционеров пока нет."); return
    pct=int(h['shares'])/max(1,int(ex['total_shares']))*100; state='👑 КОНТРОЛЬНЫЙ ПАКЕТ' if pct>=50 else '📊 Миноритарный пакет'
    bot.send_message(message.chat.id,f"🎯 <b>Контроль {html.escape(ex['ticker'])}</b>\n\n{_ex_user_mention(message.chat.id,int(h['user_id']))}\n📦 {int(h['shares']):,} акций · <b>{pct:.1f}%</b>\n\n{state}")




@bot.message_handler(commands=["поглощение", "поглотить", "takeover"])
def handle_ex_takeover(message: Message) -> None:
    p=(message.text or '').split(); uid=message.from_user.id
    if len(p)!=3:
        bot.send_message(message.chat.id,"🤝 <b>Поглощение компании</b>\n\nФормат: <code>/поглощение BUYER TARGET</code>\n\nПокупающая компания должна контролироваться тобой. Предложение уйдёт контролирующему акционеру цели с премией 20% к капитализации.")
        return
    buyer_t,target_t=p[1].upper(),p[2].upper(); conn=_ex_db(); buyer=conn.execute("SELECT * FROM ex_exchanges WHERE ticker=? AND active=1",(buyer_t,)).fetchone(); target=conn.execute("SELECT * FROM ex_exchanges WHERE ticker=? AND active=1",(target_t,)).fetchone()
    if not buyer or not target: conn.close(); bot.send_message(message.chat.id,"❌ Одна из компаний не найдена или закрыта."); return
    if buyer['id']==target['id']: conn.close(); bot.send_message(message.chat.id,"😄 Компания не может поглотить саму себя."); return
    if int(buyer['owner_id'] or 0)!=uid: conn.close(); bot.send_message(message.chat.id,"⛔ Для поглощения нужно контролировать покупающую компанию (>50% акций)."); return
    target_owner=int(target['owner_id'] or 0)
    if not target_owner: conn.close(); bot.send_message(message.chat.id,"⚖️ У цели сейчас нет акционера с >50%. Сначала рынок должен сформировать контрольный пакет."); return
    if target_owner==uid: conn.close(); bot.send_message(message.chat.id,"😄 Ты уже контролируешь эту компанию."); return
    cap=float(target['price'])*int(target['total_shares']); offer=round(cap*1.20,2)
    if float(buyer['treasury'])<offer: conn.close(); bot.send_message(message.chat.id,f"🏦 В казне <code>{buyer_t}</code> недостаточно денег.\n\nНужно: <b>{offer:,.0f} ₽</b>\nЕсть: <b>{float(buyer['treasury']):,.0f} ₽</b>"); return
    pending=conn.execute("SELECT id FROM ex_takeover_offers WHERE buyer_company_id=? AND target_company_id=? AND status='pending'",(buyer['id'],target['id'])).fetchone()
    if pending: conn.close(); bot.send_message(message.chat.id,"⏳ Предложение по этой компании уже ожидает ответа."); return
    now=datetime.now().isoformat(timespec='seconds'); cur=conn.execute("INSERT INTO ex_takeover_offers(buyer_company_id,target_company_id,from_user,to_user,price,status,created_at) VALUES(?,?,?,?,?,'pending',?)",(buyer['id'],target['id'],uid,target_owner,offer,now)); oid=cur.lastrowid; conn.commit(); conn.close()
    markup=InlineKeyboardMarkup(row_width=2).add(InlineKeyboardButton("🤝 Принять",callback_data=f"takeover_yes:{oid}"),InlineKeyboardButton("❌ Отклонить",callback_data=f"takeover_no:{oid}"))
    bot.send_message(message.chat.id,f"📨 <b>Предложение на поглощение отправлено.</b>\n\n🏢 Покупатель: <b>{html.escape(buyer['name'])}</b>\n🎯 Цель: <b>{html.escape(target['name'])}</b>\n💰 Цена сделки: <b>{offer:,.0f} ₽</b>\n📈 Премия: <b>20%</b> к капитализации.\n\nОжидаем решения контролирующего акционера цели.")
    try:
        target_chat=message.chat.id
        bot.send_message(target_owner,f"🤝 <b>Тебе предлагают продать контроль над {html.escape(target['name'])}.</b>\n\nПокупатель: { _ex_user_mention(target_chat,uid) }\n💰 Предложение: <b>{offer:,.0f} ₽</b>\n🏢 После сделки компания станет дочерней для <b>{html.escape(buyer['name'])}</b>.",reply_markup=markup)
    except Exception: logger.exception("Failed to send takeover offer")


@bot.callback_query_handler(func=lambda call: bool(call.data and call.data.startswith("takeover_")))
def handle_ex_takeover_callback(call: CallbackQuery) -> None:
    try:
        bot.answer_callback_query(call.id)
        action,raw=call.data.split(":",1); oid=int(raw); uid=call.from_user.id
        conn=_ex_db(); conn.execute("BEGIN IMMEDIATE")
        offer=conn.execute("SELECT * FROM ex_takeover_offers WHERE id=?",(oid,)).fetchone()
        if not offer or offer['status']!='pending':
            conn.rollback(); conn.close(); bot.answer_callback_query(call.id,"Предложение уже обработано.",show_alert=True); return
        if uid!=int(offer['to_user']):
            conn.rollback(); conn.close(); bot.answer_callback_query(call.id,"Это предложение адресовано другому пользователю.",show_alert=True); return
        if action=="takeover_no":
            conn.execute("UPDATE ex_takeover_offers SET status='rejected' WHERE id=?",(oid,)); conn.commit(); conn.close(); bot.edit_message_text("❌ <b>Предложение на поглощение отклонено.</b>",call.message.chat.id,call.message.message_id); return
        buyer=conn.execute("SELECT * FROM ex_exchanges WHERE id=? AND active=1",(offer['buyer_company_id'],)).fetchone(); target=conn.execute("SELECT * FROM ex_exchanges WHERE id=? AND active=1",(offer['target_company_id'],)).fetchone()
        if not buyer or not target or int(buyer['owner_id'] or 0)!=int(offer['from_user']) or int(target['owner_id'] or 0)!=uid:
            conn.execute("UPDATE ex_takeover_offers SET status='expired' WHERE id=?",(oid,)); conn.commit(); conn.close(); bot.edit_message_text("⚠️ <b>Предложение устарело:</b> контроль одной из компаний уже изменился.",call.message.chat.id,call.message.message_id); return
        price=float(offer['price'])
        if float(buyer['treasury'])<price:
            conn.execute("UPDATE ex_takeover_offers SET status='expired' WHERE id=?",(oid,)); conn.commit(); conn.close(); bot.edit_message_text("⚠️ <b>Сделка сорвалась:</b> в казне покупающей компании больше недостаточно денег.",call.message.chat.id,call.message.message_id); return
        now=datetime.now().isoformat(timespec='seconds')
        conn.execute("UPDATE ex_exchanges SET treasury=treasury-?,parent_company_id=?,acquisitions=acquisitions+1,last_activity=? WHERE id=?",(price,buyer['id'],now,buyer['id']))
        conn.execute("UPDATE ex_exchanges SET owner_id=?,parent_company_id=?,last_activity=? WHERE id=?",(int(offer['from_user']),buyer['id'],now,target['id']))
        conn.execute("UPDATE ex_traders SET balance=balance+? WHERE user_id=?",(price,uid))
        conn.execute("UPDATE ex_takeover_offers SET status='accepted' WHERE id=?",(oid,))
        conn.commit(); conn.close()
        bot.edit_message_text(f"🤝 <b>Поглощение завершено!</b>\n\n🏢 <b>{html.escape(buyer['name'])}</b> получила контроль над <b>{html.escape(target['name'])}</b>.\n💰 Оплачено из казны: <b>{price:,.0f} ₽</b>\n🏷 Статус цели: <b>дочерняя компания</b>",call.message.chat.id,call.message.message_id)
        try: bot.send_message(int(offer['from_user']),f"🎉 <b>Поглощение завершено.</b> Теперь {html.escape(target['name'])} — дочерняя компания твоей {html.escape(buyer['name'])}.")
        except Exception: pass
    except Exception:
        logger.exception("Takeover callback error")
        try: bot.answer_callback_query(call.id,"❌ Не удалось завершить сделку.",show_alert=True)
        except Exception: pass


@bot.message_handler(commands=["дочки", "дочерние"])
def handle_ex_subsidiaries(message: Message) -> None:
    conn=_ex_db(); rows=conn.execute("SELECT * FROM ex_exchanges WHERE parent_company_id IS NOT NULL AND parent_company_id IN (SELECT id FROM ex_exchanges WHERE owner_id=?) AND active=1 ORDER BY name",(message.from_user.id,)).fetchall(); conn.close()
    if not rows: bot.send_message(message.chat.id,"🏢 Дочерних компаний пока нет."); return
    lines=["🏢 <b>ДОЧЕРНИЕ КОМПАНИИ</b>",""]
    for r in rows: lines.append(f"• <b>{html.escape(r['name'])}</b> <code>{html.escape(r['ticker'])}</code> — {float(r['price']):,.2f} ₽")
    bot.send_message(message.chat.id,"\n".join(lines))

@bot.message_handler(commands=["топкомпаний", "топбиржи", "topcompanies"])
def handle_ex_top_companies(message: Message) -> None:
    conn=_ex_db()
    rows=conn.execute("""
        SELECT e.*, COALESCE((SELECT COUNT(*) FROM ex_holdings h WHERE h.exchange_id=e.id AND h.shares>0),0) holders
        FROM ex_exchanges e WHERE e.active=1
        ORDER BY (e.price*e.total_shares) DESC, e.trade_volume DESC LIMIT 20
    """).fetchall()
    conn.close()
    if not rows:
        bot.send_message(message.chat.id,"🏆 Активных компаний пока нет."); return
    lines=["🏆 <b>КРУПНЕЙШИЕ КОМПАНИИ BERLIONS</b>","<i>Рейтинг по капитализации</i>",""]
    medals=["🥇","🥈","🥉"]
    for i,r in enumerate(rows,1):
        cap=float(r['price'])*int(r['total_shares']); controller=_ex_user_mention(message.chat.id,int(r['owner_id'])) if int(r['owner_id'] or 0) else "⚖️ без большинства"
        prefix=medals[i-1] if i<=3 else f"{i}."
        lines.append(f"{prefix} <b>{html.escape(r['name'])}</b> <code>{html.escape(r['ticker'])}</code>\n   💎 {cap:,.0f} ₽ · 📊 {float(r['price']):,.2f} ₽ · 👥 {int(r['holders'])} акц.\n   👑 Контроль: {controller}")
    lines.append("\n📌 Профиль: <code>/компания TICKER</code>")
    bot.send_message(message.chat.id,"\n\n".join(lines))


@bot.message_handler(commands=["топоборота", "topvolume"])
def handle_ex_top_volume(message: Message) -> None:
    conn=_ex_db(); rows=conn.execute("SELECT * FROM ex_exchanges WHERE active=1 ORDER BY trade_volume DESC LIMIT 20").fetchall(); conn.close()
    if not rows: bot.send_message(message.chat.id,"📊 Пока нет торговой статистики."); return
    lines=["📊 <b>ТОП КОМПАНИЙ ПО ОБОРОТУ</b>",""]
    for i,r in enumerate(rows,1): lines.append(f"{i}. <b>{html.escape(r['name'])}</b> <code>{r['ticker']}</code> — <b>{float(r['trade_volume']):,.0f} ₽</b>")
    bot.send_message(message.chat.id,"\n".join(lines))


@bot.message_handler(commands=["статистика_компании", "company_stats"])
def handle_ex_company_stats(message: Message) -> None:
    p=(message.text or '').split();
    if len(p)!=2: bot.send_message(message.chat.id,"❓ Формат: <code>/статистика_компании TICKER</code>"); return
    conn=_ex_db(); ex=conn.execute("SELECT * FROM ex_exchanges WHERE ticker=?",(p[1].upper(),)).fetchone()
    if not ex: conn.close(); bot.send_message(message.chat.id,"❌ Компания не найдена."); return
    holders=conn.execute("SELECT COUNT(*) n FROM ex_holdings WHERE exchange_id=? AND shares>0",(ex['id'],)).fetchone()['n']; top=_ex_holder_control(conn,ex['id']); conn.close()
    top_line=_ex_user_mention(message.chat.id,int(top['user_id']))+f" — {int(top['shares'])/max(1,int(ex['total_shares']))*100:.2f}%" if top else "нет"
    cap=float(ex['price'])*int(ex['total_shares'])
    age=max(0,(datetime.now()-datetime.fromisoformat(str(ex['created_at']))).days)
    bot.send_message(message.chat.id,f"📊 <b>Статистика {html.escape(ex['name'])}</b> <code>{html.escape(ex['ticker'])}</code>\n\n💎 Капитализация: <b>{cap:,.0f} ₽</b>\n💰 Цена: <b>{float(ex['price']):,.2f} ₽</b>\n📦 Акций: <b>{int(ex['total_shares']):,}</b>\n🛒 В рынке: <b>{int(ex['treasury_shares']):,}</b>\n💧 Ликвидность: <b>{float(ex['market_cash'] or 0):,.0f} ₽</b>\n🏦 Казна: <b>{float(ex['treasury']):,.0f} ₽</b>\n👥 Акционеров: <b>{int(holders)}</b>\n📈 Оборот: <b>{float(ex['trade_volume']):,.0f} ₽</b>\n⭐ Уровень: <b>{int(ex['level'])}</b> · 🔥 {int(ex['popularity'])}/100\n📅 Возраст: <b>{age} дн.</b>\n🎯 Крупнейший пакет: <b>{top_line}</b>")


# ── Dice game logic ───────────────────────────────────────────────────────────

@bot.message_handler(commands=["dice", "кости"])
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
    """Быстрый callback брака: Telegram получает ответ до любой БД-операции."""
    logger.info("Marriage button pressed: data=%r user=%s", call.data, call.from_user.id)
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        logger.exception("Failed to acknowledge marriage callback")
    try:
        action, raw_id = call.data.split(":", 1)
        proposal_id = int(raw_id)
        conn = _social_db()
        proposal = conn.execute("SELECT * FROM social_marriage_proposals WHERE id=?", (proposal_id,)).fetchone()
        if not proposal or proposal["status"] != "pending":
            conn.close()
            try: bot.answer_callback_query(call.id, "Предложение уже обработано.", show_alert=True)
            except Exception: pass
            return
        if int(call.from_user.id) != int(proposal["to_user"]):
            conn.close()
            try: bot.answer_callback_query(call.id, "Это предложение не для тебя.", show_alert=True)
            except Exception: pass
            return
        if action == "marry_no":
            conn.execute("UPDATE social_marriage_proposals SET status='rejected' WHERE id=?", (proposal_id,))
            conn.commit(); conn.close()
            bot.edit_message_text("❌ <b>Предложение отклонено.</b>", call.message.chat.id, call.message.message_id)
            return
        if action != "marry_yes":
            conn.close(); return
        if (_social_active_marriage(conn, proposal["chat_id"], proposal["from_user"]) or
            _social_active_marriage(conn, proposal["chat_id"], proposal["to_user"])):
            conn.execute("UPDATE social_marriage_proposals SET status='rejected' WHERE id=?", (proposal_id,))
            conn.commit(); conn.close()
            try: bot.answer_callback_query(call.id, "У одного из вас уже есть активный брак.", show_alert=True)
            except Exception: pass
            return
        now = _social_now().isoformat(sep=" ")
        conn.execute("UPDATE social_marriage_proposals SET status='accepted' WHERE id=?", (proposal_id,))
        conn.execute("INSERT INTO social_marriages(chat_id,user1,user2,created_at,active) VALUES(?,?,?,?,1)",
                     (proposal["chat_id"], proposal["from_user"], proposal["to_user"], now))
        conn.commit(); conn.close()
        bot.edit_message_text(
            f"💍 <b>Брак заключён!</b>\n\n"
            f"{_social_user_label(proposal['chat_id'], proposal['from_user'])} ❤️ "
            f"{_social_user_label(proposal['chat_id'], proposal['to_user'])}\n"
            f"🌱 Стаж: <b>0 дней</b> — 💚 Зелёные",
            call.message.chat.id, call.message.message_id)
    except Exception:
        logger.exception("Marriage button error")
        try: bot.answer_callback_query(call.id, "❌ Не удалось обработать кнопку.", show_alert=True)
        except Exception: pass


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

@bot.message_handler(commands=["stop", "стоп"])
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

@bot.message_handler(commands=["add", "добавить"])
def handle_add(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "⛔ У тебя нет доступа к этой команде.")
        return
    _pending[message.from_user.id] = {"step": "awaiting_text"}
    bot.send_message(message.chat.id, "📝 <b>Шаг 1/2</b> — Отправь описание / текст, который увидит пользователь:")

# ── Admin: /list ──────────────────────────────────────────────────────────────

@bot.message_handler(commands=["list", "список"])
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

@bot.message_handler(commands=["delete", "удалить"])
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

@bot.message_handler(commands=["edit", "изменить"])
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

    CREATE TABLE IF NOT EXISTS social_clan_bans (
        chat_id INTEGER NOT NULL,
        clan_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        banned_at TEXT NOT NULL,
        PRIMARY KEY(chat_id, clan_id, user_id)
    );
    """)
    conn.commit()
    conn.close()


_social_init_db()


def _social_now():
    return datetime.now().replace(microsecond=0)


def _mention_user(chat_id: int | None, user_id: int, name: str | None = None) -> str:
    """Единый кликабельный Telegram-mention для любого места бота."""
    display = name or "Пользователь"

    # Сначала берём сохранённые данные соцсистемы — это работает даже там,
    # где get_chat_member недоступен (например, в некоторых сообщениях биржи).
    if chat_id is not None:
        try:
            conn = _social_db()
            row = conn.execute(
                "SELECT username,first_name,last_name FROM social_users WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            ).fetchone()
            conn.close()
            if row:
                if row["username"]:
                    display = "@" + row["username"]
                else:
                    display = " ".join(x for x in (row["first_name"], row["last_name"]) if x).strip() or display
        except Exception:
            pass

        try:
            member = bot.get_chat_member(chat_id, user_id)
            user = member.user
            if user.username:
                display = "@" + user.username
            else:
                display = " ".join(x for x in (user.first_name, user.last_name) if x).strip() or display
        except Exception:
            pass

    return f'<a href="tg://user?id={user_id}">{html.escape(str(display))}</a>'


def _social_user_label(chat_id: int, user_id: int) -> str:
    conn = _social_db()
    row = conn.execute(
        "SELECT username,first_name,last_name FROM social_users WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    ).fetchone()
    conn.close()
    if row:
        if row["username"]:
            return _mention_user(chat_id, user_id, "@" + row["username"])
        name = " ".join(x for x in (row["first_name"], row["last_name"]) if x).strip()
        if name:
            return _mention_user(chat_id, user_id, name)
    return _mention_user(chat_id, user_id)


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
    # Регистрируем автора запроса сразу, чтобы первое появление сохранялось.
    try: _social_register(message)
    except Exception: pass
    conn = _social_db()
    row = conn.execute("SELECT * FROM social_users WHERE chat_id=? AND user_id=?", (message.chat.id,target)).fetchone()
    if not row:
        try:
            member=bot.get_chat_member(message.chat.id,target); u=member.user
            now=_social_now().isoformat(sep=" ")
            conn.execute("INSERT OR IGNORE INTO social_users(chat_id,user_id,username,first_name,last_name,first_seen,last_seen) VALUES(?,?,?,?,?,?,?)",
                         (message.chat.id,target,u.username,u.first_name or "",u.last_name or "",now,now)); conn.commit()
            row=conn.execute("SELECT * FROM social_users WHERE chat_id=? AND user_id=?",(message.chat.id,target)).fetchone()
        except Exception: pass
    day=_social_activity_for_user(message.chat.id,target,"day")
    week=_social_activity_for_user(message.chat.id,target,"week")
    month=_social_activity_for_user(message.chat.id,target,"month")
    label=_social_user_label(message.chat.id,target)
    display=("@"+row["username"]) if row and row["username"] else ((row["first_name"] if row else None) or "Пользователь")

    conn=_social_db()
    awards=conn.execute("SELECT reason,created_at,from_user FROM social_awards WHERE chat_id=? AND to_user=? ORDER BY id DESC LIMIT 10",(message.chat.id,target)).fetchall()
    marriage=conn.execute("""SELECT * FROM social_marriages WHERE chat_id=? AND active=1 AND (user1=? OR user2=?) LIMIT 1""",(message.chat.id,target,target)).fetchone()
    clan=conn.execute("""SELECT c.id,c.name,c.owner_id,m.joined_at FROM social_clan_members m JOIN social_clans c ON c.id=m.clan_id WHERE m.chat_id=? AND m.user_id=? LIMIT 1""",(message.chat.id,target)).fetchone()
    conn.close()

    def duration_from(start):
        try:
            d=max(0,(_social_now()-datetime.fromisoformat(start)).days)
            if d < 30: return f"{d} дн."
            months=d//30; days=d%30
            return f"{months} мес." + (f" {days} дн." if days else "")
        except Exception: return "неизвестно"

    first_seen=duration_from(row["first_seen"]) if row else "неизвестно"
    spouse=None; marriage_duration=None
    if marriage:
        spouse_id=int(marriage["user2"] if int(marriage["user1"])==target else marriage["user1"])
        spouse=_social_user_label(message.chat.id,spouse_id)
        marriage_duration=_social_duration_text(marriage["created_at"])

    lines=[f"👤 <b>Это пользователь {label}</b>","",
           "💚 <b>Состоит в чате</b>",
           f"📅 В чате: <b>{first_seen}</b>",
           "",
           f"💬 <b>Активность:</b> {day} дн. / {week} нед. / {month} мес."]
    if clan:
        owner_mark=" 👑" if int(clan["owner_id"])==target else ""
        lines += [f"🏰 Клан: <b>{html.escape(clan['name'])}</b>{owner_mark}"]
    else:
        lines += ["🏰 Клан: <b>нет</b>"]
    if spouse:
        lines += [f"💍 Брак: {spouse} · <b>{marriage_duration}</b>"]
    else:
        lines += ["💍 Брак: <b>нет</b>"]
    lines.append("")
    lines.append("🏆 <b>НАГРАДЫ</b>")
    if awards:
        for a in awards:
            lines.append(f"🎗 {html.escape(a['reason'])} · <b>{duration_from(a['created_at'])}</b>")
    else:
        lines.append("— пока нет")
    caption="\n".join(lines)

    # Настоящая аватарка Telegram вместо нарисованной карточки.
    try:
        photos=bot.get_user_profile_photos(target,limit=1)
        if photos and photos.total_count and photos.photos and photos.photos[0]:
            file_id=photos.photos[0][-1].file_id
            file_info=bot.get_file(file_id)
            data=bot.download_file(file_info.file_path)
            bot.send_photo(message.chat.id, io.BytesIO(data), caption=caption)
            return
    except Exception:
        logger.exception("Profile avatar load error")
    bot.send_message(message.chat.id,caption)


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
    rows=conn.execute("""SELECT c.id,c.name,c.owner_id,COUNT(m.user_id) members FROM social_clans c LEFT JOIN social_clan_members m ON m.clan_id=c.id WHERE c.chat_id=? GROUP BY c.id ORDER BY members DESC,c.name LIMIT 20""",(message.chat.id,)).fetchall()
    if not rows:
        conn.close(); bot.send_message(message.chat.id,"🏰 Кланов пока нет. Создай первый: <code>создать клан название</code>"); return
    lines=["🏰 <b>Кланы Berlions</b>\n"]
    for i,r in enumerate(rows,1):
        members=conn.execute("SELECT user_id FROM social_clan_members WHERE chat_id=? AND clan_id=? ORDER BY joined_at",(message.chat.id,r["id"])).fetchall()
        names=[_social_user_label(message.chat.id,int(m["user_id"])) for m in members]
        member_text=", ".join(names) if names else "нет участников"
        lines.append(f"{i}. <b>{html.escape(r['name'])}</b> — 👥 {r['members']}\n   👑 Владелец: {_social_user_label(message.chat.id,int(r['owner_id']))}\n   👤 {member_text}")
    conn.close()
    bot.send_message(message.chat.id,"\n\n".join(lines))


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
    conn=_social_db()
    total_chat=conn.execute("SELECT COALESCE(SUM(messages),0) n FROM social_activity WHERE chat_id=?",(message.chat.id,)).fetchone()["n"]
    conn.close()
    lines=[f"<b>{title}</b>\n", f"💬 Всего сообщений в чате: <b>{int(total_chat):,}</b>\n"]
    medals=["🥇","🥈","🥉"]
    for i,r in enumerate(rows,1):
        raw_label=("@"+r['username']) if r['username'] else (r['first_name'] or "Пользователь")
        label=_mention_user(message.chat.id, int(r['user_id']), raw_label)
        prefix=medals[i-1] if i<=3 else f"{i}."
        lines.append(f"{prefix} {label} — <b>{r['messages']}</b> сообщ.")
    bot.send_message(message.chat.id,"\n".join(lines))


@bot.message_handler(commands=["topday", "topweek", "topmonth", "топдня", "топнедели", "топмесяца"])
def handle_social_top_period_command(message: Message) -> None:
    cmd=(message.text or "").split()[0].lower().lstrip("/")
    handle_social_top(message,{"topday":"day","topweek":"week","topmonth":"month","топдня":"day","топнедели":"week","топмесяца":"month"}[cmd])


@bot.message_handler(commands=["commands", "команды"])
def handle_social_commands(message: Message) -> None:
    text=(
        "📚 <b>Команды Berlions</b>\n\n"
        "👤 <b>Профиль</b>\n"
        "• <code>/профиль</code> — профиль и статистика\n"
        "• <code>/награды</code> — награды\n"
        "• <code>/команды</code> — этот список\n\n"
        "💍 <b>Отношения</b>\n"
        "• <code>/брак @user</code> — предложить брак\n"
        "• <code>/браки</code> — список браков\n"
        "• <code>/развод</code> — развестись\n\n"
        "🏰 <b>Кланы</b>\n"
        "• <code>/создать_клан Название</code> — создать клан\n"
        "• <code>/кланы</code> — кланы и участники\n"
        "• <code>/мой_клан</code> — управление своим кланом\n"
        "• <code>/кик_из_клана @user</code> — исключить участника (владелец)\n"
        "• <code>/удалить_клан</code> — удалить свой клан\n\n"
        "😘 <b>Общение</b>\n"
        "• <code>поцеловать @user</code> — действие, можно ответом и с репликой\n"
        "• <code>обнять @user</code>\n"
        "• <code>дай пять @user</code>\n"
        "• <code>погладить @user</code>\n"
        "• <code>пожать руку @user</code>\n\n"
        "🏆 <b>Активность</b>\n"
        "• <code>топ</code> — общий топ и все сообщения чата\n"
        "• <code>топ дня</code>\n"
        "• <code>топ неделя</code>\n"
        "• <code>топ месяц</code>\n\n"
        "💰 <b>Деньги и биржа</b>\n"
        "• <code>/биржа</code> — меню биржи\n"
        "• <code>/баланс</code> — баланс\n"
        "• <code>/бонус</code> — ежедневный бонус\n"
        "• <code>/работа</code> — заработать деньги раз в 30 минут\n"
        "• <code>/рынок</code> — рынок и график\n"
        "• <code>/портфель</code> — мои акции\n"
        "• <code>/купить TICKER 5</code> — купить акции\n"
        "• <code>/продать TICKER 5</code> — продать акции\n"
        "• <code>/компании</code> — список компаний\n"
        "• <code>/компания TICKER</code> — профиль компании\n"
        "• <code>/держатели TICKER</code> — владельцы акций\n"
        "• <code>/топденьги</code> — топ капитала игроков\n"
        "• <code>/топкомпаний</code> — крупнейшие компании\n"
        "• <code>/топоборота</code> — компании по обороту\n"
        "• <code>/статистика_компании TICKER</code> — статистика компании\n"
        "• <code>/контроль TICKER</code> — контрольный пакет\n"
        "• <code>/поглощение BUYER TARGET</code> — предложить поглощение\n"
        "• <code>/дочерние</code> — дочерние компании\n"
        "• <code>/управление_компанией TICKER ...</code> — управление компанией\n"
        "• <code>/дивиденды TICKER 100000</code> — дивиденды\n"
        "• <code>/улучшить TICKER маркетинг</code> — развитие\n"
        "• <code>/топакций</code> — крупнейшие акционеры\n\n"
        "🎲 <code>/кости</code> — кости\n"
        "🔎 <code>/поиск запрос</code> — поиск"
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
    if not _is_admin(message.from_user.id):
        bot.send_message(message.chat.id,"⛔ Награждать может только администратор.")
        return
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


@bot.message_handler(commands=["создать_клан"])
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
    banned=conn.execute("SELECT 1 FROM social_clan_bans WHERE chat_id=? AND clan_id=? AND user_id=?",(message.chat.id,clan['id'],message.from_user.id)).fetchone()
    if banned:
        conn.close(); bot.send_message(message.chat.id,"🚫 Владелец этого клана запретил тебе вступать в него."); return
    old=conn.execute("SELECT clan_id FROM social_clan_members WHERE chat_id=? AND user_id=?",(message.chat.id,message.from_user.id)).fetchone()
    if old and old['clan_id']==clan['id']:
        conn.close(); bot.send_message(message.chat.id,"🏰 Ты уже в этом клане!"); return
    conn.execute("DELETE FROM social_clan_members WHERE chat_id=? AND user_id=?",(message.chat.id,message.from_user.id))
    conn.execute("INSERT INTO social_clan_members(chat_id,clan_id,user_id,joined_at) VALUES(?,?,?,?)",(message.chat.id,clan['id'],message.from_user.id,_social_now().isoformat(sep=" ")))
    conn.commit(); conn.close()
    bot.send_message(message.chat.id,f"🏰 Добро пожаловать в клан <b>{html.escape(clan['name'])}</b>!")


@bot.message_handler(commands=["развод"])
def handle_social_divorce(message: Message) -> None:
    target=None
    if message.reply_to_message and message.reply_to_message.from_user:
        target=message.reply_to_message.from_user.id
    else:
        arg=" ".join((message.text or "").split()[1:])
        target=_social_target(message,arg)
    conn=_social_db()
    marriage=_social_active_marriage(conn,message.chat.id,message.from_user.id)
    if not marriage:
        conn.close(); bot.send_message(message.chat.id,"💔 У тебя нет активного брака."); return
    spouse=int(marriage["user2"] if int(marriage["user1"])==message.from_user.id else marriage["user1"])
    if target and target!=spouse and not _is_admin(message.from_user.id):
        conn.close(); bot.send_message(message.chat.id,"💔 Укажи своего супруга или ответь на его сообщение."); return
    conn.execute("UPDATE social_marriages SET active=0 WHERE id=?",(marriage["id"],)); conn.commit(); conn.close()
    bot.send_message(message.chat.id,f"💔 {_social_user_label(message.chat.id,message.from_user.id)} и {_social_user_label(message.chat.id,spouse)} больше не состоят в браке.")


@bot.message_handler(func=lambda m: bool(m.text and re.fullmatch(r"развод(?:\s+@\w+)?",m.text.strip(),re.I)), content_types=["text"])
def handle_social_divorce_plain(message: Message) -> None:
    handle_social_divorce(message)


@bot.message_handler(commands=["мой_клан", "управление_кланом"])
def handle_my_clan(message: Message) -> None:
    conn=_social_db()
    clan=conn.execute("SELECT c.* FROM social_clans c JOIN social_clan_members m ON m.clan_id=c.id WHERE c.chat_id=? AND m.user_id=?",(message.chat.id,message.from_user.id)).fetchone()
    if not clan:
        conn.close(); bot.send_message(message.chat.id,"🏰 Ты пока не состоишь в клане."); return
    members=conn.execute("SELECT user_id,joined_at FROM social_clan_members WHERE chat_id=? AND clan_id=? ORDER BY joined_at",(message.chat.id,clan["id"])).fetchall()
    bans=conn.execute("SELECT user_id FROM social_clan_bans WHERE chat_id=? AND clan_id=?",(message.chat.id,clan["id"])).fetchall()
    conn.close()
    lines=[f"🏰 <b>{html.escape(clan['name'])}</b>",f"👑 Владелец: {_social_user_label(message.chat.id,int(clan['owner_id']))}",f"👥 Участников: <b>{len(members)}</b>","","<b>Участники:</b>"]
    for m in members: lines.append(f"• {_social_user_label(message.chat.id,int(m['user_id']))}")
    if int(clan["owner_id"])==message.from_user.id:
        lines += ["", "⚙️ <b>Управление владельца:</b>","/кик_из_клана @user — исключить и запретить повторный вход","/удалить_клан — удалить клан"]
        if bans: lines.append(f"🚫 Заблокировано в клане: <b>{len(bans)}</b>")
    bot.send_message(message.chat.id,"\n".join(lines))


@bot.message_handler(func=lambda m: bool(m.text and re.fullmatch(r"мой\s+клан",m.text.strip(),re.I)), content_types=["text"])
def handle_my_clan_plain(message: Message) -> None:
    handle_my_clan(message)


def _clan_owner_for_user(conn, chat_id, user_id):
    return conn.execute("SELECT c.* FROM social_clans c JOIN social_clan_members m ON m.clan_id=c.id WHERE c.chat_id=? AND m.user_id=?",(chat_id,user_id)).fetchone()


@bot.message_handler(commands=["кик_из_клана"])
def handle_clan_kick(message: Message) -> None:
    if not _is_admin(message.from_user.id) and not message.reply_to_message and len((message.text or "").split())<2:
        bot.send_message(message.chat.id,"❓ Формат: <code>/кик_из_клана @user</code> или ответом на сообщение."); return
    target=message.reply_to_message.from_user.id if message.reply_to_message and message.reply_to_message.from_user else _social_target(message," ".join((message.text or "").split()[1:]))
    if not target: bot.send_message(message.chat.id,"❓ Не удалось определить пользователя."); return
    conn=_social_db(); clan=_clan_owner_for_user(conn,message.chat.id,message.from_user.id)
    if not clan and not _is_admin(message.from_user.id): conn.close(); bot.send_message(message.chat.id,"⛔ Только владелец клана может исключать участников."); return
    if clan and target==int(clan["owner_id"]): conn.close(); bot.send_message(message.chat.id,"❌ Нельзя исключить владельца клана."); return
    target_clan=conn.execute("SELECT clan_id FROM social_clan_members WHERE chat_id=? AND user_id=?",(message.chat.id,target)).fetchone()
    if not target_clan or (clan and int(target_clan["clan_id"])!=int(clan["id"])):
        conn.close(); bot.send_message(message.chat.id,"❌ Пользователь не состоит в твоём клане."); return
    cid=int(target_clan["clan_id"]); conn.execute("DELETE FROM social_clan_members WHERE chat_id=? AND user_id=?",(message.chat.id,target)); conn.execute("INSERT OR REPLACE INTO social_clan_bans(chat_id,clan_id,user_id,banned_at) VALUES(?,?,?,?)",(message.chat.id,cid,target,_social_now().isoformat(sep=" "))); conn.commit(); conn.close()
    bot.send_message(message.chat.id,f"🚫 {_social_user_label(message.chat.id,target)} исключён из клана и больше не сможет в него вступить.")


@bot.message_handler(commands=["удалить_клан"])
def handle_delete_clan(message: Message) -> None:
    conn=_social_db(); clan=_clan_owner_for_user(conn,message.chat.id,message.from_user.id)
    if not clan and not _is_admin(message.from_user.id): conn.close(); bot.send_message(message.chat.id,"⛔ Только владелец клана может удалить его."); return
    if clan:
        cid=int(clan["id"]); conn.execute("DELETE FROM social_clan_members WHERE chat_id=? AND clan_id=?",(message.chat.id,cid)); conn.execute("DELETE FROM social_clan_bans WHERE chat_id=? AND clan_id=?",(message.chat.id,cid)); conn.execute("DELETE FROM social_clans WHERE id=?",(cid,))
    else:
        # глобальный админ: удаление клана, в котором он состоит, либо ничего
        cidrow=conn.execute("SELECT id FROM social_clans WHERE chat_id=? AND owner_id=?",(message.chat.id,message.from_user.id)).fetchone()
        if cidrow: cid=int(cidrow["id"]); conn.execute("DELETE FROM social_clan_members WHERE clan_id=?",(cid,)); conn.execute("DELETE FROM social_clan_bans WHERE clan_id=?",(cid,)); conn.execute("DELETE FROM social_clans WHERE id=?",(cid,))
    conn.commit(); conn.close(); bot.send_message(message.chat.id,"🗑 Клан удалён.")


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


@bot.message_handler(func=lambda m: bool(m.text and re.match(r"^(поцеловать|обнять|дай пять|дать пять|погладить|пожать руку)\b",m.text.strip(),re.I)), content_types=["text"])
def handle_social_roleplay(message: Message) -> None:
    raw=message.text.strip()
    parts=raw.split(maxsplit=1)
    action=parts[0].lower()
    rest=parts[1].strip() if len(parts)>1 else ""
    target=_social_target(message,rest)
    # Для reply команда может содержать реплику без username.
    quote=""
    if message.reply_to_message and rest:
        quote=rest
    elif target and rest:
        tokens=rest.split(maxsplit=1)
        quote=tokens[1] if len(tokens)>1 else ""
    if not target:
        bot.send_message(message.chat.id,f"💫 Формат: <code>{html.escape(action)} @user</code> или ответом на сообщение."); return
    actor=_social_user_label(message.chat.id,message.from_user.id)
    target_label=_social_user_label(message.chat.id,target)
    verbs={"поцеловать":"поцеловал","обнять":"обнял","дай пять":"дал пять","дать пять":"дал пять","погладить":"погладил","пожать руку":"пожал руку"}
    emoji={"поцеловать":"😘","обнять":"🤗","дай пять":"🖐","дать пять":"🖐","погладить":"🥰","пожать руку":"🤝"}
    text=f"{emoji[action]} | {actor} <b>{verbs[action]}</b> {target_label}"
    if quote:
        text += f"\nс репликой: <i>{html.escape(quote)}</i>"
    bot.send_message(message.chat.id,text)


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
