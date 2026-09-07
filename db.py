"""Database connection management, schema, and reusable queries.

Two backends behind one set of functions -- callers never branch on which
is active:

- SQLite (default, local development): a single file, zero setup.
- PostgreSQL (production, when DATABASE_URL is set): used on hosts with an
  ephemeral filesystem, where SQLite's single file would be wiped on every
  restart/redeploy. A free provider (e.g. Neon) works for this -- see
  README's "Deploying for free" section.

The schema and queries are near-identical between the two; the only real
dialect differences (placeholder syntax, autoincrement, and how to pull the
date out of a stored ISO-8601 timestamp) are isolated in this module via the
Conn wrapper below, so query functions read the same regardless of backend.
"""
import os
import json
import sqlite3
import datetime
import logging

from flask import g, current_app

logger = logging.getLogger(__name__)

try:
    import psycopg
    from psycopg.rows import dict_row
    INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg.IntegrityError)
except ImportError:  # psycopg is only required when DATABASE_URL is actually used
    psycopg = None
    INTEGRITY_ERRORS = (sqlite3.IntegrityError,)

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    roll TEXT,
    class TEXT,
    section TEXT,
    reg_no TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attendance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    timestamp TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS face_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    data BLOB NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_artifacts (
    key TEXT PRIMARY KEY,
    data BLOB NOT NULL,
    meta_json TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('super_admin', 'user')),
    is_active INTEGER NOT NULL DEFAULT 1,
    is_approved INTEGER NOT NULL DEFAULT 0,
    is_verified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_attendance_student ON attendance(student_id);
CREATE INDEX IF NOT EXISTS idx_attendance_timestamp ON attendance(timestamp);
CREATE INDEX IF NOT EXISTS idx_students_name ON students(name);
CREATE INDEX IF NOT EXISTS idx_face_images_student ON face_images(student_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email);
"""

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    roll TEXT,
    class TEXT,
    section TEXT,
    reg_no TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attendance (
    id SERIAL PRIMARY KEY,
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    timestamp TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS face_images (
    id SERIAL PRIMARY KEY,
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    data BYTEA NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_artifacts (
    key TEXT PRIMARY KEY,
    data BYTEA NOT NULL,
    meta_json TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('super_admin', 'user')),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    is_approved BOOLEAN NOT NULL DEFAULT FALSE,
    is_verified BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_attendance_student ON attendance(student_id);
CREATE INDEX IF NOT EXISTS idx_attendance_timestamp ON attendance(timestamp);
CREATE INDEX IF NOT EXISTS idx_students_name ON students(name);
CREATE INDEX IF NOT EXISTS idx_face_images_student ON face_images(student_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email);
"""


class Conn:
    """Wraps a sqlite3 or psycopg connection behind one interface: `?`
    placeholders (translated to `%s` for Postgres), dict-like rows on both
    (sqlite3.Row and psycopg's dict_row both support row["col"]), and a
    date_expr() helper for the one recurring dialect difference queries
    below actually need.
    """

    def __init__(self, raw, is_pg):
        self.raw = raw
        self.is_pg = is_pg

    def _sql(self, sql):
        return sql.replace("?", "%s") if self.is_pg else sql

    def execute(self, sql, params=()):
        return self.raw.execute(self._sql(sql), params)

    def executemany(self, sql, seq_of_params):
        cur = self.raw.cursor()
        cur.executemany(self._sql(sql), seq_of_params)
        return cur

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()

    def date_expr(self, column):
        # Timestamps are always stored as ISO-8601 text (YYYY-MM-DDTHH:MM:SS...),
        # so the first 10 characters are exactly the date part -- a plain
        # substring, unlike a date cast/parse, is IMMUTABLE in Postgres and
        # can be used in an index expression (a ::date or to_date() cast is
        # only STABLE there and Postgres rejects it for that reason).
        return f"substring({column}, 1, 10)" if self.is_pg else f"substr({column}, 1, 10)"


def _connect(database_path, database_url=None):
    if database_url:
        if psycopg is None:
            raise RuntimeError("DATABASE_URL is set but psycopg is not installed (pip install psycopg[binary])")
        raw = psycopg.connect(database_url, row_factory=dict_row)
        return Conn(raw, is_pg=True)
    raw = sqlite3.connect(database_path)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    return Conn(raw, is_pg=False)


