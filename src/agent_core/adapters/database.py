import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from agent_core.interfaces import DatabasePort


def _clean_env_value(v):
    if v is None:
        return ""
    s = str(v).strip()
    if len(s) >= 2 and ((s[0] == s[-1] == "`") or (s[0] == s[-1] == '"') or (s[0] == s[-1] == "'")):
        s = s[1:-1].strip()
    return s


class SqliteDatabase(DatabasePort):

    def __init__(self, db_path=""):
        _HERE = os.path.dirname(os.path.abspath(__file__))
        self.db_path = (
            _clean_env_value(db_path)
            or _clean_env_value(os.getenv("AGENT_DB_PATH"))
            or os.path.join(os.path.dirname(_HERE), "agent_data.db")
        )
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS agent_sessions (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '新对话',
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_messages (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    role TEXT NOT NULL, type TEXT, content TEXT NOT NULL,
                    steps_json TEXT, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_images (
                    id TEXT PRIMARY KEY, base64 TEXT NOT NULL,
                    ref_count INTEGER DEFAULT 1, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_memory_cards (
                    id TEXT PRIMARY KEY, flow_hash TEXT,
                    intent_summary TEXT, intent_examples_json TEXT,
                    intent_vector_json TEXT, flow_signature_json TEXT,
                    steps_json TEXT, success_count INTEGER DEFAULT 0,
                    total_rounds INTEGER DEFAULT 0,
                    approved_count INTEGER DEFAULT 0,
                    rejected_count INTEGER DEFAULT 0,
                    trigger_count INTEGER DEFAULT 0,
                    scene_tag TEXT, namespace TEXT,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_memory_edges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    from_method TEXT NOT NULL, from_path TEXT NOT NULL,
                    to_method TEXT NOT NULL, to_path TEXT NOT NULL,
                    relation_type TEXT DEFAULT 'FOLLOWS',
                    total_count INTEGER DEFAULT 0,
                    approved_count INTEGER DEFAULT 0,
                    namespace TEXT,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE(from_method, from_path, to_method, to_path, namespace)
                );
                CREATE INDEX IF NOT EXISTS idx_agent_messages_session
                    ON agent_messages(session_id);
                CREATE TABLE IF NOT EXISTS agent_chat_sessions (
                    chat_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_chat_sessions_session
                    ON agent_chat_sessions(session_id);
            """)
            conn.commit()

    def create_agent_session(self, title="新对话"):
        sid = str(uuid.uuid4())
        now = time.time()
        with self._conn() as conn:
            conn.execute("INSERT INTO agent_sessions (id,title,created_at,updated_at) VALUES (?,?,?,?)",
                         (sid, title[:200], now, now))
            conn.commit()
        return sid

    def get_agent_session(self, session_id):
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM agent_sessions WHERE id=?", (session_id,)).fetchone()
            return dict(row) if row else None

    def update_agent_session_title(self, session_id, title):
        with self._conn() as conn:
            conn.execute("UPDATE agent_sessions SET title=?, updated_at=? WHERE id=?",
                         (title[:200], time.time(), session_id))
            conn.commit()

    def list_agent_sessions(self, limit=50):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, title, created_at, updated_at FROM agent_sessions ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(limit, 200)),),
            ).fetchall()
            return [dict(r) for r in rows]

    def delete_agent_session(self, session_id):
        with self._conn() as conn:
            conn.execute("DELETE FROM agent_messages WHERE session_id=?", (session_id,))
            cur = conn.execute("DELETE FROM agent_sessions WHERE id=?", (session_id,))
            conn.commit()
            return cur.rowcount > 0

    def get_chat_session(self, chat_id):
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM agent_chat_sessions WHERE chat_id=?", (chat_id,)).fetchone()
            return dict(row) if row else None

    def set_chat_session(self, chat_id, session_id):
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO agent_chat_sessions (chat_id, session_id, created_at) VALUES (?, ?, ?)",
                (chat_id, session_id, now),
            )
            conn.commit()

    def delete_chat_session(self, chat_id):
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM agent_chat_sessions WHERE chat_id=?", (chat_id,))
            conn.commit()
            return cur.rowcount > 0

    def add_agent_message(self, session_id, role, content, type="text", steps_json=None):
        mid = str(uuid.uuid4())
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO agent_messages (id,session_id,role,type,content,steps_json,created_at) VALUES (?,?,?,?,?,?,?)",
                (mid, session_id, role, type, content, steps_json, now),
            )
            conn.execute("UPDATE agent_sessions SET updated_at=? WHERE id=?", (now, session_id))
            conn.commit()
        return mid

    def get_agent_messages(self, session_id):
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, role, type, content, steps_json, created_at FROM agent_messages WHERE session_id=? ORDER BY created_at ASC",
                (session_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_agent_images_batch(self, image_ids):
        if not image_ids:
            return []
        with self._conn() as conn:
            placeholders = ", ".join("?" * len(image_ids))
            rows = conn.execute(
                f"SELECT id, base64, created_at FROM agent_images WHERE id IN ({placeholders})",
                image_ids,
            ).fetchall()
            return [dict(r) for r in rows]

    def load_all_memory_cards(self, namespace=None):
        with self._conn() as conn:
            if namespace:
                rows = conn.execute(
                    "SELECT * FROM agent_memory_cards WHERE namespace=? ORDER BY updated_at DESC",
                    (namespace,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM agent_memory_cards ORDER BY updated_at DESC").fetchall()
            cards = []
            for row in rows:
                r = dict(row)
                for key, col in [("intent_examples", "intent_examples_json"), ("intent_vector", "intent_vector_json"),
                                  ("flow_signature", "flow_signature_json"), ("steps", "steps_json")]:
                    try:
                        r[key] = json.loads(r.pop(col, "[]"))
                    except Exception:
                        r[key] = []
                cards.append(r)
            return cards

    def save_memory_card(self, card):
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO agent_memory_cards (id, flow_hash, intent_summary, intent_examples_json,
                    intent_vector_json, flow_signature_json, steps_json,
                    success_count, total_rounds, approved_count, rejected_count,
                    trigger_count, scene_tag, namespace, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    intent_summary=excluded.intent_summary,
                    intent_examples_json=excluded.intent_examples_json,
                    intent_vector_json=excluded.intent_vector_json,
                    flow_signature_json=excluded.flow_signature_json,
                    steps_json=excluded.steps_json,
                    success_count=excluded.success_count,
                    total_rounds=excluded.total_rounds,
                    approved_count=excluded.approved_count,
                    rejected_count=excluded.rejected_count,
                    trigger_count=excluded.trigger_count,
                    namespace=excluded.namespace,
                    updated_at=excluded.updated_at
            """, (
                card.get("id"), card.get("flow_hash"), card.get("intent_summary"),
                json.dumps(card.get("intent_examples") or [], ensure_ascii=False),
                json.dumps(card.get("intent_vector") or [], ensure_ascii=False),
                json.dumps(card.get("flow_signature") or {}, ensure_ascii=False),
                json.dumps(card.get("steps") or [], ensure_ascii=False),
                int(card.get("success_count") or 0), int(card.get("total_rounds") or 0),
                int(card.get("approved_count") or 0), int(card.get("rejected_count") or 0),
                int(card.get("trigger_count") or 0), card.get("scene_tag"),
                card.get("namespace"), float(card.get("created_at") or 0), float(card.get("updated_at") or 0),
            ))
            conn.commit()

    def delete_memory_card(self, card_id):
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM agent_memory_cards WHERE id=?", (card_id,))
            conn.commit()
            return cur.rowcount > 0

    def load_all_memory_edges(self, namespace=None):
        with self._conn() as conn:
            if namespace:
                rows = conn.execute(
                    "SELECT * FROM agent_memory_edges WHERE namespace=? ORDER BY updated_at DESC",
                    (namespace,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM agent_memory_edges ORDER BY updated_at DESC").fetchall()
            return [dict(r) for r in rows]

    def save_memory_edge(self, edge):
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO agent_memory_edges (from_method, from_path, to_method, to_path,
                    relation_type, total_count, approved_count, namespace, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(from_method, from_path, to_method, to_path, namespace) DO UPDATE SET
                    relation_type=excluded.relation_type, total_count=excluded.total_count,
                    approved_count=excluded.approved_count, updated_at=excluded.updated_at
            """, (
                edge.get("from_method"), edge.get("from_path"),
                edge.get("to_method"), edge.get("to_path"),
                edge.get("relation_type") or "FOLLOWS",
                int(edge.get("total_count") or 0), int(edge.get("approved_count") or 0),
                edge.get("namespace"),
                float(edge.get("created_at") or 0), float(edge.get("updated_at") or 0),
            ))
            conn.commit()
