"""
Telegram Chat Manager Bot
Покрывает модули: модерация, баны/варны/муты, чистка чата,
настройка чата, доступ команд, голосование, закладки, заметки, таймеры,
анкета, статистика, темы модераторов, реакции.

Требования: pip install python-telegram-bot==20.7 aiosqlite
Запуск: python bot.py
Установите BOT_TOKEN в переменную окружения или впишите ниже.
"""

import asyncio
import logging
import os
import re
import sqlite3
import time
import io
from datetime import datetime, timedelta
from typing import Optional

try:
    import aiohttp
    _AIOHTTP_OK = True
except ImportError:
    _AIOHTTP_OK = False

try:
    import requests as _requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False

from telegram import (
    Chat,
    ChatMember,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Message,
    Update,
    User,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ─────────────────────────── КОНФИГУРАЦИЯ ────────────────────────────────────

BOT_TOKEN = os.getenv("BOT_TOKEN", "7903572178:AAG8YwkrEPvxJH9Yc6PHzMdEoCG7RfjO0k8")

# ID главной (управляющей) группы — укажите сюда chat_id вашей главной группы.
# Команда «Стата бота» будет доступна только из неё (для ранга 3+).
MAIN_GROUP_ID: int = int(os.getenv("MAIN_GROUP_ID", "0"))  # 0 = не задан

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DB_PATH = "chatbot.db"

# Кеш username→user для поиска участников без @: заполняется из входящих сообщений
_user_cache: dict[str, "User"] = {}  # ключ: username.lower()

# Кеши для антиспама: chat_id → {user_id: [timestamps]}
_spam_cache: dict[int, dict[int, list[float]]] = {}
# Кеши для антифлуда: chat_id → {user_id: (last_text, count)}
_flood_cache: dict[int, dict[int, tuple[str, int]]] = {}


# ─────────────────────────── ВСПОМОГАТЕЛЬНЫЙ ПАРСЕР АРГУМЕНТОВ ───────────────

def _parse_args(update: Update, context: ContextTypes.DEFAULT_TYPE) -> list[str]:
    """
    Возвращает аргументы команды независимо от типа хендлера.
    - CommandHandler (/cmd arg1 arg2): context.args уже заполнен PTB
    - MessageHandler (текстовая команда или фото с подписью): парсим из text/caption,
      отрезая совпавший префикс по context.matches[0].
    """
    if context.args:
        return list(context.args)
    msg = update.message if update.message else None
    # text message or photo caption
    text = (msg and (msg.text or msg.caption)) or ""
    if not text:
        return []
    if context.matches and context.matches[0]:
        cmd_end = context.matches[0].end()
        remaining = text[cmd_end:].strip()
        return remaining.split() if remaining else []
    parts = text.split()
    return parts[1:] if len(parts) > 1 else []

# ─────────────────────────── БАЗА ДАННЫХ ─────────────────────────────────────

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = db_connect()
    cur = conn.cursor()

    cur.executescript("""
    CREATE TABLE IF NOT EXISTS moderators (
        chat_id INTEGER,
        user_id INTEGER,
        rank INTEGER DEFAULT 1,
        PRIMARY KEY (chat_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS warns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        user_id INTEGER,
        reason TEXT,
        issued_by INTEGER,
        expires_at TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS mutes (
        chat_id INTEGER,
        user_id INTEGER,
        expires_at TEXT,
        PRIMARY KEY (chat_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS bans (
        chat_id INTEGER,
        user_id INTEGER,
        reason TEXT,
        issued_by INTEGER,
        expires_at TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (chat_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        title TEXT,
        content TEXT,
        created_by INTEGER,
        created_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS bookmarks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        user_id INTEGER,
        title TEXT,
        message_id INTEGER,
        created_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS timers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        user_id INTEGER,
        fire_at TEXT,
        command TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS chat_settings (
        chat_id INTEGER PRIMARY KEY,
        welcome TEXT,
        rules TEXT,
        warn_limit INTEGER DEFAULT 3,
        warn_ban_duration TEXT DEFAULT '7 дней',
        warn_duration TEXT DEFAULT '30 дней',
        mute_default TEXT DEFAULT '7 дней',
        warn_action TEXT DEFAULT 'бан'
    );

    CREATE TABLE IF NOT EXISTS cmd_access (
        chat_id INTEGER,
        cmd_name TEXT,
        min_rank INTEGER DEFAULT 0,
        PRIMARY KEY (chat_id, cmd_name)
    );

    CREATE TABLE IF NOT EXISTS user_profiles (
        user_id INTEGER PRIMARY KEY,
        city TEXT,
        bio TEXT,
        updated_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS votes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        message_id INTEGER,
        initiator_id INTEGER,
        command TEXT,
        votes_needed INTEGER,
        min_rank INTEGER DEFAULT 0,
        current_votes INTEGER DEFAULT 0,
        status TEXT DEFAULT 'active',
        created_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS vote_participants (
        vote_id INTEGER,
        user_id INTEGER,
        PRIMARY KEY (vote_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS ban_votes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        target_id INTEGER,
        initiator_id INTEGER,
        votes_needed INTEGER DEFAULT 5,
        min_rank INTEGER DEFAULT 0,
        current_votes INTEGER DEFAULT 0,
        message_id INTEGER,
        status TEXT DEFAULT 'active',
        created_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS ban_vote_participants (
        vote_id INTEGER,
        user_id INTEGER,
        PRIMARY KEY (vote_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS mod_topics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        user_id INTEGER,
        title TEXT,
        content TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS reaction_restrictions (
        chat_id INTEGER,
        user_id INTEGER,
        allowed INTEGER DEFAULT 1,
        PRIMARY KEY (chat_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS known_users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT
    );

    CREATE TABLE IF NOT EXISTS activity (
        chat_id INTEGER,
        user_id INTEGER,
        day TEXT,
        msg_count INTEGER DEFAULT 0,
        PRIMARY KEY (chat_id, user_id, day)
    );

    CREATE TABLE IF NOT EXISTS antispam_settings (
        chat_id INTEGER PRIMARY KEY,
        enabled INTEGER DEFAULT 0,
        max_msgs INTEGER DEFAULT 5,
        interval_sec INTEGER DEFAULT 5,
        action TEXT DEFAULT 'мут',
        mute_duration INTEGER DEFAULT 300
    );

    CREATE TABLE IF NOT EXISTS antiflood_settings (
        chat_id INTEGER PRIMARY KEY,
        enabled INTEGER DEFAULT 0,
        max_same INTEGER DEFAULT 3,
        action TEXT DEFAULT 'предупреждение'
    );

    CREATE TABLE IF NOT EXISTS badwords (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        word TEXT,
        UNIQUE(chat_id, word)
    );

    CREATE TABLE IF NOT EXISTS captcha_settings (
        chat_id INTEGER PRIMARY KEY,
        enabled INTEGER DEFAULT 0,
        timeout_sec INTEGER DEFAULT 60
    );

    CREATE TABLE IF NOT EXISTS captcha_pending (
        chat_id INTEGER,
        user_id INTEGER,
        msg_id INTEGER,
        expire_at TEXT,
        PRIMARY KEY (chat_id, user_id)
    );
    """)
    conn.commit()
    # Миграции для существующих БД
    for migration in [
        "ALTER TABLE chat_settings ADD COLUMN warn_action TEXT DEFAULT 'бан'",
        """CREATE TABLE IF NOT EXISTS promo_codes (
            code TEXT PRIMARY KEY,
            coins INTEGER DEFAULT 0,
            nuggets INTEGER DEFAULT 0,
            uses_left INTEGER DEFAULT 1,
            created_by INTEGER DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS promo_used (
            code TEXT,
            user_id INTEGER,
            PRIMARY KEY (code, user_id)
        )""",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_bj_moment ON bj_photos(moment)",
        """CREATE TABLE IF NOT EXISTS nicknames (
            chat_id INTEGER,
            user_id INTEGER,
            nick TEXT NOT NULL,
            set_by INTEGER,
            PRIMARY KEY (chat_id, user_id)
        )""",
        """CREATE TABLE IF NOT EXISTS game_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cmd TEXT NOT NULL,
            used_at TEXT DEFAULT (datetime('now'))
        )""",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_dice_moment ON dice_photos(moment)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_rou_category ON roulette_photos(number)",
        """CREATE TABLE IF NOT EXISTS known_users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS geo_scores (
            user_id INTEGER, chat_id INTEGER, score INTEGER DEFAULT 0,
            correct INTEGER DEFAULT 0, wrong INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, chat_id)
        )""",
        """CREATE TABLE IF NOT EXISTS geo_scores_global (
            user_id INTEGER PRIMARY KEY, score INTEGER DEFAULT 0,
            correct INTEGER DEFAULT 0, wrong INTEGER DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS activity (
            chat_id INTEGER, user_id INTEGER, day TEXT,
            msg_count INTEGER DEFAULT 0, PRIMARY KEY (chat_id, user_id, day)
        )""",
        """CREATE TABLE IF NOT EXISTS antispam_settings (
            chat_id INTEGER PRIMARY KEY, enabled INTEGER DEFAULT 0,
            max_msgs INTEGER DEFAULT 5, interval_sec INTEGER DEFAULT 5,
            action TEXT DEFAULT 'мут', mute_duration INTEGER DEFAULT 300
        )""",
        """CREATE TABLE IF NOT EXISTS antiflood_settings (
            chat_id INTEGER PRIMARY KEY, enabled INTEGER DEFAULT 0,
            max_same INTEGER DEFAULT 3, action TEXT DEFAULT 'предупреждение'
        )""",
        """CREATE TABLE IF NOT EXISTS badwords (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER,
            word TEXT, UNIQUE(chat_id, word)
        )""",
        """CREATE TABLE IF NOT EXISTS captcha_settings (
            chat_id INTEGER PRIMARY KEY, enabled INTEGER DEFAULT 0,
            timeout_sec INTEGER DEFAULT 60
        )""",
        """CREATE TABLE IF NOT EXISTS captcha_pending (
            chat_id INTEGER, user_id INTEGER, msg_id INTEGER,
            expire_at TEXT, PRIMARY KEY (chat_id, user_id)
        )""",
        """CREATE TABLE IF NOT EXISTS geo_custom_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            file_id TEXT NOT NULL,
            answer TEXT NOT NULL,
            wrong1 TEXT NOT NULL DEFAULT '',
            wrong2 TEXT NOT NULL DEFAULT '',
            added_by INTEGER,
            added_at TEXT DEFAULT (datetime('now'))
        )""",
        """CREATE TABLE IF NOT EXISTS economy (
            user_id INTEGER PRIMARY KEY,
            nuggets INTEGER NOT NULL DEFAULT 0,
            coins INTEGER NOT NULL DEFAULT 0,
            last_farm TEXT DEFAULT '',
            last_expedition TEXT DEFAULT '',
            last_amulet TEXT DEFAULT '',
            expeditions_today INTEGER NOT NULL DEFAULT 0,
            unlocked_location INTEGER NOT NULL DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS inventory (
            user_id INTEGER NOT NULL,
            item TEXT NOT NULL,
            qty INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (user_id, item)
        )""",
        """CREATE TABLE IF NOT EXISTS duels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            caller_id INTEGER,
            target_id INTEGER,
            bet INTEGER,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT (datetime('now'))
        )""",
        """CREATE TABLE IF NOT EXISTS bj_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            moment TEXT NOT NULL,
            file_id TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS roulette_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            number INTEGER NOT NULL,
            file_id TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS dice_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            moment TEXT NOT NULL,
            file_id TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS profile_photos (
            user_id INTEGER PRIMARY KEY,
            file_id TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS expedition_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            loc_id INTEGER NOT NULL,
            event_text TEXT NOT NULL,
            file_id TEXT NOT NULL
        )""",
    ]:
        try:
            conn.execute(migration)
            conn.commit()
        except Exception:
            pass
    conn.close()


# ─────────────────────────── ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ─────────────────────────

def parse_duration(text: str) -> Optional[timedelta]:
    """Парсит строку вида '2 дня', '1 час', '30 минут' и т.д."""
    units = {
        "минут": 60, "минута": 60, "минуты": 60, "мин": 60,
        "час": 3600, "часа": 3600, "часов": 3600,
        "день": 86400, "дня": 86400, "дней": 86400, "сутки": 86400,
        "неделя": 604800, "недели": 604800, "недель": 604800, "неделю": 604800,
        "месяц": 2592000, "месяца": 2592000, "месяцев": 2592000,
        "год": 31536000, "года": 31536000, "лет": 31536000,
    }
    text = text.strip().lower()
    m = re.match(r"(\d+)\s*([а-яёa-z]+)", text, re.IGNORECASE)
    if not m:
        return None
    num, unit = int(m.group(1)), m.group(2)
    seconds = units.get(unit)
    if seconds is None:
        return None
    return timedelta(seconds=num * seconds)


MSK_OFFSET = 3  # UTC+3

def _msk_now() -> datetime:
    """Текущее время по МСК (UTC+3)."""
    return datetime.utcnow() + timedelta(hours=MSK_OFFSET)

def _msk_day() -> str:
    """Текущая дата по МСК в формате YYYY-MM-DD.
    Кулдаун сбрасывается в 00:00 МСК."""
    return _msk_now().strftime("%Y-%m-%d")


def get_rank(chat_id: int, user_id: int) -> int:
    conn = db_connect()
    row = conn.execute(
        "SELECT rank FROM moderators WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    ).fetchone()
    conn.close()
    return row["rank"] if row else 0


def set_rank(chat_id: int, user_id: int, rank: int) -> None:
    conn = db_connect()
    if rank == 0:
        conn.execute(
            "DELETE FROM moderators WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        )
    else:
        conn.execute(
            "INSERT OR REPLACE INTO moderators (chat_id, user_id, rank) VALUES (?,?,?)",
            (chat_id, user_id, rank),
        )
    conn.commit()
    conn.close()


RANK_NAMES = {
    0: "Участник",
    1: "Младший модератор",
    2: "Старший модератор",
    3: "Младший администратор",
    4: "Старший администратор",
    5: "Создатель",
}


async def get_member_rank(update: Update, user_id: int) -> int:
    """
    Возвращает ранг пользователя.
    - TG-создатель → всегда 5
    - Если есть ранг в БД → используем его
    - TG-администратор без ранга в БД → 2 (Старший модератор, как в Ирисе)
    - Иначе → 0
    """
    chat_id = update.effective_chat.id
    try:
        member = await update.effective_chat.get_member(user_id)
        if member.status == ChatMember.OWNER:
            # Убеждаемся, что создатель есть в БД с рангом 5
            if get_rank(chat_id, user_id) < 5:
                set_rank(chat_id, user_id, 5)
            return 5
        db_rank = get_rank(chat_id, user_id)
        if db_rank > 0:
            return db_rank
        if member.status == ChatMember.ADMINISTRATOR:
            return 2  # TG-админ без ранга в БД → Старший модератор
    except Exception:
        pass
    return get_rank(chat_id, user_id)


async def sync_chat_admins(bot, chat_id: int) -> None:
    """
    Синхронизирует TG-администраторов с БД рангов:
    - Создатель → ранг 5
    - Администраторы без ранга в БД → ранг 2
    - Уже имеющие ранг в БД → не трогаем
    """
    try:
        admins = await bot.get_chat_administrators(chat_id)
    except Exception as e:
        logger.error(f"sync_chat_admins error: {e}")
        return

    for admin in admins:
        if admin.user.is_bot:
            continue
        current_db_rank = get_rank(chat_id, admin.user.id)
        if admin.status == ChatMember.OWNER:
            if current_db_rank != 5:
                set_rank(chat_id, admin.user.id, 5)
                logger.info(f"Назначен создатель: {admin.user.id} в чате {chat_id}")
        elif admin.status == ChatMember.ADMINISTRATOR:
            if current_db_rank == 0:
                set_rank(chat_id, admin.user.id, 2)
                logger.info(f"Назначен ст. модератор (TG-admin): {admin.user.id} в чате {chat_id}")


def get_cmd_min_rank(chat_id: int, cmd: str) -> int:
    conn = db_connect()
    row = conn.execute(
        "SELECT min_rank FROM cmd_access WHERE chat_id=? AND cmd_name=?",
        (chat_id, cmd),
    ).fetchone()
    conn.close()
    return row["min_rank"] if row else 1


_CHAT_SETTINGS_FIELDS = frozenset({
    "welcome", "rules", "warn_limit", "warn_ban_duration",
    "warn_duration", "mute_default", "warn_action",
})


def get_chat_setting(chat_id: int, field: str, default=None):
    if field not in _CHAT_SETTINGS_FIELDS:
        raise ValueError(f"Недопустимое поле настроек: {field!r}")
    conn = db_connect()
    row = conn.execute(
        f"SELECT {field} FROM chat_settings WHERE chat_id=?",
        (chat_id,),
    ).fetchone()
    conn.close()
    if row and row[field] is not None:
        return row[field]
    return default


def set_chat_setting(chat_id: int, field: str, value) -> None:
    if field not in _CHAT_SETTINGS_FIELDS:
        raise ValueError(f"Недопустимое поле настроек: {field!r}")
    conn = db_connect()
    conn.execute(
        "INSERT OR IGNORE INTO chat_settings (chat_id) VALUES (?)", (chat_id,)
    )
    conn.execute(
        f"UPDATE chat_settings SET {field}=? WHERE chat_id=?", (value, chat_id)
    )
    conn.commit()
    conn.close()


async def resolve_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[User]:
    """Возвращает целевого пользователя из реплая или аргумента (@username / username / user_id)."""
    # 1. Реплай — самый надёжный способ
    if update.message and update.message.reply_to_message:
        return update.message.reply_to_message.from_user

    args = _parse_args(update, context)
    if not args:
        return None

    for arg in args:
        # Пропускаем числа-ставки и единицы времени
        if parse_duration("1 " + arg) is not None:
            continue

        # Числовой ID
        if re.match(r"^\d+$", arg):
            uid = int(arg)
            cached = next((u for u in _user_cache.values() if u.id == uid), None)
            if cached:
                return cached
            try:
                chat = await context.bot.get_chat(uid)
                return User(
                    id=chat.id,
                    first_name=chat.first_name or str(uid),
                    is_bot=False,
                    last_name=getattr(chat, "last_name", None),
                    username=getattr(chat, "username", None),
                )
            except Exception:
                pass
            continue

        # @username или username — пробуем кеш сначала
        clean = arg.lstrip("@").lower()
        cached = _user_cache.get(clean)
        if cached:
            return cached

        # Запрашиваем у Telegram
        username = "@" + clean
        try:
            chat = await context.bot.get_chat(username)
            return User(
                id=chat.id,
                first_name=chat.first_name or chat.title or clean,
                is_bot=False,
                last_name=getattr(chat, "last_name", None),
                username=getattr(chat, "username", None),
            )
        except Exception:
            pass

        # Фолбэк: ищем в known_users по username
        conn = db_connect()
        row = conn.execute(
            "SELECT user_id, first_name, last_name, username FROM known_users WHERE lower(username)=?",
            (clean,)
        ).fetchone()
        conn.close()
        if row:
            return User(
                id=row["user_id"],
                first_name=row["first_name"] or clean,
                is_bot=False,
                last_name=row["last_name"],
                username=row["username"],
            )

    return None


async def require_rank(update: Update, rank: int, cmd_name: str = "") -> bool:
    """Проверяет ранг. Если задан cmd_name — берёт min_rank из cmd_access (перекрывает rank)."""
    user_id = update.effective_user.id
    actual = await get_member_rank(update, user_id)
    if cmd_name:
        db_rank = get_cmd_min_rank(update.effective_chat.id, cmd_name)
        if db_rank >= 6:
            return False  # команда отключена — молча игнорируем
        rank = db_rank
    if actual < rank:
        await update.message.reply_text(
            f"⛔ Требуется ранг {rank} ({RANK_NAMES.get(rank, rank)}). У вас: {actual}."
        )
        return False
    return True


def format_dt(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y %H:%M")


# ─────────────────────────── МОДУЛЬ 1: МОДЕРАЦИЯ ─────────────────────────────

async def cmd_moder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    +Модер [ранг] <ссылка|реплай> — назначить ранг модератора (1–4).
    Примеры: +модер @user  /  +модер 3 @user
    """
    if not await require_rank(update, 3):
        return
    caller_rank = await get_member_rank(update, update.effective_user.id)

    args = _parse_args(update, context)
    rank = 1
    if args and args[0].isdigit():
        rank = int(args.pop(0))
    # sync back so resolve_target sees the remaining args
    context.args = args

    if rank < 1 or rank > 5:
        await update.message.reply_text("Ранг должен быть от 1 до 5.")
        return
    if rank >= caller_rank:
        await update.message.reply_text("Нельзя назначить ранг равный или выше вашего.")
        return

    target = await resolve_target(update, context)
    if not target:
        await update.message.reply_text("Укажите пользователя: реплай или @ссылка.")
        return

    set_rank(update.effective_chat.id, target.id, rank)
    await update.message.reply_text(
        f"✅ {target.mention_html()} получил ранг {rank} — {RANK_NAMES.get(rank)}.",
        parse_mode="HTML",
    )


async def cmd_demoder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """-Модер <ссылка|реплай> — снять ранг модератора."""
    if not await require_rank(update, 3):
        return
    target = await resolve_target(update, context)
    if not target:
        await update.message.reply_text("Укажите пользователя.")
        return

    target_rank = get_rank(update.effective_chat.id, target.id)
    caller_rank = await get_member_rank(update, update.effective_user.id)
    if target_rank >= caller_rank:
        await update.message.reply_text("Нельзя снять ранг равный или выше вашего.")
        return

    set_rank(update.effective_chat.id, target.id, 0)
    await update.message.reply_text(
        f"✅ {target.mention_html()} лишён ранга модератора.",
        parse_mode="HTML",
    )


async def cmd_who_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кто Admin — список модераторов чата."""
    conn = db_connect()
    rows = conn.execute(
        "SELECT user_id, rank FROM moderators WHERE chat_id=? ORDER BY rank DESC",
        (update.effective_chat.id,),
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("Модераторов нет.")
        return
    lines = []
    for row in rows:
        try:
            member = await update.effective_chat.get_member(row["user_id"])
            name = member.user.mention_html()
        except Exception:
            name = f"id{row['user_id']}"
        lines.append(f"{name} — {RANK_NAMES.get(row['rank'], row['rank'])}")
    await update.message.reply_text(
        "👮 <b>Модераторы чата:</b>\n" + "\n".join(lines),
        parse_mode="HTML",
    )


async def cmd_call_admins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Созыв — упомянуть всех модераторов."""
    conn = db_connect()
    rows = conn.execute(
        "SELECT user_id FROM moderators WHERE chat_id=? ORDER BY rank DESC",
        (update.effective_chat.id,),
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("Модераторов нет.")
        return
    mentions = []
    for row in rows:
        try:
            member = await update.effective_chat.get_member(row["user_id"])
            mentions.append(member.user.mention_html())
        except Exception:
            pass
    await update.message.reply_text(
        "📣 Созыв модерации!\n" + " ".join(mentions),
        parse_mode="HTML",
    )


# ─────────────────────────── МОДУЛЬ 2: БАНЫ / ВАРНЫ / МУТЫ ──────────────────

async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Варн [период] <ссылка|реплай> — выдать предупреждение.
    Следующая строка — причина.
    """
    if not await require_rank(update, 1):
        return
    target = await resolve_target(update, context)
    if not target:
        await update.message.reply_text("Укажите пользователя.")
        return

    chat_id = update.effective_chat.id
    text_lines = update.message.text.split("\n", 1)
    reason = text_lines[1].strip() if len(text_lines) > 1 else "Не указана"

    warn_duration_str = get_chat_setting(chat_id, "warn_duration", "30 дней")
    delta = parse_duration(warn_duration_str)
    expires_at = (datetime.utcnow() + delta).isoformat() if delta else None

    conn = db_connect()
    conn.execute(
        "INSERT INTO warns (chat_id, user_id, reason, issued_by, expires_at) VALUES (?,?,?,?,?)",
        (chat_id, target.id, reason, update.effective_user.id, expires_at),
    )
    conn.commit()
    count = conn.execute(
        "SELECT COUNT(*) as cnt FROM warns WHERE chat_id=? AND user_id=? AND (expires_at IS NULL OR expires_at > datetime('now'))",
        (chat_id, target.id),
    ).fetchone()["cnt"]
    warn_limit = get_chat_setting(chat_id, "warn_limit", 3)
    conn.close()

    msg = (
        f"⚠️ {target.mention_html()} получил предупреждение {count}/{warn_limit}.\n"
        f"📝 Причина: {reason}"
    )

    if count >= warn_limit:
        duration_str = get_chat_setting(chat_id, "warn_ban_duration", "7 дней")
        action = get_chat_setting(chat_id, "warn_action", "бан")
        dur_lower = duration_str.strip().lower()
        is_permanent = dur_lower in ("навсегда", "постоянно", "0")
        dur_delta = parse_duration(duration_str) if not is_permanent else None
        until = datetime.utcnow() + dur_delta if dur_delta else None
        dur_label = "навсегда" if is_permanent else duration_str

        if action == "мут":
            try:
                await update.effective_chat.restrict_member(
                    target.id,
                    ChatPermissions(can_send_messages=False),
                    until_date=until,
                )
                msg += f"\n🔇 Достигнут лимит предупреждений — мут на {dur_label}."
            except Exception as e:
                msg += f"\n❌ Не удалось замутить: {e}"
        else:
            try:
                await update.effective_chat.ban_member(
                    target.id,
                    until_date=until,
                )
                msg += f"\n🔨 Достигнут лимит предупреждений — забанен на {dur_label}."
            except Exception as e:
                msg += f"\n❌ Не удалось забанить: {e}"

    await update.message.reply_text(msg, parse_mode="HTML")


async def cmd_warns(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Варны <ссылка|реплай> — список предупреждений пользователя."""
    target = await resolve_target(update, context)
    if not target:
        target = update.effective_user
    chat_id = update.effective_chat.id
    conn = db_connect()
    rows = conn.execute(
        "SELECT id, reason, created_at, expires_at FROM warns WHERE chat_id=? AND user_id=? AND (expires_at IS NULL OR expires_at > datetime('now')) ORDER BY created_at DESC",
        (chat_id, target.id),
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text(
            f"{target.mention_html()} предупреждений нет.", parse_mode="HTML"
        )
        return
    lines = [f"⚠️ Предупреждения {target.mention_html()}:"]
    for i, r in enumerate(rows, 1):
        exp = r["expires_at"][:10] if r["expires_at"] else "∞"
        lines.append(f"{i}. [{r['id']}] {r['reason']} (до {exp})")
    if game_rows:
        lines.append("\n🎮 <b>Использование игровых команд:</b>")
        for r in game_rows:
            lines.append(f"  • {r['cmd']}: <b>{r['cnt']}</b> раз")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_unwarn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """-Варн <ссылка|реплай> — снять последнее предупреждение."""
    if not await require_rank(update, 1):
        return
    target = await resolve_target(update, context)
    if not target:
        await update.message.reply_text("Укажите пользователя.")
        return
    conn = db_connect()
    row = conn.execute(
        "SELECT id FROM warns WHERE chat_id=? AND user_id=? ORDER BY created_at DESC LIMIT 1",
        (update.effective_chat.id, target.id),
    ).fetchone()
    if not row:
        conn.close()
        await update.message.reply_text("Предупреждений нет.")
        return
    conn.execute("DELETE FROM warns WHERE id=?", (row["id"],))
    conn.commit()
    conn.close()
    await update.message.reply_text(
        f"✅ Снято последнее предупреждение у {target.mention_html()}.",
        parse_mode="HTML",
    )


async def cmd_unwarn_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Снять все варны <ссылка|реплай>."""
    if not await require_rank(update, 2):
        return
    target = await resolve_target(update, context)
    if not target:
        await update.message.reply_text("Укажите пользователя.")
        return
    conn = db_connect()
    conn.execute(
        "DELETE FROM warns WHERE chat_id=? AND user_id=?",
        (update.effective_chat.id, target.id),
    )
    conn.commit()
    conn.close()
    await update.message.reply_text(
        f"✅ Все предупреждения {target.mention_html()} сняты.", parse_mode="HTML"
    )


async def resolve_username(bot, username_raw: str, cache: dict) -> Optional["User"]:
    """
    Ищет пользователя по username (с @ или без).
    1. Кеш в памяти (из текущей сессии)
    2. База known_users (из прошлых сессий)
    3. Telegram get_chat (только для публичных аккаунтов)
    """
    from telegram import User as TGUser
    clean = username_raw.lstrip("@").lower()

    # 1. Кеш в памяти
    if clean in cache:
        return cache[clean]

    # 2. База данных
    try:
        conn = db_connect()
        row = conn.execute(
            "SELECT user_id, username, first_name, last_name FROM known_users WHERE lower(username)=?",
            (clean,),
        ).fetchone()
        conn.close()
        if row:
            # Создаём объект-заглушку с нужными полями
            class _FakeUser:
                def __init__(self, r):
                    self.id = r["user_id"]
                    self.username = r["username"]
                    self.first_name = r["first_name"] or ""
                    self.last_name = r["last_name"] or ""
                    self.is_bot = False
                def mention_html(self):
                    name = self.first_name or self.username or str(self.id)
                    return f'<a href="tg://user?id={self.id}">{name}</a>'
            return _FakeUser(row)
    except Exception:
        pass

    # 3. Telegram API
    try:
        return await bot.get_chat("@" + clean)
    except Exception:
        return None


async def parse_mod_args(text: str, has_reply: bool):
    """
    Парсит аргументы команды модерации из сырого текста.
    Возвращает (username_or_none, duration_str_or_none, delta_or_none, reason).
    text — всё что после команды (первое слово уже убрано).
    """
    # Разбиваем на строки — вторая строка и далее это причина
    lines = text.split("\n", 1)
    first_line = lines[0].strip()
    reason_from_newline = lines[1].strip() if len(lines) > 1 else ""

    tokens = first_line.split()

    def is_dur_unit(tok):
        return parse_duration("1 " + tok) is not None

    def is_number(tok):
        return re.match(r"^\d+$", tok) is not None

    username = None
    duration_str = None
    delta = None
    reason_tokens = []

    i = 0
    # Ищем username (@ или слово без цифр/единиц) — только если нет реплая
    if not has_reply and tokens:
        tok = tokens[0]
        if tok.startswith("@") or (not is_number(tok) and not is_dur_unit(tok)):
            username = tok
            i = 1

    # Ищем срок: «число единица»
    while i < len(tokens):
        if delta is None and is_number(tokens[i]) and i + 1 < len(tokens) and is_dur_unit(tokens[i+1]):
            candidate = tokens[i] + " " + tokens[i+1]
            d = parse_duration(candidate)
            if d:
                delta = d
                duration_str = candidate
                i += 2
                continue
        # Одиночная единица (например «неделю»)
        if delta is None and is_dur_unit(tokens[i]):
            d = parse_duration("1 " + tokens[i])
            if d:
                delta = d
                duration_str = "1 " + tokens[i]
                i += 1
                continue
        reason_tokens.append(tokens[i])
        i += 1

    reason = " ".join(reason_tokens).strip() or reason_from_newline
    return username, duration_str, delta, reason


async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Мут/mute [срок] [username] [причина] — заглушить пользователя.
    Форматы:
      мут 1 час @user
      мут @user 30 минут
      мут сосо 1 минута причина
      /mute @user 1 час причина
      (реплай) мут 2 часа причина
    """
    if not await require_rank(update, 1):
        return

    has_reply = bool(update.message.reply_to_message)

    # Вырезаем команду из текста (первое слово: мут/mute/заткнуть или /mute)
    raw = update.message.text or ""
    # Убираем первое слово (команду)
    after_cmd = re.sub(r"^\S+\s*", "", raw, count=1)

    username_raw, duration_str, delta, reason = await parse_mod_args(after_cmd, has_reply)

    # Получаем target
    target = None
    if has_reply:
        target = update.message.reply_to_message.from_user
    elif username_raw:
        target = await resolve_username(context.bot, username_raw, _user_cache)
        if not target:
            uname = username_raw if username_raw.startswith("@") else "@" + username_raw
            await update.message.reply_text(
                f"❌ Пользователь {uname} не найден.\n"
                "💡 Попробуйте ответить на сообщение пользователя (реплай) вместо username.",
                parse_mode="HTML",
            )
            return

    if not target:
        await update.message.reply_text(
            "❌ Укажите пользователя: ответьте на его сообщение или напишите username.\nПример: <code>мут 1 час @username</code>",
            parse_mode="HTML",
        )
        return

    if not delta:
        duration_str = get_chat_setting(update.effective_chat.id, "mute_default", "1 час")
        delta = parse_duration(duration_str)

    # until считаем от времени сообщения (оно в UTC), показываем в местном времени сервера
    msg_time = update.message.date  # aware datetime UTC
    until = msg_time + delta if delta else None

    try:
        await update.effective_chat.restrict_member(
            target.id,
            ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        # Для отображения переводим UTC→local
        until_local = until.astimezone() if until else None
        msg = f"🔇 {target.mention_html()} заглушен"
        if delta and duration_str:
            msg += f" на {duration_str}"
        if until_local:
            msg += f" (до {until_local.strftime('%d.%m.%Y %H:%M')})"
        if reason:
            msg += f"\n📝 Причина: {reason}"
        await update.message.reply_text(msg, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """-Мут <ссылка|реплай> — снять мут."""
    if not await require_rank(update, 1):
        return
    target = await resolve_target(update, context)
    if not target:
        await update.message.reply_text("Укажите пользователя.")
        return
    chat_id = update.effective_chat.id
    try:
        await update.effective_chat.restrict_member(
            target.id,
            ChatPermissions(
                can_send_messages=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            ),
        )
        # Удаляем запись из БД если есть
        conn = db_connect()
        conn.execute("DELETE FROM mutes WHERE chat_id=? AND user_id=?", (chat_id, target.id))
        conn.commit()
        conn.close()
        # Уведомление в текущем чате (где снято наказание)
        await context.bot.send_message(
            chat_id,
            f"🔈 {target.mention_html()} — мут снят.",
            parse_mode="HTML",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Бан [срок] <ссылка|реплай> — забанить пользователя.
    Пример: бан 7 дней @user
    """
    if not await require_rank(update, 2):
        return
    target = await resolve_target(update, context)
    if not target:
        await update.message.reply_text("Укажите пользователя.")
        return

    has_reply = bool(update.message.reply_to_message)
    raw = update.message.text or ""
    after_cmd = re.sub(r"^\S+\s*", "", raw, count=1)
    username_raw, duration_str, delta, reason = await parse_mod_args(after_cmd, has_reply)
    if not reason:
        reason = "Не указана"

    # Переопределяем target если нет реплая
    if not has_reply and username_raw:
        target = await resolve_username(context.bot, username_raw, _user_cache)
        if not target:
            uname = username_raw if username_raw.startswith("@") else "@" + username_raw
            await update.message.reply_text(
                f"❌ Пользователь {uname} не найден.\n"
                "💡 Попробуйте ответить на сообщение пользователя (реплай) вместо username.",
                parse_mode="HTML",
            )
            return

    msg_time = update.message.date
    until = msg_time + delta if delta else None

    conn = db_connect()
    conn.execute(
        "INSERT OR REPLACE INTO bans (chat_id, user_id, reason, issued_by, expires_at) VALUES (?,?,?,?,?)",
        (update.effective_chat.id, target.id, reason, update.effective_user.id,
         until.isoformat() if until else None),
    )
    conn.commit()
    conn.close()

    try:
        await update.effective_chat.ban_member(target.id, until_date=until)
        until_local = until.astimezone() if until else None
        msg = f"🔨 {target.mention_html()} забанен"
        if delta and duration_str:
            msg += f" на {duration_str}"
        if until_local:
            msg += f" (до {until_local.strftime('%d.%m.%Y %H:%M')})"
        msg += f"\n📝 Причина: {reason}"
        await update.message.reply_text(msg, parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Разбан <ссылка|реплай> — снять бан."""
    if not await require_rank(update, 2):
        return
    target = await resolve_target(update, context)
    if not target:
        await update.message.reply_text("Укажите пользователя.")
        return
    chat_id = update.effective_chat.id
    try:
        await update.effective_chat.unban_member(target.id, only_if_banned=True)
        conn = db_connect()
        conn.execute(
            "DELETE FROM bans WHERE chat_id=? AND user_id=?",
            (chat_id, target.id),
        )
        conn.commit()
        conn.close()
        # Уведомление в текущем чате (где снято наказание)
        await context.bot.send_message(
            chat_id,
            f"✅ {target.mention_html()} — бан снят.",
            parse_mode="HTML",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


async def _clear_mute_on_kick(chat_id: int, user_id: int, bot=None) -> None:
    """Снимает мут в Telegram и удаляет запись из БД при кике.
    Без этого Telegram сохраняет ограничение и восстанавливает его при возврате."""
    # 1. Снимаем ВСЕ ограничения через Telegram API (важно делать ДО бана)
    #    Нужно явно передавать каждое поле — новый Bot API иначе не снимает ограничения
    if bot:
        try:
            await bot.restrict_chat_member(
                chat_id, user_id,
                ChatPermissions(
                    can_send_messages=True,
                    can_send_audios=True,
                    can_send_documents=True,
                    can_send_photos=True,
                    can_send_videos=True,
                    can_send_video_notes=True,
                    can_send_voice_notes=True,
                    can_send_polls=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True,
                    can_change_info=False,
                    can_invite_users=True,
                    can_pin_messages=False,
                ),
            )
        except Exception:
            pass
    # 2. Удаляем из БД
    conn = db_connect()
    conn.execute("DELETE FROM mutes WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    conn.commit()
    conn.close()


async def cmd_kick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кик <ссылка|реплай> — исключить без бана."""
    if not await require_rank(update, 1):
        return
    target = await resolve_target(update, context)
    if not target:
        await update.message.reply_text("Укажите пользователя.")
        return
    try:
        # 1. Снимаем мут ДО бана
        await _clear_mute_on_kick(update.effective_chat.id, target.id, bot=context.bot)
        # 2. Кик = бан + разбан
        await update.effective_chat.ban_member(target.id)
        await update.effective_chat.unban_member(target.id)
        # 3. Снимаем ограничения ЕЩЁ РАЗ после разбана — Telegram сбрасывает права при бане
        try:
            await context.bot.restrict_chat_member(
                update.effective_chat.id, target.id,
                ChatPermissions(
                    can_send_messages=True,
                    can_send_audios=True,
                    can_send_documents=True,
                    can_send_photos=True,
                    can_send_videos=True,
                    can_send_video_notes=True,
                    can_send_voice_notes=True,
                    can_send_polls=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True,
                    can_change_info=False,
                    can_invite_users=True,
                    can_pin_messages=False,
                ),
            )
        except Exception:
            pass  # пользователь уже не в чате — это нормально
        await update.message.reply_text(
            f"👢 {target.mention_html()} исключён из чата.", parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


async def cmd_banlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Банлист — список забаненных (из БД)."""
    if not await require_rank(update, 1):
        return
    conn = db_connect()
    rows = conn.execute(
        "SELECT user_id, reason, expires_at FROM bans WHERE chat_id=?",
        (update.effective_chat.id,),
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("Список банов пуст.")
        return
    lines = ["🔨 <b>Список банов:</b>"]
    for r in rows:
        exp = r["expires_at"][:10] if r["expires_at"] else "навсегда"
        lines.append(f"• id{r['user_id']} — {r['reason']} (до {exp})")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_amnesty(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """!Амнистия — снять баны со всех (только из БД)."""
    if not await require_rank(update, 4):
        return
    conn = db_connect()
    rows = conn.execute(
        "SELECT user_id FROM bans WHERE chat_id=?", (update.effective_chat.id,)
    ).fetchall()
    conn.close()
    count = 0
    for row in rows:
        try:
            await update.effective_chat.unban_member(row["user_id"], only_if_banned=True)
            count += 1
        except Exception:
            pass
    conn = db_connect()
    conn.execute("DELETE FROM bans WHERE chat_id=?", (update.effective_chat.id,))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Амнистия: разбанено {count} пользователей.")


async def cmd_ban_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Причина <ссылка|реплай> — причина бана пользователя."""
    target = await resolve_target(update, context)
    if not target:
        await update.message.reply_text("Укажите пользователя.")
        return
    conn = db_connect()
    row = conn.execute(
        "SELECT reason, issued_by, created_at FROM bans WHERE chat_id=? AND user_id=?",
        (update.effective_chat.id, target.id),
    ).fetchone()
    conn.close()
    if not row:
        await update.message.reply_text("Пользователь не забанен (в базе данных бота).")
        return
    await update.message.reply_text(
        f"📋 Причина бана {target.mention_html()}:\n{row['reason']}\n"
        f"Дата: {row['created_at'][:10]}",
        parse_mode="HTML",
    )


# ─────────────────────────── МОДУЛЬ 4: ДОСТУП КОМАНД ─────────────────────────

async def cmd_dk(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str = "") -> None:
    """
    Доступ команд / дк — список настроек доступа.
    дк <команда> <ранг> — установить ранг (0=все, 6=откл).
    +дк <команда> — открыть для всех (ранг 0).
    -дк <команда> — отключить команду (ранг 6).
    """
    context.args = _parse_args(update, context)

    # +дк <команда> → открыть для всех
    if action == "open":
        if not await require_rank(update, 3):
            return
        if not context.args:
            await update.message.reply_text("Формат: +дк <команда>")
            return
        cmd_name = context.args[0].lower()
        conn = db_connect()
        conn.execute(
            "INSERT OR REPLACE INTO cmd_access (chat_id, cmd_name, min_rank) VALUES (?,?,?)",
            (update.effective_chat.id, cmd_name, 0),
        )
        conn.commit()
        conn.close()
        await update.message.reply_text(f"✅ {cmd_name}: доступ открыт для всех.")
        return

    # -дк <команда> → отключить команду
    if action == "close":
        if not await require_rank(update, 3):
            return
        if not context.args:
            await update.message.reply_text("Формат: -дк <команда>")
            return
        cmd_name = context.args[0].lower()
        conn = db_connect()
        conn.execute(
            "INSERT OR REPLACE INTO cmd_access (chat_id, cmd_name, min_rank) VALUES (?,?,?)",
            (update.effective_chat.id, cmd_name, 6),
        )
        conn.commit()
        conn.close()
        await update.message.reply_text(f"✅ {cmd_name}: команда отключена.")
        return

    # дк без аргументов → список
    if not context.args:
        conn = db_connect()
        rows = conn.execute(
            "SELECT cmd_name, min_rank FROM cmd_access WHERE chat_id=? ORDER BY cmd_name",
            (update.effective_chat.id,),
        ).fetchall()
        conn.close()
        if not rows:
            await update.message.reply_text("Настройки доступа не установлены (всё по умолчанию).")
            return
        lines = ["⚙️ <b>Настройки доступа команд:</b>"]
        for r in rows:
            rank_label = "Откл." if r["min_rank"] >= 6 else (
                "Все" if r["min_rank"] == 0 else RANK_NAMES.get(r["min_rank"], r["min_rank"])
            )
            lines.append(f"• {r['cmd_name']} — {rank_label}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return

    if not await require_rank(update, 3):
        return

    # дк <команда> <ранг> → установить конкретный ранг
    if len(context.args) >= 2:
        cmd_name = context.args[0].lower()
        try:
            rank = int(context.args[1])
        except ValueError:
            await update.message.reply_text("Ранг должен быть числом.")
            return
        conn = db_connect()
        conn.execute(
            "INSERT OR REPLACE INTO cmd_access (chat_id, cmd_name, min_rank) VALUES (?,?,?)",
            (update.effective_chat.id, cmd_name, rank),
        )
        conn.commit()
        conn.close()
        label = "Откл." if rank >= 6 else ("Все" if rank == 0 else RANK_NAMES.get(rank, rank))
        await update.message.reply_text(f"✅ {cmd_name}: доступ → {label}")
    else:
        await update.message.reply_text("Формат: дк <команда> <ранг>")


async def cmd_reset_dk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """!Сброс команд — сбросить все настройки доступа."""
    if not await require_rank(update, 5):
        return
    conn = db_connect()
    conn.execute("DELETE FROM cmd_access WHERE chat_id=?", (update.effective_chat.id,))
    conn.commit()
    conn.close()
    await update.message.reply_text("✅ Все настройки доступа команд сброшены.")


# ─────────────────────────── МОДУЛЬ 5: ЧИСТКА ЧАТА ──────────────────────────

async def cmd_del_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    -смс [число] — удалить указанное количество сообщений (до 100).
    Без аргументов — удалить текущее сообщение и реплай.
    """
    if not await require_rank(update, 1):
        return
    chat_id = update.effective_chat.id

    args = _parse_args(update, context)
    if not args:
        if update.message.reply_to_message:
            try:
                await context.bot.delete_message(chat_id, update.message.reply_to_message.message_id)
            except Exception:
                pass
        try:
            await update.message.delete()
        except Exception:
            pass
        return

    try:
        count = int(args[0])
    except ValueError:
        await update.message.reply_text("Укажите число сообщений.")
        return

    count = min(count, 100)
    msg_id = update.message.message_id
    deleted = 0
    for i in range(msg_id, msg_id - count - 1, -1):
        try:
            await context.bot.delete_message(chat_id, i)
            deleted += 1
        except Exception:
            pass

    try:
        notice = await context.bot.send_message(chat_id, f"🗑 Удалено {deleted} сообщений.")
        await asyncio.sleep(3)
        await notice.delete()
    except Exception:
        pass


async def cmd_kick_inactive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кик неактив — кикнуть участников без активности за последние 30 дней."""
    if not await require_rank(update, 2):
        return
    chat = update.effective_chat
    since = (_msk_now() - timedelta(days=30)).strftime("%Y-%m-%d")
    conn = db_connect()
    # Кто был активен за 30 дней
    active = set(
        r["user_id"] for r in
        conn.execute("SELECT DISTINCT user_id FROM activity WHERE chat_id=? AND day>=?", (chat.id, since)).fetchall()
    )
    # Кто вообще есть в activity для этого чата
    all_users = set(
        r["user_id"] for r in
        conn.execute("SELECT DISTINCT user_id FROM activity WHERE chat_id=?", (chat.id,)).fetchall()
    )
    conn.close()
    inactive = all_users - active
    if not inactive:
        await update.message.reply_text("✅ Неактивных участников не найдено.")
        return
    kicked = 0
    failed = 0
    for uid in inactive:
        try:
            member = await chat.get_member(uid)
            if member.status in ("member", "restricted"):
                await _clear_mute_on_kick(chat.id, uid, bot=context.bot)
                await chat.ban_member(uid)
                await chat.unban_member(uid)  # кик = бан + разбан
                kicked += 1
        except Exception:
            failed += 1
    await update.message.reply_text(
        f"🦵 Кик неактивных завершён!\n"
        f"✅ Кикнуто: <b>{kicked}</b>\n"
        f"❌ Ошибок: <b>{failed}</b>\n"
        f"(не писали 30+ дней)",
        parse_mode="HTML",
    )


async def cmd_kick_deleted(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кик удалённых — исключить удалённые аккаунты из known_users."""
    if not await require_rank(update, 2):
        return
    chat = update.effective_chat
    conn = db_connect()
    rows = conn.execute(
        "SELECT DISTINCT user_id FROM activity WHERE chat_id=?", (chat.id,)
    ).fetchall()
    conn.close()
    kicked = 0
    failed = 0
    for row in rows:
        uid = row["user_id"]
        try:
            member = await chat.get_member(uid)
            user = member.user
            # Удалённый аккаунт: first_name пустое или "Deleted Account"
            if (not user.first_name or user.first_name == "Deleted Account") and not user.username:
                if member.status in ("member", "restricted"):
                    await chat.ban_member(uid)
                    await chat.unban_member(uid)
                    kicked += 1
        except Exception:
            failed += 1
    await update.message.reply_text(
        f"🗑 Кик удалённых аккаунтов завершён!\n"
        f"✅ Кикнуто: <b>{kicked}</b>\n"
        f"❌ Ошибок: <b>{failed}</b>",
        parse_mode="HTML",
    )


# ─────────────────────────── МОДУЛЬ 6: НАСТРОЙКА ЧАТА ───────────────────────

async def cmd_pin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """!Закреп — закрепить сообщение (реплай)."""
    if not await require_rank(update, 2):
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Используйте команду ответом на сообщение.")
        return
    try:
        await update.effective_chat.pin_message(update.message.reply_to_message.message_id)
        await update.message.reply_text("📌 Сообщение закреплено.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


async def cmd_unpin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """!Открепить — открепить закреплённое сообщение."""
    if not await require_rank(update, 2):
        return
    try:
        await update.effective_chat.unpin_message()
        await update.message.reply_text("📌 Сообщение откреплено.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


async def cmd_set_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """!Название <текст> — изменить название чата."""
    if not await require_rank(update, 3):
        return
    args = _parse_args(update, context)
    if not args:
        await update.message.reply_text("Укажите новое название.")
        return
    title = " ".join(args)
    try:
        await update.effective_chat.set_title(title)
        await update.message.reply_text(f"✅ Название изменено на: {title}")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


async def cmd_set_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    +Описание чата
    <текст> — установить описание чата.
    """
    if not await require_rank(update, 3):
        return
    lines = update.message.text.split("\n", 1)
    if len(lines) < 2:
        await update.message.reply_text("Напишите описание на следующей строке.")
        return
    desc = lines[1].strip()
    try:
        await update.effective_chat.set_description(desc)
        await update.message.reply_text("✅ Описание чата обновлено.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


async def cmd_set_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """+Чат ссылка — создать и показать ссылку на чат."""
    if not await require_rank(update, 3):
        return
    try:
        link = await update.effective_chat.create_invite_link()
        await update.message.reply_text(f"🔗 Ссылка на чат: {link.invite_link}")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


async def cmd_set_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    +Правила
    <текст> — установить правила чата.
    """
    if not await require_rank(update, 3):
        return
    lines = update.message.text.split("\n", 1)
    if len(lines) < 2:
        await update.message.reply_text("Напишите правила на следующей строке.")
        return
    rules = lines[1].strip()
    set_chat_setting(update.effective_chat.id, "rules", rules)
    await update.message.reply_text("✅ Правила чата сохранены.")


async def cmd_get_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Правила — показать правила чата."""
    rules = get_chat_setting(update.effective_chat.id, "rules")
    if not rules:
        await update.message.reply_text("Правила не установлены.")
        return
    await update.message.reply_text(f"📜 <b>Правила чата:</b>\n{rules}", parse_mode="HTML")


async def cmd_set_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    +Приветствие
    <текст> — установить приветствие (переменные: {имя}).
    """
    if not await require_rank(update, 3):
        return
    lines = update.message.text.split("\n", 1)
    if len(lines) < 2:
        await update.message.reply_text("Напишите текст приветствия на следующей строке.")
        return
    welcome = lines[1].strip()
    set_chat_setting(update.effective_chat.id, "welcome", welcome)
    await update.message.reply_text("✅ Приветствие сохранено.")


async def on_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет приветствие новым участникам."""
    chat_id = update.effective_chat.id
    welcome = get_chat_setting(chat_id, "welcome")
    if not welcome:
        return
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue
        text = welcome.replace("{имя}", member.mention_html())
        await update.message.reply_text(text, parse_mode="HTML")


async def on_bot_joined(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Срабатывает когда бот добавлен в чат (или его права изменились).
    - Создатель чата → ранг 5
    - Существующие TG-администраторы без ранга в БД → ранг 2
    """
    result = update.my_chat_member
    if not result:
        return

    # Бот был добавлен (стал участником/администратором)
    new_status = result.new_chat_member.status
    old_status = result.old_chat_member.status

    bot_became_member = (
        old_status in (ChatMember.LEFT, ChatMember.BANNED)
        and new_status not in (ChatMember.LEFT, ChatMember.BANNED)
    )

    if not bot_became_member:
        return

    chat_id = result.chat.id
    await sync_chat_admins(context.bot, chat_id)

    # Уведомление в чат
    try:
        lines = [
            "👋 <b>Привет! Я чат-менеджер.</b>",
            "",
            "✅ Ранги автоматически назначены:",
            "⭐️⭐️⭐️⭐️⭐️ Создатель → ранг 5",
            "⭐️⭐️ TG-администраторы → ранг 2 (Старший модератор)",
            "",
            "Чтобы изменить ранги: <code>+Модер [1-4] @пользователь</code>",
            "Список команд: /help",
        ]
        await context.bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML")
    except Exception as e:
        logger.error(f"on_bot_joined notify error: {e}")


async def cmd_sync_ranks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Синхронизация — вручную синхронизировать ранги TG-администраторов."""
    if not await require_rank(update, 4):
        return
    await update.message.reply_text("⏳ Синхронизирую ранги с Telegram...")
    await sync_chat_admins(context.bot, update.effective_chat.id)
    # Показываем результат
    conn = db_connect()
    rows = conn.execute(
        "SELECT user_id, rank FROM moderators WHERE chat_id=? ORDER BY rank DESC",
        (update.effective_chat.id,),
    ).fetchall()
    conn.close()
    lines = ["✅ <b>Синхронизация завершена. Текущие ранги:</b>"]
    for row in rows:
        try:
            member = await update.effective_chat.get_member(row["user_id"])
            name = member.user.mention_html()
        except Exception:
            name = f"id{row['user_id']}"
        lines.append(f"{name} — {RANK_NAMES.get(row['rank'], row['rank'])}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_warn_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Варны лимит <число> — лимит варнов.
    Варны чс <период> — срок бана по лимиту.
    Варны период <период> — срок хранения варна.
    """
    if not await require_rank(update, 3):
        return
    context.args = _parse_args(update, context)
    if not context.args:
        chat_id = update.effective_chat.id
        limit = get_chat_setting(chat_id, "warn_limit", 3)
        ban_dur = get_chat_setting(chat_id, "warn_ban_duration", "7 дней")
        dur = get_chat_setting(chat_id, "warn_duration", "30 дней")
        action = get_chat_setting(chat_id, "warn_action", "бан")
        await update.message.reply_text(
            f"⚙️ <b>Настройки варнов:</b>\n"
            f"• Лимит: {limit}\n"
            f"• Действие при лимите: {action}\n"
            f"• Срок {action}а при лимите: {ban_dur}\n"
            f"• Срок хранения варна: {dur}\n\n"
            f"Команды: <code>Варны лимит N</code> | <code>Варны действие бан/мут</code> | "
            f"<code>Варны чс 7 дней</code> | <code>Варны период 30 дней</code>",
            parse_mode="HTML",
        )
        return

    sub = context.args[0].lower()
    if sub == "лимит" and len(context.args) >= 2:
        try:
            lim = int(context.args[1])
        except ValueError:
            await update.message.reply_text("Лимит должен быть числом.")
            return
        set_chat_setting(update.effective_chat.id, "warn_limit", lim)
        await update.message.reply_text(f"✅ Лимит варнов: {lim}")

    elif sub == "действие" and len(context.args) >= 2:
        # варны действие бан / варны действие мут
        action = context.args[1].lower()
        if action not in ("бан", "мут"):
            await update.message.reply_text("Доступные действия: бан / мут")
            return
        set_chat_setting(update.effective_chat.id, "warn_action", action)
        await update.message.reply_text(
            f"✅ Действие при достижении лимита варнов: {action}"
        )

    elif sub == "чс" and len(context.args) >= 2:
        # варны чс 7 дней  ИЛИ  варны чс навсегда  ИЛИ  варны чс 0 (отключить)
        val = " ".join(context.args[1:])
        if val.strip() == "0":
            set_chat_setting(update.effective_chat.id, "warn_ban_duration", "навсегда")
            await update.message.reply_text("✅ При достижении лимита варнов — бан навсегда.")
        else:
            delta = parse_duration(val)
            if not delta and val.strip().lower() not in ("навсегда", "постоянно"):
                await update.message.reply_text(
                    "❌ Не удалось разобрать период.\n"
                    "Примеры: <code>варны чс 7 дней</code>, <code>варны чс 2 часа</code>, <code>варны чс навсегда</code>",
                    parse_mode="HTML",
                )
                return
            set_chat_setting(update.effective_chat.id, "warn_ban_duration", val)
            await update.message.reply_text(f"✅ Срок бана/мута при достижении лимита: {val}")

    elif sub == "период" and len(context.args) >= 2:
        val = " ".join(context.args[1:])
        delta = parse_duration(val)
        if not delta:
            await update.message.reply_text(
                "❌ Не удалось разобрать период.\n"
                "Примеры: <code>варны период 30 дней</code>, <code>варны период 2 недели</code>",
                parse_mode="HTML",
            )
            return
        set_chat_setting(update.effective_chat.id, "warn_duration", val)
        await update.message.reply_text(f"✅ Срок хранения варнов: {val}")

    else:
        await update.message.reply_text(
            "📋 <b>Настройка варнов:</b>\n"
            "• <code>Варны лимит N</code> — сколько варнов до наказания\n"
            "• <code>Варны действие бан</code> — при лимите: бан\n"
            "• <code>Варны действие мут</code> — при лимите: мут\n"
            "• <code>Варны чс 7 дней</code> — срок бана/мута при лимите\n"
            "• <code>Варны чс навсегда</code> — бан навсегда\n"
            "• <code>Варны период 30 дней</code> — срок хранения варна",
            parse_mode="HTML",
        )


# ─────────────────────────── МОДУЛЬ 9: СТАТИСТИКА ───────────────────────────

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Стата — базовая статистика чата."""
    chat_id = update.effective_chat.id
    conn = db_connect()
    warns = conn.execute(
        "SELECT COUNT(*) as c FROM warns WHERE chat_id=? AND (expires_at IS NULL OR expires_at > datetime('now'))",
        (chat_id,),
    ).fetchone()["c"]
    bans = conn.execute(
        "SELECT COUNT(*) as c FROM bans WHERE chat_id=?", (chat_id,)
    ).fetchone()["c"]
    mods = conn.execute(
        "SELECT COUNT(*) as c FROM moderators WHERE chat_id=?", (chat_id,)
    ).fetchone()["c"]
    notes = conn.execute(
        "SELECT COUNT(*) as c FROM notes WHERE chat_id=?", (chat_id,)
    ).fetchone()["c"]
    bookmarks = conn.execute(
        "SELECT COUNT(*) as c FROM bookmarks WHERE chat_id=?", (chat_id,)
    ).fetchone()["c"]
    conn.close()
    try:
        chat = await context.bot.get_chat(chat_id)
        members = await chat.get_member_count()
    except Exception:
        members = "?"
    await update.message.reply_text(
        f"📊 <b>Статистика чата:</b>\n"
        f"👥 Участников: {members}\n"
        f"👮 Модераторов: {mods}\n"
        f"⚠️ Активных варнов: {warns}\n"
        f"🔨 Банов в базе: {bans}\n"
        f"📝 Заметок: {notes}\n"
        f"🔖 Закладок: {bookmarks}",
        parse_mode="HTML",
    )

async def cmd_bot_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Стата бота — общая статистика по всем группам (только из главной группы)."""
    chat_id = update.effective_chat.id

    # Проверка: только из главной группы
    if MAIN_GROUP_ID and chat_id != MAIN_GROUP_ID:
        await update.message.reply_text("⛔ Эта команда доступна только из главной группы.")
        return

    if not await require_rank(update, 3):
        return

    conn = db_connect()

    # Количество групп где есть бот
    # Считаем только реальные группы (отрицательный chat_id = группа/супергруппа)
    chats_set = set()
    for table in ("chat_settings", "moderators", "activity"):
        try:
            rows = conn.execute(f"SELECT DISTINCT chat_id FROM {table}").fetchall()
            chats_set.update(r["chat_id"] for r in rows if r["chat_id"] < 0)
        except Exception:
            pass

    total_groups = len(chats_set)

    # Суммарная статистика по всем группам
    total_warns = conn.execute(
        "SELECT COUNT(*) as c FROM warns WHERE expires_at IS NULL OR expires_at > datetime('now')"
    ).fetchone()["c"]

    total_bans = conn.execute("SELECT COUNT(*) as c FROM bans").fetchone()["c"]

    total_mods = conn.execute("SELECT COUNT(*) as c FROM moderators").fetchone()["c"]

    total_notes = conn.execute("SELECT COUNT(*) as c FROM notes").fetchone()["c"]

    total_bookmarks = conn.execute("SELECT COUNT(*) as c FROM bookmarks").fetchone()["c"]

    total_timers = conn.execute("SELECT COUNT(*) as c FROM timers").fetchone()["c"]

    total_known_users = conn.execute("SELECT COUNT(*) as c FROM known_users").fetchone()["c"]

    # Топ-5 групп по числу модераторов
    top_mods_rows = conn.execute(
        "SELECT chat_id, COUNT(*) as cnt FROM moderators GROUP BY chat_id ORDER BY cnt DESC LIMIT 5"
    ).fetchall()

    # Топ-5 групп по числу банов
    top_bans_rows = conn.execute(
        "SELECT chat_id, COUNT(*) as cnt FROM bans GROUP BY chat_id ORDER BY cnt DESC LIMIT 5"
    ).fetchall()

    # Статистика игровых команд
    game_rows = conn.execute(
        "SELECT cmd, COUNT(*) as cnt FROM game_log GROUP BY cmd ORDER BY cnt DESC"
    ).fetchall()

    conn.close()

    lines = [
        "🤖 <b>Статистика бота по всем группам</b>",
        "",
        f"🏠 Групп в базе: <b>{total_groups}</b>",
        f"👤 Известных пользователей: <b>{total_known_users}</b>",
        f"👮 Модераторов (всего): <b>{total_mods}</b>",
        f"⚠️ Активных варнов: <b>{total_warns}</b>",
        f"🔨 Банов в базе: <b>{total_bans}</b>",
        f"📝 Заметок: <b>{total_notes}</b>",
        f"🔖 Закладок: <b>{total_bookmarks}</b>",
        f"⏰ Активных таймеров: <b>{total_timers}</b>",
    ]

    if top_mods_rows:
        lines.append("\n📋 <b>Топ групп по модераторам:</b>")
        for r in top_mods_rows:
            try:
                chat = await context.bot.get_chat(r["chat_id"])
                name = chat.title or str(r["chat_id"])
            except Exception:
                name = str(r["chat_id"])
            lines.append(f"  • {name} — {r['cnt']} мод.")

    if top_bans_rows:
        lines.append("\n🔨 <b>Топ групп по банам:</b>")
        for r in top_bans_rows:
            try:
                chat = await context.bot.get_chat(r["chat_id"])
                name = chat.title or str(r["chat_id"])
            except Exception:
                name = str(r["chat_id"])
            lines.append(f"  • {name} — {r['cnt']} бан.")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")




async def cmd_mod_topic_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    +Тема <название>
    <содержание> — создать тему модераторов.
    """
    if not await require_rank(update, 1):
        return
    args = _parse_args(update, context)
    lines = update.message.text.split("\n", 1)
    title_part = " ".join(args) if args else ""
    content = lines[1].strip() if len(lines) > 1 else ""
    if not title_part:
        await update.message.reply_text("Укажите название темы.")
        return
    conn = db_connect()
    conn.execute(
        "INSERT INTO mod_topics (chat_id, user_id, title, content) VALUES (?,?,?,?)",
        (update.effective_chat.id, update.effective_user.id, title_part, content),
    )
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Тема «{title_part}» создана.")


async def cmd_mod_topics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Темы — список тем модераторов."""
    if not await require_rank(update, 1):
        return
    conn = db_connect()
    rows = conn.execute(
        "SELECT id, title, created_at FROM mod_topics WHERE chat_id=? ORDER BY created_at DESC",
        (update.effective_chat.id,),
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("Тем нет.")
        return
    lines = ["📋 <b>Темы модераторов:</b>"]
    for r in rows:
        lines.append(f"[{r['id']}] {r['title']} ({r['created_at'][:10]})")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_mod_topic_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Тема <номер> — просмотр темы."""
    if not await require_rank(update, 1):
        return
    args = _parse_args(update, context)
    if not args or not args[0].isdigit():
        await update.message.reply_text("Укажите номер темы.")
        return
    conn = db_connect()
    row = conn.execute(
        "SELECT title, content, created_at FROM mod_topics WHERE id=? AND chat_id=?",
        (int(args[0]), update.effective_chat.id),
    ).fetchone()
    conn.close()
    if not row:
        await update.message.reply_text("Тема не найдена.")
        return
    await update.message.reply_text(
        f"📋 <b>{row['title']}</b>\n{row['content'] or '—'}\n({row['created_at'][:10]})",
        parse_mode="HTML",
    )


# ─────────────────────────── МОДУЛЬ 11: ГОЛОСОВАНИЕ ─────────────────────────

async def cmd_vote_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Гб [число голосов] [ранг] <ссылка|реплай> — голосование за бан.
    По умолчанию: 5 голосов, 0 ранг.
    """
    chat_id = update.effective_chat.id
    votes_needed = 5
    min_rank = 0
    args = _parse_args(update, context)

    if args and args[0].isdigit():
        votes_needed = int(args.pop(0))
    if args and args[0].isdigit():
        min_rank = int(args.pop(0))

    target = await resolve_target(update, context)
    if not target:
        await update.message.reply_text("Укажите пользователя.")
        return

    conn = db_connect()
    existing = conn.execute(
        "SELECT id FROM ban_votes WHERE chat_id=? AND target_id=? AND status='active'",
        (chat_id, target.id),
    ).fetchone()
    if existing:
        conn.close()
        await update.message.reply_text("Голосование за бан этого пользователя уже активно.")
        return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔨 Забанить", callback_data=f"bv_yes_{target.id}"),
        InlineKeyboardButton("🕊 Помиловать", callback_data=f"bv_no_{target.id}"),
    ]])
    msg = await update.message.reply_text(
        f"🗳 <b>Голосование за бан</b>\n"
        f"Цель: {target.mention_html()}\n"
        f"Голосов за бан: 0/{votes_needed}\n"
        f"Минимальный ранг голосующего: {min_rank}",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    conn.execute(
        "INSERT INTO ban_votes (chat_id, target_id, initiator_id, votes_needed, min_rank, message_id) VALUES (?,?,?,?,?,?)",
        (chat_id, target.id, update.effective_user.id, votes_needed, min_rank, msg.message_id),
    )
    conn.commit()
    conn.close()


async def cb_ban_vote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик кнопок голосования за бан."""
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = update.effective_chat.id

    if data.startswith("bv_yes_") or data.startswith("bv_no_"):
        vote_yes = data.startswith("bv_yes_")
        target_id = int(data.split("_")[2])
        user_id = query.from_user.id

        conn = db_connect()
        vote = conn.execute(
            "SELECT * FROM ban_votes WHERE chat_id=? AND target_id=? AND status='active'",
            (chat_id, target_id),
        ).fetchone()
        if not vote:
            conn.close()
            await query.answer("Голосование завершено.")
            return

        already = conn.execute(
            "SELECT 1 FROM ban_vote_participants WHERE vote_id=? AND user_id=?",
            (vote["id"], user_id),
        ).fetchone()
        if already:
            conn.close()
            await query.answer("Вы уже голосовали.")
            return

        voter_rank = get_rank(chat_id, user_id)
        if voter_rank < vote["min_rank"]:
            conn.close()
            await query.answer(f"Требуется ранг {vote['min_rank']}.")
            return

        conn.execute(
            "INSERT INTO ban_vote_participants (vote_id, user_id) VALUES (?,?)",
            (vote["id"], user_id),
        )

        if vote_yes:
            new_votes = vote["current_votes"] + 1
            conn.execute(
                "UPDATE ban_votes SET current_votes=? WHERE id=?",
                (new_votes, vote["id"]),
            )
            conn.commit()

            if new_votes >= vote["votes_needed"]:
                conn.execute(
                    "UPDATE ban_votes SET status='done' WHERE id=?", (vote["id"],)
                )
                conn.commit()
                conn.close()
                try:
                    await context.bot.ban_chat_member(chat_id, target_id)
                except Exception:
                    pass
                await query.edit_message_text(
                    f"🔨 Голосование завершено — пользователь id{target_id} забанен!"
                )
                return
        conn.commit()
        conn.close()

        vote_now = vote["current_votes"] + (1 if vote_yes else 0)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔨 Забанить", callback_data=f"bv_yes_{target_id}"),
            InlineKeyboardButton("🕊 Помиловать", callback_data=f"bv_no_{target_id}"),
        ]])
        await query.edit_message_text(
            f"🗳 <b>Голосование за бан</b>\n"
            f"Цель: id{target_id}\n"
            f"Голосов за бан: {vote_now}/{vote['votes_needed']}",
            parse_mode="HTML",
            reply_markup=keyboard,
        )


async def cmd_vote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    +Гк <число голосов> [ранг] <команда> — голосование за выполнение команды.
    """
    args = _parse_args(update, context)
    if not args or len(args) < 2:
        await update.message.reply_text("Формат: +гк <голосов> [ранг] <команда>")
        return
    votes_needed = 5
    min_rank = 0
    if args[0].isdigit():
        votes_needed = int(args.pop(0))
    if args and args[0].isdigit():
        min_rank = int(args.pop(0))
    command = " ".join(args)

    chat_id = update.effective_chat.id
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ За", callback_data=f"vc_yes"),
        InlineKeyboardButton("❌ Против", callback_data=f"vc_no"),
    ]])
    msg = await update.message.reply_text(
        f"🗳 <b>Голосование за команду</b>\n"
        f"Команда: <code>{command}</code>\n"
        f"Голосов: 0/{votes_needed}  (ранг ≥{min_rank})",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    conn = db_connect()
    conn.execute(
        "INSERT INTO votes (chat_id, message_id, initiator_id, command, votes_needed, min_rank) VALUES (?,?,?,?,?,?)",
        (chat_id, msg.message_id, update.effective_user.id, command, votes_needed, min_rank),
    )
    conn.commit()
    conn.close()


async def cb_vote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик кнопок голосования за команду."""
    query = update.callback_query
    await query.answer()
    if not (query.data == "vc_yes" or query.data == "vc_no"):
        return

    chat_id = update.effective_chat.id
    user_id = query.from_user.id
    msg_id = query.message.message_id

    conn = db_connect()
    vote = conn.execute(
        "SELECT * FROM votes WHERE chat_id=? AND message_id=? AND status='active'",
        (chat_id, msg_id),
    ).fetchone()
    if not vote:
        conn.close()
        await query.answer("Голосование завершено.")
        return

    already = conn.execute(
        "SELECT 1 FROM vote_participants WHERE vote_id=? AND user_id=?",
        (vote["id"], user_id),
    ).fetchone()
    if already:
        conn.close()
        await query.answer("Вы уже голосовали.")
        return

    voter_rank = get_rank(chat_id, user_id)
    if voter_rank < vote["min_rank"]:
        conn.close()
        await query.answer(f"Требуется ранг {vote['min_rank']}.")
        return

    conn.execute(
        "INSERT INTO vote_participants (vote_id, user_id) VALUES (?,?)",
        (vote["id"], user_id),
    )

    if query.data == "vc_yes":
        new_votes = vote["current_votes"] + 1
        conn.execute("UPDATE votes SET current_votes=? WHERE id=?", (new_votes, vote["id"]))
        conn.commit()
        if new_votes >= vote["votes_needed"]:
            conn.execute("UPDATE votes SET status='done' WHERE id=?", (vote["id"],))
            conn.commit()
            conn.close()
            await query.edit_message_text(
                f"✅ Голосование завершено! Команда принята:\n<code>{vote['command']}</code>",
                parse_mode="HTML",
            )
            return
    conn.commit()
    conn.close()

    vote_now = vote["current_votes"] + (1 if query.data == "vc_yes" else 0)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ За", callback_data="vc_yes"),
        InlineKeyboardButton("❌ Против", callback_data="vc_no"),
    ]])
    await query.edit_message_text(
        f"🗳 <b>Голосование за команду</b>\n"
        f"Команда: <code>{vote['command']}</code>\n"
        f"Голосов: {vote_now}/{vote['votes_needed']}  (ранг ≥{vote['min_rank']})",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


# ─────────────────────────── МОДУЛЬ 23: ЗАКЛАДКИ ────────────────────────────

async def cmd_add_bookmark(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    +Закладка <название> — добавить закладку (реплаем или с текстом).
    """
    args = _parse_args(update, context)
    if not args:
        await update.message.reply_text("Укажите название закладки.")
        return
    title = " ".join(args)
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    ref_msg_id = (
        update.message.reply_to_message.message_id
        if update.message.reply_to_message
        else update.message.message_id
    )
    conn = db_connect()
    conn.execute(
        "INSERT INTO bookmarks (chat_id, user_id, title, message_id) VALUES (?,?,?,?)",
        (chat_id, user_id, title, ref_msg_id),
    )
    conn.commit()
    bid = conn.execute("SELECT last_insert_rowid() as id").fetchone()["id"]
    conn.close()
    await update.message.reply_text(f"🔖 Закладка [{bid}] «{title}» добавлена.")


async def cmd_chatbook(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Чатбук [страница] — список всех закладок чата."""
    args = _parse_args(update, context)
    page = int(args[0]) if args and args[0].isdigit() else 1
    per_page = 10
    offset = (page - 1) * per_page
    conn = db_connect()
    rows = conn.execute(
        "SELECT id, title, user_id, created_at FROM bookmarks WHERE chat_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (update.effective_chat.id, per_page, offset),
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("Закладок нет.")
        return
    lines = [f"🔖 <b>Чатбук (стр. {page}):</b>"]
    for r in rows:
        lines.append(f"[{r['id']}] {r['title']} ({r['created_at'][:10]})")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_my_bookmarks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Мои закладки — ваши закладки."""
    args = _parse_args(update, context)
    page = int(args[0]) if args and args[0].isdigit() else 1
    per_page = 10
    offset = (page - 1) * per_page
    conn = db_connect()
    rows = conn.execute(
        "SELECT id, title, created_at FROM bookmarks WHERE chat_id=? AND user_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (update.effective_chat.id, update.effective_user.id, per_page, offset),
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("У вас нет закладок.")
        return
    lines = [f"🔖 <b>Мои закладки (стр. {page}):</b>"]
    for r in rows:
        lines.append(f"[{r['id']}] {r['title']}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_del_bookmark(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Удалить закладку <номер> — удалить закладку."""
    args = _parse_args(update, context)
    if not args or not args[0].isdigit():
        await update.message.reply_text("Укажите номер закладки.")
        return
    bid = int(args[0])
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    conn = db_connect()
    row = conn.execute(
        "SELECT user_id FROM bookmarks WHERE id=? AND chat_id=?", (bid, chat_id)
    ).fetchone()
    if not row:
        conn.close()
        await update.message.reply_text("Закладка не найдена.")
        return
    caller_rank = get_rank(chat_id, user_id)
    if row["user_id"] != user_id and caller_rank < 1:
        conn.close()
        await update.message.reply_text("Нет прав на удаление этой закладки.")
        return
    conn.execute("DELETE FROM bookmarks WHERE id=?", (bid,))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Закладка [{bid}] удалена.")


# ─────────────────────────── МОДУЛЬ 24: ЗАМЕТКИ ─────────────────────────────

async def cmd_add_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    +Заметка <название>
    <текст> — создать заметку.
    """
    if not await require_rank(update, 1):
        return
    args = _parse_args(update, context)
    title = " ".join(args) if args else ""
    lines = update.message.text.split("\n", 1)
    content = lines[1].strip() if len(lines) > 1 else ""
    if not title:
        await update.message.reply_text("Укажите название заметки.")
        return
    conn = db_connect()
    count = conn.execute(
        "SELECT COUNT(*) as c FROM notes WHERE chat_id=?", (update.effective_chat.id,)
    ).fetchone()["c"]
    if count >= 100:
        conn.close()
        await update.message.reply_text("Достигнут лимит 100 заметок.")
        return
    conn.execute(
        "INSERT INTO notes (chat_id, title, content, created_by) VALUES (?,?,?,?)",
        (update.effective_chat.id, title[:40], content, update.effective_user.id),
    )
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Заметка «{title[:40]}» создана.")


async def cmd_del_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """-Заметка <название или номер> — удалить заметку."""
    if not await require_rank(update, 1):
        return
    args = _parse_args(update, context)
    if not args:
        await update.message.reply_text("Укажите название или номер заметки.")
        return
    chat_id = update.effective_chat.id
    query_arg = " ".join(args)
    conn = db_connect()
    if query_arg.isdigit():
        row = conn.execute(
            "SELECT id, title FROM notes WHERE id=? AND chat_id=?",
            (int(query_arg), chat_id),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id, title FROM notes WHERE LOWER(title)=? AND chat_id=?",
            (query_arg.lower(), chat_id),
        ).fetchone()
    if not row:
        conn.close()
        await update.message.reply_text("Заметка не найдена.")
        return
    conn.execute("DELETE FROM notes WHERE id=?", (row["id"],))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Заметка «{row['title']}» удалена.")


async def cmd_edit_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    ~Заметка <название или номер>
    <новый текст> — редактировать заметку.
    """
    if not await require_rank(update, 1):
        return
    args = _parse_args(update, context)
    if not args:
        await update.message.reply_text("Укажите название или номер заметки.")
        return
    lines = update.message.text.split("\n", 1)
    if len(lines) < 2:
        await update.message.reply_text("Напишите новый текст на следующей строке.")
        return
    new_content = lines[1].strip()
    query_arg = " ".join(args)
    chat_id = update.effective_chat.id
    conn = db_connect()
    if query_arg.isdigit():
        row = conn.execute(
            "SELECT id FROM notes WHERE id=? AND chat_id=?", (int(query_arg), chat_id)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM notes WHERE LOWER(title)=? AND chat_id=?",
            (query_arg.lower(), chat_id),
        ).fetchone()
    if not row:
        conn.close()
        await update.message.reply_text("Заметка не найдена.")
        return
    conn.execute("UPDATE notes SET content=? WHERE id=?", (new_content, row["id"]))
    conn.commit()
    conn.close()
    await update.message.reply_text("✅ Заметка обновлена.")


async def cmd_notes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Заметки [страница] — список всех заметок."""
    args = _parse_args(update, context)
    page = int(args[0]) if args and args[0].isdigit() else 1
    per_page = 15
    offset = (page - 1) * per_page
    conn = db_connect()
    rows = conn.execute(
        "SELECT id, title FROM notes WHERE chat_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
        (update.effective_chat.id, per_page, offset),
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("Заметок нет.")
        return
    lines = [f"📝 <b>Заметки (стр. {page}):</b>"]
    for r in rows:
        lines.append(f"[{r['id']}] {r['title']}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_view_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Заметка <название или номер> — просмотр заметки."""
    args = _parse_args(update, context)
    if not args:
        await update.message.reply_text("Укажите название или номер заметки.")
        return
    query_arg = " ".join(args)
    chat_id = update.effective_chat.id
    conn = db_connect()
    if query_arg.isdigit():
        row = conn.execute(
            "SELECT title, content FROM notes WHERE id=? AND chat_id=?",
            (int(query_arg), chat_id),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT title, content FROM notes WHERE LOWER(title)=? AND chat_id=?",
            (query_arg.lower(), chat_id),
        ).fetchone()
    conn.close()
    if not row:
        await update.message.reply_text("Заметка не найдена.")
        return
    await update.message.reply_text(
        f"📝 <b>{row['title']}</b>\n{row['content'] or '—'}",
        parse_mode="HTML",
    )


# ─────────────────────────── МОДУЛЬ 25: ТАЙМЕРЫ ─────────────────────────────

async def cmd_timer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Таймер через <период>
    <команда/текст> — создать таймер.

    Таймер на <дд.мм.гг чч:мм>
    <команда/текст>
    """
    caller_rank = await get_member_rank(update, update.effective_user.id)
    if caller_rank < 1:
        await update.message.reply_text("Таймеры доступны модераторам от 1 ранга.")
        return

    chat_id = update.effective_chat.id
    conn = db_connect()
    count = conn.execute(
        "SELECT COUNT(*) as c FROM timers WHERE chat_id=? AND fire_at > datetime('now')",
        (chat_id,),
    ).fetchone()["c"]
    conn.close()
    if count >= 5:
        await update.message.reply_text("Достигнут лимит 5 активных таймеров в чате.")
        return

    text = update.message.text
    lines = text.split("\n", 1)
    if len(lines) < 2:
        await update.message.reply_text("Напишите команду/текст на следующей строке.")
        return
    timer_cmd = lines[1].strip()

    first_line_parts = lines[0].split()
    if len(first_line_parts) < 3:
        await update.message.reply_text("Формат: Таймер через <период>\\n<команда>")
        return

    mode = first_line_parts[1].lower()
    fire_at = None

    if mode == "через":
        duration_str = " ".join(first_line_parts[2:])
        delta = parse_duration(duration_str)
        if not delta:
            await update.message.reply_text(f"Не удалось разобрать период: {duration_str}")
            return
        if delta.total_seconds() > 31536000:
            await update.message.reply_text("Максимальный срок таймера — 1 год.")
            return
        fire_at = (datetime.utcnow() + delta).isoformat()

    elif mode == "на":
        dt_str = " ".join(first_line_parts[2:])
        for fmt in ("%d.%m.%y %H:%M", "%d.%m.%Y %H:%M", "%d.%m.%y", "%d.%m.%Y"):
            try:
                fire_at = datetime.strptime(dt_str, fmt).isoformat()
                break
            except ValueError:
                continue
        if not fire_at:
            await update.message.reply_text(f"Не удалось разобрать дату: {dt_str}\nФормат: дд.мм.гг чч:мм")
            return
    else:
        await update.message.reply_text("Используйте: Таймер через <период> или Таймер на <дата>")
        return

    conn = db_connect()
    conn.execute(
        "INSERT INTO timers (chat_id, user_id, fire_at, command) VALUES (?,?,?,?)",
        (chat_id, update.effective_user.id, fire_at, timer_cmd),
    )
    conn.commit()
    tid = conn.execute("SELECT last_insert_rowid() as id").fetchone()["id"]
    conn.close()
    await update.message.reply_text(
        f"⏰ Таймер [{tid}] установлен.\nСработает: {fire_at[:16]}\nЗадача: {timer_cmd}"
    )


async def cmd_timers_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Таймеры — список активных таймеров."""
    caller_rank = await get_member_rank(update, update.effective_user.id)
    if caller_rank < 1:
        return
    conn = db_connect()
    rows = conn.execute(
        "SELECT id, fire_at, command FROM timers WHERE chat_id=? AND fire_at > datetime('now') ORDER BY fire_at",
        (update.effective_chat.id,),
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("Нет активных таймеров.")
        return
    lines = ["⏰ <b>Активные таймеры:</b>"]
    for r in rows:
        lines.append(f"[{r['id']}] {r['fire_at'][:16]} — {r['command']}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_del_timer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """-Таймер <номер> — удалить таймер."""
    args = _parse_args(update, context)
    if not args or not args[0].isdigit():
        await update.message.reply_text("Укажите номер таймера.")
        return
    tid = int(args[0])
    conn = db_connect()
    row = conn.execute(
        "SELECT user_id FROM timers WHERE id=? AND chat_id=?",
        (tid, update.effective_chat.id),
    ).fetchone()
    if not row:
        conn.close()
        await update.message.reply_text("Таймер не найден.")
        return
    caller_rank = await get_member_rank(update, update.effective_user.id)
    if row["user_id"] != update.effective_user.id and caller_rank < 2:
        conn.close()
        await update.message.reply_text("Нет прав на удаление этого таймера.")
        return
    conn.execute("DELETE FROM timers WHERE id=?", (tid,))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Таймер [{tid}] удалён.")


async def cmd_reset_timers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """!Сбросить таймеры — удалить все таймеры."""
    if not await require_rank(update, 3):
        return
    conn = db_connect()
    conn.execute("DELETE FROM timers WHERE chat_id=?", (update.effective_chat.id,))
    conn.commit()
    conn.close()
    await update.message.reply_text("✅ Все таймеры удалены.")




# ─────────────────────────── МОДУЛЬ 32: РЕАКЦИИ ─────────────────────────────

async def cmd_allow_reactions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """+Реакции <ссылка|реплай> — разрешить реакции пользователю."""
    if not await require_rank(update, 2):
        return
    target = await resolve_target(update, context)
    if not target:
        await update.message.reply_text("Укажите пользователя.")
        return
    conn = db_connect()
    conn.execute(
        "INSERT OR REPLACE INTO reaction_restrictions (chat_id, user_id, allowed) VALUES (?,?,1)",
        (update.effective_chat.id, target.id),
    )
    conn.commit()
    conn.close()
    await update.message.reply_text(
        f"✅ {target.mention_html()} разрешено ставить реакции.", parse_mode="HTML"
    )


async def cmd_deny_reactions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """-Реакции <ссылка|реплай> — запретить реакции пользователю."""
    if not await require_rank(update, 2):
        return
    target = await resolve_target(update, context)
    if not target:
        await update.message.reply_text("Укажите пользователя.")
        return
    conn = db_connect()
    conn.execute(
        "INSERT OR REPLACE INTO reaction_restrictions (chat_id, user_id, allowed) VALUES (?,?,0)",
        (update.effective_chat.id, target.id),
    )
    conn.commit()
    conn.close()
    await update.message.reply_text(
        f"🚫 {target.mention_html()} запрещено ставить реакции.", parse_mode="HTML"
    )


# ─────────────────────────── АНКЕТА ПОЛЬЗОВАТЕЛЯ ────────────────────────────

async def cmd_profile_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Анкета город <город> — установить город.
    Анкета bio <текст> — установить биографию.
    """
    args = _parse_args(update, context)
    if not args:
        await update.message.reply_text("Формат: анкета город <город> | анкета bio <текст>")
        return
    field = args[0].lower()
    value = " ".join(args[1:])
    user_id = update.effective_user.id
    conn = db_connect()
    conn.execute(
        "INSERT OR IGNORE INTO user_profiles (user_id) VALUES (?)", (user_id,)
    )
    if field == "город":
        conn.execute("UPDATE user_profiles SET city=?, updated_at=datetime('now') WHERE user_id=?", (value, user_id))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"✅ Город установлен: {value}")
    elif field == "bio":
        conn.execute("UPDATE user_profiles SET bio=?, updated_at=datetime('now') WHERE user_id=?", (value, user_id))
        conn.commit()
        conn.close()
        await update.message.reply_text("✅ Биография обновлена.")
    else:
        conn.close()
        await update.message.reply_text("Поле должно быть: город или bio")


async def cmd_profile_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Анкета [ссылка|реплай] — просмотр анкеты."""
    target = await resolve_target(update, context)
    if not target:
        target = update.effective_user

    conn = db_connect()
    profile = conn.execute(
        "SELECT city, bio FROM user_profiles WHERE user_id=?", (target.id,)
    ).fetchone()
    warns = conn.execute(
        "SELECT COUNT(*) as c FROM warns WHERE user_id=? AND (expires_at IS NULL OR expires_at > datetime('now'))",
        (target.id,),
    ).fetchone()["c"]
    conn.close()

    rank = get_rank(update.effective_chat.id, target.id)
    lines = [
        f"👤 <b>Анкета:</b> {target.mention_html()}",
        f"🏅 Ранг: {RANK_NAMES.get(rank, rank)}",
        f"⚠️ Варнов: {warns}",
    ]
    if profile:
        if profile["city"]:
            lines.append(f"🏙 Город: {profile['city']}")
        if profile["bio"]:
            lines.append(f"📝 О себе: {profile['bio']}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ─────────────────────────── КОМАНДА ПОМОЩИ ─────────────────────────────────

HELP_SECTIONS = {
    "main": (
        "🤖 <b>Чат-Менеджер — Главное меню</b>\n\nВыберите раздел:",
        [
            [("👮 Модерация",       "mod"),    ("⚠️ Варны/Муты/Баны", "warns")],
            [("⚙️ Настройки чата",  "chat"),   ("🛡 Автомодерация",   "automod")],
            [("📋 Доступ команд",   "dk"),     ("📊 Статистика",      "stats")],
            [("🎮 Игры (гео)",      "geo"),    ("💰 Экономика",       "econ")],
            [("🗺 Экспедиция",      "exp"),    ("📌 Разное",          "misc")],
        ]
    ),
    "mod": (
        "<b>👮 Модерация</b>\n\n"
        "• <code>+Модер [ранг] @user</code> — назначить ранг (1–4)\n"
        "• <code>-Модер @user</code> — снять ранг\n"
        "• <code>Кто Admin</code> — список модераторов\n"
        "• <code>Созыв</code> — призвать всех модераторов\n"
        "• <code>Синхронизация</code> — синхронизировать ранги с TG",
        [[("« Назад", "main")]]
    ),
    "warns": (
        "<b>⚠️ Варны / Муты / Баны</b>\n\n"
        "• <code>Варн [период] @user</code>\n"
        "• <code>Варны @user</code> | <code>Мои варны</code>\n"
        "• <code>-Варн @user</code> | <code>Снять все варны @user</code>\n"
        "• <code>Мут [срок] @user</code> | <code>-Мут @user</code>\n"
        "• <code>Бан [срок] @user</code> | <code>Разбан @user</code>\n"
        "• <code>Кик @user</code> | <code>Банлист</code> | <code>!Амнистия</code>\n"
        "• <code>Причина @user</code>\n\n"
        "<b>Настройки варнов:</b>\n"
        "• <code>Варны лимит N</code> | <code>Варны действие бан/мут</code>\n"
        "• <code>Варны чс 7 дней</code> | <code>Варны период 30 дней</code>",
        [[("« Назад", "main")]]
    ),
    "chat": (
        "<b>⚙️ Настройки чата</b>\n\n"
        "• <code>!Закреп</code> (реплай) | <code>!Открепить</code>\n"
        "• <code>!Название текст</code>\n"
        "• <code>+Описание чата</code> | <code>+Чат ссылка</code>\n"
        "• <code>+Правила</code> | <code>Правила</code>\n"
        "• <code>+Приветствие</code>\n"
        "• <code>+Реакции @user</code> | <code>-Реакции @user</code>\n"
        "• <code>-смс [N]</code> — удалить сообщения\n"
        "• <code>Кик неактив</code> | <code>Кик удалённых</code>",
        [[("« Назад", "main")]]
    ),
    "automod": (
        "<b>🛡 Автомодерация</b>\n\n"
        "• <code>Антиспам вкл/выкл</code> | лимит | период | действие\n"
        "• <code>Антифлуд вкл/выкл</code> | лимит | действие\n"
        "• <code>+Стоп-слово слово</code> | <code>-Стоп-слово</code> | <code>Стоп-слова</code>\n"
        "• <code>Капча вкл/выкл</code> | <code>Капча таймаут 60</code>",
        [[("« Назад", "main")]]
    ),
    "dk": (
        "<b>📋 Доступ команд</b>\n\n"
        "• <code>дк</code> — список настроек\n"
        "• <code>дк команда ранг</code> — установить минимальный ранг\n"
        "• <code>+дк команда</code> — открыть для всех\n"
        "• <code>-дк команда</code> — отключить команду\n"
        "• <code>!Сброс команд</code> — сбросить все настройки",
        [[("« Назад", "main")]]
    ),
    "stats": (
        "<b>📊 Статистика и активность</b>\n\n"
        "• <code>Стата</code> — статистика чата\n"
        "• <code>Стата бота</code> — по всем группам (ранг 3+)\n"
        "• <code>Топ активности [день|неделя|месяц]</code>\n"
        "• <code>Моя активность</code>\n\n"
        "<b>Темы модераторов:</b>\n"
        "• <code>+Тема название</code> | <code>Темы</code> | <code>Тема N</code>\n\n"
        "<b>Закладки:</b>\n"
        "• <code>+Закладка название</code> | <code>Чатбук</code> | <code>Мои закладки</code>\n\n"
        "<b>Заметки:</b>\n"
        "• <code>+Заметка название</code> | <code>Заметки</code> | <code>Заметка N</code>\n\n"
        "<b>Таймеры:</b>\n"
        "• <code>Таймер через 2 часа</code> | <code>Таймеры</code> | <code>-Таймер N</code>\n\n"
        "<b>Анкета:</b>\n"
        "• <code>Анкета @user</code> | <code>Анкета город Москва</code> | <code>Анкета bio</code>",
        [[("« Назад", "main")]]
    ),
    "geo": (
        "<b>🎮 Игры — Геоугадайка</b>\n\n"
        "• <code>/games</code> — меню игр\n"
        "• <code>Геотоп</code> — топ-10 чата и глобальный\n"
        "• <code>Я</code> — своя статистика\n"
        "• <code>Кто</code> (реплай) — статистика другого игрока\n\n"
        "<b>Голосования:</b>\n"
        "• <code>Гб @user</code> — голосование за бан\n"
        "• <code>+Гк N команда</code> — голосование за выполнение команды",
        [[("« Назад", "main")]]
    ),
    "econ": (
        "<b>💰 Экономика</b>\n\n"
        "• <code>Фарм</code> — добыть 1–10 🪨 (раз в день)\n"
        "• <code>Баланс</code> — посмотреть самородки и монеты\n"
        "• <code>Обмен N</code> — самородки → монеты (курс 1:5)\n"
        "• <code>Магазин</code> — список предметов за монеты\n"
        "• <code>Купить &lt;предмет&gt;</code> — купить предмет\n"
        "• <code>Инвентарь</code> — свои предметы\n"
        "• <code>Сейф</code> — открыть (+10–50 🪙)\n"
        "• <code>Сундук</code> — открыть (монеты / самородки / предмет)\n"
        "• <code>Амулет</code> — случайный предмет (раз в день)\n"
        "• <code>Украсть @user</code> — украсть 10 🪨 или 50 🪙\n"
        "• <code>Дуэль @user N</code> — дуэль на N самородков\n"
        "• <code>Блекджек N</code> — блекджек на N монет\n"
        "• <code>Рулетка N чёт|нечёт|красный|чёрный</code> — поставить на тип (выигрыш 1:1)\n"
        "• <code>Топ богатых</code> — топ-10 игроков",
        [[("« Назад", "main")]]
    ),
    "exp": (
        "<b>🗺 Экспедиция</b>\n\n"
        "• <code>Экспедиция</code> — выбрать локацию и отправиться в поход\n"
        "• Базово доступен только Лес 🌲\n"
        "• Карта 🗺️ найденная в походе открывает следующую локацию\n"
        "• Карта выпадает только на текущей максимальной локации\n"
        "• 🥤 Энергетик — доп. поход (покупается в магазине)\n"
        "• 🧭 Компас — улучшает исход (убирает плохие события)\n"
        "• Во время экспедиции (30 сек) экономика заблокирована",
        [[("« Назад", "main")]]
    ),
    "misc": (
        "<b>📌 Разное</b>\n\n"
        "• <code>Я</code> — статистика гео + баланс экономики\n"
        "• <code>/кости</code> — бросить кости против дилера\n"
        "• <code>/panel</code> — панель управления (ранг 3+, главная группа)",
        [[("« Назад", "main")]]
    ),
}


def _help_keyboard(section_key: str):
    buttons_layout = HELP_SECTIONS[section_key][1]
    rows = []
    for row in buttons_layout:
        rows.append([InlineKeyboardButton(label, callback_data=f"help_{key}") for label, key in row])
    return InlineKeyboardMarkup(rows)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start — приветствие."""
    user = update.effective_user
    name = user.first_name or user.username or "Привет"
    await update.message.reply_text(
        f"👋 <b>Привет, {name}!</b>\n\n"
        "Я — бот-менеджер чата. Помогаю управлять группой: модерация, игры, статистика и многое другое.\n\n"
        "📋 Список команд: <b>Помощь</b>\n"
        "🌍 Геоигра: нажми <b>Игры</b> в чате\n\n"
        "Добавь меня в свою группу и дай права администратора! 🛡",
        parse_mode="HTML",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Помощь / справка — меню по разделам."""
    text, _ = HELP_SECTIONS["main"]
    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=_help_keyboard("main"),
    )


async def cb_help_nav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Навигация по разделам помощи."""
    query = update.callback_query
    await query.answer()
    key = query.data[len("help_"):]  # убираем префикс help_
    if key not in HELP_SECTIONS:
        return
    text, _ = HELP_SECTIONS[key]
    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=_help_keyboard(key),
    )


# ─────────────────────────── МОДУЛЬ: ИГРЫ ────────────────────────────────────

# Набор вопросов: (url_фото, правильный_ответ, [вариант2, вариант3])
# Используем i.ibb.co — бесплатный хостинг, прямые ссылки, Telegram принимает без проблем
GEO_QUESTIONS: list[tuple[str, str, list[str]]] = [
    # ── ЕВРОПА ──────────────────────────────────────────────────────────────────
    ("https://images.unsplash.com/photo-1511739001486-6bfe10ce785f?w=1280", "Франция", ["Германия", "Италия"]),
    ("https://images.unsplash.com/photo-1552832230-c0197dd311b5?w=1280", "Италия", ["Греция", "Испания"]),
    ("https://images.unsplash.com/photo-1513635269975-59663e0ac1ad?w=1280", "Великобритания", ["Ирландия", "Германия"]),
    ("https://images.unsplash.com/photo-1555993539-1732b0258235?w=1280", "Греция", ["Турция", "Италия"]),
    ("https://images.unsplash.com/photo-1467269204594-9661b134dd2b?w=1280", "Германия", ["Австрия", "Швейцария"]),
    ("https://images.unsplash.com/photo-1583422409516-2895a77efded?w=1280", "Испания", ["Португалия", "Италия"]),
    ("https://images.unsplash.com/photo-1599833975787-5c143f373c30?w=1280", "Великобритания", ["Ирландия", "Франция"]),
    ("https://images.unsplash.com/photo-1531572753322-ad063cecc140?w=1280", "Швейцария", ["Австрия", "Норвегия"]),
    ("https://images.unsplash.com/photo-1592906209472-a36b1f3782ef?w=1280", "Чехия", ["Словакия", "Польша"]),
    ("https://images.unsplash.com/photo-1499856871958-5b9627545d1a?w=1280", "Франция", ["Италия", "Великобритания"]),
    ("https://images.unsplash.com/photo-1525874684015-58379d421a52?w=1280", "Италия", ["Испания", "Португалия"]),
    ("https://images.unsplash.com/photo-1533105079780-92b9be482077?w=1280", "Греция", ["Кипр", "Италия"]),
    ("https://images.unsplash.com/photo-1543783207-ec64e4d95325?w=1280", "Испания", ["Марокко", "Португалия"]),
    ("https://images.unsplash.com/photo-1548013146-72479768bada?w=1280", "Дания", ["Швеция", "Норвегия"]),
    ("https://images.unsplash.com/photo-1509356843151-3e7d96241e11?w=1280", "Швеция", ["Норвегия", "Финляндия"]),
    ("https://images.unsplash.com/photo-1520769945061-0a448c463865?w=1280", "Норвегия", ["Швеция", "Дания"]),
    ("https://images.unsplash.com/photo-1609743522653-52354461eb27?w=1280", "Австрия", ["Германия", "Чехия"]),
    ("https://images.unsplash.com/photo-1551730459-92db2a308d6a?w=1280", "Венгрия", ["Австрия", "Чехия"]),
    ("https://images.unsplash.com/photo-1570637171947-3ebd3a76be4e?w=1280", "Австрия", ["Швейцария", "Германия"]),
    ("https://images.unsplash.com/photo-1512470876302-972faa2aa9a4?w=1280", "Нидерланды", ["Бельгия", "Германия"]),
    ("https://images.unsplash.com/photo-1528360983277-13d401cdc186?w=1280", "Бельгия", ["Нидерланды", "Франция"]),
    ("https://images.unsplash.com/photo-1508193638397-1c4234db14d8?w=1280", "Польша", ["Чехия", "Венгрия"]),
    ("https://images.unsplash.com/photo-1539037116277-4db20889f2d4?w=1280", "Эстония", ["Латвия", "Финляндия"]),
    ("https://images.unsplash.com/photo-1548625361-58a9b86aa83b?w=1280", "Литва", ["Латвия", "Польша"]),
    ("https://images.unsplash.com/photo-1555990793-da11153b2473?w=1280", "Хорватия", ["Черногория", "Греция"]),
    # ── АЗИЯ ────────────────────────────────────────────────────────────────────
    ("https://images.unsplash.com/photo-1524492412937-b28074a5d7da?w=1280", "Индия", ["Пакистан", "Иран"]),
    ("https://images.unsplash.com/photo-1493976040374-85c8e12f0c0e?w=1280", "Япония", ["Китай", "Корея"]),
    ("https://images.unsplash.com/photo-1512453979798-5ea266f8880c?w=1280", "ОАЭ", ["Катар", "Бахрейн"]),
    ("https://images.unsplash.com/photo-1562602833-0f4ab2fc46e3?w=1280", "Камбоджа", ["Таиланд", "Вьетнам"]),
    ("https://images.unsplash.com/photo-1548690312-e9fe0ced4e3d?w=1280", "Иордания", ["Израиль", "Египет"]),
    ("https://images.unsplash.com/photo-1508804185872-d7badad00f7d?w=1280", "Китай", ["Монголия", "Корея"]),
    ("https://images.unsplash.com/photo-1527838832700-5059252407fa?w=1280", "Турция", ["Греция", "Иран"]),
    ("https://images.unsplash.com/photo-1525625293386-3f8f99389edd?w=1280", "Сингапур", ["Малайзия", "Индонезия"]),
    ("https://images.unsplash.com/photo-1596422846543-75c6fc197f07?w=1280", "Малайзия", ["Индонезия", "Сингапур"]),
    ("https://images.unsplash.com/photo-1570459027562-4a916cc6113f?w=1280", "Япония", ["Китай", "Тайвань"]),
    ("https://images.unsplash.com/photo-1552465011-b4e21bf6e79a?w=1280", "Таиланд", ["Камбоджа", "Вьетнам"]),
    ("https://images.unsplash.com/photo-1555400038-63f5ba517a47?w=1280", "Индонезия", ["Таиланд", "Индия"]),
    ("https://images.unsplash.com/photo-1508009603885-50cf7c579365?w=1280", "Китай", ["Япония", "Корея"]),
    ("https://images.unsplash.com/photo-1544735716-392fe2489ffa?w=1280", "Израиль", ["Иордания", "Египет"]),
    ("https://images.unsplash.com/photo-1549314843-36b1db1e1f91?w=1280", "Южная Корея", ["Япония", "Китай"]),
    ("https://images.unsplash.com/photo-1567608285969-48e4bbe0d399?w=1280", "Иран", ["Ирак", "Пакистан"]),
    ("https://images.unsplash.com/photo-1559386484-97dfc0e15539?w=1280", "Индия", ["Непал", "Шри-Ланка"]),
    # ── АМЕРИКА ─────────────────────────────────────────────────────────────────
    ("https://images.unsplash.com/photo-1485738422979-f5c462d49f74?w=1280", "США", ["Канада", "Франция"]),
    ("https://images.unsplash.com/photo-1501594907352-04cda38ebc29?w=1280", "США", ["Канада", "Австралия"]),
    ("https://images.unsplash.com/photo-1526392060635-9d6019884377?w=1280", "Перу", ["Бразилия", "Боливия"]),
    ("https://images.unsplash.com/photo-1518638150340-f706e86654de?w=1280", "Мексика", ["Гватемала", "Куба"]),
    ("https://images.unsplash.com/photo-1483729558449-99ef09a8c325?w=1280", "Бразилия", ["Аргентина", "Колумбия"]),
    ("https://images.unsplash.com/photo-1489447068241-b3490214e879?w=1280", "Канада", ["США", "Норвегия"]),
    ("https://images.unsplash.com/photo-1578894381163-e72c17f2d45f?w=1280", "Чили", ["Аргентина", "Перу"]),
    ("https://images.unsplash.com/photo-1586077403686-ba8e5c6c97ce?w=1280", "Аргентина", ["Бразилия", "Парагвай"]),
    ("https://images.unsplash.com/photo-1500759285222-a95626b934cb?w=1280", "Куба", ["Мексика", "Колумбия"]),
    ("https://images.unsplash.com/photo-1544644181-1484b3fdfc62?w=1280", "Канада", ["США", "Норвегия"]),
    # ── АФРИКА И ОКЕАНИЯ ─────────────────────────────────────────────────────────
    ("https://images.unsplash.com/photo-1539768942893-daf53e448371?w=1280", "Египет", ["Марокко", "Иордания"]),
    ("https://images.unsplash.com/photo-1523482580672-f109ba8cb9be?w=1280", "Австралия", ["Новая Зеландия", "ЮАР"]),
    ("https://images.unsplash.com/photo-1580060839134-75a5edca2e99?w=1280", "ЮАР", ["Намибия", "Зимбабве"]),
    ("https://images.unsplash.com/photo-1529108190281-9a4f620bc2d8?w=1280", "Австралия", ["Новая Зеландия", "Папуа Новая Гвинея"]),
    ("https://images.unsplash.com/photo-1547471080-7cc2caa01a7e?w=1280", "Танзания", ["Кения", "Уганда"]),
    ("https://images.unsplash.com/photo-1553913861-c0fddf2619ee?w=1280", "Марокко", ["Алжир", "Тунис"]),
    ("https://images.unsplash.com/photo-1504432842672-1a79f78e4084?w=1280", "Зимбабве", ["Замбия", "Мозамбик"]),
    ("https://images.unsplash.com/photo-1516026672322-bc52d61a55d5?w=1280", "Кения", ["Танзания", "Уганда"]),
    # ── РОССИЯ И СНГ ─────────────────────────────────────────────────────────────
    ("https://images.unsplash.com/photo-1547448415-e9f5b28e570d?w=1280", "Россия", ["Украина", "Беларусь"]),
    ("https://images.unsplash.com/photo-1513326738677-b964603b136d?w=1280", "Россия", ["Украина", "Казахстан"]),
    ("https://images.unsplash.com/photo-1596484552834-6a58f850e0a1?w=1280", "Россия", ["Беларусь", "Польша"]),
    ("https://images.unsplash.com/photo-1555631782-f7236e6dc679?w=900&fit=crop", "Россия", ["Монголия", "Казахстан"]),
    ("https://images.unsplash.com/photo-1578922746465-3a80a228f223?w=900&fit=crop", "Казахстан", ["Россия", "Узбекистан"]),
    ("https://images.unsplash.com/photo-1565073624497-7144969fe4e1?w=900&fit=crop", "Грузия", ["Армения", "Азербайджан"]),
    ("https://images.unsplash.com/photo-1596203517259-87b8a4f0f171?w=900&fit=crop", "Узбекистан", ["Таджикистан", "Казахстан"]),
    ("https://images.unsplash.com/photo-1520637836862-4d197d17c27a?w=900&fit=crop", "Украина", ["Россия", "Беларусь"]),
    # ── ЕВРОПА (новые) ───────────────────────────────────────────────────────────
    # Венеция — каналы и гондолы
    ("https://images.unsplash.com/photo-1534113414509-0eec2bfb493f?w=900&fit=crop", "Италия", ["Хорватия", "Греция"]),
    # Флоренция — купол Брунеллески
    ("https://images.unsplash.com/photo-1543429257-3eb0b65d9f71?w=900&fit=crop", "Италия", ["Испания", "Португалия"]),
    # Монте-Карло — казино
    ("https://images.unsplash.com/photo-1554295405-abb8fd54f153?w=900&fit=crop", "Монако", ["Франция", "Италия"]),
    # Рейкьявик — северное сияние
    ("https://images.unsplash.com/photo-1531366936337-7c912a4589a7?w=900&fit=crop", "Исландия", ["Норвегия", "Финляндия"]),
    # Голубая лагуна Исландия
    ("https://images.unsplash.com/photo-1504893524553-b855bce32c67?w=900&fit=crop", "Исландия", ["Норвегия", "Швеция"]),
    # Афины — вид на Акрополь ночью
    ("https://images.unsplash.com/photo-1603565816030-6b389eeb23cb?w=900&fit=crop", "Греция", ["Кипр", "Турция"]),
    # Рим — Пантеон
    ("https://images.unsplash.com/photo-1515542622106-78bda8ba0e5b?w=900&fit=crop", "Италия", ["Греция", "Франция"]),
    # Барселона — парк Гуэль
    ("https://images.unsplash.com/photo-1562883676-8c7feb83f09b?w=900&fit=crop", "Испания", ["Португалия", "Франция"]),
    # Мадрид — Королевский дворец
    ("https://images.unsplash.com/photo-1539037116277-4db20889f2d4?w=900&fit=crop", "Испания", ["Португалия", "Италия"]),
    # Лондон — Тауэрский мост
    ("https://images.unsplash.com/photo-1513635269975-59663e0ac1ad?w=900&fit=crop", "Великобритания", ["Ирландия", "Германия"]),
    # Дублин — Temple Bar
    ("https://images.unsplash.com/photo-1564959130747-897fb406b9af?w=900&fit=crop", "Ирландия", ["Великобритания", "Франция"]),
    # Женева — фонтан Же-до
    ("https://images.unsplash.com/photo-1524168272322-bf73616d9cb5?w=900&fit=crop", "Швейцария", ["Франция", "Германия"]),
    # Инсбрук — Альпы Австрия
    ("https://images.unsplash.com/photo-1570637171947-3ebd3a76be4e?w=900&fit=crop", "Австрия", ["Швейцария", "Германия"]),
    # Брюссель — Гран-Плас
    ("https://images.unsplash.com/photo-1528360983277-13d401cdc186?w=900&fit=crop", "Бельгия", ["Нидерланды", "Люксембург"]),
    # Варшава — Старый город
    ("https://images.unsplash.com/photo-1508193638397-1c4234db14d8?w=900&fit=crop", "Польша", ["Чехия", "Венгрия"]),
    # Рига — Домский собор
    ("https://images.unsplash.com/photo-1551632436-cbf8dd35adfa?w=900&fit=crop", "Латвия", ["Эстония", "Литва"]),
    # Хельсинки — собор
    ("https://images.unsplash.com/photo-1520103559093-0ea6e3b57e6e?w=900&fit=crop", "Финляндия", ["Швеция", "Эстония"]),
    # Осло — Оперный театр
    ("https://images.unsplash.com/photo-1531366936337-7c912a4589a7?w=900&fit=crop", "Норвегия", ["Дания", "Швеция"]),
    # Белград — крепость Калемегдан
    ("https://images.unsplash.com/photo-1555992336-03a23c7b20ee?w=900&fit=crop", "Сербия", ["Хорватия", "Румыния"]),
    # Бухарест — Дворец Парламента
    ("https://images.unsplash.com/photo-1519677100203-a0e668c92439?w=900&fit=crop", "Румыния", ["Болгария", "Венгрия"]),
    # София — Александр Невский
    ("https://images.unsplash.com/photo-1558618666-fcd25c85cd64?w=900&fit=crop", "Болгария", ["Румыния", "Сербия"]),
    # Валлетта — Мальта
    ("https://images.unsplash.com/photo-1522582688319-c4f55c38a9a4?w=900&fit=crop", "Мальта", ["Кипр", "Италия"]),
    # Порту — Лелу
    ("https://images.unsplash.com/photo-1555881400-74d7acaacd8b?w=900&fit=crop", "Португалия", ["Испания", "Бразилия"]),
    # Монтенегро — Которский залив
    ("https://images.unsplash.com/photo-1555990793-da11153b2473?w=900&fit=crop", "Черногория", ["Хорватия", "Albania"]),
    # ── АЗИЯ (новые) ─────────────────────────────────────────────────────────────
    # Дубай — пальма Джумейра
    ("https://images.unsplash.com/photo-1518684079-3c830dcef090?w=900&fit=crop", "ОАЭ", ["Саудовская Аравия", "Катар"]),
    # Токио — Сибуя ночью
    ("https://images.unsplash.com/photo-1540959733332-eab4deabeeaf?w=900&fit=crop", "Япония", ["Южная Корея", "Китай"]),
    # Токио — Синдзюку
    ("https://images.unsplash.com/photo-1503899036084-c55cdd92da26?w=900&fit=crop", "Япония", ["Китай", "Тайвань"]),
    # Шанхай — Бунд
    ("https://images.unsplash.com/photo-1538428494232-9c0d8a3ab403?w=900&fit=crop", "Китай", ["Япония", "Южная Корея"]),
    # Гонконг — ночная панорама
    ("https://images.unsplash.com/photo-1536599018102-9f803c140fc1?w=900&fit=crop", "Китай", ["Япония", "Сингапур"]),
    # Бали — рисовые террасы
    ("https://images.unsplash.com/photo-1537996194471-e657df975ab4?w=900&fit=crop", "Индонезия", ["Филиппины", "Таиланд"]),
    # Бангкок — Гранд Палас
    ("https://images.unsplash.com/photo-1563492065599-3520f775eeed?w=900&fit=crop", "Таиланд", ["Камбоджа", "Лаос"]),
    # Пхукет — скалы в море
    ("https://images.unsplash.com/photo-1552465011-b4e21bf6e79a?w=900&fit=crop", "Таиланд", ["Малайзия", "Индонезия"]),
    # Дели — Ворота Индии
    ("https://images.unsplash.com/photo-1585135497273-1a86b09fe70e?w=900&fit=crop", "Индия", ["Пакистан", "Бангладеш"]),
    # Мумбаи — Ворота в Индию
    ("https://images.unsplash.com/photo-1529253355930-ddbe423a2ac7?w=900&fit=crop", "Индия", ["Шри-Ланка", "Пакистан"]),
    # Катманду — Сваямбунатх
    ("https://images.unsplash.com/photo-1544736779-b78e8d4e2f24?w=900&fit=crop", "Непал", ["Индия", "Тибет"]),
    # Коломбо — Лотосная башня
    ("https://images.unsplash.com/photo-1586771107445-d3ca888129ff?w=900&fit=crop", "Шри-Ланка", ["Индия", "Мальдивы"]),
    # Пекин — Тяньаньмэнь
    ("https://images.unsplash.com/photo-1508804185872-d7badad00f7d?w=900&fit=crop", "Китай", ["Тайвань", "Монголия"]),
    # Стамбул — Голубая мечеть
    ("https://images.unsplash.com/photo-1524231757912-21f4fe3a7200?w=900&fit=crop", "Турция", ["Иран", "Греция"]),
    # Абу-Даби — мечеть Шейха Зайда
    ("https://images.unsplash.com/photo-1512632578888-169bbbc64f33?w=900&fit=crop", "ОАЭ", ["Оман", "Катар"]),
    # ── АМЕРИКА (новые) ──────────────────────────────────────────────────────────
    # Нью-Йорк — Манхэттен ночью
    ("https://images.unsplash.com/photo-1496442226666-8d4d0e62e6e9?w=900&fit=crop", "США", ["Канада", "Великобритания"]),
    # Чикаго — Cloud Gate
    ("https://images.unsplash.com/photo-1477959858617-67f85cf4f1df?w=900&fit=crop", "США", ["Канада", "Австралия"]),
    # Лас-Вегас — Стрип
    ("https://images.unsplash.com/photo-1605833556294-ea5c2a8a5a3d?w=900&fit=crop", "США", ["Мексика", "Канада"]),
    # Гранд-Каньон
    ("https://images.unsplash.com/photo-1509316785289-025f5b846b35?w=900&fit=crop", "США", ["Мексика", "Канада"]),
    # Рио — карнавал / фавелы
    ("https://images.unsplash.com/photo-1544989164-31ac7f3ac3e0?w=900&fit=crop", "Бразилия", ["Колумбия", "Аргентина"]),
    # Буэнос-Айрес — Каса Росада
    ("https://images.unsplash.com/photo-1612294037637-ec400d0e7b71?w=900&fit=crop", "Аргентина", ["Чили", "Уругвай"]),
    # Картахена — Колумбия
    ("https://images.unsplash.com/photo-1583010736969-c81dfecfa6a0?w=900&fit=crop", "Колумбия", ["Куба", "Венесуэла"]),
    # Солончак Уюни — Bolivia
    ("https://images.unsplash.com/photo-1553877522-43269d4ea984?w=900&fit=crop", "Боливия", ["Чили", "Перу"]),
    # Канкун — пляж Мексика
    ("https://images.unsplash.com/photo-1518638150340-f706e86654de?w=900&fit=crop", "Мексика", ["Куба", "Доминикана"]),
    # Торонто — CN Tower
    ("https://images.unsplash.com/photo-1559511260-2cca5db34c52?w=900&fit=crop", "Канада", ["США", "Великобритания"]),
    # ── АФРИКА/ОКЕАНИЯ (новые) ────────────────────────────────────────────────────
    # Луксор — Карнакский храм
    ("https://images.unsplash.com/photo-1568322445389-f64ac2515020?w=900&fit=crop", "Египет", ["Судан", "Ливия"]),
    # Марракеш — сады Мажорель
    ("https://images.unsplash.com/photo-1597212618440-806262de4f2b?w=900&fit=crop", "Марокко", ["Тунис", "Алжир"]),
    # Йоханнесбург — ЮАР
    ("https://images.unsplash.com/photo-1577948000111-9c970dfe3743?w=900&fit=crop", "ЮАР", ["Зимбабве", "Намибия"]),
    # Занзибар — белый пляж
    ("https://images.unsplash.com/photo-1516026672322-bc52d61a55d5?w=900&fit=crop", "Танзания", ["Кения", "Мозамбик"]),
    # Мельбурн — Флиндерс Стрит
    ("https://images.unsplash.com/photo-1556740749-887f6717d7e4?w=900&fit=crop", "Австралия", ["Новая Зеландия", "ЮАР"]),
    # Большой Барьерный риф
    ("https://images.unsplash.com/photo-1546500840-ae38253aba9b?w=900&fit=crop", "Австралия", ["Индонезия", "Филиппины"]),
    # Фиджи — острова
    ("https://images.unsplash.com/photo-1507699622108-4be3abd695ad?w=900&fit=crop", "Новая Зеландия", ["Австралия", "Папуа Новая Гвинея"]),
    # ── РОССИЯ И СНГ (новые) ─────────────────────────────────────────────────────
    # Москва — ВДНХ
    ("https://images.unsplash.com/photo-1547448415-e9f5b28e570d?w=900&fit=crop", "Россия", ["Беларусь", "Украина"]),
    # Петербург — Петропавловка
    ("https://images.unsplash.com/photo-1561503406-12f7b77f8ade?w=900&fit=crop", "Россия", ["Финляндия", "Эстония"]),
    # Сочи — горы зимой
    ("https://images.unsplash.com/photo-1451784462485-e6f36ad54adf?w=900&fit=crop", "Россия", ["Грузия", "Армения"]),
    # Ереван — Каскад
    ("https://images.unsplash.com/photo-1565073624497-7144969fe4e1?w=900&fit=crop", "Армения", ["Грузия", "Азербайджан"]),
    # Алматы — горы
    ("https://images.unsplash.com/photo-1578922746465-3a80a228f223?w=900&fit=crop", "Казахстан", ["Кыргызстан", "Узбекистан"]),
    # Баку — Девичья башня
    ("https://images.unsplash.com/photo-1596203517259-87b8a4f0f171?w=900&fit=crop", "Азербайджан", ["Грузия", "Армения"]),
    # Минск — Дворец республики
    ("https://images.unsplash.com/photo-1520637836862-4d197d17c27a?w=900&fit=crop", "Беларусь", ["Россия", "Украина"]),
    # ── ЕВРОПА (ещё) ─────────────────────────────────────────────────────────────
    ("https://images.unsplash.com/photo-1596797882870-8c1c9d0d5cc7?w=900&fit=crop", "Нидерланды", ["Германия", "Бельгия"]),
    ("https://images.unsplash.com/photo-1448906654166-444214831098?w=900&fit=crop", "Нидерланды", ["Германия", "Дания"]),
    ("https://images.unsplash.com/photo-1558618666-fcd25c85cd64?w=900&fit=crop", "Болгария", ["Сербия", "Румыния"]),
    ("https://images.unsplash.com/photo-1590073844006-33379778ae09?w=900&fit=crop", "Хорватия", ["Словения", "Черногория"]),
    ("https://images.unsplash.com/photo-1563492065599-3520f775eeed?w=900&fit=crop", "Тайланд", ["Малайзия", "Мьянма"]),
    ("https://images.unsplash.com/photo-1499856871958-5b9627545d1a?w=900&fit=crop", "Франция", ["Бельгия", "Люксембург"]),
    ("https://images.unsplash.com/photo-1502602898657-3e91760cbb34?w=900&fit=crop", "Франция", ["Великобритания", "Германия"]),
    ("https://images.unsplash.com/photo-1543429257-3eb0b65d9f71?w=900&fit=crop", "Италия", ["Австрия", "Словения"]),
    ("https://images.unsplash.com/photo-1467269204594-9661b134dd2b?w=900&fit=crop", "Германия", ["Польша", "Австрия"]),
    ("https://images.unsplash.com/photo-1529040181623-e04ebc611e25?w=900&fit=crop", "Германия", ["Австрия", "Швейцария"]),
    ("https://images.unsplash.com/photo-1592906209472-a36b1f3782ef?w=900&fit=crop", "Чехия", ["Польша", "Австрия"]),
    ("https://images.unsplash.com/photo-1573946191989-f7f98d069a0e?w=900&fit=crop", "Чехия", ["Германия", "Словакия"]),
    ("https://images.unsplash.com/photo-1558005137-d9619a5c539f?w=900&fit=crop", "Финляндия", ["Норвегия", "Швеция"]),
    ("https://images.unsplash.com/photo-1517990947885-de15628d11ff?w=900&fit=crop", "Швеция", ["Финляндия", "Дания"]),
    ("https://images.unsplash.com/photo-1515488042361-ee00e0ddd4e4?w=900&fit=crop", "Венгрия", ["Словакия", "Австрия"]),
    ("https://images.unsplash.com/photo-1605130284535-11dd9eedc58a?w=900&fit=crop", "Польша", ["Германия", "Чехия"]),
    ("https://images.unsplash.com/photo-1580060839134-75a5edca2e99?w=900&fit=crop", "Шотландия", ["Ирландия", "Исландия"]),
    ("https://images.unsplash.com/photo-1566073771259-6a8506099945?w=900&fit=crop", "Греция", ["Кипр", "Мальта"]),
    ("https://images.unsplash.com/photo-1533105079780-92b9be482077?w=900&fit=crop", "Греция", ["Италия", "Испания"]),
    ("https://images.unsplash.com/photo-1555990793-da11153b2473?w=900&fit=crop", "Черногория", ["Хорватия", "Косово"]),
    ("https://images.unsplash.com/photo-1587974928442-77dc3e0dba72?w=900&fit=crop", "Словения", ["Хорватия", "Австрия"]),
    ("https://images.unsplash.com/photo-1612947404944-4a4c3d21bfcb?w=900&fit=crop", "Эстония", ["Финляндия", "Латвия"]),
    ("https://images.unsplash.com/photo-1562976540-1502c2145186?w=900&fit=crop", "Литва", ["Польша", "Латвия"]),
    ("https://images.unsplash.com/photo-1551632436-cbf8dd35adfa?w=900&fit=crop", "Латвия", ["Литва", "Эстония"]),
    ("https://images.unsplash.com/photo-1574871786327-96615b3a588f?w=900&fit=crop", "Португалия", ["Испания", "Бразилия"]),
    ("https://images.unsplash.com/photo-1596422846543-75c6fc197f07?w=900&fit=crop", "Узбекистан", ["Таджикистан", "Туркменистан"]),
    ("https://images.unsplash.com/photo-1508739773434-c26b3d09e071?w=900&fit=crop", "Испания", ["Португалия", "Марокко"]),
    ("https://images.unsplash.com/photo-1512470876302-972faa2aa9a4?w=900&fit=crop", "Нидерланды", ["Бельгия", "Дания"]),
    ("https://images.unsplash.com/photo-1524492412937-b28074a5d7da?w=900&fit=crop", "Индия", ["Непал", "Пакистан"]),
    # ── АЗИЯ (ещё) ───────────────────────────────────────────────────────────────
    ("https://images.unsplash.com/photo-1587560699334-cc4ff634909a?w=900&fit=crop", "ОАЭ", ["Катар", "Кувейт"]),
    ("https://images.unsplash.com/photo-1602216056096-3b40cc0c9944?w=900&fit=crop", "Индия", ["Пакистан", "Шри-Ланка"]),
    ("https://images.unsplash.com/photo-1554015254-a90bed5dce64?w=900&fit=crop", "Южная Корея", ["Япония", "Китай"]),
    ("https://images.unsplash.com/photo-1617957772002-57adde1156fa?w=900&fit=crop", "Южная Корея", ["Северная Корея", "Япония"]),
    ("https://images.unsplash.com/photo-1555400038-63f5ba517a47?w=900&fit=crop", "Индонезия", ["Малайзия", "Бруней"]),
    ("https://images.unsplash.com/photo-1527492990737-f9a11e84e3c9?w=900&fit=crop", "Япония", ["Китай", "Тайвань"]),
    ("https://images.unsplash.com/photo-1490806843957-31f4c9a91c65?w=900&fit=crop", "Япония", ["Китай", "Корея"]),
    ("https://images.unsplash.com/photo-1528360983277-13d401cdc186?w=900&fit=crop", "Вьетнам", ["Камбоджа", "Лаос"]),
    ("https://images.unsplash.com/photo-1537996194471-e657df975ab4?w=900&fit=crop", "Индонезия", ["Таиланд", "Малайзия"]),
    ("https://images.unsplash.com/photo-1596422846543-75c6fc197f07?w=900&fit=crop", "Узбекистан", ["Таджикистан", "Казахстан"]),
    ("https://images.unsplash.com/photo-1567608285969-48e4bbe0d399?w=900&fit=crop", "Иран", ["Ирак", "Афганистан"]),
    ("https://images.unsplash.com/photo-1548690312-e9fe0ced4e3d?w=900&fit=crop", "Иордания", ["Израиль", "Саудовская Аравия"]),
    ("https://images.unsplash.com/photo-1544735716-392fe2489ffa?w=900&fit=crop", "Израиль", ["Ливан", "Сирия"]),
    ("https://images.unsplash.com/photo-1525625293386-3f8f99389edd?w=900&fit=crop", "Сингапур", ["Малайзия", "Бруней"]),
    ("https://images.unsplash.com/photo-1565073624497-7144969fe4e1?w=900&fit=crop", "Грузия", ["Армения", "Россия"]),
    ("https://images.unsplash.com/photo-1562602833-0f4ab2fc46e3?w=900&fit=crop", "Камбоджа", ["Вьетнам", "Лаос"]),
    ("https://images.unsplash.com/photo-1528360983277-13d401cdc186?w=900&fit=crop", "Мьянма", ["Таиланд", "Лаос"]),
    ("https://images.unsplash.com/photo-1586077403686-ba8e5c6c97ce?w=900&fit=crop", "Монголия", ["Китай", "Казахстан"]),
    ("https://images.unsplash.com/photo-1559386484-97dfc0e15539?w=900&fit=crop", "Индия", ["Бангладеш", "Мьянма"]),
    ("https://images.unsplash.com/photo-1596204976717-1a9ff47f74ef?w=900&fit=crop", "Пакистан", ["Афганистан", "Иран"]),
    ("https://images.unsplash.com/photo-1558618047-f4e60cef138f?w=900&fit=crop", "Катар", ["ОАЭ", "Бахрейн"]),
    ("https://images.unsplash.com/photo-1580060839134-75a5edca2e99?w=900&fit=crop", "Непал", ["Индия", "Бутан"]),
    ("https://images.unsplash.com/photo-1604999333679-b86d54738315?w=900&fit=crop", "Малайзия", ["Таиланд", "Индонезия"]),
    ("https://images.unsplash.com/photo-1552465011-b4e21bf6e79a?w=900&fit=crop", "Таиланд", ["Мьянма", "Лаос"]),
    ("https://images.unsplash.com/photo-1546412414-e1885259563a?w=900&fit=crop", "Филиппины", ["Индонезия", "Малайзия"]),
    ("https://images.unsplash.com/photo-1551003966-f7da7b96dd99?w=900&fit=crop", "Тайвань", ["Китай", "Япония"]),
    # ── РОССИЯ (ещё) ─────────────────────────────────────────────────────────────
    ("https://images.unsplash.com/photo-1547448415-e9f5b28e570d?w=900&fit=crop", "Россия", ["Монголия", "Китай"]),
    ("https://images.unsplash.com/photo-1563039404-ead9b044bfcf?w=900&fit=crop", "Россия", ["Финляндия", "Эстония"]),
    ("https://images.unsplash.com/photo-1516026672322-bc52d61a55d5?w=900&fit=crop", "Россия", ["Украина", "Беларусь"]),
    ("https://images.unsplash.com/photo-1596484552834-6a58f850e0a1?w=900&fit=crop", "Россия", ["Польша", "Германия"]),
    # ── АМЕРИКА (ещё) ─────────────────────────────────────────────────────────────
    ("https://images.unsplash.com/photo-1534430480872-3498386e7856?w=900&fit=crop", "США", ["Канада", "Австралия"]),
    ("https://images.unsplash.com/photo-1534430480872-3498386e7856?w=900&fit=crop", "Канада", ["США", "Норвегия"]),
    ("https://images.unsplash.com/photo-1489447068241-b3490214e879?w=900&fit=crop", "Канада", ["Норвегия", "Исландия"]),
    ("https://images.unsplash.com/photo-1483729558449-99ef09a8c325?w=900&fit=crop", "Бразилия", ["Аргентина", "Уругвай"]),
    ("https://images.unsplash.com/photo-1543685067-8282b99d61c4?w=900&fit=crop", "Аргентина", ["Чили", "Уругвай"]),
    ("https://images.unsplash.com/photo-1526392060635-9d6019884377?w=900&fit=crop", "Перу", ["Боливия", "Эквадор"]),
    ("https://images.unsplash.com/photo-1518638150340-f706e86654de?w=900&fit=crop", "Мексика", ["Гватемала", "Белиз"]),
    ("https://images.unsplash.com/photo-1500759285222-a95626b934cb?w=900&fit=crop", "Куба", ["Ямайка", "Доминикана"]),
    ("https://images.unsplash.com/photo-1502622736629-9d9154c01f47?w=900&fit=crop", "Чили", ["Аргентина", "Перу"]),
    ("https://images.unsplash.com/photo-1621243804936-775306a8f2e3?w=900&fit=crop", "Колумбия", ["Венесуэла", "Эквадор"]),
    ("https://images.unsplash.com/photo-1578894381163-e72c17f2d45f?w=900&fit=crop", "Чили", ["Боливия", "Аргентина"]),
    ("https://images.unsplash.com/photo-1553877522-43269d4ea984?w=900&fit=crop", "Боливия", ["Аргентина", "Перу"]),
    # ── АФРИКА (ещё) ─────────────────────────────────────────────────────────────
    ("https://images.unsplash.com/photo-1547471080-7cc2caa01a7e?w=900&fit=crop", "Кения", ["Эфиопия", "Уганда"]),
    ("https://images.unsplash.com/photo-1516026672322-bc52d61a55d5?w=900&fit=crop", "Кения", ["Танзания", "Сомали"]),
    ("https://images.unsplash.com/photo-1504432842672-1a79f78e4084?w=900&fit=crop", "Зимбабве", ["Замбия", "Мозамбик"]),
    ("https://images.unsplash.com/photo-1553913861-c0fddf2619ee?w=900&fit=crop", "Марокко", ["Алжир", "Тунис"]),
    ("https://images.unsplash.com/photo-1539768942893-daf53e448371?w=900&fit=crop", "Египет", ["Иордания", "Судан"]),
    ("https://images.unsplash.com/photo-1523482580672-f109ba8cb9be?w=900&fit=crop", "Австралия", ["Новая Зеландия", "Тасмания"]),
    ("https://images.unsplash.com/photo-1529108190281-9a4f620bc2d8?w=900&fit=crop", "Австралия", ["Новая Зеландия", "ЮАР"]),
    ("https://images.unsplash.com/photo-1507699622108-4be3abd695ad?w=900&fit=crop", "Новая Зеландия", ["Австралия", "Фиджи"]),
    ("https://images.unsplash.com/photo-1597575732470-a3a79197ef86?w=900&fit=crop", "Гана", ["Нигерия", "Кот-д'Ивуар"]),
    ("https://images.unsplash.com/photo-1534430480872-3498386e7856?w=900&fit=crop", "Эфиопия", ["Кения", "Сомали"]),
    ("https://images.unsplash.com/photo-1580060839134-75a5edca2e99?w=900&fit=crop", "ЮАР", ["Намибия", "Ботсвана"]),
    ("https://images.unsplash.com/photo-1544947950-fa07a98d237f?w=900&fit=crop", "Тунис", ["Алжир", "Ливия"]),
    ("https://images.unsplash.com/photo-1577948000111-9c970dfe3743?w=900&fit=crop", "ЮАР", ["Мозамбик", "Зимбабве"]),
]

# Активные игровые сессии: chat_id → {correct, msg_id, user_id (опционально)}
_geo_sessions: dict[int, dict] = {}

# История последних N вопросов по чату чтобы не повторяться
_geo_history: dict[int, list] = {}  # chat_id → [url, url, ...]
GEO_HISTORY_SIZE = 10  # не повторять последние 10 вопросов

# file_id кеш: сначала грузим из БД, потом обновляем в памяти и в БД
_geo_file_cache: dict[str, str] = {}


def _geo_cache_load() -> None:
    """Загружает сохранённые file_id из БД в память при старте."""
    try:
        conn = db_connect()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS geo_photo_cache "
            "(url TEXT PRIMARY KEY, file_id TEXT NOT NULL)"
        )
        conn.commit()
        rows = conn.execute("SELECT url, file_id FROM geo_photo_cache").fetchall()
        for r in rows:
            _geo_file_cache[r["url"]] = r["file_id"]
        conn.close()
        logger.info(f"geo: загружено {len(_geo_file_cache)} file_id из кеша")
    except Exception as e:
        logger.warning(f"geo_cache_load: {e}")


def _geo_cache_save(url: str, file_id: str) -> None:
    """Сохраняет file_id в память и в БД."""
    _geo_file_cache[url] = file_id
    try:
        conn = db_connect()
        conn.execute(
            "INSERT OR REPLACE INTO geo_photo_cache (url, file_id) VALUES (?,?)",
            (url, file_id),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"geo_cache_save: {e}")

POINTS_PER_CORRECT = 10


def _geo_init_db(conn: sqlite3.Connection) -> None:
    """Создаём таблицы для игры если их ещё нет."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS geo_scores (
        user_id   INTEGER,
        chat_id   INTEGER,
        score     INTEGER DEFAULT 0,
        correct   INTEGER DEFAULT 0,
        wrong     INTEGER DEFAULT 0,
        PRIMARY KEY (user_id, chat_id)
    );
    CREATE TABLE IF NOT EXISTS geo_scores_global (
        user_id   INTEGER PRIMARY KEY,
        score     INTEGER DEFAULT 0,
        correct   INTEGER DEFAULT 0,
        wrong     INTEGER DEFAULT 0
    );
    """)
    conn.commit()


def _geo_add_score(user_id: int, chat_id: int, correct: bool) -> int:
    """Начисляет/снимает очки, возвращает новый счёт в этом чате."""
    conn = db_connect()
    _geo_init_db(conn)
    delta    = POINTS_PER_CORRECT if correct else -POINTS_PER_CORRECT
    c_delta  = 1 if correct else 0
    w_delta  = 0 if correct else 1

    # Получаем текущий счёт чтобы не уйти ниже 0
    row = conn.execute(
        "SELECT score FROM geo_scores WHERE user_id=? AND chat_id=?", (user_id, chat_id)
    ).fetchone()
    current = row["score"] if row else 0
    if current + delta < 0:
        delta = -current  # обнуляем, не уходим в минус

    conn.execute(
        """INSERT INTO geo_scores (user_id, chat_id, score, correct, wrong)
           VALUES (?,?,?,?,?)
           ON CONFLICT(user_id, chat_id) DO UPDATE SET
             score   = MAX(0, score   + excluded.score),
             correct = correct + excluded.correct,
             wrong   = wrong   + excluded.wrong""",
        (user_id, chat_id, delta, c_delta, w_delta),
    )

    row_g = conn.execute(
        "SELECT score FROM geo_scores_global WHERE user_id=?", (user_id,)
    ).fetchone()
    current_g = row_g["score"] if row_g else 0
    delta_g = delta if (current_g + delta >= 0) else -current_g

    conn.execute(
        """INSERT INTO geo_scores_global (user_id, score, correct, wrong)
           VALUES (?,?,?,?)
           ON CONFLICT(user_id) DO UPDATE SET
             score   = MAX(0, score   + excluded.score),
             correct = correct + excluded.correct,
             wrong   = wrong   + excluded.wrong""",
        (user_id, delta_g, c_delta, w_delta),
    )
    conn.commit()
    row = conn.execute(
        "SELECT score FROM geo_scores WHERE user_id=? AND chat_id=?", (user_id, chat_id)
    ).fetchone()
    conn.close()
    return row["score"] if row else 0


def _geo_get_user_stats(user_id: int, chat_id: int) -> dict:
    conn = db_connect()
    _geo_init_db(conn)
    local = conn.execute(
        "SELECT score, correct, wrong FROM geo_scores WHERE user_id=? AND chat_id=?",
        (user_id, chat_id),
    ).fetchone()
    glob = conn.execute(
        "SELECT score, correct, wrong FROM geo_scores_global WHERE user_id=?",
        (user_id,),
    ).fetchone()
    conn.close()
    return {
        "local_score":   local["score"]   if local else 0,
        "local_correct": local["correct"] if local else 0,
        "local_wrong":   local["wrong"]   if local else 0,
        "global_score":   glob["score"]   if glob else 0,
        "global_correct": glob["correct"] if glob else 0,
        "global_wrong":   glob["wrong"]   if glob else 0,
    }


def _geo_leaderboard_chat(chat_id: int, limit: int = 10) -> list[sqlite3.Row]:
    conn = db_connect()
    _geo_init_db(conn)
    rows = conn.execute(
        """SELECT g.user_id, g.score, g.correct, g.wrong,
                  k.first_name, k.last_name, k.username
           FROM geo_scores g
           LEFT JOIN known_users k ON k.user_id = g.user_id
           WHERE g.chat_id=?
           ORDER BY g.score DESC
           LIMIT ?""",
        (chat_id, limit),
    ).fetchall()
    conn.close()
    return rows


def _geo_leaderboard_global(limit: int = 10) -> list[sqlite3.Row]:
    conn = db_connect()
    _geo_init_db(conn)
    rows = conn.execute(
        """SELECT g.user_id, g.score, g.correct, g.wrong,
                  k.first_name, k.last_name, k.username
           FROM geo_scores_global g
           LEFT JOIN known_users k ON k.user_id = g.user_id
           ORDER BY g.score DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return rows


def _user_display_name(row: sqlite3.Row) -> str:
    """Имя без ссылки на аккаунт."""
    first = row["first_name"] or ""
    last  = row["last_name"]  or ""
    name  = (first + " " + last).strip()
    return name if name else (row["username"] or f"id{row['user_id']}")


async def cmd_games(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Главное меню игр (/games)."""
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌍 ГеоГадайка", callback_data="game_geo_start")],
    ])
    await update.message.reply_text(
        "🎮 <b>Игры</b>\n\nВыберите игру:",
        parse_mode="HTML",
        reply_markup=kb,
    )


async def _download_photo(url: str) -> Optional[bytes]:
    """Скачивает фото. Пробует aiohttp, затем requests (синхронно)."""
    headers = {
        "User-Agent": "GeoQuizBot/1.0 (telegram_geoquiz_bot; https://t.me/)",
        "Accept": "image/webp,image/jpeg,image/*",
    }

    # Способ 1: aiohttp (асинхронный)
    if _AIOHTTP_OK:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200 and "image" in resp.headers.get("Content-Type", ""):
                        return await resp.read()
        except Exception as e:
            logger.warning(f"aiohttp download failed: {e}")

    # Способ 2: requests (синхронный, запускаем в executor)
    if _REQUESTS_OK:
        try:
            loop = asyncio.get_event_loop()
            def _sync_get():
                r = _requests.get(url, headers=headers, timeout=10)
                if r.status_code == 200 and "image" in r.headers.get("Content-Type", ""):
                    return r.content
                return None
            data = await loop.run_in_executor(None, _sync_get)
            if data:
                return data
        except Exception as e:
            logger.warning(f"requests download failed: {e}")

    return None


async def _send_geo_question(chat_id: int, context: ContextTypes.DEFAULT_TYPE, starter_id: int = 0) -> None:
    """Отправляет новый вопрос геоигры в чат."""
    import random

    history = _geo_history.get(chat_id, [])

    # Загружаем кастомные фото из БД и добавляем к стандартным
    try:
        conn = db_connect()
        custom_rows = conn.execute(
            "SELECT file_id, answer, wrong1, wrong2 FROM geo_custom_photos"
        ).fetchall()
        conn.close()
        custom_questions = [
            (f"__custom__{r['file_id']}", r["answer"],
             [x for x in [r["wrong1"], r["wrong2"]] if x])
            for r in custom_rows
        ]
    except Exception:
        custom_questions = []

    all_questions = list(GEO_QUESTIONS) + custom_questions

    # Исключаем недавно показанные вопросы
    available = [q for q in all_questions if q[0] not in history]
    # Если все вопросы уже были — сбрасываем историю
    if not available:
        _geo_history[chat_id] = []
        available = list(all_questions)

    # Кастомные всегда грузятся по file_id мгновенно; стандартные — через кеш или URL
    custom_avail = [q for q in available if q[0].startswith("__custom__")]
    cached_std   = [q for q in available if not q[0].startswith("__custom__") and _geo_file_cache.get(q[0])]
    uncached_std = [q for q in available if not q[0].startswith("__custom__") and not _geo_file_cache.get(q[0])]
    random.shuffle(custom_avail)
    random.shuffle(cached_std)
    random.shuffle(uncached_std)
    order = custom_avail + cached_std + uncached_std

    sent = False
    kb = None
    for q in order:
        photo_url, correct, wrong_variants = q

        # Кастомное фото — отправляем сразу по file_id
        if photo_url.startswith("__custom__"):
            file_id = photo_url[len("__custom__"):]
            all_opts = wrong_variants[:2] + [correct]
            # Дополняем до 3 вариантов если вдруг их меньше
            while len(all_opts) < 3:
                fallback = random.choice(GEO_QUESTIONS)[1]
                if fallback not in all_opts:
                    all_opts.append(fallback)
            random.shuffle(all_opts)
            correct_idx = all_opts.index(correct)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(o, callback_data=f"ga:{correct_idx}:{i}")]
                for i, o in enumerate(all_opts)
            ])
            _geo_sessions[chat_id] = {
                "correct": correct,
                "options": all_opts,
                "correct_idx": correct_idx,
                "starter_id": starter_id,
            }
            try:
                msg = await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=file_id,
                    caption="🌍 <b>В какой стране сделана эта фотография?</b>",
                    parse_mode="HTML",
                    reply_markup=kb,
                )
                _geo_sessions[chat_id]["msg_id"] = msg.message_id
                hist = _geo_history.setdefault(chat_id, [])
                hist.append(photo_url)
                if len(hist) > GEO_HISTORY_SIZE:
                    hist.pop(0)
                sent = True
                break
            except Exception as e:
                logger.warning(f"geo custom file_id failed: {e}")
                continue
        options = wrong_variants[:2] + [correct]
        random.shuffle(options)

        # Используем индексы вместо названий стран — избегаем Button_data_invalid
        # (лимит callback_data = 64 байта, длинные названия его превышают)
        correct_idx = options.index(correct)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(o, callback_data=f"ga:{correct_idx}:{i}")]
            for i, o in enumerate(options)
        ])

        # Сохраняем сессию с вариантами чтобы знать правильный ответ по индексу
        _geo_sessions[chat_id] = {
            "correct": correct,
            "options": options,
            "correct_idx": correct_idx,
            "starter_id": starter_id,
        }

        # 1) Закешированный file_id — самый надёжный способ
        cached_fid = _geo_file_cache.get(photo_url)
        if cached_fid:
            try:
                msg = await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=cached_fid,
                    caption="🌍 <b>В какой стране сделана эта фотография?</b>",
                    parse_mode="HTML",
                    reply_markup=kb,
                )
                _geo_sessions[chat_id]["msg_id"] = msg.message_id
                hist = _geo_history.setdefault(chat_id, [])
                hist.append(photo_url)
                if len(hist) > GEO_HISTORY_SIZE:
                    hist.pop(0)
                sent = True
                break
            except Exception as e:
                logger.warning(f"geo cached file_id failed: {e}")
                # Инвалидируем испорченный кеш
                del _geo_file_cache[photo_url]
                try:
                    conn = db_connect()
                    conn.execute("DELETE FROM geo_photo_cache WHERE url=?", (photo_url,))
                    conn.commit()
                    conn.close()
                except Exception:
                    pass

        # 2) Скачиваем байты сами и отправляем как InputFile
        photo_bytes = await _download_photo(photo_url)
        if photo_bytes:
            try:
                buf = io.BytesIO(photo_bytes)
                buf.name = "geo.jpg"
                msg = await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=InputFile(buf, filename="geo.jpg"),
                    caption="🌍 <b>В какой стране сделана эта фотография?</b>",
                    parse_mode="HTML",
                    reply_markup=kb,
                )
                if msg.photo:
                    _geo_cache_save(photo_url, msg.photo[-1].file_id)
                _geo_sessions[chat_id]["msg_id"] = msg.message_id
                hist = _geo_history.setdefault(chat_id, [])
                hist.append(photo_url)
                if len(hist) > GEO_HISTORY_SIZE:
                    hist.pop(0)
                sent = True
                break
            except Exception as e:
                logger.warning(f"geo send bytes failed: {e}")
                continue

        # 3) Прямой URL — последний шанс для этого вопроса
        try:
            msg = await context.bot.send_photo(
                chat_id=chat_id,
                photo=photo_url,
                caption="🌍 <b>В какой стране сделана эта фотография?</b>",
                parse_mode="HTML",
                reply_markup=kb,
            )
            if msg.photo:
                _geo_cache_save(photo_url, msg.photo[-1].file_id)
            _geo_sessions[chat_id]["msg_id"] = msg.message_id
            hist = _geo_history.setdefault(chat_id, [])
            hist.append(photo_url)
            if len(hist) > GEO_HISTORY_SIZE:
                hist.pop(0)
            sent = True
            break
        except Exception as e:
            logger.warning(f"geo direct URL failed ({photo_url}): {e}")
            continue  # следующий вопрос

    if not sent:
        logger.error("geo: все попытки отправить фото провалились")
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text="🌍 <b>В какой стране сделана эта фотография?</b>\n\n⚠️ Фото недоступно, попробуй ещё раз.",
            parse_mode="HTML",
            reply_markup=kb,
        )
        _geo_sessions[chat_id]["msg_id"] = msg.message_id