def _dedupe_existing_attendance(conn):
    """Keep the earliest attendance row per (student_id, date) and drop the rest.

    Only runs when the unique index can't be created because older data
    (from before this constraint existed) already has duplicates.
    """
    date_col = conn.date_expr("timestamp")
    rows = conn.execute(
        f"SELECT id, student_id, {date_col} AS d FROM attendance ORDER BY timestamp ASC"
    ).fetchall()
    seen = set()
    to_delete = []
    for row in rows:
        key = (row["student_id"], row["d"])
        if key in seen:
            to_delete.append(row["id"])
        else:
            seen.add(key)
    if to_delete:
        conn.executemany("DELETE FROM attendance WHERE id=?", [(i,) for i in to_delete])
        logger.warning("Removed %d duplicate attendance record(s) to apply uniqueness constraint", len(to_delete))
    return len(to_delete)


def init_db(database_path, database_url=None):
    """Create schema on a fresh database, or safely upgrade an existing one."""
    is_pg = bool(database_url)
    if not is_pg:
        os.makedirs(os.path.dirname(database_path) or ".", exist_ok=True)
    conn = _connect(database_path, database_url)
    try:
        schema = POSTGRES_SCHEMA if is_pg else SQLITE_SCHEMA
        for statement in schema.split(";"):
            statement = statement.strip()
            if statement:
                conn.execute(statement)
        conn.commit()

        unique_index_sql = (
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_attendance_unique_daily "
            f"ON attendance(student_id, {conn.date_expr('timestamp')})"
        )
        try:
            conn.execute(unique_index_sql)
            conn.commit()
        except INTEGRITY_ERRORS:
            conn.rollback()
            _dedupe_existing_attendance(conn)
            conn.commit()
            conn.execute(unique_index_sql)
            conn.commit()
    finally:
        conn.close()


def get_db():
    if "db" not in g:
        g.db = _connect(current_app.config["DATABASE_PATH"], current_app.config.get("DATABASE_URL"))
    return g.db


