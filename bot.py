"""
Telegram bot with deep-link support, subscription gate, dice game,
and an admin link manager.

Environment variables (Replit Secrets):
  BOT_TOKEN  — token from @BotFather
  ADMIN_ID   — primary admin Telegram user ID (integer)

IMPORTANT: Add this bot as an Administrator to @Berlions_mb so it can
check member status via getChatMember.
"""

import os
import time
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

import database

# ── Configuration ─────────────────────────────────────────────────────────────

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "")

if not BOT_TOKEN:
    raise EnvironmentError("BOT_TOKEN is not set. Add it to Replit Secrets.")

CHANNEL_USERNAME = "@Berlions_mb"
CHANNEL_URL = "https://t.me/Berlions_mb"

EXTRA_ADMIN_IDS: set[int] = {7334606634}

ADMIN_IDS: set[int] = set(EXTRA_ADMIN_IDS)
for _raw in ADMIN_ID_RAW.split(","):
    _raw = _raw.strip()
    if _raw.isdigit():
        ADMIN_IDS.add(int(_raw))

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger(__name__)
logger.info("Admin IDs: %s", ADMIN_IDS)

# ── Bot setup ─────────────────────────────────────────────────────────────────

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
database.init_db()

# ── In-memory conversation state ──────────────────────────────────────────────

_pending: dict[int, dict] = {}

# ── Subscription gate ─────────────────────────────────────────────────────────

def _is_subscribed(user_id: int) -> bool:
    """Return True if the user is a member of CHANNEL_USERNAME."""
    try:
        member = bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status not in ("left", "kicked")
    except Exception as exc:
        logger.warning("get_chat_member failed for %s: %s", user_id, exc)
        # If we can't check (bot not admin in channel), allow through
        return True


def _sub_required_markup(context: str) -> InlineKeyboardMarkup:
    """
    Inline keyboard with Subscribe and Verified buttons.
    context: "dice" or a deep-link key string.
    """
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("📢 Подписаться на канал", url=CHANNEL_URL),
        InlineKeyboardButton("✅ Я подписался", callback_data=f"verify:{context}"),
    )
    return markup


def _require_subscription(chat_id: int, user_id: int, context: str) -> bool:
    """
    Check subscription. If not subscribed, send the gate message and return False.
    Returns True if the user may proceed.
    """
    if _is_subscribed(user_id):
        return True

    bot.send_message(
        chat_id,
        "🔒 <b>Доступ закрыт</b>\n\n"
        "Чтобы получить контент, нужно подписаться на наш канал.\n\n"
        "После подписки нажми кнопку <b>«✅ Я подписался»</b>.",
        reply_markup=_sub_required_markup(context),
    )
    return False


# ── Delivery helper ───────────────────────────────────────────────────────────

def _deliver_link(chat_id: int, row: database.sqlite3.Row) -> None:
    """Send link content as a single cohesive unit."""
    files = database.get_link_files(row["key"])
    caption = row["content_text"]

    # No files — plain text or URL button
    if not files:
        if row["target_url"]:
            markup = InlineKeyboardMarkup()
            markup.add(
                InlineKeyboardButton("⬇️ Скачать / Перейти", url=row["target_url"])
            )
            bot.send_message(chat_id, caption, reply_markup=markup)
        else:
            bot.send_message(chat_id, caption)
        return

    # Single file — send with caption inline
    if len(files) == 1:
        _send_file(chat_id, files[0]["file_id"], files[0]["file_type"], caption=caption)
        return

    # Multiple files — group by type for media groups
    visuals = [f for f in files if f["file_type"] in ("photo", "video")]
    docs    = [f for f in files if f["file_type"] == "document"]
    others  = [f for f in files if f["file_type"] not in ("photo", "video", "document")]

    caption_used = False

    def _cap() -> str | None:
        nonlocal caption_used
        if caption_used:
            return None
        caption_used = True
        return caption

    # Photos / videos → media group
    if visuals:
        group = []
        for i, f in enumerate(visuals):
            c = _cap() if i == 0 else None
            if f["file_type"] == "photo":
                group.append(InputMediaPhoto(f["file_id"], caption=c, parse_mode="HTML" if c else None))
            else:
                group.append(InputMediaVideo(f["file_id"], caption=c, parse_mode="HTML" if c else None))
        bot.send_media_group(chat_id, group)

    # Documents → media group (Telegram supports document albums)
    if docs:
        if len(docs) == 1:
            _send_file(chat_id, docs[0]["file_id"], "document", caption=_cap())
        else:
            group = []
            for i, f in enumerate(docs):
                c = _cap() if i == 0 else None
                group.append(InputMediaDocument(f["file_id"], caption=c, parse_mode="HTML" if c else None))
            bot.send_media_group(chat_id, group)

    # Audio / voice / animation — send individually
    for f in others:
        _send_file(chat_id, f["file_id"], f["file_type"], caption=_cap())

    # Fallback: if nothing carried the caption, send as separate message
    if not caption_used:
        bot.send_message(chat_id, caption)


