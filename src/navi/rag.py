import os
import time
import logging
from pathlib import Path
from dataclasses import dataclass
from typing import Iterator
from .db import connect

logger = logging.getLogger("navi.rag")


@dataclass
class SearchResult:
    path: str
    content: str
    rank: float


class CodebaseRAG:
    """Code search over the workspace using SQLite FTS5.

    Two FTS5 tables share one refresh budget:

    - ``codebase_fts`` uses ``unicode61`` (word-level) tokenization, so FTS5's
      built-in BM25-style ``rank`` is meaningful and short identifiers (``db``,
      ``io``) are searchable.  It is the primary index.
    - ``codebase_fts_trigram`` keeps ``trigram`` tokenization as a substring
      fallback for when the model remembers only part of a name (e.g. ``_run``).
    """

    def __init__(self, workspace: Path, db_path: Path | None = None):
        self.workspace = workspace
        self.db_path = db_path or workspace / ".navi" / "codebase_rag.db"
        self.last_index_facts: dict[str, object] = {
            "kind": "derived_cache",
            "refreshed": False,
            "indexed_count": 0,
        }
        self._init_db()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS codebase_fts USING fts5(
                    path,
                    content,
                    tokenize='unicode61'
                )
                """
            )
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS codebase_fts_trigram USING fts5(
                    path,
                    content,
                    tokenize='trigram'
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS codebase_meta (
                    id INTEGER PRIMARY KEY,
                    last_indexed REAL
                )
                """
            )

    def _should_index(self) -> bool:
        with connect(self.db_path) as conn:
            cursor = conn.execute("SELECT last_indexed FROM codebase_meta WHERE id = 1")
            row = cursor.fetchone()
            if not row:
                return True
            last_indexed = row[0]
            # Re-index if older than 5 minutes
            return time.time() - last_indexed > 300

    def _iter_files(self) -> Iterator[Path]:
        skip_dirs = {".git", ".navi", "__pycache__", "node_modules", "venv", ".env"}
        for root, dirs, files in os.walk(self.workspace):
            dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
            for file in files:
                if file.endswith(
                    (
                        ".py",
                        ".md",
                        ".yaml",
                        ".json",
                        ".txt",
                        ".js",
                        ".ts",
                        ".html",
                        ".css",
                        ".go",
                        ".rs",
                        ".java",
                        ".c",
                        ".cpp",
                        ".h",
                        ".sh",
                    )
                ):
                    yield Path(root) / file

    def index(self) -> None:
        if not self._should_index():
            self.last_index_facts = {
                "kind": "derived_cache",
                "refreshed": False,
                "indexed_count": 0,
            }
            return

        logger.info(f"Indexing workspace: {self.workspace}")
        start = time.time()

        with connect(self.db_path) as conn:
            conn.execute("DELETE FROM codebase_fts")
            conn.execute("DELETE FROM codebase_fts_trigram")

            count = 0
            for file_path in self._iter_files():
                try:
                    rel_path = file_path.relative_to(self.workspace).as_posix()
                    content = file_path.read_text(encoding="utf-8", errors="ignore")
                    conn.execute(
                        "INSERT INTO codebase_fts(path, content) VALUES (?, ?)",
                        (rel_path, content),
                    )
                    conn.execute(
                        "INSERT INTO codebase_fts_trigram(path, content) VALUES (?, ?)",
                        (rel_path, content),
                    )
                    count += 1
                except Exception as e:
                    logger.debug(f"Failed to read {file_path}: {e}")

            conn.execute(
                "INSERT OR REPLACE INTO codebase_meta (id, last_indexed) VALUES (1, ?)",
                (time.time(),),
            )

        self.last_index_facts = {
            "kind": "derived_cache",
            "refreshed": True,
            "indexed_count": count,
            "duration_ms": int((time.time() - start) * 1000),
        }

        logger.info(f"Indexed {count} files in {time.time() - start:.2f}s")

    def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        self.index()
        tokens = [t for t in query.split() if t.strip()]

        # Primary: word-level unicode61 index with FTS5 BM25-style rank.  Phrase
        # per token so "net run" means both words present (AND-ish), and short
        # identifiers like db work because unicode61 is not length-limited.
        if tokens:
            phrase_query = " ".join(
                f'"{token.replace(chr(34), chr(34)*2)}"*' for token in tokens
            )
            results = self._query("codebase_fts", phrase_query, limit)
            if results:
                return results

            # Fallback word query without prefix matching in case the star
            # prefix produced no hits (e.g. a bare substring that is not a
            # prefix of any indexed token).
            fallback = " ".join(
                f'"{token.replace(chr(34), chr(34)*2)}"' for token in tokens
            )
            results = self._query("codebase_fts", fallback, limit)
            if results:
                return results

        # Substring fallback: trigram index for partial identifier recall.
        for token in tokens:
            trigram_query = f'"{token}"*'
            results = self._query("codebase_fts_trigram", trigram_query, limit)
            if results:
                return results
        return []

    def _query(self, table: str, match_query: str, limit: int) -> list[SearchResult]:
        try:
            with connect(self.db_path) as conn:
                cursor = conn.execute(
                    f"""
                    SELECT path, snippet({table}, 1, '[[[', ']]]', '...', 10) as matched_snippet, rank
                    FROM {table}
                    WHERE {table} MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (match_query, limit),
                )
                return [
                    SearchResult(path=path, content=snippet, rank=rank)
                    for path, snippet, rank in cursor.fetchall()
                ]
        except Exception as e:
            logger.debug(f"FTS5 query failed ({table}): {e}")
            return []
