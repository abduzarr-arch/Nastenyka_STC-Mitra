import os
import sqlite3
from datetime import datetime
from typing import Optional

from config import TIMEZONE

# Для Railway лучше задать переменную DB_FILE=/data/bot_data.db,
# где /data — mount path подключенного Railway Volume.
DB_FILE = os.getenv(
    "DB_FILE",
    os.path.join(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "."), "bot_data.db"),
)


def _ensure_db_dir() -> None:
    db_dir = os.path.dirname(os.path.abspath(DB_FILE))
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)


def _connect(row_factory: bool = False) -> sqlite3.Connection:
    _ensure_db_dir()
    conn = sqlite3.connect(DB_FILE)
    if row_factory:
        conn.row_factory = sqlite3.Row
    return conn


def _to_db_datetime(value) -> Optional[str]:
    """SQLite надежнее хранить даты как ISO-строки."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = TIMEZONE.localize(value)
    return value.astimezone(TIMEZONE).isoformat()


def parse_db_datetime(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = TIMEZONE.localize(dt)
    return dt.astimezone(TIMEZONE)


def format_db_datetime(value, empty: str = "без срока") -> str:
    dt = parse_db_datetime(value)
    if not dt:
        return empty
    return dt.strftime("%d.%m.%Y %H:%M")


def _task_from_row(row):
    task = dict(row)
    task["due_date"] = parse_db_datetime(task.get("due_date"))
    task["created_at"] = parse_db_datetime(task.get("created_at"))
    task["updated_at"] = parse_db_datetime(task.get("updated_at"))
    return task


def init_db():
    conn = _connect()
    c = conn.cursor()

    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            registered_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_activity TEXT
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            assigned_to INTEGER NOT NULL,
            assigned_by INTEGER,
            due_date TEXT,
            status TEXT DEFAULT 'active',
            progress INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT,
            FOREIGN KEY(assigned_to) REFERENCES users(user_id)
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS conversation (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            role TEXT,
            content TEXT,
            timestamp TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')


    c.execute('''
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            remind_at TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            sent_at TEXT
        )
    ''')


    c.execute('''
        CREATE TABLE IF NOT EXISTS group_agreements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            chat_title TEXT,
            message_id INTEGER,
            user_id INTEGER,
            username TEXT,
            author_name TEXT,
            text TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')


    c.execute('''
        CREATE TABLE IF NOT EXISTS group_chats (
            chat_id INTEGER PRIMARY KEY,
            alias TEXT UNIQUE NOT NULL,
            title TEXT,
            chat_type TEXT,
            registered_by INTEGER,
            registered_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT
        )
    ''')

    # Миграция для старой базы, где не было updated_at.
    c.execute("PRAGMA table_info(tasks)")
    columns = {row[1] for row in c.fetchall()}
    if "updated_at" not in columns:
        c.execute("ALTER TABLE tasks ADD COLUMN updated_at TEXT")

    conn.commit()
    conn.close()


def is_user_registered(user_id):
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
    res = c.fetchone()
    conn.close()
    return res is not None


def register_user(user_id, username, first_name, last_name):
    conn = _connect()
    c = conn.cursor()
    c.execute('''
        INSERT INTO users (user_id, username, first_name, last_name, last_activity)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_name=excluded.last_name,
            last_activity=excluded.last_activity
    ''', (user_id, username, first_name, last_name, datetime.now(TIMEZONE).isoformat()))
    conn.commit()
    conn.close()


def get_user_id_by_username(username):
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE lower(username) = lower(?)", (username.lstrip('@'),))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


def get_all_registered_users():
    conn = _connect()
    c = conn.cursor()
    c.execute("SELECT user_id, username, first_name FROM users")
    rows = c.fetchall()
    conn.close()
    return rows


def create_task(title, description, assigned_to, assigned_by, due_date):
    conn = _connect()
    c = conn.cursor()
    now = datetime.now(TIMEZONE).isoformat()
    c.execute('''
        INSERT INTO tasks (title, description, assigned_to, assigned_by, due_date, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (title, description, assigned_to, assigned_by, _to_db_datetime(due_date), now, now))
    task_id = c.lastrowid
    conn.commit()
    conn.close()
    return task_id


def get_user_tasks(user_id):
    conn = _connect(row_factory=True)
    c = conn.cursor()
    c.execute('''
        SELECT id, title, description, due_date, progress, status
        FROM tasks
        WHERE assigned_to = ? AND status = 'active'
        ORDER BY due_date IS NULL, due_date ASC
    ''', (user_id,))
    rows = c.fetchall()
    conn.close()
    return [_task_from_row(row) for row in rows]


def update_task_progress(task_id, progress, user_id):
    conn = _connect()
    c = conn.cursor()
    status = 'done' if int(progress) >= 100 else 'active'
    c.execute('''
        UPDATE tasks
        SET progress = ?, status = ?, updated_at = ?
        WHERE id = ? AND assigned_to = ?
    ''', (progress, status, datetime.now(TIMEZONE).isoformat(), task_id, user_id))
    changed = c.rowcount
    conn.commit()
    conn.close()
    return changed > 0


def get_task_by_id(task_id):
    conn = _connect(row_factory=True)
    c = conn.cursor()
    c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    row = c.fetchone()
    conn.close()
    return _task_from_row(row) if row else None


def get_all_active_tasks():
    conn = _connect(row_factory=True)
    c = conn.cursor()
    c.execute("SELECT * FROM tasks WHERE status = 'active'")
    rows = c.fetchall()
    conn.close()
    return [_task_from_row(row) for row in rows]


def add_to_conversation(user_id, role, content):
    conn = _connect()
    c = conn.cursor()
    c.execute('''
        INSERT INTO conversation (user_id, role, content)
        VALUES (?, ?, ?)
    ''', (user_id, role, content))
    conn.commit()
    conn.close()


def get_conversation_history(user_id, limit=50):
    conn = _connect(row_factory=True)
    c = conn.cursor()
    c.execute('''
        SELECT role, content FROM conversation
        WHERE user_id = ?
        ORDER BY timestamp DESC LIMIT ?
    ''', (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]



def _reminder_from_row(row):
    reminder = dict(row)
    reminder["remind_at"] = parse_db_datetime(reminder.get("remind_at"))
    reminder["created_at"] = parse_db_datetime(reminder.get("created_at"))
    reminder["sent_at"] = parse_db_datetime(reminder.get("sent_at"))
    return reminder


def create_reminder(user_id, text, remind_at):
    conn = _connect()
    c = conn.cursor()
    c.execute('''
        INSERT INTO reminders (user_id, text, remind_at, status, created_at)
        VALUES (?, ?, ?, 'pending', ?)
    ''', (user_id, text, _to_db_datetime(remind_at), datetime.now(TIMEZONE).isoformat()))
    reminder_id = c.lastrowid
    conn.commit()
    conn.close()
    return reminder_id


def get_due_reminders(now=None):
    now = now or datetime.now(TIMEZONE)
    conn = _connect(row_factory=True)
    c = conn.cursor()
    c.execute('''
        SELECT * FROM reminders
        WHERE status = 'pending' AND remind_at <= ?
        ORDER BY remind_at ASC
    ''', (_to_db_datetime(now),))
    rows = c.fetchall()
    conn.close()
    return [_reminder_from_row(row) for row in rows]


def get_user_pending_reminders(user_id):
    conn = _connect(row_factory=True)
    c = conn.cursor()
    c.execute('''
        SELECT * FROM reminders
        WHERE user_id = ? AND status = 'pending'
        ORDER BY remind_at ASC
    ''', (user_id,))
    rows = c.fetchall()
    conn.close()
    return [_reminder_from_row(row) for row in rows]


def mark_reminder_done(reminder_id, status='sent'):
    conn = _connect()
    c = conn.cursor()
    c.execute('''
        UPDATE reminders
        SET status = ?, sent_at = ?
        WHERE id = ?
    ''', (status, datetime.now(TIMEZONE).isoformat(), reminder_id))
    conn.commit()
    conn.close()

def create_group_agreement(chat_id, chat_title, message_id, user_id, username, author_name, text):
    conn = _connect()
    c = conn.cursor()
    c.execute('''
        INSERT INTO group_agreements
        (chat_id, chat_title, message_id, user_id, username, author_name, text, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        chat_id,
        chat_title,
        message_id,
        user_id,
        username,
        author_name,
        text.strip(),
        datetime.now(TIMEZONE).isoformat(),
    ))
    agreement_id = c.lastrowid
    conn.commit()
    conn.close()
    return agreement_id


def get_group_agreements(chat_id, limit=15):
    conn = _connect(row_factory=True)
    c = conn.cursor()
    c.execute('''
        SELECT * FROM group_agreements
        WHERE chat_id = ?
        ORDER BY created_at DESC
        LIMIT ?
    ''', (chat_id, int(limit)))
    rows = c.fetchall()
    conn.close()
    agreements = []
    for row in rows:
        item = dict(row)
        item["created_at"] = parse_db_datetime(item.get("created_at"))
        agreements.append(item)
    return agreements




def upsert_group_chat(chat_id, title, chat_type, alias, registered_by=None):
    conn = _connect()
    c = conn.cursor()
    now = datetime.now(TIMEZONE).isoformat()
    # Один alias должен указывать только на один чат. Если alias переназначили — удаляем старую привязку.
    c.execute("DELETE FROM group_chats WHERE lower(alias) = lower(?) AND chat_id != ?", (alias, chat_id))
    c.execute('''
        INSERT INTO group_chats (chat_id, alias, title, chat_type, registered_by, registered_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            alias=excluded.alias,
            title=excluded.title,
            chat_type=excluded.chat_type,
            registered_by=excluded.registered_by,
            updated_at=excluded.updated_at
    ''', (chat_id, alias, title, chat_type, registered_by, now, now))
    conn.commit()
    conn.close()


def get_group_chat_by_alias(alias):
    conn = _connect(row_factory=True)
    c = conn.cursor()
    c.execute("SELECT * FROM group_chats WHERE lower(alias) = lower(?)", (str(alias).strip(),))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_group_chats():
    conn = _connect(row_factory=True)
    c = conn.cursor()
    c.execute("SELECT * FROM group_chats ORDER BY alias ASC")
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]