async def cb_geo_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback: начать геоигру."""
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    starter_id = update.effective_user.id
    await query.edit_message_text(
        "🌍 <b>ГеоГадайка</b> — угадай страну по фото!\n\n"
        "✅ Правильно: +10 очков\n"
        "❌ Неправильно: −10 очков",
        parse_mode="HTML",
    )
    await _send_geo_question(chat_id, context, starter_id=starter_id)


async def cb_geo_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback: ответ на вопрос геоигры."""
    query = update.callback_query
    chat_id = update.effective_chat.id
    user = update.effective_user

    session = _geo_sessions.get(chat_id)
    # Any participant can answer — no starter restriction
    await query.answer()

    # Формат: ga:correct_idx:chosen_idx
    parts = query.data.split(":")
    if len(parts) != 3:
        return
    try:
        correct_idx = int(parts[1])
        chosen_idx  = int(parts[2])
    except ValueError:
        return

    if not session:
        await query.edit_message_caption(caption="⚠️ Сессия устарела, запусти игру заново.", parse_mode="HTML")
        return

    correct  = session.get("correct", "?")
    options  = session.get("options", [])
    chosen   = options[chosen_idx] if chosen_idx < len(options) else "?"
    is_correct = (chosen_idx == correct_idx)
    starter_id = session.get("starter_id", user.id)

    new_score = _geo_add_score(user.id, chat_id, is_correct)
    name = user.first_name or user.username or "Игрок"

    next_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("➡️ Следующий вопрос", callback_data="game_geo_next")
    ]])

    if is_correct:
        result_text = (
            f"✅ <b>{name}</b> угадал! Это <b>{correct}</b>.\n"
            f"+{POINTS_PER_CORRECT} очков (итого: {new_score})"
        )
    else:
        result_text = (
            f"❌ <b>{name}</b> ошибся. Правильный ответ: <b>{correct}</b>.\n"
            f"−{POINTS_PER_CORRECT} очков (итого: {new_score})"
        )

    try:
        await query.edit_message_caption(caption=result_text, parse_mode="HTML", reply_markup=next_kb)
    except Exception:
        try:
            await query.edit_message_text(result_text, parse_mode="HTML", reply_markup=next_kb)
        except Exception:
            pass

    _geo_sessions[chat_id] = {"correct": correct, "starter_id": starter_id, "answered": True}


