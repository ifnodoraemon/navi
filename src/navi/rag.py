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
    """Provides semantic-like search over large codebases using SQLite FTS5."""

    def __init__(self, workspace: Path, db_path: Path | None = None):
        self.workspace = workspace
        self.db_path = db_path or workspace / ".navi" / "codebase_rag.db"
        self._init_db()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS codebase_fts USING fts5(
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
            return

        logger.info(f"Indexing workspace: {self.workspace}")
        start = time.time()

        with connect(self.db_path) as conn:
            conn.execute("DELETE FROM codebase_fts")

            count = 0
            for file_path in self._iter_files():
                try:
                    rel_path = file_path.relative_to(self.workspace).as_posix()
                    content = file_path.read_text(encoding="utf-8", errors="ignore")
                    conn.execute(
                        "INSERT INTO codebase_fts(path, content) VALUES (?, ?)", (rel_path, content)
                    )
                    count += 1
                except Exception as e:
                    logger.debug(f"Failed to read {file_path}: {e}")

            conn.execute(
                "INSERT OR REPLACE INTO codebase_meta (id, last_indexed) VALUES (1, ?)",
                (time.time(),),
            )

        logger.info(f"Indexed {count} files in {time.time() - start:.2f}s")

    def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        self.index()
        results: list[SearchResult] = []

        # Simple trigram-like query matching using FTS5 match syntax
        match_query = " OR ".join(f'"{token}"*' for token in query.split() if token.strip())
        if not match_query:
            return []

        with connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                SELECT path, snippet(codebase_fts, 1, '[[[', ']]]', '...', 10) as matched_snippet, rank
                FROM codebase_fts 
                WHERE codebase_fts MATCH ? 
                ORDER BY rank 
                LIMIT ?
                """,
                (match_query, limit),
            )

            results = []
            for path, snippet, rank in cursor.fetchall():
                results.append(SearchResult(path=path, content=snippet, rank=rank))

            # If literal trigram fails, try token-based OR query
            if not results:
                tokens = [t for t in query.split() if len(t) > 2]
                if tokens:
                    or_query = " OR ".join(f'"{t.replace('"', '""')}"' for t in tokens)
                    cursor = conn.execute(
                        """
                        SELECT path, snippet(codebase_fts, 1, '[[[', ']]]', '...', 10) as matched_snippet, rank
                        FROM codebase_fts 
                        WHERE codebase_fts MATCH ? 
                        ORDER BY rank 
                        LIMIT ?
                        """,
                        (or_query, limit),
                    )
                    for path, snippet, rank in cursor.fetchall():
                        results.append(SearchResult(path=path, content=snippet, rank=rank))

            return results
