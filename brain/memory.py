"""
Jarvis Memory System
=====================
SQLite-based persistent memory with semantic recall.

Stores:
- Interaction history (all commands, results, timestamps)
- User preferences (learned + explicit)
- Workflows (user-defined + auto-detected patterns)
"""

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from core import config

logger = logging.getLogger("jarvis.memory")


class MemorySystem:
    """
    Persistent memory using SQLite.
    
    Features:
    - Interaction logging for all commands
    - User preference storage (explicit + inferred)
    - Contextual recall for LLM prompt injection
    - Periodic cleanup of old data
    """

    def __init__(self):
        cfg = config.get("memory", default={})
        self._db_path = cfg.get("db_path", "")
        self._max_interactions = cfg.get("max_interactions", 10000)
        self._context_window = cfg.get("context_window", 10)
        self._conn: Optional[sqlite3.Connection] = None

    async def initialize(self) -> None:
        """Create database and tables if needed."""
        if not self._db_path:
            logger.warning("No memory DB path configured — memory disabled")
            return

        db_path = Path(self._db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")

        self._create_tables()
        logger.info("Memory system initialized at %s", db_path)

    def _create_tables(self) -> None:
        """Create database schema."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS interactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                user_input TEXT NOT NULL,
                action TEXT,
                params TEXT,
                result TEXT,
                success INTEGER DEFAULT 1,
                response_time_ms REAL
            );

            CREATE TABLE IF NOT EXISTS preferences (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                confidence REAL DEFAULT 1.0,
                last_updated REAL NOT NULL,
                source TEXT DEFAULT 'explicit',
                access_count INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                importance REAL DEFAULT 0.5
            );

            CREATE INDEX IF NOT EXISTS idx_interactions_timestamp
                ON interactions(timestamp);
            CREATE INDEX IF NOT EXISTS idx_interactions_action
                ON interactions(action);
            CREATE INDEX IF NOT EXISTS idx_facts_category
                ON facts(category);
        """)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Interaction logging
    # ------------------------------------------------------------------

    async def log_interaction(self, data: dict) -> None:
        """Log an action completion event."""
        if self._conn is None:
            return

        try:
            self._conn.execute(
                """INSERT INTO interactions
                   (timestamp, user_input, action, params, result, success, response_time_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    time.time(),
                    data.get("text", data.get("user_input", "")),
                    data.get("action", ""),
                    json.dumps(data.get("params", {})),
                    str(data.get("result", data.get("response", "")))[:500],
                    1 if "error" not in data else 0,
                    data.get("elapsed", 0) * 1000,
                ),
            )
            self._conn.commit()

            # Cleanup if too many
            count = self._conn.execute(
                "SELECT COUNT(*) FROM interactions"
            ).fetchone()[0]

            if count > self._max_interactions:
                self._conn.execute(
                    """DELETE FROM interactions WHERE id IN
                       (SELECT id FROM interactions ORDER BY timestamp ASC LIMIT ?)""",
                    (count - self._max_interactions,),
                )
                self._conn.commit()

        except Exception:
            logger.exception("Failed to log interaction")

    def get_recent_interactions(self, limit: int = 10) -> list[dict]:
        """Get recent interactions for context."""
        if self._conn is None:
            return []

        rows = self._conn.execute(
            """SELECT user_input, action, result, timestamp
               FROM interactions ORDER BY timestamp DESC LIMIT ?""",
            (limit,),
        ).fetchall()

        return [dict(row) for row in reversed(rows)]

    # ------------------------------------------------------------------
    # Preferences
    # ------------------------------------------------------------------

    def set_preference(
        self,
        key: str,
        value: Any,
        source: str = "explicit",
        confidence: float = 1.0,
    ) -> None:
        """Store a user preference."""
        if self._conn is None:
            return

        self._conn.execute(
            """INSERT OR REPLACE INTO preferences
               (key, value, confidence, last_updated, source, access_count)
               VALUES (?, ?, ?, ?, ?,
                       COALESCE((SELECT access_count FROM preferences WHERE key = ?), 0))""",
            (key, json.dumps(value), confidence, time.time(), source, key),
        )
        self._conn.commit()
        logger.debug("Preference set: %s = %s (source=%s)", key, value, source)

    def get_preference(self, key: str, default: Any = None) -> Any:
        """Get a user preference."""
        if self._conn is None:
            return default

        row = self._conn.execute(
            "SELECT value FROM preferences WHERE key = ?",
            (key,),
        ).fetchone()

        if row:
            # Update access count
            self._conn.execute(
                """UPDATE preferences SET access_count = access_count + 1
                   WHERE key = ?""",
                (key,),
            )
            self._conn.commit()
            return json.loads(row[0])

        return default

    def get_all_preferences(self) -> dict[str, Any]:
        """Get all preferences as a dict."""
        if self._conn is None:
            return {}

        rows = self._conn.execute(
            "SELECT key, value FROM preferences ORDER BY key"
        ).fetchall()

        return {row[0]: json.loads(row[1]) for row in rows}

    # ------------------------------------------------------------------
    # Facts / semantic memory
    # ------------------------------------------------------------------

    def remember(self, content: str, category: str = "general", importance: float = 0.5) -> None:
        """Store a fact or piece of information."""
        if self._conn is None:
            return

        self._conn.execute(
            """INSERT INTO facts (category, content, created_at, importance)
               VALUES (?, ?, ?, ?)""",
            (category, content, time.time(), importance),
        )
        self._conn.commit()
        logger.debug("Remembered: [%s] %s", category, content[:100])

    def recall(self, query: str = "", category: str = "", limit: int = 5) -> list[str]:
        """
        Recall facts matching a query or category.
        
        Uses simple keyword matching (semantic search is future phase).
        """
        if self._conn is None:
            return []

        if category:
            rows = self._conn.execute(
                """SELECT content FROM facts WHERE category = ?
                   ORDER BY importance DESC, created_at DESC LIMIT ?""",
                (category, limit),
            ).fetchall()
        elif query:
            # Simple keyword search
            keywords = query.lower().split()
            conditions = " AND ".join(
                [f"LOWER(content) LIKE ?" for _ in keywords]
            )
            params = [f"%{kw}%" for kw in keywords]
            params.append(limit)

            rows = self._conn.execute(
                f"""SELECT content FROM facts WHERE {conditions}
                    ORDER BY importance DESC, created_at DESC LIMIT ?""",
                params,
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT content FROM facts
                   ORDER BY importance DESC, created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()

        return [row[0] for row in rows]

    # ------------------------------------------------------------------
    # Context for LLM
    # ------------------------------------------------------------------

    def get_context_for_llm(self) -> str:
        """
        Build a memory context string for the LLM prompt.
        
        Includes recent interactions and key preferences.
        """
        parts = []

        # Recent interactions
        recent = self.get_recent_interactions(5)
        if recent:
            parts.append("Recent commands:")
            for interaction in recent:
                parts.append(f"  - \"{interaction['user_input']}\" → {interaction['action']}")

        # Key preferences
        prefs = self.get_all_preferences()
        if prefs:
            parts.append("\nUser preferences:")
            for key, value in list(prefs.items())[:10]:
                parts.append(f"  - {key}: {value}")

        return "\n".join(parts) if parts else "No memory data available yet."

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self) -> None:
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
            logger.info("Memory system closed")