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
            f"• /dice — сыграть в кости 🎲\n\n"
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

    # Основной вариант — красивый график matplotlib.
    if plt is not None:
        fig, ax = plt.subplots(figsize=(10, 5.4), dpi=130)
        ax.plot(range(len(prices)), prices, linewidth=2.5)
        ax.fill_between(range(len(prices)), prices, min(prices) * 0.995, alpha=0.12)
        ax.grid(True, alpha=0.22)
        ax.set_title(f"{ex['name']}  •  {ex['ticker']}   {prices[-1]:,.2f} ₽   ({change:+.2f}%)")
        ax.set_xlabel("Движение рынка")
        ax.set_ylabel("Цена, ₽")
        fig.tight_layout()
        fig.savefig(path, bbox_inches="tight")
        plt.close(fig)
        return path

    # Резервный вариант — Pillow, если matplotlib не установлен.
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RuntimeError("Не установлен модуль для построения графика. Установи matplotlib или Pillow.") from exc

    width, height = 1200, 650
    image = Image.new("RGB", (width, height), "#111827")
    draw = ImageDraw.Draw(image)
    margin = 80
    chart_w = width - margin * 2
    chart_h = height - 190
    low, high = min(prices), max(prices)
    if high == low:
        high += 1
        low -= 1

    points = []
    for i, price in enumerate(prices):
        x = margin + (chart_w * i / max(1, len(prices) - 1))
        y = margin + (high - price) / (high - low) * chart_h
        points.append((x, y))

    draw.text((margin, 25), f"{ex['name']} • {ex['ticker']}", fill="white")
    draw.text((margin, 55), f"{prices[-1]:,.2f} ₽   ({change:+.2f}%)", fill="white")
    draw.rectangle((margin, margin + 40, width - margin, margin + 40 + chart_h), outline="#374151", width=2)
    if len(points) > 1:
        draw.line(points, fill="#22c55e" if change >= 0 else "#ef4444", width=6)
    image.save(path, "PNG")
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


@bot.message_handler(commands=["top"])
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


@bot.message_handler(commands=["ban"])
def handle_ban(message: Message) -> None:
    if _get_user_rank(message.chat.id, message.from_user.id) < 3: bot.send_message(message.chat.id, "⛔ Требуется минимум 3 ранг."); return
    target_id = _parse_target(message)
    if not target_id: bot.send_message(message.chat.id, "❓ Ответь на сообщение или /ban ID"); return
    if not _can_manage(message.chat.id, message.from_user.id, target_id, 3): bot.send_message(message.chat.id, "⛔ Недостаточно прав."); return
    try:
        bot.ban_chat_member(message.chat.id, target_id)
        bot.send_message(message.chat.id, f"🚫 Пользователь {target_id} забанен.")
    except Exception as e: bot.send_message(message.chat.id, f"❌ Ошибка: {e}")

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

@bot.message_handler(commands=["mute"])
def handle_mute(message: Message) -> None:
    if _get_user_rank(message.chat.id, message.from_user.id) < 2: bot.send_message(message.chat.id, "⛔ Требуется минимум 2 ранг."); return
    target_id = _parse_target(message)
    if not target_id: bot.send_message(message.chat.id, "❓ /mute (ответ)"); return
    if not _can_manage(message.chat.id, message.from_user.id, target_id, 2): bot.send_message(message.chat.id, "⛔ Недостаточно прав."); return
    try:
        bot.restrict_chat_member(message.chat.id, target_id, can_send_messages=False)
        bot.send_message(message.chat.id, f"🔇 Пользователь {target_id} замучен.")
    except Exception as e: bot.send_message(message.chat.id, f"❌ Ошибка: {e}")

@bot.message_handler(commands=["unmute"])
def handle_unmute(message: Message) -> None:
    if _get_user_rank(message.chat.id, message.from_user.id) < 2: bot.send_message(message.chat.id, "⛔ Требуется минимум 2 ранг."); return
    target_id = _parse_target(message)
    if not target_id: bot.send_message(message.chat.id, "❓ /unmute"); return
    try:
        bot.restrict_chat_member(message.chat.id, target_id, can_send_messages=True)
        bot.send_message(message.chat.id, f"🔊 Пользователь {target_id} размучен.")
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


@bot.message_handler(commands=["learn"])
def handle_learn(message: Message) -> None:
    if not _is_admin(message.from_user.id) and _get_user_rank(message.chat.id, message.from_user.id) < 5:
        bot.send_message(message.chat.id, "⛔ Только глава или глобальный.")
        return
    bot.send_message(message.chat.id, "✅ /learn активирован. Полная история чата пока не поддерживается (ограничения Telegram).")

# ── Advertising/autopost ───────────────────────────────────────────────────────
# Автопостинг рекламы удалён по запросу. Остальная логика бота не зависит от него.

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    bot.remove_webhook()
    logger.info("Bot started. Polling…")
    bot.polling(non_stop=True, interval=0)
