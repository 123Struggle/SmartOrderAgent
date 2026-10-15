"""会话凭证和预订草稿；每次操作独立连接，支持同机多 worker。"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time


@contextmanager
def connection():
    path = Path(os.getenv("APP_STATE_DB", str(Path(__file__).resolve().parents[1] / ".runtime" / "state.db")))
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, expires REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reservation_drafts (
                id TEXT PRIMARY KEY, session_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                payload TEXT NOT NULL, expires REAL NOT NULL, order_id INTEGER
            );
            CREATE INDEX IF NOT EXISTS draft_session ON reservation_drafts(session_id, fingerprint);
        """)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_session():
    session_id, token = secrets.token_urlsafe(24), secrets.token_urlsafe(32)
    with connection() as conn:
        conn.execute("INSERT INTO sessions VALUES (?, ?, ?)",
                     (session_id, hashlib.sha256(token.encode()).hexdigest(), time.time() + 7 * 86400))
    return {"session_id": session_id, "session_token": token}


def authenticate(token):
    with connection() as conn:
        row = conn.execute("SELECT id FROM sessions WHERE token_hash=? AND expires>?",
                           (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
    return row["id"] if row else None


def create_draft(session_id, payload):
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    fingerprint = hashlib.sha256(data.encode()).hexdigest()
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT id FROM reservation_drafts WHERE session_id=? AND fingerprint=? AND expires>? ORDER BY expires DESC LIMIT 1",
            (session_id, fingerprint, time.time()),
        ).fetchone()
        draft_id = existing["id"] if existing else secrets.token_urlsafe(24)
        if not existing:
            conn.execute("INSERT INTO reservation_drafts VALUES (?, ?, ?, ?, ?, NULL)",
                         (draft_id, session_id, fingerprint, data, time.time() + 1800))
    return get_draft(session_id, draft_id)


def get_draft(session_id, draft_id):
    with connection() as conn:
        row = conn.execute("SELECT * FROM reservation_drafts WHERE id=? AND session_id=?",
                           (draft_id, session_id)).fetchone()
    if row is None:
        raise LookupError("预订草稿不存在")
    if row["order_id"] is None and row["expires"] <= time.time():
        raise ValueError("预订草稿已过期，请重新填写")
    return {"draft_id": row["id"], "details": json.loads(row["payload"]), "order_id": row["order_id"]}


def pending_draft(session_id):
    with connection() as conn:
        row = conn.execute("SELECT id FROM reservation_drafts WHERE session_id=? AND order_id IS NULL AND expires>? ORDER BY expires DESC LIMIT 1",
                           (session_id, time.time())).fetchone()
    return get_draft(session_id, row["id"]) if row else None


def mark_submitted(session_id, draft_id, order_id):
    with connection() as conn:
        conn.execute("UPDATE reservation_drafts SET order_id=? WHERE id=? AND session_id=?",
                     (order_id, draft_id, session_id))