# ── File helpers ──────────────────────────────────────────────────────────────

def _extract_file(message: Message) -> tuple[str, str] | tuple[None, None]:
    if message.document:
        return message.document.file_id, "document"
    if message.photo:
        return message.photo[-1].file_id, "photo"
    if message.video:
        return message.video.file_id, "video"
    if message.audio:
        return message.audio.file_id, "audio"
    if message.voice:
        return message.voice.file_id, "voice"
    if message.animation:
        return message.animation.file_id, "animation"
    return None, None


def _send_file(chat_id: int, file_id: str, file_type: str,
               caption: str | None = None) -> None:
    senders = {
        "document":  bot.send_document,
        "photo":     bot.send_photo,
        "video":     bot.send_video,
        "audio":     bot.send_audio,
        "voice":     bot.send_voice,
        "animation": bot.send_animation,
    }
    fn = senders.get(file_type, bot.send_document)
    kwargs: dict = {}
    if caption:
        kwargs["caption"] = caption
    fn(chat_id, file_id, **kwargs)


def _files_added_reply(chat_id: int, count: int) -> None:
    bot.send_message(
        chat_id,
        f"✅ Файл добавлен (<b>{count}</b> шт.). "
        "Отправьте ещё файл или напишите /stop для сохранения.",
    )


# ── Admin helpers ─────────────────────────────────────────────────────────────

def _is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def _bot_username() -> str:
    try:
        return bot.get_me().username or "MyBot"
    except Exception:
        return "MyBot"


def _deep_link(key: str) -> str:
    return f"https://t.me/{_bot_username()}?start={key}"


# ── /start ────────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["start"])
def handle_start(message: Message) -> None:
    _pending.pop(message.from_user.id, None)

    parts = message.text.strip().split(maxsplit=1)
    key = parts[1].strip() if len(parts) > 1 else None

    if not key:
        # Plain /start — show welcome + dice button
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🎲 Сыграть в кости", callback_data="play_dice"))
        bot.send_message(
            message.chat.id,
            "👋 <b>Привет!</b>\n\n"
            "Я помогаю получить контент по специальным ссылкам из каналов.\n"
            "Перейди по ссылке из канала, чтобы получить нужный файл или игру.\n\n"
            "Также можешь сыграть в кости — нажми кнопку ниже! 🎲",
            reply_markup=markup,
        )
        return

    # Deep-link — subscription check first
    if not _require_subscription(message.chat.id, message.from_user.id, f"key:{key}"):
        return

    row = database.get_link(key)
    if row is None:
        bot.send_message(message.chat.id, "❌ Ссылка не найдена или устарела.")
        logger.warning("Unknown deep-link key: %r", key)
        return

    _deliver_link(message.chat.id, row)
    logger.info("Served key %r to user %s", key, message.from_user.id)


# ── /search ───────────────────────────────────────────────────────────────────

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
    rows = database.search_links(query)

    if not rows:
        bot.send_message(
            message.chat.id,
            f"😔 По запросу <b>«{query}»</b> ничего не найдено.\n\n"
            "Попробуй другое название.",
        )
        return

    markup = InlineKeyboardMarkup(row_width=1)
    for row in rows[:15]:  # максимум 15 кнопок
        title = row["content_text"]
        # Обрезаем длинные названия для кнопки
        btn_label = title if len(title) <= 60 else title[:57] + "…"
        markup.add(
            InlineKeyboardButton(
                f"🎮 {btn_label}",
                callback_data=f"search_pick:{row['key']}",
            )
        )

    count = len(rows)
    bot.send_message(
        message.chat.id,
        f"🔍 По запросу <b>«{query}»</b> найдено результатов: <b>{count}</b>\n\n"
        "Выбери что тебя интересует 👇",
        reply_markup=markup,
    )


# ── /dice command ─────────────────────────────────────────────────────────────

@bot.message_handler(commands=["dice"])
def handle_dice_command(message: Message) -> None:
    if not _require_subscription(message.chat.id, message.from_user.id, "dice"):
        return
    _play_dice(message.chat.id)