async def cb_geo_next(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback: кнопка 'Следующий вопрос'."""
    query = update.callback_query
    chat_id = update.effective_chat.id
    user = update.effective_user

    # Any participant can move to the next question
    session = _geo_sessions.get(chat_id)
    await query.answer()
    starter_id = session["starter_id"] if session else user.id

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _send_geo_question(chat_id, context, starter_id=starter_id)


async def cmd_geo_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/geotop — таблица лидеров чата и глобальная."""
    chat_id = update.effective_chat.id

    local_rows = _geo_leaderboard_chat(chat_id)
    global_rows = _geo_leaderboard_global()

    lines = ["🏆 <b>Топ-10 этого чата:</b>"]
    if local_rows:
        for i, r in enumerate(local_rows, 1):
            lines.append(f"{i}. {_user_display_name(r)} — {r['score']} очков ({r['correct']}✅ {r['wrong']}❌)")
    else:
        lines.append("Пока никто не играл.")

    lines.append("\n🌐 <b>Глобальный топ-10:</b>")
    if global_rows:
        for i, r in enumerate(global_rows, 1):
            lines.append(f"{i}. {_user_display_name(r)} — {r['score']} очков ({r['correct']}✅ {r['wrong']}❌)")
    else:
        lines.append("Пока никто не играл.")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_geo_me(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Я / кто — статистика игрока в геоигре."""
    # Определяем цель: реплай → тот пользователь, иначе → сам
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
    else:
        target = update.effective_user

    chat_id = update.effective_chat.id
    stats = _geo_get_user_stats(target.id, chat_id)

    nick = _get_nick(update.effective_chat.id, target.id)
    nick_str = f" | 🏷 <b>{nick}</b>" if nick else " | 🏷 никнейм не установлен"
    name = target.first_name or target.username or "Игрок"
    total_local  = stats["local_correct"]  + stats["local_wrong"]
    total_global = stats["global_correct"] + stats["global_wrong"]
    acc_local  = round(stats["local_correct"]  / total_local  * 100) if total_local  else 0
    acc_global = round(stats["global_correct"] / total_global * 100) if total_global else 0

    econ = _econ_get(target.id)
    inv  = _inv_get(target.id)
    inv_str = " ".join(
        f"{ITEM_EMOJI.get(it, '📦')}{qty}" for it, qty in inv.items()
    ) if inv else "пусто"

    text = (
        f"🎮 <b>Статистика: {name}</b>{nick_str}\n\n"
        f"<b>Геоигра — в этом чате:</b>\n"
        f"  Очков: {stats['local_score']} | Правильно: {stats['local_correct']} | Неверно: {stats['local_wrong']}\n"
        f"  Точность: {acc_local}%\n\n"
        f"<b>Геоигра — глобально:</b>\n"
        f"  Очков: {stats['global_score']} | Правильно: {stats['global_correct']} | Неверно: {stats['global_wrong']}\n"
        f"  Точность: {acc_global}%\n\n"
        f"<b>Экономика:</b>\n"
        f"  🪨 Самородки: {econ['nuggets']} | 🪙 Монеты: {econ['coins']}\n"
        f"  🎒 Инвентарь: {inv_str}"
    )

    # Фото профиля если загружено через панель
    conn = db_connect()
    photo_row = conn.execute(
        "SELECT file_id FROM profile_photos WHERE user_id=0 LIMIT 1"
    ).fetchone()
    conn.close()

    if photo_row:
        await update.message.reply_photo(
            photo=photo_row["file_id"],
            caption=text,
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(text, parse_mode="HTML")



# ─────────────────────────── НИКИ ────────────────────────────────────────────

async def cmd_set_nick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """+ник @user НИК — установить ник пользователю."""
    if not await require_rank(update, 1):
        return
    args = _parse_args(update, context)
    # Формат: +ник @user Ник Ник  или реплай +ник Ник Ник
    target = None
    nick_parts = []
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
        nick_parts = args
    elif args:
        target = await resolve_target(update, context)
        # Ник — всё после первого аргумента (username)
        nick_parts = args[1:] if len(args) > 1 else []

    if not target:
        await update.message.reply_text("Укажи пользователя: реплай или @username")
        return
    if not nick_parts:
        await update.message.reply_text("Укажи ник. Пример: <code>+ник @user Крутой Чел</code>", parse_mode="HTML")
        return

    nick = " ".join(nick_parts)
    if len(nick) > 32:
        await update.message.reply_text("Ник слишком длинный (макс 32 символа).")
        return

    chat_id = update.effective_chat.id
    conn = db_connect()
    conn.execute(
        "INSERT OR REPLACE INTO nicknames (chat_id, user_id, nick, set_by) VALUES (?,?,?,?)",
        (chat_id, target.id, nick, update.effective_user.id)
    )
    conn.commit(); conn.close()
    await update.message.reply_text(
        f"✅ Ник <b>{nick}</b> установлен для {target.mention_html()}.",
        parse_mode="HTML",
    )


async def cmd_del_nick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """-ник @user — убрать ник."""
    if not await require_rank(update, 1):
        return
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
    else:
        target = await resolve_target(update, context)
    if not target:
        await update.message.reply_text("Укажи пользователя.")
        return
    chat_id = update.effective_chat.id
    conn = db_connect()
    conn.execute("DELETE FROM nicknames WHERE chat_id=? AND user_id=?", (chat_id, target.id))
    conn.commit(); conn.close()
    await update.message.reply_text(f"✅ Ник {target.mention_html()} удалён.", parse_mode="HTML")


async def cmd_nicklist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """никлист / nlist — список ников в чате."""
    chat_id = update.effective_chat.id
    conn = db_connect()
    rows = conn.execute(
        """SELECT n.user_id, n.nick, k.first_name, k.username
           FROM nicknames n
           LEFT JOIN known_users k ON k.user_id = n.user_id
           WHERE n.chat_id=?
           ORDER BY n.nick""",
        (chat_id,)
    ).fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("Ников нет.")
        return
    lines = ["📋 <b>Ники в этом чате:</b>"]
    for r in rows:
        name = r["first_name"] or r["username"] or str(r["user_id"])
        lines.append(f"  • <b>{r['nick']}</b> — {name}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


def _get_nick(chat_id: int, user_id: int) -> Optional[str]:
    """Возвращает ник пользователя в чате или None."""
    conn = db_connect()
    row = conn.execute(
        "SELECT nick FROM nicknames WHERE chat_id=? AND user_id=?", (chat_id, user_id)
    ).fetchone()
    conn.close()
    return row["nick"] if row else None


# ─────────────────────────── МОДУЛЬ: РЕЙТИНГ АКТИВНОСТИ ─────────────────────

def _activity_add(chat_id: int, user_id: int) -> None:
    """Засчитываем +1 сообщение пользователю за сегодня."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    try:
        conn = db_connect()
        conn.execute(
            """INSERT INTO activity (chat_id, user_id, day, msg_count)
               VALUES (?,?,?,1)
               ON CONFLICT(chat_id, user_id, day)
               DO UPDATE SET msg_count = msg_count + 1""",
            (chat_id, user_id, today),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


async def cmd_activity_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Топ активности: день / неделя / месяц. Использование: Активность [день|неделя|месяц]"""
    args = _parse_args(update, context)
    period_arg = args[0].lower() if args else "день"

    if period_arg in ("неделя", "неделю", "week", "7"):
        label = "неделю"
        days = 7
    elif period_arg in ("месяц", "month", "30"):
        label = "месяц"
        days = 30
    else:
        label = "день"
        days = 1

    since = (datetime.utcnow() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    chat_id = update.effective_chat.id
    conn = db_connect()
    rows = conn.execute(
        """SELECT a.user_id, SUM(a.msg_count) as total,
                  k.first_name, k.last_name, k.username
           FROM activity a
           LEFT JOIN known_users k ON k.user_id = a.user_id
           WHERE a.chat_id=? AND a.day >= ?
           GROUP BY a.user_id
           ORDER BY total DESC
           LIMIT 10""",
        (chat_id, since),
    ).fetchall()
    conn.close()

    medals = ["🥇", "🥈", "🥉"]
    lines = [f"📊 <b>Топ активности за {label}:</b>\n"]
    if not rows:
        lines.append("Пока нет данных.")
    for i, r in enumerate(rows):
        first = r["first_name"] or ""
        last  = r["last_name"]  or ""
        name  = (first + " " + last).strip() or r["username"] or f"id{r['user_id']}"
        icon  = medals[i] if i < 3 else f"{i+1}."
        lines.append(f"{icon} {name} — {r['total']} сообщений")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_my_activity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Моя активность — статистика текущего пользователя."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    conn = db_connect()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    week_start  = (datetime.utcnow() - timedelta(days=6)).strftime("%Y-%m-%d")
    month_start = (datetime.utcnow() - timedelta(days=29)).strftime("%Y-%m-%d")

    def _sum(since):
        r = conn.execute(
            "SELECT SUM(msg_count) as s FROM activity WHERE chat_id=? AND user_id=? AND day>=?",
            (chat_id, user.id, since),
        ).fetchone()
        return r["s"] or 0

    day_cnt   = _sum(today)
    week_cnt  = _sum(week_start)
    month_cnt = _sum(month_start)

    # Позиция в чате за сегодня
    rank_row = conn.execute(
        """SELECT COUNT(*)+1 as pos FROM (
               SELECT user_id, SUM(msg_count) as t FROM activity
               WHERE chat_id=? AND day=? GROUP BY user_id
           ) WHERE t > ?""",
        (chat_id, today, day_cnt),
    ).fetchone()
    conn.close()

    name = user.first_name or user.username or "Игрок"
    pos  = rank_row["pos"] if rank_row else "—"
    text = (
        f"📈 <b>Активность: {name}</b>\n\n"
        f"Сегодня: <b>{day_cnt}</b> сообщений (место #{pos})\n"
        f"За неделю: <b>{week_cnt}</b>\n"
        f"За месяц: <b>{month_cnt}</b>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


# ─────────────────────────── МОДУЛЬ: АНТИСПАМ ────────────────────────────────

async def _antispam_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Возвращает True если сообщение заблокировано (пользователь спамит)."""
    chat_id = update.effective_chat.id
    user    = update.effective_user
    if not user or not update.message:
        return False

    # Модераторы не проверяются
    if get_rank(chat_id, user.id) >= 1:
        return False

    conn = db_connect()
    row = conn.execute(
        "SELECT enabled, max_msgs, interval_sec, action, mute_duration FROM antispam_settings WHERE chat_id=?",
        (chat_id,),
    ).fetchone()
    conn.close()
    if not row or not row["enabled"]:
        return False

    max_msgs     = row["max_msgs"]
    interval_sec = row["interval_sec"]
    action       = row["action"]
    mute_dur     = row["mute_duration"]

    now = time.time()
    if chat_id not in _spam_cache:
        _spam_cache[chat_id] = {}
    if user.id not in _spam_cache[chat_id]:
        _spam_cache[chat_id][user.id] = []

    timestamps = _spam_cache[chat_id][user.id]
    timestamps = [t for t in timestamps if now - t < interval_sec]
    timestamps.append(now)
    _spam_cache[chat_id][user.id] = timestamps

    if len(timestamps) > max_msgs:
        name = user.first_name or user.username or "Пользователь"
        try:
            await update.message.delete()
        except Exception:
            pass
        if action == "мут":
            until = datetime.utcnow() + timedelta(seconds=mute_dur)
            try:
                await context.bot.restrict_chat_member(
                    chat_id, user.id,
                    ChatPermissions(can_send_messages=False),
                    until_date=until,
                )
                mins = mute_dur // 60
                await context.bot.send_message(
                    chat_id,
                    f"🚫 <b>{name}</b> замучен на {mins} мин за спам.",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error(f"antispam mute error: {e}")
        elif action == "бан":
            try:
                await context.bot.ban_chat_member(chat_id, user.id)
                await context.bot.send_message(
                    chat_id,
                    f"🚫 <b>{name}</b> забанен за спам.",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error(f"antispam ban error: {e}")
        elif action == "предупреждение":
            await context.bot.send_message(
                chat_id,
                f"⚠️ <b>{name}</b>, не спами!",
                parse_mode="HTML",
            )
        _spam_cache[chat_id][user.id] = []
        return True
    return False


async def cmd_antispam(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Антиспам [вкл|выкл|настройки|лимит N|период N|действие мут/бан/предупреждение|мут N]"""
    if not await require_rank(update, 2):
        return
    chat_id = update.effective_chat.id
    args = _parse_args(update, context)

    conn = db_connect()
    conn.execute("INSERT OR IGNORE INTO antispam_settings (chat_id) VALUES (?)", (chat_id,))
    conn.commit()

    if not args or args[0].lower() == "настройки":
        row = conn.execute("SELECT * FROM antispam_settings WHERE chat_id=?", (chat_id,)).fetchone()
        conn.close()
        status = "✅ включён" if row["enabled"] else "❌ выключен"
        await update.message.reply_text(
            f"🛡 <b>Антиспам</b> — {status}\n"
            f"Лимит: {row['max_msgs']} сообщений за {row['interval_sec']} сек\n"
            f"Действие: {row['action']}"
            + (f" на {row['mute_duration']//60} мин" if row['action'] == 'мут' else ""),
            parse_mode="HTML",
        )
        return

    sub = args[0].lower()
    if sub == "вкл":
        conn.execute("UPDATE antispam_settings SET enabled=1 WHERE chat_id=?", (chat_id,))
        conn.commit(); conn.close()
        await update.message.reply_text("✅ Антиспам включён.")
    elif sub == "выкл":
        conn.execute("UPDATE antispam_settings SET enabled=0 WHERE chat_id=?", (chat_id,))
        conn.commit(); conn.close()
        await update.message.reply_text("❌ Антиспам выключен.")
    elif sub == "лимит" and len(args) > 1 and args[1].isdigit():
        conn.execute("UPDATE antispam_settings SET max_msgs=? WHERE chat_id=?", (int(args[1]), chat_id))
        conn.commit(); conn.close()
        await update.message.reply_text(f"✅ Лимит: {args[1]} сообщений.")
    elif sub == "период" and len(args) > 1 and args[1].isdigit():
        conn.execute("UPDATE antispam_settings SET interval_sec=? WHERE chat_id=?", (int(args[1]), chat_id))
        conn.commit(); conn.close()
        await update.message.reply_text(f"✅ Период: {args[1]} сек.")
    elif sub == "действие" and len(args) > 1 and args[1].lower() in ("мут", "бан", "предупреждение"):
        conn.execute("UPDATE antispam_settings SET action=? WHERE chat_id=?", (args[1].lower(), chat_id))
        conn.commit(); conn.close()
        await update.message.reply_text(f"✅ Действие: {args[1].lower()}.")
    elif sub == "мут" and len(args) > 1 and args[1].isdigit():
        conn.execute("UPDATE antispam_settings SET mute_duration=? WHERE chat_id=?", (int(args[1])*60, chat_id))
        conn.commit(); conn.close()
        await update.message.reply_text(f"✅ Длительность мута: {args[1]} мин.")
    else:
        conn.close()
        await update.message.reply_text(
            "Использование:\n"
            "<code>Антиспам вкл</code> | <code>Антиспам выкл</code>\n"
            "<code>Антиспам лимит 5</code> — сообщений за период\n"
            "<code>Антиспам период 5</code> — секунд\n"
            "<code>Антиспам действие мут/бан/предупреждение</code>\n"
            "<code>Антиспам мут 5</code> — мут на N минут",
            parse_mode="HTML",
        )


# ─────────────────────────── МОДУЛЬ: АНТИФЛУД ────────────────────────────────

async def _antiflood_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Возвращает True если сообщение заблокировано (повторный флуд)."""
    chat_id = update.effective_chat.id
    user    = update.effective_user
    if not user or not update.message or not update.message.text:
        return False
    if get_rank(chat_id, user.id) >= 1:
        return False

    conn = db_connect()
    row = conn.execute(
        "SELECT enabled, max_same, action FROM antiflood_settings WHERE chat_id=?",
        (chat_id,),
    ).fetchone()
    conn.close()
    if not row or not row["enabled"]:
        return False

    max_same = row["max_same"]
    action   = row["action"]
    text     = update.message.text.strip()

    if chat_id not in _flood_cache:
        _flood_cache[chat_id] = {}
    last_text, count = _flood_cache[chat_id].get(user.id, ("", 0))

    if text == last_text:
        count += 1
    else:
        count = 1
    _flood_cache[chat_id][user.id] = (text, count)

    if count > max_same:
        name = user.first_name or user.username or "Пользователь"
        try:
            await update.message.delete()
        except Exception:
            pass
        _flood_cache[chat_id][user.id] = ("", 0)
        if action == "предупреждение":
            await context.bot.send_message(
                chat_id,
                f"⚠️ <b>{name}</b>, не флуди одним сообщением!",
                parse_mode="HTML",
            )
        elif action == "мут":
            until = datetime.utcnow() + timedelta(minutes=5)
            try:
                await context.bot.restrict_chat_member(
                    chat_id, user.id,
                    ChatPermissions(can_send_messages=False),
                    until_date=until,
                )
                await context.bot.send_message(
                    chat_id,
                    f"🚫 <b>{name}</b> замучен на 5 мин за флуд.",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error(f"antiflood mute error: {e}")
        return True
    return False


async def cmd_antiflood(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Антифлуд [вкл|выкл|лимит N|действие предупреждение/мут]"""
    if not await require_rank(update, 2):
        return
    chat_id = update.effective_chat.id
    args = _parse_args(update, context)

    conn = db_connect()
    conn.execute("INSERT OR IGNORE INTO antiflood_settings (chat_id) VALUES (?)", (chat_id,))
    conn.commit()

    if not args or args[0].lower() == "настройки":
        row = conn.execute("SELECT * FROM antiflood_settings WHERE chat_id=?", (chat_id,)).fetchone()
        conn.close()
        status = "✅ включён" if row["enabled"] else "❌ выключен"
        await update.message.reply_text(
            f"🌊 <b>Антифлуд</b> — {status}\n"
            f"Лимит повторов: {row['max_same']}\n"
            f"Действие: {row['action']}",
            parse_mode="HTML",
        )
        return

    sub = args[0].lower()
    if sub == "вкл":
        conn.execute("UPDATE antiflood_settings SET enabled=1 WHERE chat_id=?", (chat_id,))
        conn.commit(); conn.close()
        await update.message.reply_text("✅ Антифлуд включён.")
    elif sub == "выкл":
        conn.execute("UPDATE antiflood_settings SET enabled=0 WHERE chat_id=?", (chat_id,))
        conn.commit(); conn.close()
        await update.message.reply_text("❌ Антифлуд выключен.")
    elif sub == "лимит" and len(args) > 1 and args[1].isdigit():
        conn.execute("UPDATE antiflood_settings SET max_same=? WHERE chat_id=?", (int(args[1]), chat_id))
        conn.commit(); conn.close()
        await update.message.reply_text(f"✅ Лимит повторов: {args[1]}.")
    elif sub == "действие" and len(args) > 1 and args[1].lower() in ("предупреждение", "мут"):
        conn.execute("UPDATE antiflood_settings SET action=? WHERE chat_id=?", (args[1].lower(), chat_id))
        conn.commit(); conn.close()
        await update.message.reply_text(f"✅ Действие: {args[1].lower()}.")
    else:
        conn.close()
        await update.message.reply_text(
            "Использование:\n"
            "<code>Антифлуд вкл</code> | <code>Антифлуд выкл</code>\n"
            "<code>Антифлуд лимит 3</code>\n"
            "<code>Антифлуд действие предупреждение/мут</code>",
            parse_mode="HTML",
        )


# ─────────────────────────── МОДУЛЬ: ФИЛЬТР СЛОВ ─────────────────────────────

async def _badwords_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Удаляет сообщение если содержит запрещённое слово."""
    chat_id = update.effective_chat.id
    user    = update.effective_user
    if not user or not update.message or not update.message.text:
        return False
    if get_rank(chat_id, user.id) >= 2:
        return False

    conn = db_connect()
    words = [r["word"] for r in conn.execute(
        "SELECT word FROM badwords WHERE chat_id=?", (chat_id,)
    ).fetchall()]
    conn.close()
    if not words:
        return False

    text_lower = update.message.text.lower()
    for w in words:
        if w in text_lower:
            try:
                await update.message.delete()
            except Exception:
                pass
            name = user.first_name or user.username or "Пользователь"
            await context.bot.send_message(
                chat_id,
                f"🚫 <b>{name}</b>, сообщение удалено (запрещённое слово).",
                parse_mode="HTML",
            )
            return True
    return False


async def cmd_badwords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """+Стоп-слово слово / -Стоп-слово слово / Стоп-слова"""
    if not await require_rank(update, 2):
        return
    chat_id = update.effective_chat.id
    text    = update.message.text.strip()

    if re.match(r"^[+]стоп.слово\b", text, re.IGNORECASE):
        word = text.split(None, 1)[1].strip().lower() if len(text.split(None, 1)) > 1 else ""
        if not word:
            await update.message.reply_text("Укажите слово: +Стоп-слово <слово>")
            return
        try:
            conn = db_connect()
            conn.execute("INSERT OR IGNORE INTO badwords (chat_id, word) VALUES (?,?)", (chat_id, word))
            conn.commit(); conn.close()
            await update.message.reply_text(f"✅ Слово «{word}» добавлено в фильтр.")
        except Exception:
            await update.message.reply_text("Ошибка добавления.")
    elif re.match(r"^[-]стоп.слово\b", text, re.IGNORECASE):
        word = text.split(None, 1)[1].strip().lower() if len(text.split(None, 1)) > 1 else ""
        if not word:
            await update.message.reply_text("Укажите слово: -Стоп-слово <слово>")
            return
        conn = db_connect()
        conn.execute("DELETE FROM badwords WHERE chat_id=? AND word=?", (chat_id, word))
        conn.commit(); conn.close()
        await update.message.reply_text(f"✅ Слово «{word}» удалено из фильтра.")
    else:
        conn = db_connect()
        rows = conn.execute("SELECT word FROM badwords WHERE chat_id=? ORDER BY word", (chat_id,)).fetchall()
        conn.close()
        if not rows:
            await update.message.reply_text("Список стоп-слов пуст.")
        else:
            words = ", ".join(r["word"] for r in rows)
            await update.message.reply_text(f"🚫 <b>Стоп-слова:</b>\n{words}", parse_mode="HTML")


# ─────────────────────────── МОДУЛЬ: КАПЧА ───────────────────────────────────

async def cmd_captcha_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Капча [вкл|выкл|таймаут N]"""
    if not await require_rank(update, 2):
        return
    chat_id = update.effective_chat.id
    args = _parse_args(update, context)

    conn = db_connect()
    conn.execute("INSERT OR IGNORE INTO captcha_settings (chat_id) VALUES (?)", (chat_id,))
    conn.commit()

    if not args:
        row = conn.execute("SELECT * FROM captcha_settings WHERE chat_id=?", (chat_id,)).fetchone()
        conn.close()
        status = "✅ включена" if row["enabled"] else "❌ выключена"
        await update.message.reply_text(
            f"🔐 <b>Капча</b> — {status}\nТаймаут: {row['timeout_sec']} сек",
            parse_mode="HTML",
        )
        return

    sub = args[0].lower()
    if sub == "вкл":
        conn.execute("UPDATE captcha_settings SET enabled=1 WHERE chat_id=?", (chat_id,))
        conn.commit(); conn.close()
        await update.message.reply_text("✅ Капча включена. Новые участники должны нажать кнопку.")
    elif sub == "выкл":
        conn.execute("UPDATE captcha_settings SET enabled=0 WHERE chat_id=?", (chat_id,))
        conn.commit(); conn.close()
        await update.message.reply_text("❌ Капча выключена.")
    elif sub == "таймаут" and len(args) > 1 and args[1].isdigit():
        conn.execute("UPDATE captcha_settings SET timeout_sec=? WHERE chat_id=?", (int(args[1]), chat_id))
        conn.commit(); conn.close()
        await update.message.reply_text(f"✅ Таймаут: {args[1]} сек.")
    else:
        conn.close()
        await update.message.reply_text(
            "Использование:\n"
            "<code>Капча вкл</code> | <code>Капча выкл</code>\n"
            "<code>Капча таймаут 60</code>",
            parse_mode="HTML",
        )


async def on_new_member_captcha(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляем капчу новому участнику если включена."""
    chat_id = update.effective_chat.id
    conn = db_connect()
    row = conn.execute(
        "SELECT enabled, timeout_sec FROM captcha_settings WHERE chat_id=?", (chat_id,)
    ).fetchone()
    conn.close()
    if not row or not row["enabled"]:
        return

    timeout = row["timeout_sec"]
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue
        # Запрещаем писать до прохождения капчи
        try:
            await context.bot.restrict_chat_member(
                chat_id, member.id,
                ChatPermissions(can_send_messages=False),
            )
        except Exception:
            pass

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "✅ Я не бот!",
                callback_data=f"captcha_ok:{member.id}",
            )
        ]])
        name = member.first_name or member.username or "Новый участник"
        msg = await update.message.reply_text(
            f"👋 <b>{name}</b>, добро пожаловать!\n"
            f"Нажми кнопку в течение {timeout} сек, иначе будешь удалён.",
            parse_mode="HTML",
            reply_markup=kb,
        )

        expire_at = (datetime.utcnow() + timedelta(seconds=timeout)).strftime("%Y-%m-%d %H:%M:%S")
        conn = db_connect()
        conn.execute(
            "INSERT OR REPLACE INTO captcha_pending (chat_id, user_id, msg_id, expire_at) VALUES (?,?,?,?)",
            (chat_id, member.id, msg.message_id, expire_at),
        )
        conn.commit(); conn.close()

        # Планируем кик по таймауту
        context.application.create_task(
            _captcha_timeout(context, chat_id, member.id, msg.message_id, timeout)
        )


async def _captcha_timeout(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, msg_id: int, timeout: int) -> None:
    await asyncio.sleep(timeout)
    conn = db_connect()
    row = conn.execute(
        "SELECT user_id FROM captcha_pending WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    ).fetchone()
    conn.close()
    if row:
        try:
            await context.bot.ban_chat_member(chat_id, user_id)
            await context.bot.unban_chat_member(chat_id, user_id)  # кик без бана
            await context.bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
        conn = db_connect()
        conn.execute("DELETE FROM captcha_pending WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        conn.commit(); conn.close()


async def cb_captcha_ok(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback: пользователь нажал 'Я не бот'."""
    query = update.callback_query
    data  = query.data  # captcha_ok:USER_ID
    parts = data.split(":")
    if len(parts) != 2:
        return
    target_id = int(parts[1])

    if query.from_user.id != target_id:
        await query.answer("Это не для тебя!", show_alert=True)
        return

    chat_id = update.effective_chat.id
    conn = db_connect()
    conn.execute("DELETE FROM captcha_pending WHERE chat_id=? AND user_id=?", (chat_id, target_id))
    conn.commit(); conn.close()

    # Восстанавливаем ВСЕ права (новый Bot API требует явно указывать каждое)
    full_perms = ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_change_info=False,
        can_invite_users=True,
        can_pin_messages=False,
    )
    try:
        await context.bot.restrict_chat_member(chat_id, target_id, full_perms)
        await query.answer("✅ Проверка пройдена!", show_alert=False)
        try:
            await query.edit_message_text(
                f"✅ <b>{query.from_user.first_name}</b> прошёл проверку!",
                parse_mode="HTML",
            )
        except Exception:
            pass
    except Exception as e:
        # Права не удалось снять — сообщаем об ошибке
        await query.answer("⚠️ Ошибка при снятии ограничений, обратитесь к администратору.", show_alert=True)
        logger.error(f"captcha restrict error for {target_id} in {chat_id}: {e}")


# ─────────────────────────── МОДУЛЬ: УПРАВЛЕНИЕ ГEOФОТО ─────────────────────


# ─────────────────────── /panel — панель управления геоигрой ─────────────────
# Состояния диалога добавления вопроса
_PANEL_PHOTO        = 1   # ждём фото (геоигра)
_PANEL_ANSWER       = 2   # ждём правильный ответ
_PANEL_WRONGS       = 3   # ждём неправильные варианты
_PANEL_DELETE       = 4   # ждём номер для удаления
_PANEL_EXP_LOC      = 5   # ждём выбор локации для фото экспедиции
_PANEL_EXP_EVENT    = 6   # ждём выбор события
_PANEL_EXP_PHOTO    = 7   # ждём фото для события экспедиции
_PANEL_EXP_DEL_LOC  = 8   # ждём локацию для удаления фото
_PANEL_EXP_DEL_NUM  = 9   # ждём номер фото для удаления
_PANEL_BJ_MOMENT    = 10  # ждём выбор момента блекджека
_PANEL_BJ_PHOTO     = 11  # ждём фото блекджека
_PANEL_ROU_NUMBER   = 12  # ждём выбор числа рулетки
_PANEL_ROU_PHOTO    = 13  # ждём фото рулетки
_PANEL_DICE_MOMENT  = 14  # ждём выбор момента костей
_PANEL_DICE_PHOTO   = 15  # ждём фото костей
_PANEL_PROFILE_PHOTO = 16  # ждём фото профиля
_PANEL_BROADCAST    = 17  # ждём текст рассылки
_PANEL_PROMO_ADD    = 18  # ждём данные нового промокода
_PANEL_PROMO_DEL    = 19  # ждём код для удаления


async def cmd_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/panel — панель управления геоигрой."""
    if MAIN_GROUP_ID and update.effective_chat.id != MAIN_GROUP_ID:
        await update.message.reply_text("Эта команда доступна только в основной группе.")
        return
    if not await require_rank(update, 3):
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Гео: добавить вопрос",   callback_data="panel_add"),
         InlineKeyboardButton("🗑 Гео: удалить вопрос",    callback_data="panel_del")],
        [InlineKeyboardButton("🖼 Фото экспедиций: +",     callback_data="panel_exp_add"),
         InlineKeyboardButton("🗑 Фото экспедиций: -",     callback_data="panel_exp_del")],
        [InlineKeyboardButton("🃏 Фото блекджека",         callback_data="panel_bj"),
         InlineKeyboardButton("🎡 Фото рулетки",           callback_data="panel_rou")],
        [InlineKeyboardButton("🎲 Фото костей",            callback_data="panel_dice"),
         InlineKeyboardButton("👤 Фото профиля (Я)",       callback_data="panel_profile")],
        [InlineKeyboardButton("📢 Рассылка по всем чатам", callback_data="panel_broadcast")],
        [InlineKeyboardButton("🎟 Добавить промокод",        callback_data="panel_promo_add"),
         InlineKeyboardButton("🗑 Убрать промокод",          callback_data="panel_promo_del")],
    ])
    await update.message.reply_text(
        "🎮 <b>Панель управления</b>\n\nВыберите действие:",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def cb_panel_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Кнопка «Добавить вопрос» — просим прислать фото."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "📸 Отправьте фото для нового вопроса.\n\n"
        "Чтобы отменить — напишите /cancel"
    )
    return _PANEL_PHOTO


async def cb_panel_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Кнопка «Удалить вопрос» — показываем список и просим номер."""
    query = update.callback_query
    await query.answer()

    conn = db_connect()
    rows = conn.execute(
        "SELECT id, answer FROM geo_custom_photos ORDER BY id DESC LIMIT 30"
    ).fetchall()
    conn.close()

    if not rows:
        await query.edit_message_text("Кастомных вопросов пока нет.")
        return ConversationHandler.END

    lines = ["🗑 <b>Введите номер вопроса для удаления:</b>\n"]
    for r in rows:
        lines.append(f"[{r['id']}] {r['answer']}")
    await query.edit_message_text("\n".join(lines), parse_mode="HTML")
    return _PANEL_DELETE


async def panel_recv_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получили фото — сохраняем file_id, просим правильный ответ."""
    if not update.message.photo:
        await update.message.reply_text("Нужно именно фото. Попробуйте ещё раз или /cancel")
        return _PANEL_PHOTO

    context.user_data["panel_file_id"] = update.message.photo[-1].file_id
    await update.message.reply_text(
        "✅ Фото получено!\n\n"
        "Теперь напишите <b>правильный ответ</b> (страну/город):",
        parse_mode="HTML",
    )
    return _PANEL_ANSWER


async def panel_recv_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получили правильный ответ — просим неправильные варианты."""
    answer = update.message.text.strip()
    if not answer:
        await update.message.reply_text("Пустой ответ. Попробуйте ещё раз или /cancel")
        return _PANEL_ANSWER

    context.user_data["panel_answer"] = answer
    await update.message.reply_text(
        f"Правильный ответ: <b>{answer}</b>\n\n"
        "Теперь напишите <b>2 неправильных варианта</b> через запятую:\n"
        "<i>Например: Германия, Италия</i>\n\n"
        "Или отправьте <code>-</code> чтобы пропустить.",
        parse_mode="HTML",
    )
    return _PANEL_WRONGS


async def panel_recv_wrongs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получили неправильные варианты — сохраняем вопрос в БД."""
    text = update.message.text.strip()
    wrong1, wrong2 = "", ""
    if text != "-":
        parts = [p.strip() for p in text.split(",")]
        wrong1 = parts[0] if len(parts) > 0 else ""
        wrong2 = parts[1] if len(parts) > 1 else ""

    file_id = context.user_data.pop("panel_file_id", None)
    answer  = context.user_data.pop("panel_answer", None)

    if not file_id or not answer:
        await update.message.reply_text("Что-то пошло не так. Начните заново — /panel")
        return ConversationHandler.END

    conn = db_connect()
    conn.execute(
        "INSERT INTO geo_custom_photos (chat_id, file_id, answer, wrong1, wrong2, added_by) "
        "VALUES (?,?,?,?,?,?)",
        (update.effective_chat.id, file_id, answer, wrong1, wrong2, update.effective_user.id),
    )
    conn.commit()
    pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()

    variants = f"\nНеправильные варианты: {wrong1}, {wrong2}" if wrong1 else ""
    await update.message.reply_text(
        f"✅ Вопрос <b>#{pid}</b> добавлен!\n"
        f"Страна: <b>{answer}</b>{variants}",
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def panel_recv_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получили номер для удаления — удаляем запись."""
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("Введите число — номер вопроса. Или /cancel")
        return _PANEL_DELETE

    pid = int(text)
    conn = db_connect()
    row = conn.execute(
        "SELECT id, answer FROM geo_custom_photos WHERE id=?", (pid,)
    ).fetchone()
    if not row:
        conn.close()
        await update.message.reply_text(f"Вопрос #{pid} не найден. Введите другой номер или /cancel")
        return _PANEL_DELETE

    conn.execute("DELETE FROM geo_custom_photos WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Вопрос <b>#{pid}</b> ({row['answer']}) удалён.", parse_mode="HTML")
    return ConversationHandler.END


async def panel_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отмена диалога."""
    context.user_data.pop("panel_file_id", None)
    context.user_data.pop("panel_answer", None)
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END


# ─────────────────── Фото экспедиций в панели ────────────────────────────────

def _exp_loc_keyboard():
    """Инлайн-клавиатура выбора локации экспедиции."""
    buttons = []
    for loc_id, loc in LOCATIONS.items():
        buttons.append([InlineKeyboardButton(loc["name"], callback_data=f"exp_loc_{loc_id}")])
    return InlineKeyboardMarkup(buttons)


async def cb_panel_exp_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Кнопка «Фото экспедиций: добавить» — выбор локации."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🖼 Выберите локацию для добавления фото:",
        reply_markup=_exp_loc_keyboard(),
    )
    return _PANEL_EXP_LOC


async def cb_exp_loc_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Пользователь выбрал локацию — показываем список событий."""
    query = update.callback_query
    await query.answer()
    loc_id = int(query.data.split("_")[-1])
    context.user_data["exp_loc_id"] = loc_id
    loc = LOCATIONS[loc_id]

    # Показываем события с номерами
    events = [e for e in loc["events"] if e[4] > 0]
    lines = [f"📍 <b>{loc['name']}</b>\nВведите номер события:\n"]
    for i, e in enumerate(events):
        lines.append(f"[{i+1}] {e[0][:80]}...")
    context.user_data["exp_events"] = events

    await query.edit_message_text("\n".join(lines), parse_mode="HTML")
    return _PANEL_EXP_EVENT


async def panel_exp_recv_event(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получили номер события — просим фото."""
    text = update.message.text.strip()
    events = context.user_data.get("exp_events", [])
    if not text.isdigit() or int(text) < 1 or int(text) > len(events):
        await update.message.reply_text(f"Введите число от 1 до {len(events)} или /cancel")
        return _PANEL_EXP_EVENT
    idx = int(text) - 1
    event = events[idx]
    context.user_data["exp_event_text"] = event[0]
    await update.message.reply_text(
        f"✅ Событие выбрано:\n<i>{event[0][:120]}</i>\n\nТеперь отправьте фото для него:",
        parse_mode="HTML",
    )
    return _PANEL_EXP_PHOTO


async def panel_exp_recv_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получили фото — сохраняем в expedition_photos."""
    if not update.message.photo:
        await update.message.reply_text("Нужно именно фото. Попробуйте ещё раз или /cancel")
        return _PANEL_EXP_PHOTO

    file_id   = update.message.photo[-1].file_id
    loc_id    = context.user_data.pop("exp_loc_id", None)
    event_txt = context.user_data.pop("exp_event_text", None)
    context.user_data.pop("exp_events", None)

    if loc_id is None or not event_txt:
        await update.message.reply_text("Что-то пошло не так. Начните заново — /panel")
        return ConversationHandler.END

    conn = db_connect()
    conn.execute(
        "INSERT INTO expedition_photos (loc_id, event_text, file_id) VALUES (?,?,?)",
        (loc_id, event_txt, file_id),
    )
    conn.commit()
    conn.close()

    loc_name = LOCATIONS[loc_id]["name"]
    await update.message.reply_text(
        f"✅ Фото добавлено для локации <b>{loc_name}</b>!",
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def cb_panel_exp_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Кнопка «Фото экспедиций: удалить» — выбор локации."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🗑 Выберите локацию для удаления фото:",
        reply_markup=_exp_loc_keyboard(),
    )
    return _PANEL_EXP_DEL_LOC


async def cb_exp_loc_del_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Пользователь выбрал локацию для удаления — показываем список фото."""
    query = update.callback_query
    await query.answer()
    loc_id = int(query.data.split("_")[-1])
    context.user_data["exp_del_loc_id"] = loc_id

    conn = db_connect()
    rows = conn.execute(
        "SELECT id, event_text FROM expedition_photos WHERE loc_id=? ORDER BY id",
        (loc_id,),
    ).fetchall()
    conn.close()

    if not rows:
        await query.edit_message_text("В этой локации нет загруженных фото.")
        return ConversationHandler.END

    lines = [f"🗑 <b>{LOCATIONS[loc_id]['name']}</b>\nВведите номер фото для удаления:\n"]
    for r in rows:
        lines.append(f"[{r['id']}] {r['event_text'][:80]}...")
    await query.edit_message_text("\n".join(lines), parse_mode="HTML")
    return _PANEL_EXP_DEL_NUM


async def panel_exp_del_num(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получили номер фото — удаляем."""
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("Введите число — id фото. Или /cancel")
        return _PANEL_EXP_DEL_NUM

    pid = int(text)
    conn = db_connect()
    row = conn.execute(
        "SELECT id, event_text FROM expedition_photos WHERE id=?", (pid,)
    ).fetchone()
    if not row:
        conn.close()
        await update.message.reply_text(f"Фото #{pid} не найдено. Введите другой номер или /cancel")
        return _PANEL_EXP_DEL_NUM

    conn.execute("DELETE FROM expedition_photos WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Фото <b>#{pid}</b> удалено.", parse_mode="HTML")
    return ConversationHandler.END


# ─────────────────── Команда выдачи валюты (только главная группа, ранг 3+) ──

async def cmd_give_currency(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`выдать @user N самородков/монет` — выдать валюту игроку."""
    if MAIN_GROUP_ID and update.effective_chat.id != MAIN_GROUP_ID:
        return
    if not await require_rank(update, 3):
        return

    msg  = update.message
    # Формат: выдать [@user] <количество> <самородков|монет>
    # При реплае @user можно не указывать
    raw_args = (msg.text or "").split()[1:]  # убираем саму команду

    # Ищем число и валюту среди аргументов
    amount_str   = next((a for a in raw_args if a.isdigit()), None)
    currency_arg = raw_args[-1].lower() if raw_args else ""

    if not amount_str:
        await msg.reply_text("Формат: выдать @user <количество> <самородков|монет>\nили ответь на сообщение: выдать <количество> <самородков|монет>")
        return
    amount = int(amount_str)

    if "самородк" in currency_arg or currency_arg in ("nuggets", "самородок"):
        field = "nuggets"
        emoji = "🪨"
        label = "самородков"
    elif "монет" in currency_arg or currency_arg in ("coins", "монета"):
        field = "coins"
        emoji = "🪙"
        label = "монет"
    else:
        await msg.reply_text("Укажи валюту: самородков или монет")
        return

    target = await resolve_target(update, context)
    if not target:
        await msg.reply_text("Не могу найти пользователя. Укажи @username или ответь на его сообщение.")
        return

    data = _econ_get(target.id)
    new_val = data[field] + amount
    _econ_update(target.id, **{field: new_val})

    await msg.reply_text(
        f"✅ {target.mention_html()} получил <b>{amount} {emoji}</b> {label}!\n"
        f"Новый баланс: {new_val} {emoji}",
        parse_mode="HTML",
    )


# ─── Панель: фото блекджека, рулетки, костей, профиля ────────────────────────

_BJ_MOMENTS   = ["start", "win", "lose", "bust", "tie"]
_DICE_MOMENTS = ["win", "lose", "tie"]

async def cb_panel_bj(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    buttons = [[InlineKeyboardButton(m, callback_data=f"bj_moment_{m}")] for m in _BJ_MOMENTS]
    await query.edit_message_text("🃏 Выберите момент блекджека:", reply_markup=InlineKeyboardMarkup(buttons))
    return _PANEL_BJ_MOMENT

async def cb_bj_moment_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    moment = query.data[len("bj_moment_"):]
    context.user_data["bj_moment"] = moment
    await query.edit_message_text(f"🃏 Момент: <b>{moment}</b>\nОтправьте фото:", parse_mode="HTML")
    return _PANEL_BJ_PHOTO

async def panel_bj_recv_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text("Нужно фото. Или /cancel")
        return _PANEL_BJ_PHOTO
    moment  = context.user_data.pop("bj_moment", None)
    file_id = update.message.photo[-1].file_id
    conn = db_connect()
    conn.execute(
        "INSERT INTO bj_photos (moment, file_id) VALUES (?,?) "
        "ON CONFLICT(moment) DO UPDATE SET file_id=excluded.file_id",
        (moment, file_id)
    )
    conn.commit(); conn.close()
    await update.message.reply_text(f"✅ Фото для блекджека ({moment}) добавлено!")
    return ConversationHandler.END

_ROU_CATEGORIES = ["win", "lose", "zero"]
_ROU_CATEGORY_LABELS = {"win": "🏆 Победа", "lose": "😞 Поражение", "zero": "🟢 Зеро (0)"}

async def cb_panel_rou(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    buttons = [[InlineKeyboardButton(_ROU_CATEGORY_LABELS[c], callback_data=f"rou_num_{c}")]
               for c in _ROU_CATEGORIES]
    await query.edit_message_text("🎡 Выберите категорию фото рулетки:", reply_markup=InlineKeyboardMarkup(buttons))
    return _PANEL_ROU_NUMBER

async def cb_rou_number_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    category = query.data[len("rou_num_"):]
    context.user_data["rou_number"] = category
    label = _ROU_CATEGORY_LABELS.get(category, category)
    await query.edit_message_text(f"🎡 Категория: <b>{label}</b>\nОтправьте фото:", parse_mode="HTML")
    return _PANEL_ROU_PHOTO

async def panel_rou_recv_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text("Нужно фото. Или /cancel")
        return _PANEL_ROU_PHOTO
    category = context.user_data.pop("rou_number", None)
    file_id = update.message.photo[-1].file_id
    conn = db_connect()
    conn.execute(
        "INSERT INTO roulette_photos (number, file_id) VALUES (?,?) "
        "ON CONFLICT(number) DO UPDATE SET file_id=excluded.file_id",
        (category, file_id)
    )
    conn.commit(); conn.close()
    label = _ROU_CATEGORY_LABELS.get(category, category)
    await update.message.reply_text(f"✅ Фото для рулетки ({label}) добавлено!")
    return ConversationHandler.END

async def cb_panel_dice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    buttons = [[InlineKeyboardButton(m, callback_data=f"dice_moment_{m}")] for m in _DICE_MOMENTS]
    await query.edit_message_text("🎲 Выберите момент костей:", reply_markup=InlineKeyboardMarkup(buttons))
    return _PANEL_DICE_MOMENT

async def cb_dice_moment_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    moment = query.data[len("dice_moment_"):]
    context.user_data["dice_moment"] = moment
    await query.edit_message_text(f"🎲 Момент: <b>{moment}</b>\nОтправьте фото:", parse_mode="HTML")
    return _PANEL_DICE_PHOTO

async def panel_dice_recv_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text("Нужно фото. Или /cancel")
        return _PANEL_DICE_PHOTO
    moment  = context.user_data.pop("dice_moment", None)
    file_id = update.message.photo[-1].file_id
    conn = db_connect()
    conn.execute(
        "INSERT INTO dice_photos (moment, file_id) VALUES (?,?) "
        "ON CONFLICT(moment) DO UPDATE SET file_id=excluded.file_id",
        (moment, file_id)
    )
    conn.commit(); conn.close()
    await update.message.reply_text(f"✅ Фото для костей ({moment}) добавлено!")
    return ConversationHandler.END

async def cb_panel_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    await query.edit_message_text(
        "👤 Отправьте фото профиля для команды <b>Я</b>\n"
        "(Заменяет текущее фото)\n\n/cancel — отмена",
        parse_mode="HTML",
    )
    return _PANEL_PROFILE_PHOTO

async def panel_profile_recv_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text("Нужно фото. Или /cancel")
        return _PANEL_PROFILE_PHOTO
    file_id = update.message.photo[-1].file_id
    conn = db_connect()
    # profile_photos хранит одно фото на user_id — для глобальной заставки используем user_id=0
    conn.execute(
        "INSERT OR REPLACE INTO profile_photos (user_id, file_id) VALUES (0, ?)", (file_id,)
    )
    conn.commit(); conn.close()
    await update.message.reply_text("✅ Фото профиля добавлено!")
    return ConversationHandler.END


async def cb_panel_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Кнопка «Рассылка» — просим текст."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "📢 <b>Рассылка по всем чатам</b>\n\n"
        "Отправьте текст сообщения (поддерживается HTML).\n"
        "Рассылка уйдёт во все чаты из базы данных.\n\n"
        "/cancel — отмена",
        parse_mode="HTML",
    )
    return _PANEL_BROADCAST


async def panel_broadcast_recv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получаем текст рассылки и рассылаем по всем чатам."""
    text = update.message.text or ""
    if not text:
        await update.message.reply_text("Нужен текст сообщения. Или /cancel")
        return _PANEL_BROADCAST

    # Собираем все реальные группы из базы
    conn = db_connect()
    chats_set = set()
    for table in ("chat_settings", "moderators", "activity"):
        try:
            rows = conn.execute(f"SELECT DISTINCT chat_id FROM {table}").fetchall()
            chats_set.update(r["chat_id"] for r in rows if r["chat_id"] < 0)
        except Exception:
            pass
    conn.close()

    if not chats_set:
        await update.message.reply_text("❌ Нет чатов в базе для рассылки.")
        return ConversationHandler.END

    sent = 0
    failed = 0
    for chat_id in chats_set:
        try:
            await update.get_bot().send_message(chat_id, text, parse_mode="HTML")
            sent += 1
        except Exception:
            failed += 1

    await update.message.reply_text(
        f"📢 Рассылка завершена!\n"
        f"✅ Отправлено: <b>{sent}</b>\n"
        f"❌ Ошибок: <b>{failed}</b>",
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def cb_panel_promo_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "🎟 <b>Добавить промокод</b>\n\n"
        "Отправьте данные в формате:\n"
        "<code>КОД МОНЕТЫ САМОРОДКИ ИСПОЛЬЗОВАНИЙ</code>\n\n"
        "Пример: <code>ЛЕТО2025 500 10 50</code>\n"
        "(монеты и самородки — сколько выдаёт, использований — сколько раз можно активировать)\n\n"
        "/cancel — отмена",
        parse_mode="HTML",
    )
    return _PANEL_PROMO_ADD


async def panel_promo_add_recv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    parts = (update.message.text or "").split()
    # Код — первое слово, числа — все цифровые токены в любом порядке
    code = None
    numbers = []
    for p in parts:
        if p.isdigit():
            numbers.append(int(p))
        elif code is None and not p.isdigit():
            code = p.upper()
    if not code or len(numbers) < 3:
        await update.message.reply_text(
            "Формат: <code>КОД МОНЕТЫ САМОРОДКИ ИСПОЛЬЗОВАНИЙ</code>\n"
            "Пример: <code>ЛЕТО2025 500 10 50</code>\n\nИли /cancel",
            parse_mode="HTML",
        )
        return _PANEL_PROMO_ADD
    coins, nuggets, uses = numbers[0], numbers[1], numbers[2]
    conn = db_connect()
    conn.execute(
        "INSERT OR REPLACE INTO promo_codes (code, coins, nuggets, uses_left, created_by) VALUES (?,?,?,?,?)",
        (code, coins, nuggets, uses, update.effective_user.id)
    )
    conn.commit(); conn.close()
    await update.message.reply_text(
        f"✅ Промокод <code>{code}</code> создан!\n"
        f"💰 +{coins} монет, 🪨 +{nuggets} самородков\n"
        f"🔢 Использований: {uses}",
        parse_mode="HTML",
    )
    return ConversationHandler.END


async def cb_panel_promo_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    conn = db_connect()
    rows = conn.execute("SELECT code, coins, nuggets, uses_left FROM promo_codes ORDER BY code").fetchall()
    conn.close()
    if not rows:
        await query.edit_message_text("Активных промокодов нет.")
        return ConversationHandler.END
    lines = ["🗑 <b>Введите код для удаления:</b>\n"]
    for r in rows:
        lines.append(f"• <code>{r['code']}</code> — {r['coins']}🪙 {r['nuggets']}🪨 (осталось: {r['uses_left']})")
    await query.edit_message_text("\n".join(lines), parse_mode="HTML")
    return _PANEL_PROMO_DEL


async def panel_promo_del_recv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = (update.message.text or "").strip().upper()
    conn = db_connect()
    cur = conn.execute("DELETE FROM promo_codes WHERE code=?", (code,))
    conn.commit(); conn.close()
    if cur.rowcount:
        await update.message.reply_text(f"✅ Промокод <code>{code}</code> удалён.", parse_mode="HTML")
    else:
        await update.message.reply_text(f"❌ Промокод <code>{code}</code> не найден.", parse_mode="HTML")
    return ConversationHandler.END


async def cmd_mention_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """все / @все — упомянуть всех активных участников чата."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    min_rank = get_cmd_min_rank(chat_id, "все")
    actual_rank = await get_member_rank(update, user_id)
    if actual_rank < min_rank:
        await update.message.reply_text(
            f"⛔ Требуется ранг {min_rank} ({RANK_NAMES.get(min_rank, min_rank)})."
        )
        return

    # Берём пользователей активных за последние 30 дней в этом чате
    since = (_msk_now() - timedelta(days=30)).strftime("%Y-%m-%d")
    conn = db_connect()
    rows = conn.execute(
        """SELECT DISTINCT a.user_id, k.first_name, k.username
           FROM activity a
           LEFT JOIN known_users k ON k.user_id = a.user_id
           WHERE a.chat_id=? AND a.day>=?""",
        (chat_id, since)
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("Нет активных участников за последние 30 дней.")
        return

    # Разбиваем на чанки по 20 упоминаний (лимит Telegram)
    mentions = []
    for r in rows:
        if r["username"]:
            mentions.append(f"@{r['username']}")
        else:
            name = r["first_name"] or str(r["user_id"])
            mentions.append(f'<a href="tg://user?id={r["user_id"]}">{name}</a>')

    chunk_size = 20
    for i in range(0, len(mentions), chunk_size):
        chunk = mentions[i:i + chunk_size]
        await update.message.reply_text(" ".join(chunk), parse_mode="HTML")



async def cmd_promo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """промокод <КОД> — активировать промокод."""
    _log_game_cmd("промокод")
    chat_id = update.effective_chat.id
    if get_cmd_min_rank(chat_id, "промокод") > await get_member_rank(update, update.effective_user.id):
        return
    args = _parse_args(update, context)
    if not args:
        await update.message.reply_text("Формат: <code>промокод КОД</code>", parse_mode="HTML")
        return
    code = args[0].upper()
    user_id = update.effective_user.id
    conn = db_connect()
    row = conn.execute("SELECT * FROM promo_codes WHERE code=?", (code,)).fetchone()
    if not row:
        conn.close()
        await update.message.reply_text("❌ Промокод не найден или уже недействителен.")
        return
    used = conn.execute("SELECT 1 FROM promo_used WHERE code=? AND user_id=?", (code, user_id)).fetchone()
    if used:
        conn.close()
        await update.message.reply_text("❌ Вы уже использовали этот промокод.")
        return
    if row["uses_left"] <= 0:
        conn.close()
        await update.message.reply_text("❌ Промокод исчерпан.")
        return
    # Зачисляем
    data = _econ_get(user_id)
    _econ_update(user_id, coins=data["coins"] + row["coins"], nuggets=data["nuggets"] + row["nuggets"])
    conn.execute("INSERT INTO promo_used (code, user_id) VALUES (?,?)", (code, user_id))
    conn.execute("UPDATE promo_codes SET uses_left=uses_left-1 WHERE code=?", (code,))
    conn.commit(); conn.close()
    await update.message.reply_text(
        f"✅ Промокод активирован!\n+{row['coins']} 🪙  +{row['nuggets']} 🪨",
        parse_mode="HTML",
    )



async def cmd_geo_add_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    +геофото <страна> [вариант2] [вариант3] — добавить фото в гео-игру.
    Отправьте фото с подписью или реплаем на фото.
    Только для администраторов главной группы.
    """
    if MAIN_GROUP_ID and update.effective_chat.id != MAIN_GROUP_ID:
        await update.message.reply_text("Эта команда доступна только в основной группе.")
        return
    if not await require_rank(update, 3):
        return

    msg = update.message
    photo_msg = msg
    # Если команда вызвана реплаем на фото
    if msg.reply_to_message and msg.reply_to_message.photo:
        photo_msg = msg.reply_to_message

    if not photo_msg.photo:
        await msg.reply_text(
            "Отправьте фото с подписью:\n"
            "<code>+геофото Франция Германия Италия</code>\n"
            "или реплаем на фото с командой.",
            parse_mode="HTML",
        )
        return

    file_id = photo_msg.photo[-1].file_id
    args = _parse_args(update, context)
    if not args:
        await msg.reply_text("Укажите страну: <code>+геофото Франция Германия Италия</code>", parse_mode="HTML")
        return

    answer = args[0]
    wrong1 = args[1] if len(args) > 1 else ""
    wrong2 = args[2] if len(args) > 2 else ""

    conn = db_connect()
    conn.execute(
        "INSERT INTO geo_custom_photos (chat_id, file_id, answer, wrong1, wrong2, added_by) VALUES (?,?,?,?,?,?)",
        (update.effective_chat.id, file_id, answer, wrong1, wrong2, update.effective_user.id),
    )
    conn.commit()
    pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()

    await msg.reply_text(
        f"✅ Фото #{pid} добавлено в гео-игру!\n"
        f"Страна: <b>{answer}</b>"
        + (f"\nВарианты: {wrong1}, {wrong2}" if wrong1 else ""),
        parse_mode="HTML",
    )


async def cmd_geo_del_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    -геофото <номер> — удалить кастомное фото из игры.
    Только для администраторов главной группы.
    """
    if MAIN_GROUP_ID and update.effective_chat.id != MAIN_GROUP_ID:
        await update.message.reply_text("Эта команда доступна только в основной группе.")
        return
    if not await require_rank(update, 3):
        return

    args = _parse_args(update, context)
    if not args or not args[0].isdigit():
        await update.message.reply_text("Укажите номер фото: <code>-геофото 5</code>", parse_mode="HTML")
        return

    pid = int(args[0])
    conn = db_connect()
    row = conn.execute("SELECT id, answer FROM geo_custom_photos WHERE id=?", (pid,)).fetchone()
    if not row:
        conn.close()
        await update.message.reply_text(f"Фото #{pid} не найдено.")
        return
    conn.execute("DELETE FROM geo_custom_photos WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ Фото #{pid} ({row['answer']}) удалено из гео-игры.")


async def cmd_geo_list_photos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Геофото — список добавленных кастомных фото (только главная группа)."""
    if MAIN_GROUP_ID and update.effective_chat.id != MAIN_GROUP_ID:
        await update.message.reply_text("Эта команда доступна только в основной группе.")
        return
    if not await require_rank(update, 1):
        return

    conn = db_connect()
    rows = conn.execute(
        "SELECT id, answer, wrong1, wrong2, added_at FROM geo_custom_photos ORDER BY id DESC LIMIT 50"
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("Кастомных фото пока нет. Добавьте командой +геофото.")
        return

    lines = ["📸 <b>Кастомные фото гео-игры:</b>"]
    for r in rows:
        variants = f" | {r['wrong1']}, {r['wrong2']}" if r["wrong1"] else ""
        lines.append(f"[{r['id']}] <b>{r['answer']}</b>{variants} — {r['added_at'][:10]}")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_geo_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/geo_upload — предзагрузить все фото геоигры в Telegram (запускать один раз)."""
    if not await require_rank(update, 3):
        return
    chat_id = update.effective_chat.id
    total = len(GEO_QUESTIONS)
    cached_count = sum(1 for q in GEO_QUESTIONS if _geo_file_cache.get(q[0]))
    msg = await update.message.reply_text(
        f"⏳ Загрузка запущена в фоне, бот продолжает работать.\n"
        f"Уже в кеше: {cached_count}/{total}\n"
        f"Осталось загрузить: {total - cached_count}"
    )

    async def _do_upload():
        ok = 0
        skip = 0
        fail_list = []
        _headers = {
            "User-Agent": "GeoQuizBot/1.0 (telegram_geoquiz_bot; https://t.me/)",
            "Accept": "image/webp,image/jpeg,image/*",
        }
        _last_edit_text = ""

        async def _safe_edit(text: str) -> None:
            nonlocal _last_edit_text
            if text == _last_edit_text:
                return
            try:
                await msg.edit_text(text)
                _last_edit_text = text
            except Exception:
                pass

        for i, q in enumerate(GEO_QUESTIONS):
            url, correct, _ = q

            if _geo_file_cache.get(url):
                skip += 1
                continue

            if i % 5 == 0:
                done = sum(1 for qq in GEO_QUESTIONS if _geo_file_cache.get(qq[0]))
                await _safe_edit(f"⏳ Фоновая загрузка: {done}/{total} в кеше...")

            photo_bytes = None

            # Способ 1: aiohttp (асинхронный, предпочтительный)
            if _AIOHTTP_OK:
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url, headers=_headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                            if resp.status == 200:
                                data = await resp.read()
                                if len(data) > 500:
                                    photo_bytes = data
                except Exception as e:
                    logger.warning(f"geo_upload aiohttp #{i+1}: {e}")

            # Способ 2: requests (если aiohttp нет или упал)
            if not photo_bytes and _REQUESTS_OK:
                try:
                    loop = asyncio.get_event_loop()
                    def _dl():
                        r = _requests.get(url, headers=_headers, timeout=15)
                        if r.status_code == 200 and len(r.content) > 500:
                            return r.content
                        return None
                    photo_bytes = await loop.run_in_executor(None, _dl)
                except Exception as e:
                    logger.warning(f"geo_upload requests #{i+1}: {e}")

            if not photo_bytes:
                fail_list.append(f"#{i+1} {correct}: не удалось скачать")
                continue

            try:
                buf = io.BytesIO(photo_bytes)
                buf.name = "geo.jpg"
                sent = await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=InputFile(buf, filename="geo.jpg"),
                    caption=f"#{i+1} 🌍 {correct}",
                )
                if sent.photo:
                    _geo_cache_save(url, sent.photo[-1].file_id)
                    ok += 1
                await asyncio.sleep(1)
            except Exception as e:
                fail_list.append(f"#{i+1} {correct}: {e}")
                await asyncio.sleep(3)

        total_cached = sum(1 for q in GEO_QUESTIONS if _geo_file_cache.get(q[0]))
        report = (
            f"✅ Загружено: {ok}\n"
            f"⏭ Уже было: {skip}\n"
            f"❌ Ошибок: {len(fail_list)}\n"
            f"📦 Всего в кеше: {total_cached}/{total}"
        )
        if fail_list:
            report += "\n\nПроблемные:\n" + "\n".join(fail_list[:8])
        await _safe_edit(report)

    asyncio.create_task(_do_upload())


# ─────────────────────────── РЕГИСТРАЦИЯ ОБРАБОТЧИКОВ ────────────────────────

async def _cache_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кешируем каждого пишущего пользователя в памяти и в БД. Считаем активность."""
    user = update.effective_user
    if not user:
        return
    if user.username:
        _user_cache[user.username.lower()] = user
    try:
        conn = db_connect()
        conn.execute(
            """INSERT INTO known_users (user_id, username, first_name, last_name)
               VALUES (?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 username=excluded.username,
                 first_name=excluded.first_name,
                 last_name=excluded.last_name""",
            (user.id, user.username, user.first_name, user.last_name),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

    # Считаем активность только для обычных сообщений в группах
    if update.message and update.effective_chat and update.effective_chat.type in ("group", "supergroup"):
        _activity_add(update.effective_chat.id, user.id)


async def _automod_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Проверяем антиспам, антифлуд и стоп-слова для каждого сообщения."""
    if not update.message or not update.message.text:
        return
    if update.effective_chat and update.effective_chat.type not in ("group", "supergroup"):
        return
    # Проверки по порядку (если одна сработала — следующие не нужны)
    if await _antispam_check(update, context):
        return
    if await _antiflood_check(update, context):
        return
    await _badwords_check(update, context)


def register_handlers(app: Application) -> None:
    def cmd(*commands):
        return filters.TEXT & filters.Regex(
            re.compile(r"^[!+\-~/.]?(" + "|".join(re.escape(c) for c in commands) + r")\b",
                       re.IGNORECASE)
        )

    # Кеш пользователей (group=-1 — выполняется до всех остальных)
    app.add_handler(MessageHandler(filters.ALL, _cache_user), group=-1)
    # Автомодерация (group=0 — только для обычных сообщений)
    # ВАЖНО: все командные MessageHandler-ы идут в group=1,
    # иначе automod в group=0 поглощает их (PTB останавливается на первом совпадении в группе)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _automod_middleware), group=0)

    G = 1  # группа для командных обработчиков

    # Старт и помощь
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(cmd("помощь", "справка"), cmd_help), G)

    # Модерация
    app.add_handler(MessageHandler(cmd("+модер", "+админ", "повысить"), cmd_moder), G)
    app.add_handler(MessageHandler(cmd("-модер", "-админ", "понизить"), cmd_demoder), G)
    app.add_handler(MessageHandler(cmd("кто admin", "кто модер", "кто админ"), cmd_who_admin), G)
    app.add_handler(MessageHandler(cmd("созыв"), cmd_call_admins), G)

    # Варны
    app.add_handler(MessageHandler(cmd("варн", "пред", "предупреждение"), cmd_warn), G)
    app.add_handler(MessageHandler(cmd("варны", "мои варны"), cmd_warns), G)
    app.add_handler(MessageHandler(cmd("-варн"), cmd_unwarn), G)
    app.add_handler(MessageHandler(cmd("снять все варны"), cmd_unwarn_all), G)
    app.add_handler(MessageHandler(cmd("варны лимит", "варны чс", "варны период", "варны действие"), cmd_warn_settings), G)

    # Мут
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("unmute", cmd_unmute))
    app.add_handler(MessageHandler(cmd("мут", "заткнуть"), cmd_mute), G)
    app.add_handler(MessageHandler(cmd("-мут", "снять мут", "размут", "говори"), cmd_unmute), G)

    # Бан
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(MessageHandler(cmd("бан", "чс"), cmd_ban), G)
    app.add_handler(MessageHandler(cmd("разбан", "вернуть"), cmd_unban), G)
    app.add_handler(MessageHandler(cmd("кик неактив"), cmd_kick_inactive), G)
    app.add_handler(MessageHandler(cmd("кик удалённых", "кик удаленных", "кик собак"), cmd_kick_deleted), G)
    app.add_handler(MessageHandler(cmd("кик"), cmd_kick), G)
    app.add_handler(MessageHandler(cmd("банлист"), cmd_banlist), G)
    app.add_handler(MessageHandler(cmd("!амнистия"), cmd_amnesty), G)
    app.add_handler(MessageHandler(cmd("причина"), cmd_ban_reason), G)

    # Настройка чата
    app.add_handler(MessageHandler(cmd("!закреп", "!пин"), cmd_pin), G)
    app.add_handler(MessageHandler(cmd("!открепить", "!анпин"), cmd_unpin), G)
    app.add_handler(MessageHandler(cmd("!название"), cmd_set_title), G)
    app.add_handler(MessageHandler(cmd("+описание чата"), cmd_set_description), G)
    app.add_handler(MessageHandler(cmd("+чат ссылка"), cmd_set_link), G)
    app.add_handler(MessageHandler(cmd("+правила"), cmd_set_rules), G)
    app.add_handler(MessageHandler(cmd("правила"), cmd_get_rules), G)
    app.add_handler(MessageHandler(cmd("+приветствие"), cmd_set_welcome), G)

    # Доступ команд
    app.add_handler(MessageHandler(cmd("доступ команд", "дк"), cmd_dk), G)
    app.add_handler(MessageHandler(cmd(r"\+дк"), lambda u, c: cmd_dk(u, c, action="open")), G)
    app.add_handler(MessageHandler(cmd(r"\-дк"), lambda u, c: cmd_dk(u, c, action="close")), G)
    app.add_handler(MessageHandler(cmd("!сброс команд"), cmd_reset_dk), G)

    # Чистка
    app.add_handler(MessageHandler(cmd("-смс"), cmd_del_msg), G)

    # Статистика (более длинные команды регистрируем первыми!)
    app.add_handler(MessageHandler(cmd("стата бота", "статистика бота"), cmd_bot_stats), G)
    app.add_handler(MessageHandler(cmd("стата", "статистика"), cmd_stats), G)

    # Темы модераторов
    app.add_handler(MessageHandler(cmd("+тема"), cmd_mod_topic_add), G)
    app.add_handler(MessageHandler(cmd("темы"), cmd_mod_topics), G)
    app.add_handler(MessageHandler(cmd("тема"), cmd_mod_topic_view), G)

    # Голосование
    app.add_handler(MessageHandler(cmd("гб"), cmd_vote_ban), G)
    app.add_handler(MessageHandler(cmd("+гк"), cmd_vote_cmd), G)
    app.add_handler(CallbackQueryHandler(cb_ban_vote, pattern=r"^bv_(yes|no)_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_vote_cmd, pattern=r"^vc_(yes|no)$"))

    # Закладки
    app.add_handler(MessageHandler(cmd("+закладка"), cmd_add_bookmark), G)
    app.add_handler(MessageHandler(cmd("чатбук"), cmd_chatbook), G)
    app.add_handler(MessageHandler(cmd("мои закладки"), cmd_my_bookmarks), G)
    app.add_handler(MessageHandler(cmd("удалить закладку", "-закладка"), cmd_del_bookmark), G)

    # Заметки
    app.add_handler(MessageHandler(cmd("+заметка"), cmd_add_note), G)
    app.add_handler(MessageHandler(cmd("-заметка"), cmd_del_note), G)
    app.add_handler(MessageHandler(cmd("~заметка"), cmd_edit_note), G)
    app.add_handler(MessageHandler(cmd("заметки"), cmd_notes), G)
    app.add_handler(MessageHandler(cmd("заметка"), cmd_view_note), G)

    # Таймеры
    app.add_handler(MessageHandler(cmd("таймер через", "таймер на"), cmd_timer), G)
    app.add_handler(MessageHandler(cmd("таймеры"), cmd_timers_list), G)
    app.add_handler(MessageHandler(cmd("-таймер"), cmd_del_timer), G)
    app.add_handler(MessageHandler(cmd("!сбросить таймеры", "!удалить все таймеры"), cmd_reset_timers), G)

    # Анкета
    app.add_handler(MessageHandler(cmd("анкета город", "анкета bio"), cmd_profile_set), G)
    app.add_handler(MessageHandler(cmd("анкета"), cmd_profile_view), G)

    # Реакции
    app.add_handler(MessageHandler(cmd("+реакции"), cmd_allow_reactions), G)
    app.add_handler(MessageHandler(cmd("-реакции"), cmd_deny_reactions), G)

    # Новые участники (приветствие + капча — разные группы, чтобы оба сработали)
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_member), group=1)
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_member_captcha), group=2)

    # Бот добавлен в чат → автоназначение рангов
    app.add_handler(ChatMemberHandler(on_bot_joined, ChatMemberHandler.MY_CHAT_MEMBER))

    # Ручная синхронизация рангов с TG
    app.add_handler(MessageHandler(cmd("синхронизация", "синхронизировать ранги"), cmd_sync_ranks), G)

    # Рейтинг активности
    app.add_handler(MessageHandler(cmd("активность топ", "топ активности", "топ актив"), cmd_activity_top), G)
    app.add_handler(MessageHandler(cmd("моя активность", "моя актив"), cmd_my_activity), G)

    # Автомодерация: антиспам
    app.add_handler(MessageHandler(cmd("антиспам"), cmd_antispam), G)

    # Автомодерация: антифлуд
    app.add_handler(MessageHandler(cmd("антифлуд"), cmd_antiflood), G)

    # Автомодерация: стоп-слова
    app.add_handler(MessageHandler(
        filters.TEXT & filters.Regex(re.compile(r"^[+\-]?стоп.слов", re.IGNORECASE)),
        cmd_badwords,
    ), G)

    # Капча
    app.add_handler(MessageHandler(cmd("капча"), cmd_captcha_settings), G)
    app.add_handler(CallbackQueryHandler(cb_captcha_ok, pattern=r"^captcha_ok:\d+$"))

    # Игры
    app.add_handler(CommandHandler("games", cmd_games))
    app.add_handler(CommandHandler("geotop", cmd_geo_top))
    app.add_handler(MessageHandler(cmd("геотоп", "топ игры", "лидеры"), cmd_geo_top), G)
    app.add_handler(MessageHandler(
        filters.TEXT & filters.Regex(re.compile(r"^[!+\-~/.]*(?:я|кто)\s*$", re.IGNORECASE)),
        cmd_geo_me
    ), G)
    app.add_handler(CallbackQueryHandler(cb_geo_start,  pattern=r"^game_geo_start$"))
    app.add_handler(CallbackQueryHandler(cb_geo_next,   pattern=r"^game_geo_next$"))
    app.add_handler(CallbackQueryHandler(cb_geo_answer, pattern=r"^ga:\d+:\d+$"))
    app.add_handler(CommandHandler("geo_upload", cmd_geo_upload))

    # /panel — панель управления геоигрой (ConversationHandler)
    panel_conv = ConversationHandler(
        entry_points=[
            CommandHandler("panel", cmd_panel),
            CallbackQueryHandler(cb_panel_add,     pattern=r"^panel_add$"),
            CallbackQueryHandler(cb_panel_del,     pattern=r"^panel_del$"),
            CallbackQueryHandler(cb_panel_exp_add, pattern=r"^panel_exp_add$"),
            CallbackQueryHandler(cb_panel_exp_del, pattern=r"^panel_exp_del$"),
            CallbackQueryHandler(cb_panel_bj,      pattern=r"^panel_bj$"),
            CallbackQueryHandler(cb_panel_rou,     pattern=r"^panel_rou$"),
            CallbackQueryHandler(cb_panel_dice,    pattern=r"^panel_dice$"),
            CallbackQueryHandler(cb_panel_profile,   pattern=r"^panel_profile$"),
            CallbackQueryHandler(cb_panel_broadcast,  pattern=r"^panel_broadcast$"),
            CallbackQueryHandler(cb_panel_promo_add,  pattern=r"^panel_promo_add$"),
            CallbackQueryHandler(cb_panel_promo_del,  pattern=r"^panel_promo_del$"),
        ],
        states={
            _PANEL_PHOTO:        [MessageHandler(filters.PHOTO, panel_recv_photo)],
            _PANEL_ANSWER:       [MessageHandler(filters.TEXT & ~filters.COMMAND, panel_recv_answer)],
            _PANEL_WRONGS:       [MessageHandler(filters.TEXT & ~filters.COMMAND, panel_recv_wrongs)],
            _PANEL_DELETE:       [MessageHandler(filters.TEXT & ~filters.COMMAND, panel_recv_delete)],
            _PANEL_EXP_LOC:      [CallbackQueryHandler(cb_exp_loc_select,     pattern=r"^exp_loc_\d+$")],
            _PANEL_EXP_EVENT:    [MessageHandler(filters.TEXT & ~filters.COMMAND, panel_exp_recv_event)],
            _PANEL_EXP_PHOTO:    [MessageHandler(filters.PHOTO, panel_exp_recv_photo)],
            _PANEL_EXP_DEL_LOC:  [CallbackQueryHandler(cb_exp_loc_del_select, pattern=r"^exp_loc_\d+$")],
            _PANEL_EXP_DEL_NUM:  [MessageHandler(filters.TEXT & ~filters.COMMAND, panel_exp_del_num)],
            _PANEL_BJ_MOMENT:    [CallbackQueryHandler(cb_bj_moment_select,   pattern=r"^bj_moment_")],
            _PANEL_BJ_PHOTO:     [MessageHandler(filters.PHOTO, panel_bj_recv_photo)],
            _PANEL_ROU_NUMBER:   [CallbackQueryHandler(cb_rou_number_select,  pattern=r"^rou_num_\d+$")],
            _PANEL_ROU_PHOTO:    [MessageHandler(filters.PHOTO, panel_rou_recv_photo)],
            _PANEL_DICE_MOMENT:  [CallbackQueryHandler(cb_dice_moment_select, pattern=r"^dice_moment_")],
            _PANEL_DICE_PHOTO:   [MessageHandler(filters.PHOTO, panel_dice_recv_photo)],
            _PANEL_PROFILE_PHOTO:[MessageHandler(filters.PHOTO, panel_profile_recv_photo)],
            _PANEL_BROADCAST:    [MessageHandler(filters.TEXT & ~filters.COMMAND, panel_broadcast_recv)],
            _PANEL_PROMO_ADD:    [MessageHandler(filters.TEXT & ~filters.COMMAND, panel_promo_add_recv)],
            _PANEL_PROMO_DEL:    [MessageHandler(filters.TEXT & ~filters.COMMAND, panel_promo_del_recv)],
        },
        fallbacks=[CommandHandler("cancel", panel_cancel)],
        per_chat=True,
        per_user=True,
        name="panel_conv",
        allow_reentry=True,
    )
    app.add_handler(panel_conv, group=-2)

    # Управление гео-фото (только главная группа, ранг 3+)
    app.add_handler(MessageHandler(
        filters.PHOTO & filters.CaptionRegex(re.compile(r"^\+геофото\b", re.IGNORECASE)),
        cmd_geo_add_photo,
    ), G)
    app.add_handler(MessageHandler(cmd("+геофото"), cmd_geo_add_photo), G)
    app.add_handler(MessageHandler(cmd("-геофото"), cmd_geo_del_photo), G)
    app.add_handler(MessageHandler(cmd("геофото"), cmd_geo_list_photos), G)

    # ── Экономика ──────────────────────────────────────────────────────────────
    app.add_handler(MessageHandler(cmd("+ник"), cmd_set_nick), G)
    app.add_handler(MessageHandler(cmd("-ник"), cmd_del_nick), G)
    app.add_handler(MessageHandler(cmd("никлист", "nlist", "nicklist"), cmd_nicklist), G)
    app.add_handler(MessageHandler(cmd("промокод", "promo"), cmd_promo), G)
    app.add_handler(MessageHandler(
        filters.TEXT & filters.Regex(re.compile(r"^[!+\-~/.]*(?:все|@все)\s*$", re.IGNORECASE)),
        cmd_mention_all
    ), G)
    app.add_handler(MessageHandler(cmd("фарм"), cmd_farm), G)
    app.add_handler(MessageHandler(cmd("баланс", "кошелёк"), cmd_balance), G)
    app.add_handler(MessageHandler(cmd("обмен"), cmd_exchange), G)
    app.add_handler(MessageHandler(cmd("блекджек", "блэкджек"), cmd_blackjack), G)
    app.add_handler(MessageHandler(cmd("рулетка"), cmd_roulette), G)
    app.add_handler(MessageHandler(cmd("кости"), cmd_dice), G)
    app.add_handler(CallbackQueryHandler(cb_bj,  pattern=r"^bj_(hit|stand)_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_exp_go, pattern=r"^exp_go_\d+_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_exp_confirm, pattern=r"^exp_confirm_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_shop_buy, pattern=r"^shop_buy_\d+_.+_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_exp_cancel, pattern=r"^exp_cancel_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_help_nav, pattern=r"^help_"))
    app.add_handler(MessageHandler(cmd("магазин"), cmd_shop), G)
    app.add_handler(MessageHandler(cmd("купить"), cmd_buy), G)
    app.add_handler(MessageHandler(cmd("инвентарь", "инв"), cmd_inventory), G)
    app.add_handler(MessageHandler(cmd("сейф"), cmd_open_safe), G)
    app.add_handler(MessageHandler(cmd("сундук"), cmd_open_chest), G)
    app.add_handler(MessageHandler(cmd("амулет"), cmd_amulet), G)
    app.add_handler(MessageHandler(cmd("украсть"), cmd_steal), G)
    app.add_handler(MessageHandler(cmd("экспедиция", "поход"), cmd_expedition), G)
    app.add_handler(MessageHandler(cmd("дуэль"), cmd_duel), G)
    app.add_handler(MessageHandler(cmd("крестики", "кнн"), cmd_ttt), G)
    app.add_handler(CallbackQueryHandler(cb_ttt, pattern=r"^ttt_"))
    app.add_handler(MessageHandler(cmd("топ богатых", "топ богачей"), cmd_rich_top), G)
    app.add_handler(MessageHandler(cmd("выдать"), cmd_give_currency), G)
    app.add_handler(CallbackQueryHandler(cb_duel,  pattern=r"^duel_[ad]_\d+_\d+_\d+$"))



# ═══════════════════════════════════════════════════════════════════════════════
# ЭКОНОМИКА: САМОРОДКИ, МОНЕТЫ, ЭКСПЕДИЦИЯ, КАЗИНО, МАГАЗИН, ДУЭЛИ
# ═══════════════════════════════════════════════════════════════════════════════

import random as _random

# ─────────────────────────── ДАННЫЕ ЭКСПЕДИЦИЙ ───────────────────────────────

# Предметы которые могут выпасть
ITEM_COMPAS      = "компас"
ITEM_ENERGY      = "энергетик"
ITEM_GUIDE       = "руководство по краже"
ITEM_AMULET      = "амулет"
ITEM_KEY         = "ключ"
ITEM_SAFE        = "сейф"
ITEM_CHEST       = "сундук"
ITEM_MAP         = "карта"

# Эмодзи предметов
ITEM_EMOJI = {
    ITEM_COMPAS:  "🧭",
    ITEM_ENERGY:  "🥤",
    ITEM_GUIDE:   "📖",
    ITEM_AMULET:  "🧿",
    ITEM_KEY:     "🔑",
    ITEM_SAFE:    "🗄️",
    ITEM_CHEST:   "📦",
    ITEM_MAP:     "🗺️",
}

# Цены в магазине (монеты)
SHOP_PRICES = {
    ITEM_ENERGY: 40,
    ITEM_COMPAS: 60,
}

# ─── Локации ───────────────────────────────────────────────────────────────────
# Каждое событие: (текст, nuggets, coins, items, weight)
# items = список предметов которые добавляются в инвентарь
# weight = вес для random.choices (выше = чаще)

LOCATIONS = {
    0: {
        "name": "🌲 Лес",
        "events": [
            # Нейтральные
            ("{u} заблудился в лесу и не смог вынести ничего хорошего.", 0, 0, [], 8),
            ("{u} услышал странный вой в глубине леса и поспешил домой.", 0, 0, [], 6),
            ("{u} заметил силуэт между деревьями, но он исчез.", 0, 0, [], 6),
            ("{u} нашёл камень с древними символами, не заметил и выкинул в озеро.", 0, 0, [], 5),
            ("{u} увидел, как над болотом вспыхнули странные огни.", 0, 0, [], 5),
            ("{u} услышал треск веток за спиной и убежал в глубь леса.", 0, 0, [], 5),
            ("{u} встретил агрессивного кабана и еле убежал.", 0, 0, [], 5),
            ("{u} нашёл ручей с чистой водой и отдохнул.", 0, 0, [], 5),
            ("{u} услышал шум в кустах и убежал от страха.", 0, 0, [], 5),
            ("{u} долго бродил по лесу, но ничего интересного не нашёл.", 0, 0, [], 5),
            # Плохие
            ("{u} упал в яму и травмировался.", 0, -10, [], 7),
            ("{u} был растерзан волками.", 0, -10, [], 6),
            ("{u} провалился в охотничью яму.", 0, -20, [], 5),
            ("{u} встретил стаю волков и потерял часть добычи.", -5, 0, [], 5),
            ("{u} съел странные ягоды и почувствовал слабость.", 0, -15, [], 5),
            ("{u} заблудился в лесу и потратил все припасы.", 0, -25, [], 4),
            ("{u} наступил на капкан охотника.", 0, -20, [], 4),
            ("{u} полез за мёдом и был искусан пчёлами.", 0, -10, [], 5),
            # Хорошие
            ("{u} шёл по лесу и нашёл пещеру с горой самородков.", 15, 0, [], 6),
            ("{u} свернул не той тропой, но нашёл мешочек монет.", 0, 20, [], 6),
            ("{u} нашёл заброшенный домик лесника.", 0, 20, [ITEM_COMPAS], 4),
            ("{u} наткнулся на грибную поляну и продал грибы на рынке.", 0, 20, [], 6),
            ("{u} нашёл мешочек с самородками под деревом.", 10, 0, [], 6),
            ("{u} нашёл золотой самородок невероятного размера.", 15, 0, [], 4),
            ("{u} наткнулся на легендарное дерево желаний.", 0, 100, [], 2),
            ("{u} нашёл под деревом мешочек с монетами.", 0, 15, [], 6),
            ("{u} собрал ягод и продал их на рынке.", 0, 20, [], 6),
            ("{u} наткнулся на старую охотничью тропу и нашёл пару самородков.", 3, 0, [], 6),
            ("{u} увидел белку с блестящей монетой и сумел её поймать.", 0, 10, [], 6),
            ("{u} нашёл заброшенный лагерь путешественников.", 0, 25, [], 5),
            ("{u} нашёл дерево с ульем и добыл немного мёда.", 0, 15, [], 5),
            ("{u} наткнулся на старый костёр и нашёл возле него пару монет.", 0, 10, [], 6),
            ("{u} выкопал маленький железный сейф возле хижины.", 0, 0, [ITEM_SAFE], 3),
            ("{u} нашёл древний амулет друида.", 0, 0, [ITEM_AMULET], 3),
            # Книги
            ("{u} нашёл старую хижину и обнаружил на столе подозрительную книгу.", 0, 0, [ITEM_GUIDE], 4),
            ("{u} нашёл библиотеку в старом заброшенном доме.", 0, 0, [ITEM_GUIDE], 4),
            ("{u} нашёл странную книгу в кустах.", 0, 0, [ITEM_GUIDE], 4),
            ("{u} набрёл на домик лесника — на столе лежала интересная книга.", 0, 0, [ITEM_GUIDE], 4),
            # Карта (редко)
            ("{u} шёл по лесу и встретил старого картографа, который подарил карту.", 0, 0, [ITEM_MAP], 1),
        ],
    },
    1: {
        "name": "🏚️ Заброшенная деревня",
        "events": [
            # Нейтральные
            ("{u} пришёл в заброшенную деревню, увидел вдали жителя и упал в яму.", 0, 0, [], 6),
            ("{u} испугался что встретит призрака и убежал.", 0, 0, [], 6),
            ("{u} зашёл в дом, испугался паука и убежал.", 0, 0, [], 6),
            ("{u} услышал шаги в пустом доме.", 0, 0, [], 5),
            ("{u} увидел силуэт в окне заброшенной школы.", 0, 0, [], 5),
            ("{u} нашёл детскую игрушку посреди улицы.", 0, 0, [], 5),
            ("{u} заметил, как в одном из домов внезапно загорелся свет — там оказался бомж.", 0, 0, [], 5),
            # Плохие
            ("{u} попил воды из старого колодца и отравился.", 0, -20, [], 6),
            ("{u} встретил стаю диких собак и потерял часть добычи.", 0, -15, [], 5),
            ("{u} случайно разбил старую лампу и устроил пожар.", 0, -25, [], 4),
            ("{u} испугался странного шума и уронил монеты в грязь.", 0, -10, [], 6),
            ("{u} полез в старый подвал и оказался в ловушке.", 0, -30, [], 4),
            ("{u} зашёл в разрушенный дом, и на него упала балка.", 0, -20, [], 5),
            ("{u} провалился через прогнивший пол.", 0, -20, [], 5),
            ("{u} шёл по деревне и потерял пару самородков, но чуть позже нашёл мешочек монет.", -5, 15, [], 5),
            # Хорошие
            ("{u} разведал обстановку в одном из заброшенных домов и нашёл горстку монет.", 0, 20, [], 6),
            ("{u} нашёл подвал с соленьями и продал пару банок на рынке.", 0, 30, [], 5),
            ("{u} нашёл под деревом мешочек с монетами.", 0, 15, [], 6),
            ("{u} собрал ягод и продал их на рынке.", 0, 20, [], 6),
            ("{u} наткнулся на старую охотничью тропу и нашёл пару самородков.", 4, 0, [], 6),
            ("{u} увидел белку с блестящей монетой и сумел её поймать.", 0, 10, [], 6),
            ("{u} нашёл древний амулет в одном из заброшенных домов.", 0, 0, [ITEM_AMULET], 3),
            ("{u} нашёл заброшенный лагерь путешественников.", 0, 25, [], 5),
            ("{u} нашёл дерево с ульем и добыл немного мёда.", 0, 15, [], 5),
            ("{u} наткнулся на старый костёр и нашёл возле него пару монет.", 0, 10, [], 6),
            ("{u} нашёл старый пыльный сейф в одном из домов.", 0, 0, [ITEM_SAFE], 3),
            ("{u} нашёл в доме старый гнилой сундук с вещами.", 0, 0, [ITEM_COMPAS, ITEM_ENERGY], 3),
            ("{u} нашёл потрёпанную карту местности и обменял на рынке на пару монет.", 0, 10, [], 5),
            ("{u} выкопал сундук возле разрушенного дома.", 0, 0, [ITEM_CHEST], 3),
            ("{u} обнаружил связку ржавых ключей.", 0, 0, [ITEM_KEY], 4),
            # Книги
            ("{u} зашёл в дом и нашёл на столе интересную книгу.", 0, 0, [ITEM_GUIDE], 4),
            ("{u} встретил странника — тот подарил книгу в обмен на пару монет.", 0, -10, [ITEM_GUIDE], 4),
            ("{u} зашёл в заброшенную библиотеку и нашёл пыльную книгу.", 0, 0, [ITEM_GUIDE], 4),
            ("{u} нашёл почти уничтоженную книгу, но смог разобрать что там написано.", 0, 0, [ITEM_GUIDE], 4),
            # Карта (редко)
            ("{u} услышал тихий шёпот со стороны колодца — там оказался охрипший путник. Вытащил его и отвёл в больницу, тот наградил картой и самородками.", 40, 0, [ITEM_MAP], 1),
        ],
    },
    2: {
        "name": "⚓ Пиратский порт",
        "events": [
            # Нейтральные
            ("{u} попытался открыть бочку с ромом, но получил по голове крышкой.", 0, 0, [], 5),
            ("{u} нашёл старую карту, но она рассыпалась в руках.", 0, 0, [], 5),
            ("{u} увидел силуэт корабля-призрака в тумане и ушёл.", 0, 0, [], 5),
            ("{u} услышал странное пение со стороны моря, подумал что это русалки, и убежал.", 0, 0, [], 5),
            ("{u} заметил, как кто-то следит за ним из переулка, ушёл оглядываясь назад.", 0, 0, [], 5),
            ("{u} нашёл старую пиратскую шляпу и примерил её.", 0, 0, [], 4),
            ("{u} услышал легенду о сокровищах Чёрного капитана.", 0, 0, [], 4),
            # Плохие
            ("{u} хотел обворовать пирата и попался.", -5, -30, [], 6),
            ("{u} тайно прокрался на один из кораблей и был пойман с поличным.", -10, 0, [], 6),
            ("{u} случайно наступил на ржавый гвоздь и потратился на лечение.", 0, -15, [], 6),
            ("{u} ввязался в драку в таверне.", 0, -20, [], 5),
            ("{u} поскользнулся на мокром пирсе и уронил монеты в воду.", 0, -25, [], 5),
            ("{u} попал под шторм и потерял часть добычи.", -5, 0, [], 5),
            ("{u} открыл подозрительный сундук, который оказался ловушкой.", 0, -30, [], 4),
            ("{u} встретил карманника на рынке.", 0, -15, [], 5),
            ("{u} попытался обокрасть пирата и был пойман.", 0, -35, [], 4),
            # Хорошие
            ("{u} тихо прокрался в пиратский порт и обворовал одного из пиратов.", 0, 50, [ITEM_COMPAS], 4),
            ("{u} переоделся в пирата, прокрался на корабль и нашёл сундук с наживой.", 10, 40, [], 4),
            ("{u} нашёл под пирсом старый кошелёк моряка.", 0, 15, [], 6),
            ("{u} помог пьяному пирату дойти до таверны.", 0, 10, [], 6),
            ("{u} услышал шум в переулке и нашёл мешочек монет.", 0, 20, [], 6),
            ("{u} продал найденную ракушку коллекционеру.", 0, 25, [], 5),
            ("{u} зашёл в старую лавку и нашёл пару самородков.", 2, 0, [], 6),
            ("{u} рыбачил у причала и выловил старую монету.", 0, 10, [], 6),
            ("{u} заблудился в тумане и вышел прямо к рынку.", 0, 10, [], 5),
            ("{u} выиграл спор у старого капитана.", 0, 40, [], 4),
            ("{u} нашёл древний золотой дублон.", 0, 15, [], 5),
            ("{u} нашёл тайник контрабандистов.", 0, 50, [], 3),
            ("{u} пробрался на заброшенный корабль и нашёл мешок самородков.", 7, 0, [], 4),
            ("{u} нашёл старый револьвер и обменял его на пару самородков.", 10, 0, [], 4),
            ("{u} нашёл сейф возле старого склада.", 0, 0, [ITEM_SAFE], 3),
            ("{u} обнаружил сундук, закопанный возле маяка.", 0, 0, [ITEM_CHEST], 3),
            ("{u} нашёл редкий пиратский компас.", 0, 0, [ITEM_COMPAS], 3),
            ("{u} откопал сундук с припасами.", 0, 20, [ITEM_ENERGY], 3),
            ("{u} нашёл связку старых ключей.", 0, 0, [ITEM_KEY], 4),
            ("{u} нашёл закрытую дверь и открыл её при помощи ключа — внутри оказался сундук.", 50, 70, [], 0),  # спец событие с ключом
            # Книги
            ("{u} прокрался в кабинет одного из пиратов и украл интересную книгу.", 0, 0, [ITEM_GUIDE], 4),
            ("{u} выменял книгу на пару самородков у одного из пиратов.", -5, 0, [ITEM_GUIDE], 4),
            ("{u} встретил тихо крадущегося вора и побил его, забрав книгу.", 0, 0, [ITEM_GUIDE], 4),
            ("{u} зашёл на корабль, забрёл в каюту капитана и нашёл там странную пыльную книгу.", 0, 0, [ITEM_GUIDE], 4),
            # Карта (редко)
            ("{u} заметил в воде бутылку с картой сокровищ.", 0, 0, [ITEM_MAP], 1),
        ],
    },
    3: {
        "name": "🌋 Древние руины",
        "events": [
            # Нейтральные
            ("{u} услышал странный гул из глубины руин.", 0, 0, [], 5),
            ("{u} заметил силуэт в разрушенном проходе.", 0, 0, [], 5),
            ("{u} увидел древние символы, светящиеся на стенах.", 0, 0, [], 5),
            ("{u} почувствовал холодный ветер из подземелья.", 0, 0, [], 5),
            ("{u} услышал звук шагов, хотя рядом никого не было.", 0, 0, [], 5),
            ("{u} попытался сдвинуть древний камень, но он оказался слишком тяжёлым.", 0, 0, [], 5),
            ("{u} нашёл старую табличку с непонятными символами.", 0, 0, [], 5),
            # Плохие
            ("{u} наступил на древнюю ловушку.", 0, -25, [], 6),
            ("{u} провалился в трещину между плитами.", 0, -20, [], 5),
            ("{u} задел старую колонну, и часть руин обрушилась.", 0, -30, [], 4),
            ("{u} встретил ядовитую змею среди камней.", 0, -15, [], 6),
            ("{u} заблудился в подземных проходах и потерял добычу.", -3, 0, [], 5),
            ("{u} попытался открыть древний сундук, но сработала ловушка.", 0, -35, [], 4),
            ("{u} вдохнул странную пыль и почувствовал слабость.", 0, -10, [], 6),
            # Хорошие
            ("{u} нашёл старые монеты среди обломков колонн.", 0, 20, [], 6),
            ("{u} обследовал руины и нашёл пару самородков.", 3, 0, [], 6),
            ("{u} откопал древнюю вазу и продал её коллекционеру.", 0, 25, [], 5),
            ("{u} нашёл заброшенный лагерь исследователей.", 0, 35, [], 5),
            ("{u} нашёл разрушенный алтарь с мешочком монет.", 0, 30, [], 5),
            ("{u} обнаружил древний факел и осветил проход — там нашлись монеты.", 0, 30, [], 5),
            ("{u} нашёл скрытую комнату с сокровищами.", 0, 60, [], 3),
            ("{u} обнаружил тайник древних жителей.", 7, 0, [], 4),
            ("{u} нашёл редкий древний амулет.", 0, 0, [ITEM_AMULET], 3),
            ("{u} пробрался в подземелье и нашёл сундук с припасами.", 0, 0, [ITEM_CHEST], 3),
            ("{u} обнаружил старый компас путешественника.", 0, 0, [ITEM_COMPAS], 3),
            ("{u} нашёл забытый рюкзак исследователя.", 0, 20, [ITEM_ENERGY], 3),
            ("{u} обнаружил древний сейф под завалами.", 0, 0, [ITEM_SAFE], 3),
            ("{u} нашёл древний ключ с символами.", 0, 0, [ITEM_KEY], 4),
            ("{u} выкопал запечатанный сундук из песка.", 0, 0, [ITEM_CHEST], 3),
            ("{u} открыл потайную комнату с золотыми самородками.", 20, 0, [], 3),
            ("{u} нашёл потрёпанную карту руин.", 0, 0, [ITEM_MAP], 2),
            # Специальное: механизм с ключом (50% шанс двери)
            ("{u} случайно активировал старый механизм...", 0, 0, [], 0),  # обрабатывается отдельно
            # Книги
            ("{u} нашёл странную книгу в одном из разрушенных домов.", 0, 0, [ITEM_GUIDE, ITEM_GUIDE], 3),
            ("{u} нашёл странную библиотеку и взял пару книг.", 0, 0, [ITEM_GUIDE, ITEM_GUIDE], 3),
            ("{u} наткнулся на лежащую на полу книгу.", 0, 0, [ITEM_GUIDE], 4),
            ("{u} встретил странника с интересной книгой.", -10, 0, [ITEM_GUIDE], 4),
            # Карта (редко)
            ("{u} нашёл старый свиток с картой руин.", 0, 0, [ITEM_MAP], 1),
        ],
    },
}

# Курс обмена самородков в монеты
NUGGETS_TO_COINS_RATE = 5  # 1 самородок = 5 монет

# Активные экспедиции: {user_id: timestamp_окончания}
_active_expeditions: dict[int, float] = {}
EXPEDITION_DURATION = 30  # секунд

# ─────────────────────────── ВСПОМОГАТЕЛЬНЫЕ ─────────────────────────────────

def _in_expedition(user_id: int) -> bool:
    """Проверяет находится ли игрок в экспедиции прямо сейчас."""
    import time
    end_time = _active_expeditions.get(user_id)
    if end_time is None:
        return False
    if time.time() > end_time:
        _active_expeditions.pop(user_id, None)
        return False
    return True


def _expedition_guard(func):
    """Декоратор: блокирует экономические команды во время экспедиции."""
    import functools
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user and _in_expedition(user.id):
            import time
            end_time = _active_expeditions.get(user.id, 0)
            secs_left = max(0, int(end_time - time.time()))
            await update.message.reply_text(
                f"🏃 Ты сейчас в экспедиции! Подожди ещё ~{secs_left} сек."
            )
            return
        return await func(update, context)
    return wrapper


def _econ_get(user_id: int) -> dict:
    """Возвращает запись игрока или создаёт новую."""
    conn = db_connect()
    row = conn.execute(
        "SELECT * FROM economy WHERE user_id=?", (user_id,)
    ).fetchone()
    if not row:
        conn.execute(
            "INSERT OR IGNORE INTO economy (user_id) VALUES (?)", (user_id,)
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM economy WHERE user_id=?", (user_id,)
        ).fetchone()
    conn.close()
    return dict(row)


def _econ_update(user_id: int, **kwargs) -> None:
    """Обновляет поля записи игрока."""
    if not kwargs:
        return
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [user_id]
    conn = db_connect()
    conn.execute(f"UPDATE economy SET {sets} WHERE user_id=?", vals)
    conn.commit()
    conn.close()


def _inv_get(user_id: int) -> dict:
    """Возвращает инвентарь игрока {item: count}."""
    conn = db_connect()
    rows = conn.execute(
        "SELECT item, qty FROM inventory WHERE user_id=?", (user_id,)
    ).fetchall()
    conn.close()
    return {r["item"]: r["qty"] for r in rows}


def _inv_add(user_id: int, item: str, qty: int = 1) -> None:
    conn = db_connect()
    conn.execute(
        "INSERT INTO inventory (user_id, item, qty) VALUES (?,?,?) "
        "ON CONFLICT(user_id, item) DO UPDATE SET qty=qty+?",
        (user_id, item, qty, qty),
    )
    conn.commit()
    conn.close()


def _inv_remove(user_id: int, item: str, qty: int = 1) -> bool:
    """Убирает предмет из инвентаря. Возвращает True если успешно."""
    conn = db_connect()
    row = conn.execute(
        "SELECT qty FROM inventory WHERE user_id=? AND item=?", (user_id, item)
    ).fetchone()
    if not row or row["qty"] < qty:
        conn.close()
        return False
    if row["qty"] == qty:
        conn.execute("DELETE FROM inventory WHERE user_id=? AND item=?", (user_id, item))
    else:
        conn.execute(
            "UPDATE inventory SET qty=qty-? WHERE user_id=? AND item=?", (qty, user_id, item)
        )
    conn.commit()
    conn.close()
    return True


def _fmt_items(items: list) -> str:
    """Форматирует список предметов для сообщения."""
    if not items:
        return ""
    parts = []
    counted = {}
    for it in items:
        counted[it] = counted.get(it, 0) + 1
    for it, cnt in counted.items():
        emoji = ITEM_EMOJI.get(it, "📦")
        parts.append(f"{emoji} {it}" + (f" x{cnt}" if cnt > 1 else ""))
    return " + ".join(parts)


def _mention(user) -> str:
    return user.mention_html()


# ─────────────────────────── КОМАНДЫ ─────────────────────────────────────────

@_expedition_guard
async def cmd_farm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`фарм` — раз в день получить 1-10 самородков."""
    _log_game_cmd("фарм")
    user = update.effective_user
    data = _econ_get(user.id)

    last = data.get("last_farm")
    now_str = _msk_day()
    if last == now_str:
        await update.message.reply_text(
            f"⛏️ {_mention(user)}, ты уже фармил сегодня! Приходи завтра.",
            parse_mode="HTML",
        )
        return

    amount = _random.randint(1, 10)
    new_nuggets = data["nuggets"] + amount
    _econ_update(user.id, nuggets=new_nuggets, last_farm=_msk_day())
    await update.message.reply_text(
        f"⛏️ {_mention(user)} отправился на поиски и нашёл "
        f"<b>{amount} самородк{'ов' if amount != 1 else ''}!</b>\n"
        f"💰 Баланс: {new_nuggets} 🪨",
        parse_mode="HTML",
    )


@_expedition_guard
async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`баланс` — показать баланс."""
    user = update.effective_user
    data = _econ_get(user.id)
    await update.message.reply_text(
        f"💼 Баланс {_mention(user)}:\n"
        f"🪨 Самородки: <b>{data['nuggets']}</b>\n"
        f"🪙 Монеты: <b>{data['coins']}</b>",
        parse_mode="HTML",
    )


@_expedition_guard
async def cmd_exchange(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`обмен N` — обменять N самородков на монеты."""
    user = update.effective_user
    args = (update.message.text or "").split()
    if len(args) < 2 or not args[1].isdigit():
        await update.message.reply_text(
            f"Формат: обмен <количество>\n"
            f"Курс: 1 🪨 = {NUGGETS_TO_COINS_RATE} 🪙"
        )
        return
    amount = int(args[1])
    if amount <= 0:
        await update.message.reply_text("Укажи количество больше нуля.")
        return
    data = _econ_get(user.id)
    if data["nuggets"] < amount:
        await update.message.reply_text(
            f"Недостаточно самородков. У тебя: {data['nuggets']} 🪨"
        )
        return
    coins_gain = amount * NUGGETS_TO_COINS_RATE
    _econ_update(
        user.id,
        nuggets=data["nuggets"] - amount,
        coins=data["coins"] + coins_gain,
    )
    await update.message.reply_text(
        f"💱 {_mention(user)} обменял <b>{amount} 🪨</b> → <b>{coins_gain} 🪙</b>",
        parse_mode="HTML",
    )


# ─────────────────────────── БЛЕКДЖЕК ────────────────────────────────────────

# ── Колода с мастями ─────────────────────────────────────────────────────────
_BJ_SUITS  = ["♠️", "♥️", "♦️", "♣️"]
_BJ_FACES  = {10: ["10", "Валет", "Дама", "Король"], 11: ["Туз"]}

def _bj_card() -> tuple:
    """Возвращает (value, name_with_suit)."""
    suit = _random.choice(_BJ_SUITS)
    # 2-9 → числовая карта
    # 10 → 10, Валет, Дама, Король (каждый с шансом 1/4)
    # 11 → Туз
    roll = _random.randint(1, 13)
    if roll <= 9:
        v = roll + 1   # 2..10
        if v == 10:
            face = "10"
        else:
            face = str(v)
        return (v, f"{face}{suit}")
    elif roll <= 12:
        face_names = ["Валет", "Дама", "Король"]
        face = face_names[roll - 10]
        return (10, f"{face}{suit}")
    else:
        return (11, f"Туз{suit}")

def _bj_hand_value(cards: list) -> int:
    """Считает сумму руки (cards — список tuple или int)."""
    vals = [c[0] if isinstance(c, tuple) else c for c in cards]
    total = sum(vals)
    aces = vals.count(11)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total

def _bj_hand_str(cards: list) -> str:
    parts = [c[1] if isinstance(c, tuple) else str(c) for c in cards]
    return " + ".join(parts)

# Активные партии блекджека: {user_id: {bet, player, dealer_hidden, dealer_open}}
def _log_game_cmd(cmd: str) -> None:
    """Логирует использование игровой команды."""
    try:
        conn = db_connect()
        conn.execute("INSERT INTO game_log (cmd) VALUES (?)", (cmd,))
        conn.commit()
        conn.close()
    except Exception:
        pass


_bj_games: dict = {}

@_expedition_guard
async def cmd_blackjack(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`блекджек N` — начать партию блекджека на N монет."""
    _log_game_cmd("блекджек")
    user = update.effective_user
    args = (update.message.text or "").split()
    if len(args) < 2 or not args[1].isdigit():
        await update.message.reply_text("Формат: блекджек <ставка>")
        return
    bet = int(args[1])
    if bet <= 0:
        await update.message.reply_text("Ставка должна быть больше нуля.")
        return
    data = _econ_get(user.id)
    if data["coins"] < bet:
        await update.message.reply_text(f"Недостаточно монет. У тебя: {data['coins']} 🪙")
        return
    if user.id in _bj_games:
        await update.message.reply_text("У тебя уже идёт партия! Заверши её: взять или стоп.")
        return

    p = [_bj_card(), _bj_card()]
    d = [_bj_card(), _bj_card()]
    _bj_games[user.id] = {"bet": bet, "player": p, "dealer": d}
    _econ_update(user.id, coins=data["coins"] - bet)

    pv = _bj_hand_value(p[:])
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🃏 Взять карту", callback_data=f"bj_hit_{user.id}"),
        InlineKeyboardButton("✋ Стоп",         callback_data=f"bj_stand_{user.id}"),
    ]])

    # Фото блекджека если загружено
    conn = db_connect()
    photo = conn.execute("SELECT file_id FROM bj_photos WHERE moment='start'").fetchone()
    conn.close()

    dealer_show = d[0][1] if isinstance(d[0], tuple) else str(d[0])
    text = (
        f"🃏 <b>Блекджек</b> — ставка {bet} 🪙\n\n"
        f"Твои карты: {_bj_hand_str(p)} = <b>{pv}</b>\n"
        f"Дилер: {dealer_show} + 🂠\n\n"
        f"Взять ещё карту или остановиться?"
    )
    if photo:
        await update.message.reply_photo(photo["file_id"], caption=text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def cb_bj(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка ходов блекджека."""
    query = update.callback_query
    await query.answer()
    parts   = query.data.split("_")
    action  = parts[1]   # hit или stand
    user_id = int(parts[2])

    if query.from_user.id != user_id:
        await query.answer("Это не твоя игра.", show_alert=True)
        return

    game = _bj_games.get(user_id)
    if not game:
        await query.edit_message_text("Игра не найдена.")
        return

    player = game["player"]
    dealer = game["dealer"]
    bet    = game["bet"]

    if action == "hit":
        player.append(_bj_card())
        pv = _bj_hand_value(player[:])
        if pv > 21:
            # Перебор
            del _bj_games[user_id]
            conn = db_connect()
            photo = conn.execute("SELECT file_id FROM bj_photos WHERE moment='bust'").fetchone()
            conn.close()
            text = (
                f"💥 <b>Перебор!</b>\n\n"
                f"Твои карты: {_bj_hand_str(player)} = {pv}\n"
                f"−{bet} 🪙 (ставка сгорела)"
            )
            if photo:
                await query.message.reply_photo(photo["file_id"], caption=text, parse_mode="HTML")
                await query.delete_message()
            else:
                await query.edit_message_text(text, parse_mode="HTML")
            return

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🃏 Взять карту", callback_data=f"bj_hit_{user_id}"),
            InlineKeyboardButton("✋ Стоп",         callback_data=f"bj_stand_{user_id}"),
        ]])
        dealer_show = dealer[0][1] if isinstance(dealer[0], tuple) else str(dealer[0])
        text = (
            f"🃏 <b>Блекджек</b> — ставка {bet} 🪙\n\n"
            f"Твои карты: {_bj_hand_str(player)} = <b>{pv}</b>\n"
            f"Дилер: {dealer_show} + 🂠"
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
        return

    # stand — ход дилера
    del _bj_games[user_id]
    while _bj_hand_value(dealer[:]) < 17:
        dealer.append(_bj_card())

    pv = _bj_hand_value(player[:])
    dv = _bj_hand_value(dealer[:])
    data = _econ_get(user_id)

    if dv > 21 or pv > dv:
        winnings = bet * 2
        _econ_update(user_id, coins=data["coins"] + winnings)
        moment = "win"
        result = f"🏆 <b>Победа!</b> +{bet} 🪙"
    elif pv == dv:
        _econ_update(user_id, coins=data["coins"] + bet)
        moment = "tie"
        result = "🤝 <b>Ничья.</b> Ставка возвращена."
    else:
        moment = "lose"
        result = f"😞 <b>Поражение.</b> -{bet} 🪙"

    conn = db_connect()
    photo = conn.execute("SELECT file_id FROM bj_photos WHERE moment=?", (moment,)).fetchone()
    conn.close()

    dv_str = f"{dv}" if dv <= 21 else f"<s>{dv}</s> 💥перебор"
    text = (
        f"🃏 <b>Блекджек — итог</b>\n\n"
        f"Твои карты: {_bj_hand_str(player)} = <b>{pv}</b>\n"
        f"Дилер: {_bj_hand_str(dealer)} = <b>{dv_str}</b>\n\n"
        f"{result}"
    )
    if photo:
        await query.message.reply_photo(photo["file_id"], caption=text, parse_mode="HTML")
        await query.delete_message()
    else:
        await query.edit_message_text(text, parse_mode="HTML")


# ─────────────────────────── РУЛЕТКА ─────────────────────────────────────────


_ROULETTE_RED = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
_ROULETTE_BLACK = {2,4,6,8,10,11,13,15,17,20,22,24,26,28,29,31,33,35}

def _roulette_color(n: int) -> str:
    if n == 0:
        return "зелёный 🟢"
    return "красный 🔴" if n in _ROULETTE_RED else "чёрный ⚫"

async def cmd_roulette(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`рулетка <ставка> <тип>` или `рулетка <тип> <ставка>` — поставить."""
    _log_game_cmd("рулетка")
    user = update.effective_user
    args = (update.message.text or "").split()

    bet = None
    bet_type = None
    for a in args[1:]:
        if a.isdigit() and bet is None:
            bet = int(a)
        elif not a.isdigit() and bet_type is None:
            bet_type = a.lower()

    if bet is None or bet_type is None:
        await update.message.reply_text(
            "Формат: <code>рулетка &lt;ставка&gt; &lt;тип&gt;</code>\n\n"
            "Типы ставок:\n"
            "• <code>чёт</code> / <code>нечёт</code>\n"
            "• <code>красный</code> / <code>чёрный</code>",
            parse_mode="HTML",
        )
        return

    VALID_BETS = {"чёт", "чет", "нечет", "нечёт", "красный", "красное", "чёрный", "черный", "черное", "чёрное"}
    if bet_type not in VALID_BETS:
        await update.message.reply_text(
            "Неверный тип ставки.\n"
            "Доступно: <code>чёт</code>, <code>нечёт</code>, <code>красный</code>, <code>чёрный</code>",
            parse_mode="HTML",
        )
        return

    if bet <= 0:
        await update.message.reply_text("Ставка должна быть больше нуля.")
        return

    data = _econ_get(user.id)
    if data["coins"] < bet:
        await update.message.reply_text(f"Недостаточно монет. У тебя: {data['coins']} 🪙")
        return

    result = _random.randint(0, 36)
    color = _roulette_color(result)

    won = False
    if bet_type in ("чёт", "чет"):
        won = result != 0 and result % 2 == 0
    elif bet_type in ("нечет", "нечёт"):
        won = result % 2 == 1
    elif bet_type in ("красный", "красное"):
        won = result in _ROULETTE_RED
    elif bet_type in ("черный", "чёрный", "черное", "чёрное"):
        won = result in _ROULETTE_BLACK

    if won:
        win = bet
        new_coins = data["coins"] + win
        _econ_update(user.id, coins=new_coins)
        text = (
            f"🎡 Шарик остановился на <b>{result}</b> ({color})\n\n"
            f"🏆 Ты выиграл! +{win} 🪙\n"
            f"Баланс: {new_coins} 🪙"
        )
    else:
        new_coins = data["coins"] - bet
        _econ_update(user.id, coins=new_coins)
        text = (
            f"🎡 Шарик остановился на <b>{result}</b> ({color})\n\n"
            f"😞 Не повезло. -{bet} 🪙\n"
            f"Баланс: {new_coins} 🪙"
        )

    # Определяем категорию фото
    if result == 0:
        rou_category = "zero"
    elif won:
        rou_category = "win"
    else:
        rou_category = "lose"

    conn = db_connect()
    photo = conn.execute(
        "SELECT file_id FROM roulette_photos WHERE number=?", (rou_category,)
    ).fetchone()
    conn.close()

    if photo:
        await update.message.reply_photo(photo["file_id"], caption=text, parse_mode="HTML")
    else:
        await update.message.reply_text(text, parse_mode="HTML")


# ─────────────────────────── КОСТИ ───────────────────────────────────────────

async def cmd_dice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/кости` — бросить кости против дилера."""
    _log_game_cmd("кости")
    user = update.effective_user
    dealer_roll = _random.randint(1, 6)
    player_roll = _random.randint(1, 6)

    # Определяем момент для фото
    if player_roll > dealer_roll:
        moment = "win"
        result = "🏆 <b>Ты выиграл!</b>"
    elif player_roll < dealer_roll:
        moment = "lose"
        result = "😞 <b>Дилер победил.</b>"
    else:
        moment = "tie"
        result = "🤝 <b>Ничья!</b>"

    conn = db_connect()
    photo = conn.execute(
        "SELECT file_id FROM dice_photos WHERE moment=? LIMIT 1", (moment,)
    ).fetchone()
    conn.close()

    dealer_text = f"🎲 Дилер бросает... выпало <b>{dealer_roll}</b>"
    player_text = f"🎲 Твой бросок — <b>{player_roll}</b>\n\n{result}"

    # Сначала число дилера
    if photo:
        msg = await update.message.reply_text(dealer_text, parse_mode="HTML")
        await asyncio.sleep(1.5)
        await update.message.reply_photo(photo["file_id"], caption=player_text, parse_mode="HTML")
    else:
        await update.message.reply_text(dealer_text, parse_mode="HTML")
        await asyncio.sleep(1.5)
        await update.message.reply_text(player_text, parse_mode="HTML")


@_expedition_guard
async def cmd_shop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`магазин` — список предметов с кнопками покупки."""
    user = update.effective_user
    data = _econ_get(user.id)
    descs = {
        ITEM_ENERGY: "Даёт доп. экспедицию (без лимита в день)",
        ITEM_COMPAS: "Улучшает исход экспедиции",
    }
    lines = [f"🏪 <b>Магазин</b>  |  у тебя: {data['coins']} 🪙\n"]
    buttons = []
    for item, price in SHOP_PRICES.items():
        emoji = ITEM_EMOJI.get(item, "📦")
        lines.append(f"{emoji} <b>{item}</b> — {price} 🪙\n   {descs.get(item, '')}")
        buttons.append([
            InlineKeyboardButton(f"Купить {emoji} x1 ({price} 🪙)", callback_data=f"shop_buy_{user.id}_{item}_1"),
            InlineKeyboardButton(f"x5 ({price*5} 🪙)", callback_data=f"shop_buy_{user.id}_{item}_5"),
        ])
    lines.append("\nИли текстом: <code>купить &lt;предмет&gt; [кол-во]</code>")
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cb_shop_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Покупка товара через кнопку в магазине."""
    query = update.callback_query
    # format: shop_buy_{user_id}_{item}_{qty}
    parts = query.data.split("_", 4)
    user_id = int(parts[2])
    item    = parts[3]
    qty     = int(parts[4])

    if query.from_user.id != user_id:
        await query.answer("Это не твой магазин.", show_alert=True)
        return

    await query.answer()
    price = SHOP_PRICES.get(item)
    if not price:
        await query.message.reply_text("Товар не найден.")
        return

    total = price * qty
    data = _econ_get(user_id)
    if data["coins"] < total:
        await query.answer(
            f"Недостаточно монет! Нужно {total} 🪙, у тебя {data['coins']} 🪙.",
            show_alert=True,
        )
        return

    _econ_update(user_id, coins=data["coins"] - total)
    _inv_add(user_id, item, qty)
    emoji = ITEM_EMOJI.get(item, "📦")
    qty_str = f"x{qty} " if qty > 1 else ""
    await query.message.reply_text(
        f"✅ {query.from_user.mention_html()} купил {emoji} <b>{item}</b> {qty_str}за {total} 🪙!",
        parse_mode="HTML",
    )


@_expedition_guard
async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`купить <предмет> [кол-во]` — купить предмет в магазине."""
    user = update.effective_user
    text = (update.message.text or "").strip()
    parts = text.split(None, 1)
    if len(parts) < 2:
        await update.message.reply_text("Формат: <code>купить &lt;предмет&gt; [кол-во]</code>", parse_mode="HTML")
        return
    arg = parts[1].strip()

    # Пытаемся извлечь кол-во из конца строки (например: "энергетик 3")
    qty = 1
    arg_parts = arg.rsplit(None, 1)
    if len(arg_parts) == 2 and arg_parts[1].isdigit():
        qty = max(1, int(arg_parts[1]))
        item_name = arg_parts[0].lower().strip()
    else:
        item_name = arg.lower().strip()

    # Ищем предмет по частичному совпадению
    matched = None
    for item in SHOP_PRICES:
        if item_name in item or item in item_name:
            matched = item
            break
    if not matched:
        await update.message.reply_text(
            f"Предмет не найден. Напиши <code>магазин</code> чтобы увидеть список.",
            parse_mode="HTML",
        )
        return

    price = SHOP_PRICES[matched]
    total = price * qty
    data = _econ_get(user.id)
    if data["coins"] < total:
        await update.message.reply_text(
            f"Недостаточно монет. Нужно {total} 🪙 (x{qty}), у тебя {data['coins']} 🪙."
        )
        return

    _econ_update(user.id, coins=data["coins"] - total)
    _inv_add(user.id, matched, qty)
    emoji = ITEM_EMOJI.get(matched, "📦")
    qty_str = f"x{qty} " if qty > 1 else ""
    await update.message.reply_text(
        f"✅ {_mention(user)} купил {emoji} <b>{matched}</b> {qty_str}за {total} 🪙!",
        parse_mode="HTML",
    )


@_expedition_guard
async def cmd_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`инвентарь` — показать предметы."""
    user = update.effective_user
    inv = _inv_get(user.id)
    if not inv:
        await update.message.reply_text(
            f"🎒 {_mention(user)}, твой инвентарь пуст.", parse_mode="HTML"
        )
        return
    lines = [f"🎒 <b>Инвентарь {_mention(user)}:</b>"]
    for item, qty in inv.items():
        emoji = ITEM_EMOJI.get(item, "📦")
        lines.append(f"{emoji} {item} — {qty} шт.")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


@_expedition_guard
async def cmd_open_safe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`сейф` — открыть найденный сейф."""
    user = update.effective_user
    if not _inv_remove(user.id, ITEM_SAFE):
        await update.message.reply_text("У тебя нет сейфа.")
        return
    coins = _random.randint(10, 50)
    data = _econ_get(user.id)
    _econ_update(user.id, coins=data["coins"] + coins)
    await update.message.reply_text(
        f"🗄️ {_mention(user)} вскрыл сейф и нашёл <b>{coins} 🪙</b>!",
        parse_mode="HTML",
    )


@_expedition_guard
async def cmd_open_chest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`сундук` — открыть найденный сундук."""
    user = update.effective_user
    if not _inv_remove(user.id, ITEM_CHEST):
        await update.message.reply_text("У тебя нет сундука.")
        return
    roll = _random.random()
    data = _econ_get(user.id)
    if roll < 0.33:
        coins = _random.randint(50, 100)
        _econ_update(user.id, coins=data["coins"] + coins)
        text = f"📦 {_mention(user)} открыл сундук — <b>{coins} 🪙</b>!"
    elif roll < 0.66:
        nuggets = _random.randint(10, 30)
        _econ_update(user.id, nuggets=data["nuggets"] + nuggets)
        text = f"📦 {_mention(user)} открыл сундук — <b>{nuggets} 🪨</b>!"
    else:
        items = [ITEM_ENERGY, ITEM_COMPAS, ITEM_GUIDE]
        item = _random.choice(items)
        _inv_add(user.id, item)
        emoji = ITEM_EMOJI.get(item, "📦")
        text = f"📦 {_mention(user)} открыл сундук и нашёл {emoji} <b>{item}</b>!"
    await update.message.reply_text(text, parse_mode="HTML")


@_expedition_guard
async def cmd_amulet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`амулет` — получить случайный предмет из магазина."""
    user = update.effective_user

    # Проверяем кулдаун (раз в день)
    data = _econ_get(user.id)
    if data.get("last_amulet") == _msk_day():
        await update.message.reply_text("🧿 Амулет уже использован сегодня.")
        return

    if not _inv_remove(user.id, ITEM_AMULET):
        await update.message.reply_text("У тебя нет амулета.")
        return

    item = _random.choice(list(SHOP_PRICES.keys()))
    _inv_add(user.id, item)
    _econ_update(user.id, last_amulet=_msk_day())
    emoji = ITEM_EMOJI.get(item, "📦")
    await update.message.reply_text(
        f"🧿 {_mention(user)} активировал амулет и получил {emoji} <b>{item}</b>!",
        parse_mode="HTML",
    )


@_expedition_guard
async def cmd_steal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`украсть @user` — использовать руководство по краже (рандомно монеты или самородки)."""
    _log_game_cmd("украсть")
    user = update.effective_user
    msg  = update.message

    if not _inv_remove(user.id, ITEM_GUIDE):
        await msg.reply_text("У тебя нет 📖 руководства по краже.")
        return

    target = await resolve_target(update, context)
    if not target:
        _inv_add(user.id, ITEM_GUIDE)
        await msg.reply_text("Укажи цель: украсть @username")
        return
    if target.id == user.id:
        _inv_add(user.id, ITEM_GUIDE)
        await msg.reply_text("Нельзя украсть у себя.")
        return

    # Рандомно выбираем что красть
    kind = _random.choice(["n", "c"])  # n=самородки, c=монеты

    conn = db_connect()
    try:
        if kind == "n":
            amt = 10
            row = conn.execute(
                "SELECT nuggets FROM economy WHERE user_id=?", (target.id,)
            ).fetchone()
            if not row or row["nuggets"] < amt:
                await msg.reply_text(
                    f"😔 {_mention(user)} попытался украсть самородки у {target.mention_html()}, "
                    f"но у жертвы их не хватило!",
                    parse_mode="HTML",
                )
                return
            cur = conn.execute(
                "UPDATE economy SET nuggets=nuggets-? WHERE user_id=? AND nuggets>=?",
                (amt, target.id, amt)
            )
            if cur.rowcount == 0:
                await msg.reply_text("Кража не удалась — у жертвы не хватило самородков.")
                return
            conn.execute(
                "INSERT INTO economy (user_id, nuggets) VALUES (?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET nuggets=nuggets+?",
                (user.id, amt, amt)
            )
            conn.commit()
            text = (
                f"🤫 {_mention(user)} незаметно украл у {target.mention_html()} "
                f"<b>{amt} самородков</b> 🪨!"
            )
        else:
            amt = 50
            row = conn.execute(
                "SELECT coins FROM economy WHERE user_id=?", (target.id,)
            ).fetchone()
            if not row or row["coins"] < amt:
                await msg.reply_text(
                    f"😔 {_mention(user)} попытался украсть монеты у {target.mention_html()}, "
                    f"но у жертвы их не хватило!",
                    parse_mode="HTML",
                )
                return
            cur = conn.execute(
                "UPDATE economy SET coins=coins-? WHERE user_id=? AND coins>=?",
                (amt, target.id, amt)
            )
            if cur.rowcount == 0:
                await msg.reply_text("Кража не удалась — у жертвы не хватило монет.")
                return
            conn.execute(
                "INSERT INTO economy (user_id, coins) VALUES (?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET coins=coins+?",
                (user.id, amt, amt)
            )
            conn.commit()
            text = (
                f"🤫 {_mention(user)} незаметно украл у {target.mention_html()} "
                f"<b>{amt} монет</b> 🪙!"
            )
    finally:
        conn.close()

    await msg.reply_text(text, parse_mode="HTML")


# ─────────────────────────── ДУЭЛЬ ───────────────────────────────────────────

@_expedition_guard
async def cmd_duel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`дуэль @user ставка` — вызов на дуэль."""
    _log_game_cmd("дуэль")
    user = update.effective_user
    msg  = update.message
    args = (msg.text or "").split()

    if len(args) < 3 or not args[-1].isdigit():
        await msg.reply_text("Формат: дуэль @username <ставка>")
        return

    bet = int(args[-1])
    if bet <= 0:
        await msg.reply_text("Ставка должна быть больше нуля.")
        return

    data = _econ_get(user.id)
    if data["nuggets"] < bet:
        await msg.reply_text(f"Недостаточно самородков. У тебя: {data['nuggets']} 🪨")
        return

    target = await resolve_target(update, context)
    if not target:
        await msg.reply_text("Укажи цель: дуэль @username <ставка>")
        return
    if target.id == user.id:
        await msg.reply_text("Нельзя вызвать себя.")
        return

    target_data = _econ_get(target.id)
    if target_data["nuggets"] < bet:
        await msg.reply_text(
            f"У {target.mention_html()} недостаточно самородков для этой ставки.",
            parse_mode="HTML",
        )
        return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("⚔️ Принять", callback_data=f"duel_a_{user.id}_{target.id}_{bet}"),
        InlineKeyboardButton("❌ Отказать", callback_data=f"duel_d_{user.id}_{target.id}_{bet}"),
    ]])
    sent = await msg.reply_text(
        f"⚔️ {_mention(user)} вызывает {target.mention_html()} на дуэль!\n"
        f"Ставка: <b>{bet} 🪨</b>\n\n"
        f"{target.mention_html()}, принимаешь вызов? (120 сек)",
        parse_mode="HTML",
        reply_markup=keyboard,
    )

    async def _duel_expire(ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await ctx.bot.edit_message_reply_markup(
                chat_id=sent.chat_id, message_id=sent.message_id, reply_markup=None
            )
            await ctx.bot.send_message(
                sent.chat_id,
                f"⏰ Дуэль от {_mention(user)} истекла — никто не ответил.",
                parse_mode="HTML",
            )
        except Exception:
            pass

    if context.job_queue:
        context.job_queue.run_once(_duel_expire, 120)


async def cb_duel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработка ответа на дуэль."""
    query = update.callback_query
    parts = query.data.split("_")
    action    = parts[1]  # a или d
    caller_id = int(parts[2])
    target_id = int(parts[3])
    bet       = int(parts[4])

    if query.from_user.id != target_id:
        await query.answer("Это не твой вызов.", show_alert=True)
        return

    await query.answer()

    if action == "d":
        await query.edit_message_text("❌ Дуэль отклонена.")
        return

    # Проверяем балансы
    caller_data = _econ_get(caller_id)
    target_data = _econ_get(target_id)
    if caller_data["nuggets"] < bet or target_data["nuggets"] < bet:
        await query.edit_message_text("У одного из участников недостаточно самородков.")
        return

    winner_id = _random.choice([caller_id, target_id])
    loser_id  = target_id if winner_id == caller_id else caller_id

    winner_data = _econ_get(winner_id)
    loser_data  = _econ_get(loser_id)
    _econ_update(winner_id, nuggets=winner_data["nuggets"] + bet)
    _econ_update(loser_id,  nuggets=loser_data["nuggets"]  - bet)

    try:
        winner_user = await context.bot.get_chat(winner_id)
        winner_name = winner_user.first_name or str(winner_id)
    except Exception:
        winner_name = str(winner_id)

    await query.edit_message_text(
        f"⚔️ Дуэль завершена!\n"
        f"🏆 Победитель: <b>{winner_name}</b> +{bet} 🪨",
        parse_mode="HTML",
    )




# ─────────────────────────── КРЕСТИКИ-НОЛИКИ ─────────────────────────────────

_ttt_games: dict = {}

def _ttt_board_keyboard(board: list, game_id: str) -> "InlineKeyboardMarkup":
    LABELS = {0: "·", 1: "✖️", 2: "⭕"}
    rows = []
    for r in range(3):
        row = []
        for c in range(3):
            idx = r * 3 + c
            val = board[idx]
            row.append(InlineKeyboardButton(
                LABELS[val],
                callback_data=f"ttt_move_{game_id}_{idx}" if val == 0 else f"ttt_noop"
            ))
        rows.append(row)
    return InlineKeyboardMarkup(rows)

def _ttt_check_winner(board: list) -> int:
    wins = [(0,1,2),(3,4,5),(6,7,8),(0,3,6),(1,4,7),(2,5,8),(0,4,8),(2,4,6)]
    for a,b,c in wins:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    if all(board):
        return -1
    return 0

def _ttt_status(game: dict) -> str:
    sym  = "✖️" if game["turn"] == 1 else "⭕"
    name = game["x_name"] if game["turn"] == 1 else game["o_name"]
    return f"✖️ {game['x_name']}  vs  ⭕ {game['o_name']}\nХод: {sym} {name}"

async def cmd_ttt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg  = update.message
    user = update.effective_user

    target = None
    if msg.reply_to_message and msg.reply_to_message.from_user:
        target = msg.reply_to_message.from_user
    else:
        ttt_args = _parse_args(update, context)
        username = ttt_args[0].lstrip("@") if ttt_args else None
        if username:
            conn = db_connect()
            row = conn.execute(
                "SELECT user_id, first_name FROM known_users WHERE lower(username)=?",
                (username.lower(),)
            ).fetchone()
            conn.close()
            if row:
                class _FU:
                    id = row["user_id"]
                    first_name = row["first_name"]
                    def mention_html(self): return f'<a href="tg://user?id={self.id}">{self.first_name}</a>'
                target = _FU()

    if not target:
        await msg.reply_text(
            "Ответь на сообщение или укажи @username:\n<code>крестики @username</code>",
            parse_mode="HTML",
        )
        return
    if target.id == user.id:
        await msg.reply_text("Нельзя играть с собой.")
        return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Принять", callback_data=f"ttt_accept_{user.id}_{target.id}"),
        InlineKeyboardButton("❌ Отказать", callback_data=f"ttt_decline_{user.id}_{target.id}"),
    ]])
    sent = await msg.reply_text(
        f"✖️⭕ {user.mention_html()} вызывает {target.mention_html()} в крестики-нолики!\n"
        f"Принимаешь вызов? (120 сек)",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    async def _expire(ctx):
        try:
            await ctx.bot.edit_message_reply_markup(
                chat_id=sent.chat_id, message_id=sent.message_id, reply_markup=None
            )
        except Exception:
            pass
    if context.job_queue:
        context.job_queue.run_once(_expire, 120)


async def cb_ttt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data  = query.data
    user  = query.from_user

    if data == "ttt_noop":
        await query.answer("Клетка занята.", show_alert=True)
        return

    if data.startswith("ttt_accept_") or data.startswith("ttt_decline_"):
        parts     = data.split("_")
        action    = parts[1]
        caller_id = int(parts[2])
        target_id = int(parts[3])
        if user.id != target_id:
            await query.answer("Это не твой вызов.", show_alert=True)
            return
        await query.answer()
        if action == "decline":
            await query.edit_message_text("❌ Вызов отклонён.")
            return
        game_id = "".join(_random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=8))
        try:
            xu = await context.bot.get_chat(caller_id)
            x_name = xu.first_name or str(caller_id)
        except Exception:
            x_name = str(caller_id)
        o_name = user.first_name or str(user.id)
        game = {"board":[0]*9,"x_id":caller_id,"o_id":target_id,
                "x_name":x_name,"o_name":o_name,"turn":1}
        _ttt_games[game_id] = game
        kb  = _ttt_board_keyboard(game["board"], game_id)
        txt = f"✖️⭕ <b>Крестики-нолики</b>\n\n{_ttt_status(game)}"
        await query.edit_message_text(txt, parse_mode="HTML", reply_markup=kb)
        return

    if data.startswith("ttt_move_"):
        parts   = data.split("_")
        game_id = parts[2]
        idx     = int(parts[3])
        game = _ttt_games.get(game_id)
        if not game:
            await query.answer("Игра не найдена.", show_alert=True)
            return
        expected = game["x_id"] if game["turn"] == 1 else game["o_id"]
        if user.id != expected:
            sym = "✖️" if game["turn"] == 1 else "⭕"
            await query.answer(f"Сейчас ход {sym}.", show_alert=True)
            return
        if game["board"][idx] != 0:
            await query.answer("Клетка занята.", show_alert=True)
            return
        await query.answer()
        game["board"][idx] = game["turn"]
        result = _ttt_check_winner(game["board"])
        kb = _ttt_board_keyboard(game["board"], game_id)
        if result == 0:
            game["turn"] = 2 if game["turn"] == 1 else 1
            txt = f"✖️⭕ <b>Крестики-нолики</b>\n\n{_ttt_status(game)}"
            await query.edit_message_text(txt, parse_mode="HTML", reply_markup=kb)
        elif result == -1:
            del _ttt_games[game_id]
            txt = f"✖️⭕ <b>Крестики-нолики</b>\n\n✖️ {game['x_name']}  vs  ⭕ {game['o_name']}\n\n🤝 Ничья!"
            await query.edit_message_text(txt, parse_mode="HTML", reply_markup=kb)
        else:
            del _ttt_games[game_id]
            wname = game["x_name"] if result == 1 else game["o_name"]
            wsym  = "✖️" if result == 1 else "⭕"
            txt = f"✖️⭕ <b>Крестики-нолики</b>\n\n✖️ {game['x_name']}  vs  ⭕ {game['o_name']}\n\n🏆 Победил {wsym} {wname}!"
            await query.edit_message_text(txt, parse_mode="HTML", reply_markup=kb)

# ─────────────────────────── ЭКСПЕДИЦИЯ ──────────────────────────────────────

async def cmd_expedition(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`экспедиция` — показать доступные локации для выбора."""
    _log_game_cmd("крестики")
    _log_game_cmd("экспедиция")
    import time
    user = update.effective_user

    if _in_expedition(user.id):
        end_time = _active_expeditions.get(user.id, 0)
        secs_left = max(0, int(end_time - time.time()))
        await update.message.reply_text(f"🏃 Ты уже в экспедиции! Подожди ещё ~{secs_left} сек.")
        return

    data = _econ_get(user.id)
    inv  = _inv_get(user.id)
    now_str = _msk_day()

    used_today   = data.get("expeditions_today", 0)
    last_exp_day = data.get("last_expedition", "")
    if last_exp_day != now_str:
        used_today = 0

    energy_count = inv.get(ITEM_ENERGY, 0)
    if used_today > 0 and energy_count == 0:
        await update.message.reply_text(
            f"😴 {_mention(user)}, ты уже был в экспедиции сегодня!\n"
            f"Купи 🥤 энергетик в магазине чтобы сходить ещё раз.",
            parse_mode="HTML",
        )
        return

    max_loc = min(data.get("unlocked_location", 0), 3)

    # Если не первый поход сегодня — спрашиваем подтверждение на трату энергетика
    if used_today > 0:
        energy_count = inv.get(ITEM_ENERGY, 0)
        confirm_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Да, потратить 🥤 энергетик (осталось: {energy_count})", callback_data=f"exp_confirm_{user.id}")],
            [InlineKeyboardButton("❌ Отмена", callback_data=f"exp_cancel_{user.id}")],
        ])
        await update.message.reply_text(
            f"⚡ {_mention(user)}, ты уже ходил в экспедицию сегодня!\n"
            f"Потратить 🥤 <b>энергетик</b> ({energy_count} шт.) для ещё одного похода?",
            parse_mode="HTML",
            reply_markup=confirm_keyboard,
        )
        return

    # Строим кнопки доступных локаций
    buttons = []
    for loc_id in range(max_loc + 1):
        loc = LOCATIONS[loc_id]
        buttons.append([InlineKeyboardButton(loc["name"], callback_data=f"exp_go_{user.id}_{loc_id}")])

    keyboard = InlineKeyboardMarkup(buttons)
    has_compas = inv.get(ITEM_COMPAS, 0) > 0
    hint = " | 🧭 Компас активен" if has_compas else ""
    await update.message.reply_text(
        f"🗺 <b>Выбери локацию для экспедиции:</b>{hint}",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def cb_exp_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пользователь подтвердил трату энергетика — показываем выбор локации."""
    query = update.callback_query
    parts = query.data.split("_")
    user_id = int(parts[2])

    if query.from_user.id != user_id:
        await query.answer("Это не твой выбор.", show_alert=True)
        return

    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)

    inv = _inv_get(user_id)
    if inv.get(ITEM_ENERGY, 0) == 0:
        await query.message.reply_text(
            f"😴 У тебя нет 🥤 энергетика. Купи в магазине: <code>магазин</code>",
            parse_mode="HTML",
        )
        return

    data = _econ_get(user_id)
    max_loc = min(data.get("unlocked_location", 0), 3)
    has_compas = inv.get(ITEM_COMPAS, 0) > 0
    hint = " | 🧭 Компас активен" if has_compas else ""

    buttons = []
    for loc_id in range(max_loc + 1):
        loc = LOCATIONS[loc_id]
        buttons.append([InlineKeyboardButton(loc["name"], callback_data=f"exp_go_{user_id}_{loc_id}")])

    await query.message.reply_text(
        f"🗺 <b>Выбери локацию для экспедиции:</b>{hint}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cb_exp_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пользователь отменил экспедицию."""
    query = update.callback_query
    parts = query.data.split("_")
    user_id = int(parts[2])

    if query.from_user.id != user_id:
        await query.answer("Это не твой выбор.", show_alert=True)
        return

    await query.answer("Отменено.")
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("🚶 Экспедиция отменена.")


async def cb_exp_go(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пользователь выбрал локацию — запускаем экспедицию."""
    import time
    query   = update.callback_query
    parts   = query.data.split("_")
    user_id = int(parts[2])
    loc_id  = int(parts[3])

    if query.from_user.id != user_id:
        await query.answer("Это не твой выбор.", show_alert=True)
        return

    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)

    user = query.from_user
    if _in_expedition(user_id):
        await query.message.reply_text("Ты уже в экспедиции!")
        return

    data = _econ_get(user_id)
    inv  = _inv_get(user_id)
    now_str = _msk_day()

    used_today   = data.get("expeditions_today", 0)
    last_exp_day = data.get("last_expedition", "")
    if last_exp_day != now_str:
        used_today = 0

    # Если не первый поход — тратим энергетик
    if used_today > 0:
        if not _inv_remove(user_id, ITEM_ENERGY):
            await query.message.reply_text(
                f"😴 {user.mention_html()}, ты уже был в экспедиции сегодня!\n"
                f"Купи 🥤 энергетик в магазине чтобы сходить ещё раз.",
                parse_mode="HTML",
            )
            return

    max_loc = min(data.get("unlocked_location", 0), 3)
    if loc_id > max_loc:
        await query.message.reply_text("Эта локация ещё не открыта.")
        return

    loc_data = LOCATIONS[loc_id]

    # Компас — убираем плохие события
    has_compas = inv.get(ITEM_COMPAS, 0) > 0
    if has_compas:
        _inv_remove(user_id, ITEM_COMPAS)
        events = [e for e in loc_data["events"] if e[1] >= 0 and e[2] >= 0]
    else:
        events = loc_data["events"]

    # Убираем события с weight=0
    normal_events = [e for e in events if e[4] > 0]

    # Карта выпадает только на максимальной локации
    if loc_id < max_loc:
        normal_events = [e for e in normal_events if ITEM_MAP not in e[3]]

    weights = [e[4] for e in normal_events]
    event   = _random.choices(normal_events, weights=weights, k=1)[0]

    text_tpl, nuggets, coins, items, _ = event
    mention = user.mention_html()
    text    = text_tpl.replace("{u}", mention)

    new_nuggets = max(0, data["nuggets"] + nuggets)
    new_coins   = max(0, data["coins"]   + coins)

    _econ_update(user_id, nuggets=new_nuggets, coins=new_coins,
                 last_expedition=now_str, expeditions_today=used_today + 1)

    items_text = ""
    next_loc = data.get("unlocked_location", 0) + 1
    for item in items:
        if item == ITEM_MAP:
            if next_loc <= 3:
                _econ_update(user_id, unlocked_location=next_loc)
                items_text = f"\n🗺️ Открыта новая локация: <b>{LOCATIONS[next_loc]['name']}</b>!"
            else:
                items_text = "\n🗺️ Ты нашёл карту, но все локации уже открыты."
        else:
            _inv_add(user_id, item)

    result_parts = []
    if nuggets > 0:  result_parts.append(f"+{nuggets} 🪨")
    elif nuggets < 0: result_parts.append(f"{nuggets} 🪨")
    if coins > 0:    result_parts.append(f"+{coins} 🪙")
    elif coins < 0:  result_parts.append(f"{coins} 🪙")
    non_map_items = [i for i in items if i != ITEM_MAP]
    if non_map_items:
        result_parts.append(_fmt_items(non_map_items))
    result_str = " | ".join(result_parts) if result_parts else "ничего"

    full_text = (
        f"{loc_data['name']}\n\n{text}\n\n"
        f"<b>Итог:</b> {result_str}{items_text}"
    )

    # Блокировка на время экспедиции — запускаем в фоне, чтобы не блокировать других пользователей
    _active_expeditions[user_id] = time.time() + EXPEDITION_DURATION
    await query.message.reply_text(
        f"🎒 {mention} отправляется в экспедицию...\n⏳ Результат через {EXPEDITION_DURATION} секунд.",
        parse_mode="HTML",
    )

    async def _finish_expedition():
        await asyncio.sleep(EXPEDITION_DURATION)
        _active_expeditions.pop(user_id, None)
        conn2 = db_connect()
        photo_row = conn2.execute(
            "SELECT file_id FROM expedition_photos WHERE loc_id=? AND event_text=? LIMIT 1",
            (loc_id, text_tpl),
        ).fetchone()
        conn2.close()
        if photo_row:
            await query.message.reply_photo(photo=photo_row["file_id"], caption=full_text, parse_mode="HTML")
        else:
            await query.message.reply_text(full_text, parse_mode="HTML")

    asyncio.create_task(_finish_expedition())


@_expedition_guard
async def cmd_rich_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`топ богатых` — рейтинг по самородкам."""
    conn = db_connect()
    rows = conn.execute(
        "SELECT user_id, nuggets, coins FROM economy ORDER BY nuggets DESC LIMIT 10"
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("Ещё никто не фармил.")
        return

    lines = ["🏆 <b>Топ богатых:</b>\n"]
    medals = ["🥇", "🥈", "🥉"]
    for i, r in enumerate(rows):
        medal = medals[i] if i < 3 else f"{i+1}."
        try:
            chat = await context.bot.get_chat(r["user_id"])
            name = chat.first_name or str(r["user_id"])
        except Exception:
            name = str(r["user_id"])
        lines.append(f"{medal} {name} — {r['nuggets']} 🪨 | {r['coins']} 🪙")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ─────────────────────────── ТОЧКА ВХОДА ─────────────────────────────────────

async def timer_loop(app: Application) -> None:
    """Фоновый цикл проверки таймеров каждые 30 секунд."""
    while True:
        await asyncio.sleep(30)
        conn = db_connect()
        rows = conn.execute(
            "SELECT id, chat_id, command FROM timers WHERE fire_at <= datetime('now')"
        ).fetchall()
        for row in rows:
            try:
                await app.bot.send_message(
                    row["chat_id"],
                    f"⏰ <b>Таймер сработал:</b>\n{row['command']}",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error(f"Timer send error: {e}")
            conn.execute("DELETE FROM timers WHERE id=?", (row["id"],))
        conn.commit()
        conn.close()


async def post_init(app: Application) -> None:
    """Запускает фоновый цикл таймеров после старта бота."""
    task = asyncio.create_task(timer_loop(app))
    app.bot_data["_timer_task"] = task  # сохраняем ссылку, чтобы GC не собрал


def main() -> None:
    init_db()
    _geo_cache_load()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    register_handlers(app)

    logger.info("Бот запущен. Нажмите Ctrl+C для остановки.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