def close_db(exception=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def register_db(app):
    init_db(app.config["DATABASE_PATH"], app.config.get("DATABASE_URL"))
    app.teardown_appcontext(close_db)


# ---------- Reusable queries ----------

def create_student(name, roll, class_, section, reg_no):
    db = get_db()
    now = datetime.datetime.utcnow().isoformat()
    cur = db.execute(
        "INSERT INTO students (name, roll, class, section, reg_no, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
        (name, roll, class_, section, reg_no, now),
    )
    row = cur.fetchone()
    db.commit()
    return row["id"]


# SQLite's INTEGER storage class (and Postgres's INTEGER column) is a
# signed 64-bit int. Flask's <int:id> URL converter has no upper bound
# (Python ints are arbitrary precision), so a URL like
# /students/999999999999999999999 would otherwise reach the database driver
# and raise OverflowError, surfacing as an unhandled 500 instead of a clean
# 404 for what is simply never a valid id.
_MAX_INT64 = 2**63 - 1


def get_student(student_id):
    if not (0 <= student_id <= _MAX_INT64):
        return None
    db = get_db()
    return db.execute(
        "SELECT id, name, roll, class, section, reg_no, created_at FROM students WHERE id=?",
        (student_id,),
    ).fetchone()


def delete_student(student_id):
    if not (0 <= student_id <= _MAX_INT64):
        return
    db = get_db()
    db.execute("DELETE FROM students WHERE id=?", (student_id,))
    db.commit()


def list_students(search=None):
    db = get_db()
    if search:
        like = f"%{search}%"
        rows = db.execute(
            """SELECT id, name, roll, class, section, reg_no, created_at FROM students
               WHERE name LIKE ? OR roll LIKE ? OR reg_no LIKE ? ORDER BY id DESC""",
            (like, like, like),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT id, name, roll, class, section, reg_no, created_at FROM students ORDER BY id DESC"
        ).fetchall()
    return rows


def count_students():
    db = get_db()
    return db.execute("SELECT COUNT(*) AS c FROM students").fetchone()["c"]


def attendance_count_for_student(student_id):
    db = get_db()
    return db.execute(
        "SELECT COUNT(*) AS c FROM attendance WHERE student_id=?", (student_id,)
    ).fetchone()["c"]


def has_marked_today(student_id):
    db = get_db()
    today = datetime.date.today().isoformat()
    return db.execute(
        f"SELECT id FROM attendance WHERE student_id=? AND {db.date_expr('timestamp')}=?",
        (student_id, today),
    ).fetchone() is not None


def mark_attendance(student_id, name):
    """Insert an attendance row. Relies on the unique index as the source of
    truth for same-day duplicates (safe under concurrent requests); the
    caller should still pre-check has_marked_today() for a fast, friendly
    response before hitting the DB.
    """
    db = get_db()
    ts = datetime.datetime.utcnow().isoformat()
    try:
        db.execute(
            "INSERT INTO attendance (student_id, name, timestamp) VALUES (?, ?, ?)",
            (student_id, name, ts),
        )
        db.commit()
        return True
    except INTEGRITY_ERRORS:
        db.rollback()
        return False


def today_attendance_count():
    db = get_db()
    today = datetime.date.today().isoformat()
    return db.execute(
        f"SELECT COUNT(*) AS c FROM attendance WHERE {db.date_expr('timestamp')}=?", (today,)
    ).fetchone()["c"]


def recent_attendance(limit=8):
    db = get_db()
    return db.execute(
        "SELECT id, student_id, name, timestamp FROM attendance ORDER BY timestamp DESC LIMIT ?",
        (limit,),
    ).fetchall()


def attendance_records(period="all", search=None, page=1, per_page=25):
    db = get_db()
    date_col = db.date_expr("timestamp")
    where = []
    params = []
    if period == "daily":
        where.append(f"{date_col} = ?")
        params.append(datetime.date.today().isoformat())
    elif period == "weekly":
        where.append(f"{date_col} >= ?")
        params.append((datetime.date.today() - datetime.timedelta(days=7)).isoformat())
    elif period == "monthly":
        where.append(f"{date_col} >= ?")
        params.append((datetime.date.today() - datetime.timedelta(days=30)).isoformat())
    if search:
        where.append("name LIKE ?")
        params.append(f"%{search}%")

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    total = db.execute(f"SELECT COUNT(*) AS c FROM attendance {where_sql}", params).fetchone()["c"]

    offset = max(0, (page - 1) * per_page)
    rows = db.execute(
        f"SELECT id, student_id, name, timestamp FROM attendance {where_sql} "
        f"ORDER BY timestamp DESC LIMIT ? OFFSET ?",
        params + [per_page, offset],
    ).fetchall()
    return rows, total


def all_attendance_for_export():
    db = get_db()
    return db.execute(
        "SELECT id, student_id, name, timestamp FROM attendance ORDER BY timestamp DESC"
    ).fetchall()


def attendance_by_day(days=30):
    db = get_db()
    date_col = db.date_expr("timestamp")
    start = (datetime.date.today() - datetime.timedelta(days=days - 1)).isoformat()
    rows = db.execute(
        f"SELECT {date_col} AS d, COUNT(*) AS c FROM attendance WHERE {date_col} >= ? GROUP BY d",
        (start,),
    ).fetchall()
    counts_by_day = {str(row["d"]): row["c"] for row in rows}
    dates = [(datetime.date.today() - datetime.timedelta(days=i)) for i in range(days - 1, -1, -1)]
    labels = [d.strftime("%d-%b") for d in dates]
    counts = [counts_by_day.get(d.isoformat(), 0) for d in dates]
    return labels, counts


def attendance_by_student(limit=10):
    db = get_db()
    return db.execute(
        """SELECT s.id, s.name, COUNT(a.id) AS total
           FROM students s LEFT JOIN attendance a ON a.student_id = s.id
           GROUP BY s.id, s.name ORDER BY total DESC LIMIT ?""",
        (limit,),
    ).fetchall()


# ---------- Face images (database storage backend) ----------

def save_face_image(student_id, filename, data):
    db = get_db()
    now = datetime.datetime.utcnow().isoformat()
    db.execute(
        "INSERT INTO face_images (student_id, filename, data, created_at) VALUES (?, ?, ?, ?)",
        (student_id, filename, data, now),
    )
    db.commit()


def list_face_image_blobs(student_id):
    db = get_db()
    rows = db.execute(
        "SELECT data FROM face_images WHERE student_id=? ORDER BY id", (student_id,)
    ).fetchall()
    return [bytes(r["data"]) for r in rows]


def count_face_images(student_id):
    db = get_db()
    return db.execute(
        "SELECT COUNT(*) AS c FROM face_images WHERE student_id=?", (student_id,)
    ).fetchone()["c"]


def delete_face_images(student_id):
    db = get_db()
    db.execute("DELETE FROM face_images WHERE student_id=?", (student_id,))
    db.commit()


def student_ids_with_images():
    db = get_db()
    rows = db.execute("SELECT DISTINCT student_id FROM face_images").fetchall()
    return [r["student_id"] for r in rows]


# ---------- Model artifact (database storage backend) ----------

def save_model_artifact(key, data, meta_dict):
    db = get_db()
    now = datetime.datetime.utcnow().isoformat()
    meta_json = json.dumps(meta_dict) if meta_dict is not None else None
    db.execute(
        """INSERT INTO model_artifacts (key, data, meta_json, updated_at) VALUES (?, ?, ?, ?)
           ON CONFLICT (key) DO UPDATE SET
             data = excluded.data, meta_json = excluded.meta_json, updated_at = excluded.updated_at""",
        (key, data, meta_json, now),
    )
    db.commit()


def load_model_artifact(key):
    db = get_db()
    row = db.execute(
        "SELECT data, meta_json, updated_at FROM model_artifacts WHERE key=?", (key,)
    ).fetchone()
    if not row:
        return None
    meta = json.loads(row["meta_json"]) if row["meta_json"] else None
    return {"data": bytes(row["data"]), "meta": meta, "updated_at": row["updated_at"]}


# ---------- Users (authentication) ----------

_USER_COLUMNS = (
    "id, name, email, password_hash, role, is_active, is_approved, is_verified, "
    "created_at, updated_at, last_login_at"
)


def ensure_super_admin(name, email, password_hash):
    """Create the super_admin account if no user with this email exists yet.

    Never overwrites an existing account's password -- safe to call on
    every startup. This is the ONLY way SUPER_ADMIN_EMAIL/PASSWORD are ever
    used; once the row exists, login is entirely database-driven like any
    other account.
    """
    db = get_db()
    existing = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if existing:
        return
    now = datetime.datetime.utcnow().isoformat()
    db.execute(
        "INSERT INTO users (name, email, password_hash, role, is_active, is_approved, is_verified, "
        "created_at, updated_at) VALUES (?, ?, ?, 'super_admin', ?, ?, ?, ?, ?)",
        (name, email, password_hash, True, True, True, now, now),
    )
    db.commit()
    logger.info("Provisioned super_admin account for %s", email)


def create_user(name, email, password_hash, role="user", is_active=True, is_approved=False, is_verified=False):
    db = get_db()
    now = datetime.datetime.utcnow().isoformat()
    cur = db.execute(
        "INSERT INTO users (name, email, password_hash, role, is_active, is_approved, is_verified, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (name, email, password_hash, role, is_active, is_approved, is_verified, now, now),
    )
    row = cur.fetchone()
    db.commit()
    return row["id"]


def get_user(user_id):
    if not (0 <= user_id <= _MAX_INT64):
        return None
    db = get_db()
    return db.execute(f"SELECT {_USER_COLUMNS} FROM users WHERE id=?", (user_id,)).fetchone()


def get_user_by_email(email):
    db = get_db()
    return db.execute(f"SELECT {_USER_COLUMNS} FROM users WHERE email=?", (email,)).fetchone()


def list_users():
    db = get_db()
    return db.execute(f"SELECT {_USER_COLUMNS} FROM users ORDER BY created_at DESC").fetchall()


def count_super_admins(active_only=True):
    db = get_db()
    if active_only:
        row = db.execute(
            "SELECT COUNT(*) AS c FROM users WHERE role='super_admin' AND is_active=? AND is_approved=?",
            (True, True),
        ).fetchone()
    else:
        row = db.execute("SELECT COUNT(*) AS c FROM users WHERE role='super_admin'").fetchone()
    return row["c"]


def user_counts_by_status():
    """Returns {"total": n, "active": n, "pending": n, "disabled": n} for the admin dashboard."""
    db = get_db()
    total = db.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    pending = db.execute("SELECT COUNT(*) AS c FROM users WHERE is_approved=?", (False,)).fetchone()["c"]
    disabled = db.execute(
        "SELECT COUNT(*) AS c FROM users WHERE is_approved=? AND is_active=?", (True, False)
    ).fetchone()["c"]
    active = total - pending - disabled
    return {"total": total, "active": active, "pending": pending, "disabled": disabled}


def update_last_login(user_id):
    db = get_db()
    db.execute(
        "UPDATE users SET last_login_at=?, updated_at=? WHERE id=?",
        (datetime.datetime.utcnow().isoformat(), datetime.datetime.utcnow().isoformat(), user_id),
    )
    db.commit()


def set_user_approved(user_id, approved):
    db = get_db()
    db.execute(
        "UPDATE users SET is_approved=?, updated_at=? WHERE id=?",
        (approved, datetime.datetime.utcnow().isoformat(), user_id),
    )
    db.commit()


def set_user_active(user_id, active):
    db = get_db()
    db.execute(
        "UPDATE users SET is_active=?, updated_at=? WHERE id=?",
        (active, datetime.datetime.utcnow().isoformat(), user_id),
    )
    db.commit()


def set_user_role(user_id, role):
    db = get_db()
    db.execute(
        "UPDATE users SET role=?, updated_at=? WHERE id=?",
        (role, datetime.datetime.utcnow().isoformat(), user_id),
    )
    db.commit()


def delete_user(user_id):
    db = get_db()
    db.execute("DELETE FROM users WHERE id=?", (user_id,))
    db.commit()
