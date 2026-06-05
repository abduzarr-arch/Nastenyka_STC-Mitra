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
        CREATE TABLE IF NOT EXISTS controlled_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_by INTEGER,
            target_name TEXT,
            target_username TEXT,
            target_user_id INTEGER,
            chat_id INTEGER NOT NULL,
            chat_alias TEXT,
            chat_title TEXT,
            object_name TEXT,
            task_text TEXT NOT NULL,
            deadline_text TEXT,
            cadence_days INTEGER DEFAULT 1,
            next_check_at TEXT NOT NULL,
            last_check_at TEXT,
            last_message_id INTEGER,
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS employee_aliases (
            alias TEXT PRIMARY KEY,
            user_id INTEGER,
            username TEXT,
            display_name TEXT,
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT
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



    c.execute('''
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alias TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            description TEXT,
            default_chat_alias TEXT,
            status TEXT DEFAULT 'active',
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS operational_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER,
            parent_task_id INTEGER,
            title TEXT NOT NULL,
            description TEXT,
            assigned_to_name TEXT,
            assigned_to_username TEXT,
            assigned_to_user_id INTEGER,
            assigned_by INTEGER,
            chat_alias TEXT,
            chat_id INTEGER,
            deadline_text TEXT,
            due_date TEXT,
            status TEXT DEFAULT 'active',
            progress INTEGER DEFAULT 0,
            priority TEXT DEFAULT 'normal',
            risk_level TEXT DEFAULT 'низкий',
            control_enabled INTEGER DEFAULT 0,
            control_cadence_days INTEGER DEFAULT 1,
            next_check_at TEXT,
            last_check_at TEXT,
            last_check_message_id INTEGER,
            last_status_text TEXT,
            last_status_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT,
            FOREIGN KEY(project_id) REFERENCES projects(id),
            FOREIGN KEY(parent_task_id) REFERENCES operational_tasks(id)
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS operational_task_updates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            user_id INTEGER,
            username TEXT,
            author_name TEXT,
            update_text TEXT NOT NULL,
            progress INTEGER,
            status TEXT,
            risk_level TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(task_id) REFERENCES operational_tasks(id)
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS team_memory_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            chat_title TEXT,
            user_id INTEGER,
            username TEXT,
            author_name TEXT,
            event_type TEXT,
            content TEXT NOT NULL,
            task_id INTEGER,
            project_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
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


def _controlled_task_from_row(row):
    task = dict(row)
    for key in ("next_check_at", "last_check_at", "created_at", "updated_at"):
        task[key] = parse_db_datetime(task.get(key))
    return task


def create_controlled_task(
    created_by,
    target_name,
    target_username,
    target_user_id,
    chat_id,
    chat_alias,
    chat_title,
    object_name,
    task_text,
    deadline_text,
    cadence_days,
    next_check_at,
):
    conn = _connect()
    c = conn.cursor()
    now = datetime.now(TIMEZONE).isoformat()
    c.execute('''
        INSERT INTO controlled_tasks
        (created_by, target_name, target_username, target_user_id, chat_id, chat_alias, chat_title,
         object_name, task_text, deadline_text, cadence_days, next_check_at, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
    ''', (
        created_by,
        target_name,
        (target_username or "").lstrip("@") or None,
        target_user_id,
        chat_id,
        chat_alias,
        chat_title,
        object_name,
        task_text,
        deadline_text,
        int(cadence_days or 1),
        _to_db_datetime(next_check_at),
        now,
        now,
    ))
    task_id = c.lastrowid
    conn.commit()
    conn.close()
    return task_id


def get_due_controlled_tasks(now=None):
    now = now or datetime.now(TIMEZONE)
    conn = _connect(row_factory=True)
    c = conn.cursor()
    c.execute('''
        SELECT * FROM controlled_tasks
        WHERE status = 'active' AND next_check_at <= ?
        ORDER BY next_check_at ASC
    ''', (_to_db_datetime(now),))
    rows = c.fetchall()
    conn.close()
    return [_controlled_task_from_row(row) for row in rows]


def update_controlled_task_ping(task_id, next_check_at, last_message_id=None, last_check_at=None):
    conn = _connect()
    c = conn.cursor()
    c.execute('''
        UPDATE controlled_tasks
        SET next_check_at = ?, last_check_at = COALESCE(?, last_check_at),
            last_message_id = COALESCE(?, last_message_id), updated_at = ?
        WHERE id = ?
    ''', (
        _to_db_datetime(next_check_at),
        _to_db_datetime(last_check_at),
        last_message_id,
        datetime.now(TIMEZONE).isoformat(),
        task_id,
    ))
    conn.commit()
    conn.close()


def list_controlled_tasks(status='active', limit=30):
    conn = _connect(row_factory=True)
    c = conn.cursor()
    if status:
        c.execute('''
            SELECT * FROM controlled_tasks
            WHERE status = ?
            ORDER BY next_check_at ASC
            LIMIT ?
        ''', (status, int(limit)))
    else:
        c.execute('''
            SELECT * FROM controlled_tasks
            ORDER BY created_at DESC
            LIMIT ?
        ''', (int(limit),))
    rows = c.fetchall()
    conn.close()
    return [_controlled_task_from_row(row) for row in rows]


def mark_controlled_task_done(task_id, status='done'):
    conn = _connect()
    c = conn.cursor()
    c.execute('''
        UPDATE controlled_tasks
        SET status = ?, updated_at = ?
        WHERE id = ?
    ''', (status, datetime.now(TIMEZONE).isoformat(), task_id))
    changed = c.rowcount
    conn.commit()
    conn.close()
    return changed > 0


def get_controlled_task_by_last_message(chat_id, message_id):
    conn = _connect(row_factory=True)
    c = conn.cursor()
    c.execute('''
        SELECT * FROM controlled_tasks
        WHERE chat_id = ? AND last_message_id = ? AND status = 'active'
        ORDER BY updated_at DESC
        LIMIT 1
    ''', (chat_id, message_id))
    row = c.fetchone()
    conn.close()
    return _controlled_task_from_row(row) if row else None


def upsert_employee_alias(alias, username=None, display_name=None, user_id=None, created_by=None):
    alias_key = str(alias or '').strip().lower().replace('@', '')
    username = (username or '').strip().lstrip('@') or None
    now = datetime.now(TIMEZONE).isoformat()
    conn = _connect()
    c = conn.cursor()
    c.execute('''
        INSERT INTO employee_aliases (alias, user_id, username, display_name, created_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(alias) DO UPDATE SET
            user_id=COALESCE(excluded.user_id, employee_aliases.user_id),
            username=COALESCE(excluded.username, employee_aliases.username),
            display_name=COALESCE(excluded.display_name, employee_aliases.display_name),
            updated_at=excluded.updated_at
    ''', (alias_key, user_id, username, display_name or alias, created_by, now, now))
    conn.commit()
    conn.close()


def get_employee_alias(alias):
    alias_key = str(alias or '').strip().lower().replace('@', '')
    conn = _connect(row_factory=True)
    c = conn.cursor()
    c.execute('''
        SELECT * FROM employee_aliases
        WHERE lower(alias) = lower(?) OR lower(display_name) = lower(?) OR lower(username) = lower(?)
        LIMIT 1
    ''', (alias_key, alias_key, alias_key))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def list_employee_aliases():
    conn = _connect(row_factory=True)
    c = conn.cursor()
    c.execute("SELECT * FROM employee_aliases ORDER BY alias ASC")
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]



def _project_from_row(row):
    project = dict(row)
    project["created_at"] = parse_db_datetime(project.get("created_at"))
    project["updated_at"] = parse_db_datetime(project.get("updated_at"))
    return project


def create_project(alias, name, description=None, default_chat_alias=None, created_by=None):
    alias_key = str(alias or name or "project").strip().lower().replace("ё", "е")
    alias_key = alias_key.replace("@", "")
    import re as _re
    alias_key = _re.sub(r"\s+", "_", alias_key)
    alias_key = _re.sub(r"[^0-9a-zа-я_\-]+", "", alias_key, flags=_re.IGNORECASE).strip("_-") or "project"
    now = datetime.now(TIMEZONE).isoformat()
    conn = _connect()
    c = conn.cursor()
    c.execute('''
        INSERT INTO projects (alias, name, description, default_chat_alias, status, created_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
        ON CONFLICT(alias) DO UPDATE SET
            name=excluded.name,
            description=COALESCE(excluded.description, projects.description),
            default_chat_alias=COALESCE(excluded.default_chat_alias, projects.default_chat_alias),
            status='active',
            updated_at=excluded.updated_at
    ''', (alias_key, name, description, default_chat_alias, created_by, now, now))
    project_id = c.lastrowid
    if not project_id:
        c.execute("SELECT id FROM projects WHERE alias = ?", (alias_key,))
        row = c.fetchone()
        project_id = row[0] if row else None
    conn.commit()
    conn.close()
    return project_id


def get_project_by_alias_or_name(value):
    if not value:
        return None
    key = str(value).strip().lower().replace("ё", "е")
    key_us = key.replace(" ", "_")
    conn = _connect(row_factory=True)
    c = conn.cursor()
    c.execute('''
        SELECT * FROM projects
        WHERE lower(alias) = lower(?) OR lower(name) = lower(?) OR lower(alias) = lower(?)
        LIMIT 1
    ''', (key, key, key_us))
    row = c.fetchone()
    if not row:
        c.execute('''
            SELECT * FROM projects
            WHERE lower(name) LIKE lower(?) OR lower(alias) LIKE lower(?)
            ORDER BY updated_at DESC LIMIT 1
        ''', (f"%{key}%", f"%{key}%"))
        row = c.fetchone()
    conn.close()
    return _project_from_row(row) if row else None


def get_projects(status='active', limit=100):
    conn = _connect(row_factory=True)
    c = conn.cursor()
    if status:
        c.execute("SELECT * FROM projects WHERE status = ? ORDER BY name ASC LIMIT ?", (status, int(limit)))
    else:
        c.execute("SELECT * FROM projects ORDER BY name ASC LIMIT ?", (int(limit),))
    rows = c.fetchall()
    conn.close()
    return [_project_from_row(r) for r in rows]


def _op_task_from_row(row):
    task = dict(row)
    for key in ("due_date", "next_check_at", "last_check_at", "last_status_at", "created_at", "updated_at"):
        task[key] = parse_db_datetime(task.get(key))
    return task


def create_operational_task(
    project_id=None,
    parent_task_id=None,
    title=None,
    description=None,
    assigned_to_name=None,
    assigned_to_username=None,
    assigned_to_user_id=None,
    assigned_by=None,
    chat_alias=None,
    chat_id=None,
    deadline_text=None,
    due_date=None,
    status='active',
    progress=0,
    priority='normal',
    risk_level='низкий',
    control_enabled=False,
    control_cadence_days=1,
    next_check_at=None,
):
    now = datetime.now(TIMEZONE).isoformat()
    conn = _connect()
    c = conn.cursor()
    c.execute('''
        INSERT INTO operational_tasks
        (project_id, parent_task_id, title, description, assigned_to_name, assigned_to_username, assigned_to_user_id,
         assigned_by, chat_alias, chat_id, deadline_text, due_date, status, progress, priority, risk_level,
         control_enabled, control_cadence_days, next_check_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        project_id, parent_task_id, title or 'Операционная задача', description,
        assigned_to_name, (assigned_to_username or '').lstrip('@') or None, assigned_to_user_id,
        assigned_by, chat_alias, chat_id, deadline_text, _to_db_datetime(due_date), status,
        int(progress or 0), priority, risk_level, 1 if control_enabled else 0,
        int(control_cadence_days or 1), _to_db_datetime(next_check_at), now, now,
    ))
    task_id = c.lastrowid
    conn.commit()
    conn.close()
    return task_id


def get_operational_task(task_id):
    conn = _connect(row_factory=True)
    c = conn.cursor()
    c.execute('''
        SELECT t.*, p.name AS project_name, p.alias AS project_alias
        FROM operational_tasks t
        LEFT JOIN projects p ON p.id = t.project_id
        WHERE t.id = ?
    ''', (task_id,))
    row = c.fetchone()
    conn.close()
    return _op_task_from_row(row) if row else None


def get_operational_tasks(project_id=None, assigned_to=None, chat_id=None, status='active', parent_task_id_marker='top', limit=100):
    conditions = []
    params = []
    if project_id is not None:
        conditions.append("t.project_id = ?")
        params.append(project_id)
    if assigned_to:
        key = str(assigned_to).strip().lower().lstrip('@')
        conditions.append("(lower(t.assigned_to_name) LIKE lower(?) OR lower(t.assigned_to_username) = lower(?))")
        params.extend([f"%{key}%", key])
    if chat_id is not None:
        conditions.append("t.chat_id = ?")
        params.append(chat_id)
    if status:
        conditions.append("t.status = ?")
        params.append(status)
    if parent_task_id_marker == 'top':
        conditions.append("t.parent_task_id IS NULL")
    elif parent_task_id_marker is not None:
        conditions.append("t.parent_task_id = ?")
        params.append(parent_task_id_marker)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(int(limit))
    conn = _connect(row_factory=True)
    c = conn.cursor()
    c.execute(f'''
        SELECT t.*, p.name AS project_name, p.alias AS project_alias
        FROM operational_tasks t
        LEFT JOIN projects p ON p.id = t.project_id
        {where}
        ORDER BY CASE t.status WHEN 'blocked' THEN 0 WHEN 'active' THEN 1 ELSE 2 END,
                 t.next_check_at IS NULL, t.next_check_at ASC, t.updated_at DESC
        LIMIT ?
    ''', params)
    rows = c.fetchall()
    conn.close()
    return [_op_task_from_row(r) for r in rows]


def get_due_operational_tasks(now=None):
    now = now or datetime.now(TIMEZONE)
    conn = _connect(row_factory=True)
    c = conn.cursor()
    c.execute('''
        SELECT t.*, p.name AS project_name, p.alias AS project_alias
        FROM operational_tasks t
        LEFT JOIN projects p ON p.id = t.project_id
        WHERE t.status = 'active'
          AND t.control_enabled = 1
          AND t.next_check_at IS NOT NULL
          AND t.next_check_at <= ?
        ORDER BY t.next_check_at ASC
    ''', (_to_db_datetime(now),))
    rows = c.fetchall()
    conn.close()
    return [_op_task_from_row(r) for r in rows]


def update_operational_task_status(task_id, progress=None, status=None, risk_level=None, last_status_text=None):
    current = get_operational_task(task_id)
    if not current:
        return False
    if progress is None:
        progress = current.get('progress')
    if status is None:
        status = current.get('status')
    if risk_level is None:
        risk_level = current.get('risk_level')
    now = datetime.now(TIMEZONE).isoformat()
    conn = _connect()
    c = conn.cursor()
    c.execute('''
        UPDATE operational_tasks
        SET progress = ?, status = ?, risk_level = ?, last_status_text = COALESCE(?, last_status_text),
            last_status_at = ?, updated_at = ?
        WHERE id = ?
    ''', (int(progress or 0), status, risk_level, last_status_text, now, now, task_id))
    changed = c.rowcount
    conn.commit()
    conn.close()
    return changed > 0


def update_operational_task_control_ping(task_id, next_check_at=None, last_message_id=None, last_check_at=None):
    conn = _connect()
    c = conn.cursor()
    c.execute('''
        UPDATE operational_tasks
        SET next_check_at = COALESCE(?, next_check_at),
            last_check_at = COALESCE(?, last_check_at),
            last_check_message_id = COALESCE(?, last_check_message_id),
            updated_at = ?
        WHERE id = ?
    ''', (_to_db_datetime(next_check_at), _to_db_datetime(last_check_at), last_message_id, datetime.now(TIMEZONE).isoformat(), task_id))
    conn.commit()
    conn.close()


def mark_operational_task_done(task_id, status='done'):
    conn = _connect()
    c = conn.cursor()
    c.execute('''
        UPDATE operational_tasks
        SET status = ?, progress = 100, control_enabled = 0, updated_at = ?
        WHERE id = ?
    ''', (status, datetime.now(TIMEZONE).isoformat(), task_id))
    changed = c.rowcount
    conn.commit()
    conn.close()
    return changed > 0


def get_operational_task_by_last_message(chat_id, message_id):
    conn = _connect(row_factory=True)
    c = conn.cursor()
    c.execute('''
        SELECT t.*, p.name AS project_name, p.alias AS project_alias
        FROM operational_tasks t
        LEFT JOIN projects p ON p.id = t.project_id
        WHERE t.chat_id = ? AND t.last_check_message_id = ? AND t.status = 'active'
        ORDER BY t.updated_at DESC
        LIMIT 1
    ''', (chat_id, message_id))
    row = c.fetchone()
    conn.close()
    return _op_task_from_row(row) if row else None


def create_task_update(task_id, user_id=None, username=None, author_name=None, update_text='', progress=None, status=None, risk_level=None):
    conn = _connect()
    c = conn.cursor()
    c.execute('''
        INSERT INTO operational_task_updates
        (task_id, user_id, username, author_name, update_text, progress, status, risk_level, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (task_id, user_id, username, author_name, update_text, progress, status, risk_level, datetime.now(TIMEZONE).isoformat()))
    update_id = c.lastrowid
    conn.commit()
    conn.close()
    return update_id


def get_task_updates(task_id, limit=20):
    conn = _connect(row_factory=True)
    c = conn.cursor()
    c.execute('''
        SELECT * FROM operational_task_updates
        WHERE task_id = ?
        ORDER BY created_at DESC
        LIMIT ?
    ''', (task_id, int(limit)))
    rows = c.fetchall()
    conn.close()
    result = []
    for r in rows:
        item = dict(r)
        item['created_at'] = parse_db_datetime(item.get('created_at'))
        result.append(item)
    return result


def save_team_memory_event(chat_id=None, chat_title=None, user_id=None, username=None, author_name=None, event_type=None, content='', task_id=None, project_id=None):
    if not content:
        return None
    conn = _connect()
    c = conn.cursor()
    c.execute('''
        INSERT INTO team_memory_events
        (chat_id, chat_title, user_id, username, author_name, event_type, content, task_id, project_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (chat_id, chat_title, user_id, username, author_name, event_type, content, task_id, project_id, datetime.now(TIMEZONE).isoformat()))
    event_id = c.lastrowid
    conn.commit()
    conn.close()
    return event_id


def get_recent_team_events(chat_id=None, project_id=None, limit=20):
    conditions = []
    params = []
    if chat_id is not None:
        conditions.append("chat_id = ?")
        params.append(chat_id)
    if project_id is not None:
        conditions.append("project_id = ?")
        params.append(project_id)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(int(limit))
    conn = _connect(row_factory=True)
    c = conn.cursor()
    c.execute(f'''
        SELECT * FROM team_memory_events
        {where}
        ORDER BY created_at DESC
        LIMIT ?
    ''', params)
    rows = c.fetchall()
    conn.close()
    result = []
    for r in rows:
        item = dict(r)
        item['created_at'] = parse_db_datetime(item.get('created_at'))
        result.append(item)
    return result
