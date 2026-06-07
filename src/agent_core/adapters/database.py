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
                    steps_json TEXT, reasoning_content TEXT,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_images (
                    id TEXT PRIMARY KEY, base64 TEXT NOT NULL,
                    ref_count INTEGER DEFAULT 1, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_memory_cards (
                    id TEXT PRIMARY KEY, flow_hash TEXT,
                    intent_summary TEXT,
                    intent_vector_json TEXT,
                    steps_json TEXT, success_count INTEGER DEFAULT 0,
                    total_rounds INTEGER DEFAULT 0,
                    trigger_count INTEGER DEFAULT 0,
                    scene_tag TEXT, namespace TEXT,
                    experience_notes TEXT,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_edges_ml (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level INTEGER NOT NULL,
                    from_code INTEGER NOT NULL,
                    to_code INTEGER NOT NULL,
                    total_count INTEGER DEFAULT 0,
                    namespace TEXT,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE(level, from_code, to_code, namespace)
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
                CREATE TABLE IF NOT EXISTS agent_chat_mentions (
                    chat_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (chat_id, name)
                );
                CREATE INDEX IF NOT EXISTS idx_agent_chat_mentions_chat
                    ON agent_chat_mentions(chat_id);
            """)
            conn.commit()
            # Migrate existing databases
            try:
                conn.execute("ALTER TABLE agent_messages ADD COLUMN reasoning_content TEXT")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE agent_messages ADD COLUMN root_id TEXT")
            except Exception:
                pass
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

    def add_agent_message(self, session_id, role, content, type="text", steps_json=None, reasoning_content=None, root_id=None):
        mid = str(uuid.uuid4())
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO agent_messages (id,session_id,role,type,content,steps_json,reasoning_content,root_id,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (mid, session_id, role, type, content, steps_json, reasoning_content, root_id, now),
            )
            conn.execute("UPDATE agent_sessions SET updated_at=? WHERE id=?", (now, session_id))
            conn.commit()
        return mid

    def get_agent_messages(self, session_id, root_id=None):
        with self._conn() as conn:
            if root_id:
                rows = conn.execute(
                    "SELECT id, role, type, content, steps_json, created_at FROM agent_messages WHERE session_id=? AND root_id=? ORDER BY created_at ASC",
                    (session_id, root_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, role, type, content, steps_json, created_at FROM agent_messages WHERE session_id=? AND root_id IS NULL ORDER BY created_at ASC",
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
                                  ("flow_signature", "flow_signature_json"), ("steps", "steps_json"),
                                  ("experience_notes", "experience_notes")]:
                    try:
                        val = r.pop(col)
                        if isinstance(val, str):
                            r[key] = json.loads(val)
                        else:
                            r[key] = val or []
                    except Exception:
                        r[key] = []
                cards.append(r)
            return cards

    def save_memory_card(self, card):
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO agent_memory_cards (id, flow_hash, intent_summary,
                    intent_vector_json, steps_json,
                    success_count, total_rounds,
                    trigger_count, scene_tag, namespace, created_at, updated_at,
                    experience_notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    intent_summary=excluded.intent_summary,
                    intent_vector_json=excluded.intent_vector_json,
                    steps_json=excluded.steps_json,
                    success_count=excluded.success_count,
                    total_rounds=excluded.total_rounds,
                    trigger_count=excluded.trigger_count,
                    namespace=excluded.namespace,
                    experience_notes=excluded.experience_notes,
                    updated_at=excluded.updated_at
            """, (
                card.get("id"), card.get("flow_hash"), card.get("intent_summary"),
                json.dumps(card.get("intent_vector") or [], ensure_ascii=False),
                json.dumps(card.get("steps") or [], ensure_ascii=False),
                int(card.get("success_count") or 0), int(card.get("total_rounds") or 0),
                int(card.get("trigger_count") or 0), card.get("scene_tag"),
                card.get("namespace"), float(card.get("created_at") or 0), float(card.get("updated_at") or 0),
                json.dumps(card.get("experience_notes") or [], ensure_ascii=False),
            ))
            conn.commit()

    def delete_memory_card(self, card_id):
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM agent_memory_cards WHERE id=?", (card_id,))
            conn.commit()
            return cur.rowcount > 0

    def save_ml_edge(self, edge):
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO agent_edges_ml (level, from_code, to_code, total_count, namespace, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(level, from_code, to_code, namespace) DO UPDATE SET
                    total_count=total_count+excluded.total_count, updated_at=excluded.updated_at
            """, (
                int(edge.get("level") or 2),
                int(edge.get("from_code") or 0),
                int(edge.get("to_code") or 0),
                int(edge.get("total_count") or 0),
                edge.get("namespace"),
                float(edge.get("created_at") or 0), float(edge.get("updated_at") or 0),
            ))
            conn.commit()

    def load_ml_edges(self, level=None, from_code=None, namespace=None):
        with self._conn() as conn:
            where = []
            params = []
            if level is not None:
                where.append("level=?")
                params.append(level)
            if from_code is not None:
                where.append("from_code=?")
                params.append(from_code)
            if namespace:
                where.append("namespace=?")
                params.append(namespace)
            sql = "SELECT * FROM agent_edges_ml"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY total_count DESC"
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [dict(r) for r in rows]

    def load_chat_mentions(self, chat_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT name, user_id FROM agent_chat_mentions WHERE chat_id=?",
                (chat_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def save_chat_mention(self, chat_id: str, name: str, user_id: str) -> None:
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO agent_chat_mentions (chat_id, name, user_id, updated_at) VALUES (?, ?, ?, ?)",
                (chat_id, name, user_id, now),
            )
            conn.commit()

    def delete_chat_mentions(self, chat_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM agent_chat_mentions WHERE chat_id=?", (chat_id,))
            conn.commit()
