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
import sqlite3
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
    ReplyKeyboardMarkup,
    KeyboardButton,
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
_order_pending: dict[int, int] = {}  # user_id -> chat_id, пока пользователь вводит заказ
_stupid_stats: dict[int, dict] = {} # пока не используется, но оставлено по структуре

# ── BERLIONS GAME REQUESTS ─────────────────────────────────────────────────────
# Отдельная БД: не смешиваем заказы с основной БД игр/контента.
ORDERS_DB = "orders.db"
RANDOM_GAME_COOLDOWN = 30 * 60

def _orders_db():
    conn = sqlite3.connect(ORDERS_DB, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn

def _orders_init_db() -> None:
    conn = _orders_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS game_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        name_key TEXT NOT NULL UNIQUE,
        first_user_id INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open'
    );
    CREATE INDEX IF NOT EXISTS idx_game_requests_status ON game_requests(status);
    CREATE TABLE IF NOT EXISTS game_request_votes (
        request_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        vote INTEGER NOT NULL CHECK(vote IN (-1, 1)),
        updated_at TEXT NOT NULL,
        PRIMARY KEY(request_id, user_id),
        FOREIGN KEY(request_id) REFERENCES game_requests(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS random_game_cooldowns (
        user_id INTEGER PRIMARY KEY,
        last_used REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS game_search_users (
        game_key TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        game_name TEXT NOT NULL,
        found_at TEXT NOT NULL,
        PRIMARY KEY(game_key, user_id)
    );
    CREATE INDEX IF NOT EXISTS idx_game_search_users_game ON game_search_users(game_key);
    """)
    conn.commit()
    conn.close()

_orders_init_db()

def _normalize_game_name(name: str) -> str:
    # Нормализация нужна, чтобы Minecraft / minecraft /  Minecraft  не создавали дубли.
    return " ".join(name.casefold().split())

def _menu_keyboard() -> ReplyKeyboardMarkup:
    markup = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False, row_width=2)
    markup.add(
        KeyboardButton("📝 Заказать игру"),
        KeyboardButton("📋 Стол заказов"),
        KeyboardButton("🎲 Рандомная игра"),
        KeyboardButton("👤 Профиль"),
        KeyboardButton("🔥 Самая разыскиваемая"),
    )
    return markup


def _record_game_searches(user_id: int, rows) -> None:
    """Записывает уникальных пользователей, которые нашли игру через /поиск или /search."""
    if not rows:
        return
    now = datetime.now().isoformat(timespec="seconds") if 'datetime' in globals() else time.strftime("%Y-%m-%d %H:%M:%S")
    conn = _orders_db()
    try:
        conn.executemany(
            "INSERT OR IGNORE INTO game_search_users(game_key,user_id,game_name,found_at) VALUES(?,?,?,?)",
            [(str(row["key"]), int(user_id), str(row["content_text"]), now) for row in rows[:15]],
        )
        conn.commit()
    finally:
        conn.close()


def _send_most_wanted(chat_id: int) -> None:
    """Показывает топ игр по числу уникальных пользователей, нашедших их через поиск."""
    conn = _orders_db()
    rows = conn.execute("""
        SELECT game_key, MAX(game_name) AS game_name, COUNT(*) AS users, MAX(found_at) AS last_found
        FROM game_search_users
        GROUP BY game_key
        ORDER BY users DESC, last_found DESC, game_name COLLATE NOCASE ASC
        LIMIT 10
    """).fetchall()
    conn.close()

    if not rows:
        bot.send_message(
            chat_id,
            "🔥 <b>Самая разыскиваемая игра</b>\n\n"
            "Пока статистики нет. Найди игру через <code>/поиск название</code> — и она появится здесь!",
        )
        return

    lines = [
        "🔥 <b>САМЫЕ РАЗЫСКИВАЕМЫЕ ИГРЫ</b>",
        "",
        "Рейтинг по количеству <b>уникальных пользователей</b>, которые нашли игру через поиск:",
        "",
    ]
    medals = ["🥇", "🥈", "🥉"]
    for i, row in enumerate(rows, 1):
        prefix = medals[i - 1] if i <= 3 else f"<b>{i}.</b>"
        n = int(row["users"])
        word = "человек" if n == 1 else ("человека" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "человек")
        lines.append(f"{prefix} 🎮 <b>{html.escape(str(row['game_name']))}</b> — 👥 <b>{n}</b> {word}")

    bot.send_message(chat_id, "\n".join(lines))

def _send_main_menu(chat_id: int, text: str | None = None) -> None:
    bot.send_message(chat_id, text or "🎮 <b>Главное меню Berlions</b>\nВыбирай нужное действие ниже 👇", reply_markup=_menu_keyboard())

def _order_keyboard(request_id: int) -> InlineKeyboardMarkup:
    conn = _orders_db()
    row = conn.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN vote=1 THEN 1 ELSE 0 END),0) likes,
            COALESCE(SUM(CASE WHEN vote=-1 THEN 1 ELSE 0 END),0) dislikes
        FROM game_request_votes WHERE request_id=?
    """, (request_id,)).fetchone()
    conn.close()
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton(f"👍 Хочу {int(row['likes'])}", callback_data=f"order_vote:+:{request_id}"),
        InlineKeyboardButton(f"👎 Не жду {int(row['dislikes'])}", callback_data=f"order_vote:-:{request_id}"),
    )
    return markup

def _request_summary(request_id: int) -> str:
    conn = _orders_db()
    row = conn.execute("SELECT name,status,created_at FROM game_requests WHERE id=?", (request_id,)).fetchone()
    votes = conn.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN vote=1 THEN 1 ELSE 0 END),0) likes,
            COALESCE(SUM(CASE WHEN vote=-1 THEN 1 ELSE 0 END),0) dislikes,
            COUNT(*) total
        FROM game_request_votes WHERE request_id=?
    """, (request_id,)).fetchone()
    conn.close()
    if not row:
        return "❌ Заказ не найден."
    status = "🟢 открыт" if row['status'] == 'open' else "✅ добавлена"
    return (
        f"🎮 <b>{html.escape(row['name'])}</b>\n"
        f"{status} · 👥 ждут: <b>{int(votes['total'])}</b>\n"
        f"👍 <b>{int(votes['likes'])}</b>   👎 <b>{int(votes['dislikes'])}</b>"
    )

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
        welcome_text = (
            "👋 <b>Привет! Я Berlions чат-бот.</b>\n\n"
            "🎮 Здесь можно быстро найти игру, заказать отсутствующую игру "
            "или получить случайную игру из базы.\n\n"
            "Нажми на кнопку меню под полем ввода 👇"
        )
        _send_main_menu(message.chat.id, welcome_text)
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
    _send_main_menu(message.chat.id,
        "📚 <b>Меню Berlions</b>\n\n"
        "📝 Заказать игру — оставить заявку на игру, которой пока нет в базе.\n"
        "📋 Стол заказов — посмотреть, что чаще всего хотят пользователи.\n"
        "🎲 Рандомная игра — случайная игра из базы, раз в 30 минут.\n"
        "👤 Профиль — твой профиль, активность и награды.\n\n"
        "Команды по-прежнему работают, но вводить их вручную необязательно.")


# ── Главная клавиатура / меню ────────────────────────────────────────────────

@bot.message_handler(func=lambda m: bool(m.chat.type == "private" and m.text and m.text.strip() == "📝 Заказать игру"), content_types=["text"])
def handle_menu_order(message: Message) -> None:
    if not _require_subscription(message.chat.id, message.from_user.id, "order"):
        return
    _order_pending[message.from_user.id] = message.chat.id
    bot.send_message(
        message.chat.id,
        "📝 <b>Заказ игры</b>\n\n"
        "Напиши название игры, которую хочешь увидеть в базе.\n"
        "Можно написать не идеально — например <i>rdr2</i>, <i>Red Dead 2</i> или полное название.\n\n"
        "Если такая заявка уже есть, новая копия не создастся — ты просто добавишь свой голос.\n\n"
        "❌ Чтобы отменить, напиши <code>отмена</code>.",
    )


@bot.message_handler(func=lambda m: bool(m.chat.type == "private" and m.text and m.text.strip() == "📋 Стол заказов"), content_types=["text"])
def handle_menu_orders(message: Message) -> None:
    if not _require_subscription(message.chat.id, message.from_user.id, "orders"):
        return
    _send_orders_list(message.chat.id, page=0)


@bot.message_handler(func=lambda m: bool(m.chat.type == "private" and m.text and m.text.strip() == "🎲 Рандомная игра"), content_types=["text"])
def handle_menu_random_game(message: Message) -> None:
    if not _require_subscription(message.chat.id, message.from_user.id, "random_game"):
        return
    _play_random_game(message)


@bot.message_handler(func=lambda m: bool(m.chat.type == "private" and m.text and m.text.strip() == "👤 Профиль"), content_types=["text"])
def handle_menu_profile(message: Message) -> None:
    if not _require_subscription(message.chat.id, message.from_user.id, "profile"):
        return
    handle_social_profile(message)


@bot.message_handler(func=lambda m: bool(m.chat.type == "private" and m.text and m.text.strip() == "🔥 Самая разыскиваемая"), content_types=["text"])
def handle_menu_most_wanted(message: Message) -> None:
    if not _require_subscription(message.chat.id, message.from_user.id, "most_wanted"):
        return
    _send_most_wanted(message.chat.id)


@bot.message_handler(commands=["топигр", "mostwanted", "разыскиваемые"])
def handle_most_wanted_command(message: Message) -> None:
    if not _require_subscription(message.chat.id, message.from_user.id, "most_wanted_cmd"):
        return
    _send_most_wanted(message.chat.id)


@bot.message_handler(func=lambda m: bool(
    m.chat.type == "private" and m.text and m.from_user and
    _order_pending.get(m.from_user.id) == m.chat.id and
    m.text.strip().lower() == "отмена"
), content_types=["text"])
def handle_order_cancel(message: Message) -> None:
    _order_pending.pop(message.from_user.id, None)
    bot.send_message(message.chat.id, "❌ <b>Заказ отменён.</b>")


@bot.message_handler(func=lambda m: bool(
    m.chat.type == "private" and m.text and m.from_user and
    _order_pending.get(m.from_user.id) == m.chat.id
), content_types=["text"])
def handle_order_text(message: Message) -> None:
    user_id = message.from_user.id
    chat_id = _order_pending.pop(user_id)
    name = " ".join(message.text.strip().split())
    if len(name) < 2:
        bot.send_message(chat_id, "⚠️ Название слишком короткое. Напиши нормальное название игры.")
        _order_pending[user_id] = chat_id
        return
    if len(name) > 100:
        bot.send_message(chat_id, "⚠️ Название слишком длинное. До 100 символов, пожалуйста.")
        _order_pending[user_id] = chat_id
        return
    key = _normalize_game_name(name)
    conn = _orders_db()
    now = datetime.now().isoformat(timespec="seconds")
    row = conn.execute("SELECT * FROM game_requests WHERE name_key=?", (key,)).fetchone()
    if row and row['status'] == 'open':
        request_id = int(row['id'])
        conn.execute("UPDATE game_requests SET updated_at=? WHERE id=?", (now, request_id))
        created = False
    elif row:
        request_id = int(row['id'])
        conn.execute("UPDATE game_requests SET name=?,updated_at=?,status='open' WHERE id=?", (name, now, request_id))
        created = False
    else:
        cur = conn.execute("INSERT INTO game_requests(name,name_key,first_user_id,created_at,updated_at,status) VALUES(?,?,?,?,?,'open')", (name,key,user_id,now,now))
        request_id = int(cur.lastrowid)
        created = True
    conn.execute("INSERT INTO game_request_votes(request_id,user_id,vote,updated_at) VALUES(?,?,1,?) ON CONFLICT(request_id,user_id) DO UPDATE SET vote=1,updated_at=excluded.updated_at", (request_id,user_id,now))
    conn.commit()
    conn.close()
    if created:
        bot.send_message(chat_id, f"✅ <b>Заявка добавлена!</b>\n\n{_request_summary(request_id)}\n\nЕсли игра нужна — жми 👍, если нет — 👎.", reply_markup=_order_keyboard(request_id))
        # Уведомляем всех админов, чтобы они видели спрос даже если не открывают стол.
        for admin_id in ADMIN_IDS:
            try:
                bot.send_message(admin_id, f"📥 <b>Новый заказ игры</b>\n\n{_request_summary(request_id)}\n\n👤 От: {_social_user_label(chat_id,user_id)}", reply_markup=_order_keyboard(request_id))
            except Exception as exc:
                logger.warning("Order admin notification failed for %s: %s", admin_id, exc)
    else:
        bot.send_message(chat_id, f"👍 <b>Твой голос добавлен к уже существующей заявке.</b>\n\n{_request_summary(request_id)}", reply_markup=_order_keyboard(request_id))


@bot.message_handler(commands=["dice", "кости"])
def handle_dice_command(message: Message) -> None:
    user_id = message.from_user.id
    now = time.time()
    last_roll = _dice_cooldown.get(user_id, 0)
    if now - last_roll < 60:
        bot.send_message(message.chat.id, f"⏳ Подожди {int(60 - (now - last_roll))} секунд перед следующим броском!")
        return
    if not _require_subscription(message.chat.id, user_id, "dice"):
        return
    _dice_cooldown[user_id] = now
    try:
        sent = bot.send_dice(message.chat.id, emoji="🎲")
        value = getattr(sent.dice, "value", None)
        if value is not None:
            bot.send_message(message.chat.id, f"🎲 Выпало: <b>{value}</b>")
    except Exception:
        logger.exception("Dice error")
        bot.send_message(message.chat.id, "❌ Не удалось бросить кости. Попробуй ещё раз.")


def _edit_orders_list(chat_id: int, message_id: int, page: int = 0, page_size: int = 8) -> None:
    conn = _orders_db()
    total = int(conn.execute("SELECT COUNT(*) n FROM game_requests WHERE status='open'").fetchone()["n"])
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    rows = conn.execute("""
        SELECT r.id,r.name,
               COALESCE(SUM(CASE WHEN v.vote=1 THEN 1 ELSE 0 END),0) likes,
               COALESCE(SUM(CASE WHEN v.vote=-1 THEN 1 ELSE 0 END),0) dislikes,
               COUNT(v.user_id) voters
        FROM game_requests r LEFT JOIN game_request_votes v ON v.request_id=r.id
        WHERE r.status='open' GROUP BY r.id
        ORDER BY (likes-dislikes) DESC,voters DESC,r.updated_at DESC LIMIT ? OFFSET ?
    """, (page_size,page*page_size)).fetchall()
    conn.close()
    if not rows:
        bot.edit_message_text("📋 <b>Стол заказов пуст.</b>", chat_id, message_id)
        return
    lines=[f"📋 <b>СТОЛ ЗАКАЗОВ</b> · {total} заявок","","Самые востребованные игры сверху 👇",""]
    for i,r in enumerate(rows,page*page_size+1):
        lines.append(f"<b>{i}. 🎮 {html.escape(r['name'])}</b>\n   👥 {int(r['voters'])} · 👍 {int(r['likes'])} · 👎 {int(r['dislikes'])}")
    markup=InlineKeyboardMarkup(row_width=1)
    for r in rows: markup.add(InlineKeyboardButton(f"🎮 {str(r['name'])[:45]}",callback_data=f"order_open:{int(r['id'])}"))
    nav=[]
    if page>0: nav.append(InlineKeyboardButton("⬅️",callback_data=f"orders_page:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{pages}",callback_data="orders_noop"))
    if page<pages-1: nav.append(InlineKeyboardButton("➡️",callback_data=f"orders_page:{page+1}"))
    markup.row(*nav)
    bot.edit_message_text("\n".join(lines),chat_id,message_id,reply_markup=markup)


def _send_orders_list(chat_id: int, page: int = 0, page_size: int = 8) -> None:
    conn = _orders_db()
    total = int(conn.execute("SELECT COUNT(*) n FROM game_requests WHERE status='open'").fetchone()["n"])
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    rows = conn.execute("""
        SELECT r.id,r.name,r.status,
               COALESCE(SUM(CASE WHEN v.vote=1 THEN 1 ELSE 0 END),0) likes,
               COALESCE(SUM(CASE WHEN v.vote=-1 THEN 1 ELSE 0 END),0) dislikes,
               COUNT(v.user_id) voters
        FROM game_requests r LEFT JOIN game_request_votes v ON v.request_id=r.id
        WHERE r.status='open'
        GROUP BY r.id
        ORDER BY (likes - dislikes) DESC, voters DESC, r.updated_at DESC
        LIMIT ? OFFSET ?
    """, (page_size, page * page_size)).fetchall()
    conn.close()
    if not rows:
        bot.send_message(chat_id, "📋 <b>Стол заказов пуст.</b>\n\nПока никто ничего не заказал.")
        return
    lines = [f"📋 <b>СТОЛ ЗАКАЗОВ</b> · {total} заявок", "", "Самые востребованные игры сверху 👇", ""]
    for i, r in enumerate(rows, page * page_size + 1):
        lines.append(f"<b>{i}. 🎮 {html.escape(r['name'])}</b>\n   👥 {int(r['voters'])} · 👍 {int(r['likes'])} · 👎 {int(r['dislikes'])}")
    markup = InlineKeyboardMarkup(row_width=1)
    for r in rows:
        markup.add(InlineKeyboardButton(f"🎮 {str(r['name'])[:45]}", callback_data=f"order_open:{int(r['id'])}"))
    nav=[]
    if page > 0: nav.append(InlineKeyboardButton("⬅️", callback_data=f"orders_page:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="orders_noop"))
    if page < pages - 1: nav.append(InlineKeyboardButton("➡️", callback_data=f"orders_page:{page+1}"))
    markup.row(*nav)
    bot.send_message(chat_id, "\n".join(lines), reply_markup=markup)


def _play_random_game(message: Message) -> None:
    user_id = message.from_user.id
    now = time.time()
    conn = _orders_db()
    row = conn.execute("SELECT last_used FROM random_game_cooldowns WHERE user_id=?", (user_id,)).fetchone()
    if row:
        remaining = RANDOM_GAME_COOLDOWN - (now - float(row['last_used']))
        if remaining > 0:
            mins = int(remaining // 60)
            secs = int(remaining % 60)
            conn.close()
            bot.send_message(message.chat.id, f"⏳ <b>Рандом уже использован.</b>\nПопробуй снова через <b>{mins} мин. {secs:02d} сек.</b>")
            return
    # Получаем список существующих игр из основной БД. Это сохраняет совместимость со старой базой.
    rows = database.list_links()
    if not rows:
        conn.close()
        bot.send_message(message.chat.id, "📭 В базе пока нет игр для рандома.")
        return
    chosen = random.choice(rows)
    conn.execute("INSERT INTO random_game_cooldowns(user_id,last_used) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET last_used=excluded.last_used", (user_id, now))
    conn.commit()
    conn.close()
    bot.send_message(message.chat.id, f"🎲 <b>Тебе выпала случайная игра!</b>\n\n🎮 <b>{html.escape(str(chosen['content_text']))}</b>")
    try:
        _deliver_link(message.chat.id, chosen)
    except Exception:
        logger.exception("Random game delivery failed")
        bot.send_message(message.chat.id, "⚠️ Игра выбрана, но файл не удалось отправить. Администратору стоит проверить эту запись.")


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

    # Засчитываем только те игры, которые реально показали пользователю.
    # Повторные поиски одним и тем же пользователем рейтинг не накручивают.
    _record_game_searches(message.from_user.id, filtered_rows[:15])

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


# ── BERLIONS MONEY SYSTEM ─────────────────────────────────────────────────────
# Отдельная система игровой валюты.

from datetime import datetime

MONEY_DB = "money.db"
MONEY_START_BALANCE = 10_000.0
MONEY_DAILY_BONUS = 1_000.0
MONEY_WORK_COOLDOWN = 30 * 60
_money_work_cooldown: dict[int, float] = {}


def _money_db():
    conn = sqlite3.connect(MONEY_DB, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _money_init_db() -> None:
    conn = _money_db()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS wallets ("
        "user_id INTEGER PRIMARY KEY,"
        "balance REAL NOT NULL DEFAULT 10000,"
        "last_bonus TEXT,"
        "last_work TEXT"
        ")"
    )
    conn.commit()
    conn.close()


_money_init_db()


def _money_ensure(user_id: int):
    conn = _money_db()
    conn.execute(
        "INSERT OR IGNORE INTO wallets(user_id,balance) VALUES(?,?)",
        (user_id, MONEY_START_BALANCE),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM wallets WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row


def _money_change(user_id: int, delta: float) -> float:
    conn = _money_db()
    conn.execute(
        "INSERT OR IGNORE INTO wallets(user_id,balance) VALUES(?,?)",
        (user_id, MONEY_START_BALANCE),
    )
    conn.execute("UPDATE wallets SET balance=balance+? WHERE user_id=?", (delta, user_id))
    row = conn.execute("SELECT balance FROM wallets WHERE user_id=?", (user_id,)).fetchone()
    conn.commit()
    conn.close()
    return float(row["balance"])


@bot.message_handler(commands=["balance", "баланс"])
def handle_money_balance(message: Message) -> None:
    row = _money_ensure(message.from_user.id)
    bot.send_message(
        message.chat.id,
        f"💰 <b>Твой баланс</b>\n\n"
        f"💵 <b>{float(row['balance']):,.2f} ₽</b>\n\n"
        f"🎁 Ежедневный бонус: /бонус\n"
        f"💼 Работа: /работа",
    )


@bot.message_handler(commands=["bonus", "бонус"])
def handle_money_bonus(message: Message) -> None:
    user_id = message.from_user.id
    _money_ensure(user_id)
    today = datetime.now().date().isoformat()
    conn = _money_db()
    row = conn.execute(
        "SELECT balance,last_bonus FROM wallets WHERE user_id=?", (user_id,)
    ).fetchone()

    if row["last_bonus"] == today:
        conn.close()
        bot.send_message(
            message.chat.id,
            "⏳ <b>Бонус уже получен сегодня.</b>\nВозвращайся завтра!",
        )
        return

    new_balance = float(row["balance"]) + MONEY_DAILY_BONUS
    conn.execute(
        "UPDATE wallets SET balance=?,last_bonus=? WHERE user_id=?",
        (new_balance, today, user_id),
    )
    conn.commit()
    conn.close()

    bot.send_message(
        message.chat.id,
        f"🎁 <b>Ежедневный бонус!</b>\n\n"
        f"💵 +{MONEY_DAILY_BONUS:,.0f} ₽\n"
        f"💰 Баланс: <b>{new_balance:,.2f} ₽</b>",
    )


@bot.message_handler(commands=["работа", "work"])
def handle_money_work(message: Message) -> None:
    user_id = message.from_user.id
    _money_ensure(user_id)

    conn = _money_db()
    row = conn.execute(
        "SELECT balance,last_work FROM wallets WHERE user_id=?", (user_id,)
    ).fetchone()
    now = datetime.now()

    if row["last_work"]:
        try:
            elapsed = (now - datetime.fromisoformat(row["last_work"])).total_seconds()
            if elapsed < MONEY_WORK_COOLDOWN:
                mins = max(1, int((MONEY_WORK_COOLDOWN - elapsed + 59) // 60))
                conn.close()
                bot.send_message(
                    message.chat.id,
                    f"⏳ Ты уже работал.\nСледующая смена через <b>{mins} мин.</b>",
                )
                return
        except Exception:
            pass

    reward = random.randint(500, 3000)
    new_balance = float(row["balance"]) + reward
    conn.execute(
        "UPDATE wallets SET balance=?,last_work=? WHERE user_id=?",
        (new_balance, now.isoformat(timespec="seconds"), user_id),
    )
    conn.commit()
    conn.close()

    jobs = [
        "Отработал смену",
        "Выполнил заказ",
        "Подзаработал",
        "Закрыл рабочий день",
    ]
    bot.send_message(
        message.chat.id,
        f"💼 <b>{random.choice(jobs)}!</b>\n\n"
        f"💰 Получено: <b>+{reward:,} ₽</b>\n"
        f"💳 Баланс: <b>{new_balance:,.2f} ₽</b>\n\n"
        f"⏱ Следующая работа — через 30 минут.",
    )


@bot.message_handler(commands=["топденьги", "topmoney", "toptraders"])
def handle_money_top(message: Message) -> None:
    conn = _money_db()
    rows = conn.execute(
        "SELECT user_id,balance FROM wallets ORDER BY balance DESC LIMIT 20"
    ).fetchall()
    conn.close()

    if not rows:
        bot.send_message(message.chat.id, "🏆 Пока никто не имеет денег.")
        return

    lines = ["🏆 <b>ТОП ПО ДЕНЬГАМ</b>\n"]
    for i, row in enumerate(rows, 1):
        uid = int(row["user_id"])
        try:
            label = _social_user_label(
                message.chat.id,
                uid,
            )
        except Exception:
            label = f"Пользователь {uid}"
        lines.append(f"{i}. {label} — <b>{float(row['balance']):,.2f} ₽</b>")

    bot.send_message(message.chat.id, "\n".join(lines))


def _money_target(message: Message, arg: str = "") -> int | None:
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id

    try:
        target = _social_target(message, arg)
        if target:
            return target
    except Exception:
        pass

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
        bot.send_message(
            message.chat.id,
            "⛔ Только администратор может использовать эту команду.",
        )
        return

    parts = (message.text or "").split(maxsplit=2)
    if message.reply_to_message:
        target = message.reply_to_message.from_user.id
        amount_raw = parts[1] if len(parts) > 1 else ""
    else:
        target = _money_target(
            message,
            parts[1] if len(parts) > 1 else "",
        )
        amount_raw = parts[2] if len(parts) > 2 else ""

    if not target or not amount_raw.isdigit():
        bot.send_message(
            message.chat.id,
            "❓ Формат: <code>/забрать @user 1000</code> "
            "или ответом: <code>/забрать 1000</code>",
        )
        return

    amount = int(amount_raw)
    if amount <= 0:
        bot.send_message(message.chat.id, "❌ Сумма должна быть больше нуля.")
        return

    row = _money_ensure(target)
    old = float(row["balance"])
    taken = min(old, float(amount))
    new = old - taken

    conn = _money_db()
    conn.execute("UPDATE wallets SET balance=? WHERE user_id=?", (new, target))
    conn.commit()
    conn.close()

    try:
        target_label = _social_user_label(message.chat.id, target)
    except Exception:
        target_label = f"Пользователь {target}"

    bot.send_message(
        message.chat.id,
        f"💸 У {target_label} забрано <b>{taken:,.2f} ₽</b>.\n"
        f"💰 Новый баланс: <b>{new:,.2f} ₽</b>",
    )


@bot.message_handler(commands=["выдать"])
def handle_admin_give_money(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        bot.send_message(
            message.chat.id,
            "⛔ Только администратор может использовать эту команду.",
        )
        return

    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit() or int(parts[1]) <= 0:
        bot.send_message(
            message.chat.id,
            "❓ Формат: <code>/выдать 100000</code>",
        )
        return

    amount = int(parts[1])
    new_balance = _money_change(message.from_user.id, amount)

    bot.send_message(
        message.chat.id,
        f"💰 Тебе выдано <b>+{amount:,} ₽</b>.\n"
        f"Баланс: <b>{new_balance:,.2f} ₽</b>",
    )

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
    # где get_chat_member недоступен.
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
        "💰 <b>Деньги</b>\n"
        "• <code>/баланс</code> — мой баланс\n"
        "• <code>/бонус</code> — ежедневный бонус\n"
        "• <code>/работа</code> — заработать раз в 30 минут\n"
        "• <code>/топденьги</code> — топ игроков по деньгам\n\n"
        "🎲 <code>/кости</code> — кости\n"
        "🔎 <code>/поиск запрос</code> — поиск\n\n"
        "💡 Основные функции доступны через меню-кнопки под полем ввода."
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


def _play_dice_legacy(chat_id: int, user_id: int) -> None:
    now = time.time()
    last_roll = _dice_cooldown.get(user_id, 0)
    if now - last_roll < 60:
        bot.send_message(chat_id, f"⏳ Подожди {int(60 - (now - last_roll))} секунд перед следующим броском!")
        return
    if not _require_subscription(chat_id, user_id, "dice"):
        return
    _dice_cooldown[user_id] = now
    try:
        sent = bot.send_dice(chat_id, emoji="🎲")
        value = getattr(sent.dice, "value", None)
        if value is not None:
            bot.send_message(chat_id, f"🎲 Выпало: <b>{value}</b>")
    except Exception:
        logger.exception("Dice callback error")
        bot.send_message(chat_id, "❌ Не удалось бросить кости. Попробуй ещё раз.")


@bot.callback_query_handler(func=lambda call: bool(call.data and (
    call.data.startswith("verify:") or call.data.startswith("search_pick:") or call.data == "play_dice" or
    call.data.startswith("order_vote:") or call.data.startswith("order_open:") or call.data.startswith("orders_page:") or
    call.data.startswith("order_close:") or call.data == "orders_noop" or call.data.startswith("marry_yes:") or call.data.startswith("marry_no:")
)))
def handle_bot_callbacks(call: CallbackQuery) -> None:
    try:
        data = call.data or ""
        if data.startswith("verify:"):
            context = data[len("verify:"):]
            if not _is_subscribed(call.from_user.id):
                bot.answer_callback_query(call.id, "❌ Сначала подпишись на канал и чат.", show_alert=True)
                return
            bot.answer_callback_query(call.id, "✅ Подписка подтверждена!")
            try: bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception: pass
            if context == "dice":
                _play_dice_legacy(call.message.chat.id, call.from_user.id)
            elif context.startswith("key:"):
                row = database.get_link(context[len("key:"):])
                if row: _deliver_link(call.message.chat.id, row)
                else: bot.send_message(call.message.chat.id, "❌ Ссылка не найдена.")
            return
        if data.startswith("search_pick:"):
            key = data[len("search_pick:"):]
            if not _require_subscription(call.message.chat.id, call.from_user.id, "search"):
                bot.answer_callback_query(call.id, "🔒 Нужна подписка.", show_alert=True)
                return
            bot.answer_callback_query(call.id)
            try: bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception: pass
            row = database.get_link(key)
            if row: _deliver_link(call.message.chat.id, row)
            else: bot.send_message(call.message.chat.id, "❌ Контент не найден.")
            return
        if data == "play_dice":
            bot.answer_callback_query(call.id)
            _play_dice_legacy(call.message.chat.id, call.from_user.id)
            return
        if data == "orders_noop":
            bot.answer_callback_query(call.id)
            return
        if data.startswith("orders_page:"):
            page = max(0, int(data.split(":",1)[1]))
            bot.answer_callback_query(call.id)
            # Редактируем список, чтобы чат не засорялся.
            _edit_orders_list(call.message.chat.id, call.message.message_id, page)
            return
        if data.startswith("order_open:"):
            request_id = int(data.split(":",1)[1])
            bot.answer_callback_query(call.id)
            detail_markup = _order_keyboard(request_id)
            if _is_admin(call.from_user.id):
                detail_markup.add(InlineKeyboardButton("✅ Игра добавлена — закрыть заявку", callback_data=f"order_close:{request_id}"))
            bot.edit_message_text(_request_summary(request_id) + "\n\nЕсли игра тебе нужна — голосуй:", call.message.chat.id, call.message.message_id, reply_markup=detail_markup)
            return
        if data.startswith("order_close:"):
            if not _is_admin(call.from_user.id):
                bot.answer_callback_query(call.id, "⛔ Только администратор.", show_alert=True)
                return
            request_id = int(data.split(":",1)[1])
            conn = _orders_db()
            conn.execute("UPDATE game_requests SET status='closed',updated_at=? WHERE id=?", (datetime.now().isoformat(timespec="seconds"), request_id))
            conn.commit(); conn.close()
            bot.answer_callback_query(call.id, "✅ Заявка закрыта.")
            _edit_orders_list(call.message.chat.id, call.message.message_id, 0)
            return
        if data.startswith("order_vote:"):
            _, sign, raw_id = data.split(":",2)
            request_id = int(raw_id)
            vote = 1 if sign == "+" else -1
            conn = _orders_db()
            exists = conn.execute("SELECT id FROM game_requests WHERE id=? AND status='open'", (request_id,)).fetchone()
            if not exists:
                conn.close(); bot.answer_callback_query(call.id, "Заявка уже закрыта.", show_alert=True); return
            now = datetime.now().isoformat(timespec="seconds")
            old = conn.execute("SELECT vote FROM game_request_votes WHERE request_id=? AND user_id=?", (request_id, call.from_user.id)).fetchone()
            if old and int(old['vote']) == vote:
                conn.execute("DELETE FROM game_request_votes WHERE request_id=? AND user_id=?", (request_id, call.from_user.id))
                message = "Голос убран."
            else:
                conn.execute("INSERT INTO game_request_votes(request_id,user_id,vote,updated_at) VALUES(?,?,?,?) ON CONFLICT(request_id,user_id) DO UPDATE SET vote=excluded.vote,updated_at=excluded.updated_at", (request_id,call.from_user.id,vote,now))
                message = "👍 Учтено!" if vote == 1 else "👎 Учтено!"
            conn.commit(); conn.close()
            bot.answer_callback_query(call.id, message)
            try:
                refresh_markup = _order_keyboard(request_id)
                if _is_admin(call.from_user.id):
                    refresh_markup.add(InlineKeyboardButton("✅ Игра добавлена — закрыть заявку", callback_data=f"order_close:{request_id}"))
                bot.edit_message_text(_request_summary(request_id) + "\n\nЕсли игра тебе нужна — голосуй:", call.message.chat.id, call.message.message_id, reply_markup=refresh_markup)
            except Exception:
                pass
            return
        if data.startswith("marry_yes:") or data.startswith("marry_no:"):
            handle_social_marriage_callback(call)
            return
    except Exception:
        logger.exception("Bot callback error")
        try: bot.answer_callback_query(call.id, "❌ Ошибка. Попробуй ещё раз.", show_alert=True)
        except Exception: pass


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
