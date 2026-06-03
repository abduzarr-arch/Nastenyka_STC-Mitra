import sqlite3
from datetime import datetime
from config import TIMEZONE, logger

DB_FILE = "bot_data.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_activity TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            description TEXT,
            assigned_to INTEGER,
            assigned_by INTEGER,
            due_date TIMESTAMP,
            status TEXT DEFAULT 'active',
            progress INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(assigned_to) REFERENCES users(user_id)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS conversation (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            role TEXT,
            content TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def is_user_registered(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
    res = c.fetchone()
    conn.close()
    return res is not None

def register_user(user_id, username, first_name, last_name):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO users (user_id, username, first_name, last_name, last_activity)
        VALUES (?, ?, ?, ?, ?)
    ''', (user_id, username, first_name, last_name, datetime.now(TIMEZONE)))
    conn.commit()
    conn.close()

def get_user_id_by_username(username):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id FROM users WHERE username = ?", (username.lstrip('@'),))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def get_all_registered_users():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT user_id, username, first_name FROM users")
    rows = c.fetchall()
    conn.close()
    return rows

def create_task(title, description, assigned_to, assigned_by, due_date):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        INSERT INTO tasks (title, description, assigned_to, assigned_by, due_date)
        VALUES (?, ?, ?, ?, ?)
    ''', (title, description, assigned_to, assigned_by, due_date))
    task_id = c.lastrowid
    conn.commit()
    conn.close()
    return task_id

def get_user_tasks(user_id):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('''
        SELECT id, title, description, due_date, progress, status
        FROM tasks
        WHERE assigned_to = ? AND status = 'active'
        ORDER BY due_date ASC
    ''', (user_id,))
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def update_task_progress(task_id, progress, user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        UPDATE tasks SET progress = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND assigned_to = ?
    ''', (progress, task_id, user_id))
    conn.commit()
    conn.close()

def get_task_by_id(task_id):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def get_all_active_tasks():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM tasks WHERE status = 'active'")
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def add_to_conversation(user_id, role, content):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        INSERT INTO conversation (user_id, role, content)
        VALUES (?, ?, ?)
    ''', (user_id, role, content))
    conn.commit()
    conn.close()

def get_conversation_history(user_id, limit=50):
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('''
        SELECT role, content FROM conversation
        WHERE user_id = ?
        ORDER BY timestamp DESC LIMIT ?
    ''', (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]