#!/usr/bin/env python3
"""لایه‌ی نازک SQLite برای smsgw — بدون ORM، فقط stdlib.

هر thread کانکشن خودش را دارد (ThreadingHTTPServer چندنخی است و کانکشن‌های
sqlite3 بین threadها قابل اشتراک نیستند). حالت autocommit است؛ برای چند
دستور اتمیک از context manager ‏`tx()` استفاده کن.
"""
import os, sqlite3, threading, time

DATA_DIR = os.environ.get("SMSGW_DATA_DIR",
                          os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
DB_PATH = os.path.join(DATA_DIR, "smsgw.db")

_local = threading.local()

# ---------------------------------------------------------------- migrations
# هر عضو این لیست یک گام مهاجرت است. فقط به انتها اضافه کن، هرگز ویرایش نکن.
MIGRATIONS = [
    # v1 — طرح پایه‌ی فاز ۲
    """
    CREATE TABLE users (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        username        TEXT    NOT NULL UNIQUE,
        password_hash   BLOB    NOT NULL,
        salt            BLOB    NOT NULL,
        role            TEXT    NOT NULL DEFAULT 'viewer',
        display_name    TEXT    NOT NULL DEFAULT '',
        enabled         INTEGER NOT NULL DEFAULT 1,
        must_change_pw  INTEGER NOT NULL DEFAULT 0,
        created_at      INTEGER NOT NULL,
        last_login_at   INTEGER,
        failed_attempts INTEGER NOT NULL DEFAULT 0,
        locked_until    INTEGER
    );

    CREATE TABLE sessions (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        token_hash TEXT    NOT NULL UNIQUE,
        csrf       TEXT    NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        user_agent TEXT    NOT NULL DEFAULT '',
        ip         TEXT    NOT NULL DEFAULT ''
    );
    CREATE INDEX idx_sessions_expires ON sessions(expires_at);
    CREATE INDEX idx_sessions_user    ON sessions(user_id);

    CREATE TABLE api_tokens (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        name         TEXT    NOT NULL UNIQUE,
        token_hash   TEXT    NOT NULL UNIQUE,
        scopes       TEXT    NOT NULL DEFAULT '',
        enabled      INTEGER NOT NULL DEFAULT 1,
        created_at   INTEGER NOT NULL,
        last_used_at INTEGER,
        created_by   INTEGER REFERENCES users(id) ON DELETE SET NULL
    );

    CREATE TABLE contacts (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT    NOT NULL,
        phone      TEXT    NOT NULL UNIQUE,
        note       TEXT    NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE INDEX idx_contacts_name ON contacts(name);

    CREATE TABLE contact_groups (
        id    INTEGER PRIMARY KEY AUTOINCREMENT,
        name  TEXT NOT NULL UNIQUE,
        color TEXT NOT NULL DEFAULT '#4f8cff'
    );

    CREATE TABLE contact_group_members (
        contact_id INTEGER NOT NULL REFERENCES contacts(id)       ON DELETE CASCADE,
        group_id   INTEGER NOT NULL REFERENCES contact_groups(id) ON DELETE CASCADE,
        PRIMARY KEY (contact_id, group_id)
    );
    CREATE INDEX idx_cgm_group ON contact_group_members(group_id);

    CREATE TABLE messages (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        direction   TEXT    NOT NULL,            -- in | out
        phone       TEXT    NOT NULL,
        content     TEXT    NOT NULL,
        status      TEXT    NOT NULL,            -- queued|sending|sent|failed|received
        error       TEXT    NOT NULL DEFAULT '',
        modem_index INTEGER,
        user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
        created_at  INTEGER NOT NULL,
        sent_at     INTEGER
    );
    CREATE INDEX idx_messages_phone_created ON messages(phone, created_at);
    CREATE INDEX idx_messages_dir_created   ON messages(direction, created_at);
    -- آینه‌سازی نباید تکراری بسازد؛ modem_index برای پیام‌های محلی NULL است
    -- و NULLها در UNIQUE سطح sqlite با هم برخورد نمی‌کنند.
    CREATE UNIQUE INDEX idx_messages_modem ON messages(direction, modem_index);

    CREATE TABLE queue (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        phone        TEXT    NOT NULL,
        content      TEXT    NOT NULL,
        scheduled_at INTEGER NOT NULL,
        attempts     INTEGER NOT NULL DEFAULT 0,
        status       TEXT    NOT NULL DEFAULT 'queued',  -- queued|sending|sent|failed|canceled
        error        TEXT    NOT NULL DEFAULT '',
        batch_id     TEXT    NOT NULL DEFAULT '',
        message_id   INTEGER REFERENCES messages(id) ON DELETE SET NULL,
        user_id      INTEGER REFERENCES users(id)    ON DELETE SET NULL,
        created_at   INTEGER NOT NULL,
        next_try_at  INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX idx_queue_status_sched ON queue(status, scheduled_at);
    CREATE INDEX idx_queue_batch        ON queue(batch_id);

    CREATE TABLE templates (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT    NOT NULL UNIQUE,
        content    TEXT    NOT NULL,
        created_at INTEGER NOT NULL,
        user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL
    );

    CREATE TABLE audit_log (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
        actor      TEXT    NOT NULL DEFAULT '',
        action     TEXT    NOT NULL,
        detail     TEXT    NOT NULL DEFAULT '',
        ip         TEXT    NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL
    );
    CREATE INDEX idx_audit_created ON audit_log(created_at);

    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    # v2 — حذف نرم پیام: مکالمه پاک می‌شود ولی آمار تاریخی دست‌نخورده است.
    """
    ALTER TABLE messages ADD COLUMN deleted_at INTEGER;
    ALTER TABLE messages ADD COLUMN deleted_by INTEGER REFERENCES users(id) ON DELETE SET NULL;
    CREATE INDEX idx_messages_visible_phone ON messages(deleted_at, phone, created_at);
    """,
]


def now():
    return int(time.time())


def conn():
    c = getattr(_local, "conn", None)
    if c is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=30.0, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA synchronous=NORMAL")
        _local.conn = c
    return c


def close():
    """کانکشن همین thread را می‌بندد (برای اسکریپت‌های کوتاه‌عمر)."""
    c = getattr(_local, "conn", None)
    if c is not None:
        c.close()
        _local.conn = None


class tx:
    """context manager برای تراکنش اتمیک روی کانکشن همین thread."""

    def __enter__(self):
        self.c = conn()
        self.c.execute("BEGIN IMMEDIATE")
        return self.c

    def __exit__(self, et, ev, tb):
        self.c.execute("ROLLBACK" if et else "COMMIT")
        return False


def q(sql, args=()):
    return conn().execute(sql, args).fetchall()


def q1(sql, args=()):
    return conn().execute(sql, args).fetchone()


def ex(sql, args=()):
    cur = conn().execute(sql, args)
    return cur


def insert(sql, args=()):
    return conn().execute(sql, args).lastrowid


def setting(key, default=None):
    r = q1("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else default


def set_setting(key, value):
    ex("INSERT INTO settings(key,value) VALUES(?,?) "
       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def migrate():
    """مهاجرت افزایشی بر پایه‌ی PRAGMA user_version. backward-compatible:
    فقط گام‌های جدید اجرا می‌شوند و دیتای موجود دست‌نخورده می‌ماند."""
    c = conn()
    v = c.execute("PRAGMA user_version").fetchone()[0]
    for i in range(v, len(MIGRATIONS)):
        # BEGIN/COMMIT باید داخل خود اسکریپت باشد: executescript هر تراکنش
        # بازِ بیرونی را قبل از اجرا implicit commit می‌کند.
        script = "BEGIN IMMEDIATE;\n%s\nPRAGMA user_version=%d;\nCOMMIT;" % (
            MIGRATIONS[i], i + 1)
        try:
            c.executescript(script)
        except Exception:
            try:
                c.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
    return len(MIGRATIONS), v


if __name__ == "__main__":
    to, frm = migrate()
    print("migrated %s -> %s (%s)" % (frm, to, DB_PATH))
    for r in q("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        print(" table", r["name"])