# ── Dice game logic ───────────────────────────────────────────────────────────

def _play_dice(chat_id: int) -> None:
    """Send a dice and announce the result after animation."""
    bot.send_message(chat_id, "🎲 Бросаю кубик…")
    dice_msg = bot.send_dice(chat_id, emoji="🎲")
    value = dice_msg.dice.value

    # Wait for the animation to finish (~3 seconds)
    time.sleep(3)

    if value < 3:
        result = (
            f"😢 <b>Выпало {value}</b> — ты проиграл!\n\n"
            "Меньше 3 — не повезло. Попробуй ещё раз!"
        )
    elif value > 5:
        result = (
            f"🏆 <b>Выпало {value}</b> — ты победил!\n\n"
            "Максимум! Ты настоящий везунчик 🎉"
        )
    else:
        result = (
            f"😐 <b>Выпало {value}</b> — ничья!\n\n"
            "Не выиграл, но и не проиграл. Попробуй снова?"
        )

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🎲 Играть ещё", callback_data="play_dice"))
    bot.send_message(chat_id, result, reply_markup=markup)


# ── Callback handler ──────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call: CallbackQuery) -> None:
    data = call.data
    user_id = call.from_user.id
    chat_id = call.message.chat.id

    # ── Subscription verified — retry original action ─────────────────────────
    if data.startswith("verify:"):
        context = data[len("verify:"):]

        if not _is_subscribed(user_id):
            bot.answer_callback_query(
                call.id,
                "❌ Ты ещё не подписан! Подпишись и попробуй снова.",
                show_alert=True,
            )
            return

        bot.answer_callback_query(call.id, "✅ Подписка подтверждена!")
        # Remove the gate message
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception:
            pass

        if context == "dice":
            _play_dice(chat_id)
        elif context.startswith("key:"):
            key = context[len("key:"):]
            row = database.get_link(key)
            if row is None:
                bot.send_message(chat_id, "❌ Ссылка не найдена или устарела.")
                return
            _deliver_link(chat_id, row)
            logger.info("Served key %r to user %s (after sub verify)", key, user_id)
        return

    # ── Search result picked ──────────────────────────────────────────────────
    if data.startswith("search_pick:"):
        key = data[len("search_pick:"):]
        bot.answer_callback_query(call.id)
        if not _require_subscription(chat_id, user_id, f"key:{key}"):
            return
        row = database.get_link(key)
        if row is None:
            bot.send_message(chat_id, "❌ Контент не найден — возможно, он был удалён.")
            return
        _deliver_link(chat_id, row)
        logger.info("Served key %r to user %s (via search)", key, user_id)
        return

    # ── Dice game ─────────────────────────────────────────────────────────────
    if data == "play_dice":
        bot.answer_callback_query(call.id)
        if not _require_subscription(chat_id, user_id, "dice"):
            return
        _play_dice(chat_id)
        return

    bot.answer_callback_query(call.id)


# ── /stop — finalize file collection ─────────────────────────────────────────

@bot.message_handler(commands=["stop"])
def handle_stop(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return

    state = _pending.get(message.from_user.id)
    if state is None or state["step"] not in ("collecting_files", "edit_collecting_files"):
        bot.send_message(
            message.chat.id,
            "ℹ️ /stop используется только во время добавления файлов.",
        )
        return

    files: list[tuple[str, str]] = state.get("files", [])
    step = state["step"]

    if step == "collecting_files":
        if not files:
            bot.send_message(
                message.chat.id,
                "⚠️ Ты не добавил ни одного файла. Отправь хотя бы один файл или URL.",
            )
            return
        content_text = state["content_text"]
        del _pending[message.from_user.id]
        try:
            key = database.create_link(content_text=content_text, files=files)
        except Exception as exc:
            logger.exception("create_link failed: %s", exc)
            bot.send_message(message.chat.id, f"❌ Ошибка при сохранении: {exc}")
            return
        deep = _deep_link(key)
        bot.send_message(
            message.chat.id,
            f"✅ <b>Ссылка создана!</b> ({len(files)} файл(ов))\n\n"
            f"🔑 Ключ: <code>{key}</code>\n\n"
            f"📎 Вставь эту ссылку в канал:\n<code>{deep}</code>",
            disable_web_page_preview=True,
        )
        logger.info("Admin created key %r with %d file(s)", key, len(files))

    elif step == "edit_collecting_files":
        key = state["key"]
        new_text = state.get("new_text")
        if not files and new_text is None:
            # nothing changed — just cancel
            del _pending[message.from_user.id]
            bot.send_message(message.chat.id, f"ℹ️ Редактирование <code>{key}</code> отменено.")
            return
        del _pending[message.from_user.id]
        try:
            database.update_link(
                key,
                content_text=new_text,
                files=files if files else None,
                clear_url=bool(files),
            )
        except Exception as exc:
            bot.send_message(message.chat.id, f"❌ Ошибка: {exc}")
            return
        bot.send_message(
            message.chat.id,
            f"✅ Ключ <code>{key}</code> обновлён"
            + (f" — {len(files)} файл(ов) сохранено." if files else " (текст обновлён)."),
        )
        logger.info("Admin edited key %r → %d file(s)", key, len(files))


# ── Admin: /add ───────────────────────────────────────────────────────────────

@bot.message_handler(commands=["add"])
def handle_add(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "⛔ У тебя нет доступа к этой команде.")
        return
    _pending[message.from_user.id] = {"step": "awaiting_text"}
    bot.send_message(
        message.chat.id,
        "📝 <b>Шаг 1/2</b> — Отправь описание / текст, который увидит пользователь:",
    )


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
        if files:
            content_type = f"📎 {len(files)} файл(ов)"
        elif row["target_url"]:
            content_type = "🔗 ссылка"
        else:
            content_type = "📄 текст"
        lines.append(
            f"🔑 <code>{row['key']}</code>  ({created})  [{content_type}]\n"
            f"   📄 {row['content_text'][:60]}"
            f"{'…' if len(row['content_text']) > 60 else ''}\n"
            f"   👉 <a href='{_deep_link(row['key'])}'>Ссылка для канала</a>\n"
        )
    bot.send_message(message.chat.id, "\n".join(lines), disable_web_page_preview=True)


# ── Admin: /delete ────────────────────────────────────────────────────────────

@bot.message_handler(commands=["delete"])
def handle_delete(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "⛔ У тебя нет доступа к этой команде.")
        return
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        bot.send_message(
            message.chat.id,
            "❓ Укажи ключ: <code>/delete ключ</code>\n"
            "Ключи можно посмотреть командой /list",
        )
        return
    key = parts[1].strip()
    row = database.get_link(key)
    if row is None:
        bot.send_message(message.chat.id, f"❌ Ключ <code>{key}</code> не найден.")
        return
    database.delete_link(key)
    bot.send_message(
        message.chat.id,
        f"🗑 Ссылка <code>{key}</code> удалена.\n"
        f"📄 Текст был: {row['content_text'][:80]}",
    )
    logger.info("Admin deleted key %r", key)


# ── Admin: /edit ──────────────────────────────────────────────────────────────

@bot.message_handler(commands=["edit"])
def handle_edit(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "⛔ У тебя нет доступа к этой команде.")
        return
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        bot.send_message(
            message.chat.id,
            "❓ Укажи ключ: <code>/edit ключ</code>\n"
            "Ключи можно посмотреть командой /list",
        )
        return
    key = parts[1].strip()
    row = database.get_link(key)
    if row is None:
        bot.send_message(message.chat.id, f"❌ Ключ <code>{key}</code> не найден.")
        return
    files = database.get_link_files(key)
    if files:
        content_type = f"📎 {len(files)} файл(ов)"
    elif row["target_url"]:
        content_type = f"🔗 {row['target_url']}"
    else:
        content_type = "📄 только текст"
    bot.send_message(
        message.chat.id,
        f"✏️ <b>Редактируем</b> <code>{key}</code>\n\n"
        f"📄 Текст: {row['content_text']}\n"
        f"Контент: {content_type}\n\n"
        f"<b>Шаг 1/2</b> — Отправь новый текст описания.\n"
        f"Чтобы оставить текущий — отправь <code>-</code>",
    )
    _pending[message.from_user.id] = {"step": "edit_text", "key": key}


# ── Admin conversation ────────────────────────────────────────────────────────

@bot.message_handler(
    func=lambda m: (
        m.from_user.id in _pending
        and not (m.text and m.text.startswith("/"))
    ),
    content_types=[
        "text", "document", "photo", "video",
        "audio", "voice", "animation",
    ],
)
def handle_conversation(message: Message) -> None:
    state = _pending.get(message.from_user.id)
    if state is None:
        return
    step = state["step"]

    # ══ /add — step 1 ════════════════════════════════════════════════════════
    if step == "awaiting_text":
        if message.content_type != "text":
            bot.send_message(message.chat.id, "⚠️ На этом шаге нужен текст:")
            return
        state["content_text"] = message.text.strip()
        state["step"] = "collecting_files"
        state["files"] = []
        bot.send_message(
            message.chat.id,
            "📎 <b>Шаг 2/2</b> — Отправь файл(ы) (фото, видео, документ…).\n\n"
            "• Добавляй по одному — бот подтвердит каждый.\n"
            "• Когда всё загружено — напиши /stop для сохранения.\n"
            "• Или отправь текстом <b>ссылку</b> (http://…) вместо файла.",
        )

    # ══ /add — step 2: collect files or URL ══════════════════════════════════
    elif step == "collecting_files":
        file_id, file_type = _extract_file(message)
        if file_id:
            state["files"].append((file_id, file_type))
            _files_added_reply(message.chat.id, len(state["files"]))
        elif message.content_type == "text":
            url = message.text.strip()
            if not (url.startswith("http://") or url.startswith("https://")):
                bot.send_message(
                    message.chat.id,
                    "⚠️ Ссылка должна начинаться с <code>http://</code> или "
                    "<code>https://</code>. Попробуй ещё раз или отправь файл.",
                )
                return
            content_text = state["content_text"]
            del _pending[message.from_user.id]
            try:
                key = database.create_link(content_text=content_text, target_url=url)
            except Exception as exc:
                logger.exception("create_link failed: %s", exc)
                bot.send_message(message.chat.id, f"❌ Ошибка при сохранении: {exc}")
                return
            deep = _deep_link(key)
            bot.send_message(
                message.chat.id,
                f"✅ <b>Ссылка создана!</b>\n\n"
                f"🔑 Ключ: <code>{key}</code>\n\n"
                f"📎 Вставь эту ссылку в канал:\n<code>{deep}</code>",
                disable_web_page_preview=True,
            )
            logger.info("Admin created url-link key %r", key)
        else:
            bot.send_message(
                message.chat.id,
                "⚠️ Отправь файл, ссылку, или /stop чтобы сохранить добавленное.",
            )

    # ══ /edit — step 1 ═══════════════════════════════════════════════════════
    elif step == "edit_text":
        if message.content_type != "text":
            bot.send_message(
                message.chat.id,
                "⚠️ На этом шаге нужен текст. Отправь новый текст или <code>-</code>:",
            )
            return
        text = message.text.strip()
        state["new_text"] = None if text == "-" else text
        state["step"] = "edit_collecting_files"
        state["files"] = []
        bot.send_message(
            message.chat.id,
            "📎 <b>Шаг 2/2</b> — Отправь новые файлы (старые заменятся).\n\n"
            "• Отправляй по одному — бот подтвердит каждый.\n"
            "• Когда всё готово — напиши /stop для сохранения.\n"
            "• Или отправь текстом <b>ссылку</b> (http://…) вместо файлов.\n"
            "• Чтобы оставить текущий контент — напиши /stop сразу.",
        )

    # ══ /edit — step 2: collect new files or URL ══════════════════════════════
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
                if new_text is not None:
                    database.update_link(key, content_text=new_text)
                bot.send_message(
                    message.chat.id,
                    f"✅ Ключ <code>{key}</code> обновлён (контент не изменён).",
                )
                logger.info("Admin edited key %r (text only)", key)
                return
            if not (url.startswith("http://") or url.startswith("https://")):
                bot.send_message(
                    message.chat.id,
                    "⚠️ Ссылка должна начинаться с <code>http://</code> или "
                    "<code>https://</code>. Попробуй ещё раз, или отправь файл, "
                    "или <code>-</code> чтобы оставить текущее.",
                )
                return
            key = state["key"]
            new_text = state.get("new_text")
            del _pending[message.from_user.id]
            try:
                database.update_link(key, content_text=new_text, target_url=url, clear_files=True)
            except Exception as exc:
                bot.send_message(message.chat.id, f"❌ Ошибка: {exc}")
                return
            bot.send_message(
                message.chat.id,
                f"✅ Ключ <code>{key}</code> обновлён — новая ссылка сохранена.",
            )
            logger.info("Admin edited key %r → new url", key)
        else:
            bot.send_message(
                message.chat.id,
                "⚠️ Отправь файл, ссылку, <code>-</code> чтобы оставить текущее, "
                "или /stop чтобы сохранить уже добавленные файлы.",
            )


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    bot.remove_webhook()
    logger.info("Bot started. Polling…")
    bot.infinity_polling(timeout=30, long_polling_timeout=20)
