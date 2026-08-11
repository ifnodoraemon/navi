from __future__ import annotations

import hashlib
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .db import connect
from .trace import TraceStore


@dataclass(frozen=True, slots=True)
class EvolutionCandidate:
    id: str
    failure_domain: str
    evaluation_rule: str
    occurrences: int
    first_seen_at: float
    last_seen_at: float
    sample_trace_ids: tuple[str, ...]
    source: str = "trace_evaluations"
    next_action: str = "model_review_required"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["sample_trace_ids"] = list(self.sample_trace_ids)
        return data


class EvolutionCandidateScanner:
    """Project repeated trace failures without turning them into policy.

    The scanner deliberately stops before choosing a target, authoring a
    candidate, or creating a proposal. Those are semantic decisions owned by
    the model and governed by the evolution proposal/experiment/approval path.
    """

    def __init__(self, home: Path):
        self.store = TraceStore(home)

    def scan(
        self,
        *,
        window_days: int = 7,
        min_occurrences: int = 3,
        limit: int = 100,
        now: float | None = None,
    ) -> list[EvolutionCandidate]:
        cutoff = (now if now is not None else time.time()) - max(1, window_days) * 86400
        candidates: list[EvolutionCandidate] = []
        threshold = max(2, min_occurrences)
        classification_sql = """
            SELECT
                CASE
                    WHEN failure_domain = '' THEN 'unclassified'
                    ELSE failure_domain
                END AS classified_domain,
                CASE
                    WHEN json_valid(evidence_json) THEN COALESCE(
                        NULLIF(json_extract(evidence_json, '$.evaluation_rule'), ''),
                        NULLIF(json_extract(evidence_json, '$.rule'), ''),
                        NULLIF(json_extract(evidence_json, '$.reason'), ''),
                        'unclassified'
                    )
                    ELSE 'unclassified'
                END AS classified_rule,
                trace_id,
                created_at
            FROM trace_evaluations
            WHERE outcome = 'failure' AND created_at >= ?
        """
        with connect(self.store.db_path) as conn:
            groups = conn.execute(
                f"""
                WITH classified AS ({classification_sql})
                SELECT classified_domain, classified_rule, COUNT(*),
                       MIN(created_at), MAX(created_at)
                FROM classified
                GROUP BY classified_domain, classified_rule
                HAVING COUNT(*) >= ?
                ORDER BY COUNT(*) DESC, MAX(created_at) DESC
                LIMIT ?
                """,
                (cutoff, threshold, max(1, min(500, limit))),
            ).fetchall()
            for failure_domain, rule, occurrences, first_seen, last_seen in groups:
                sample_rows = conn.execute(
                    f"""
                    WITH classified AS ({classification_sql})
                    SELECT trace_id FROM classified
                    WHERE classified_domain = ? AND classified_rule = ?
                    ORDER BY created_at DESC LIMIT 10
                    """,
                    (cutoff, str(failure_domain), str(rule)),
                ).fetchall()
                sample_trace_ids = tuple(
                    str(row[0]) for row in reversed(sample_rows)
                )
            digest = hashlib.sha256(f"{failure_domain}\0{rule}".encode()).hexdigest()[:12]
            candidates.append(
                EvolutionCandidate(
                    id=f"trace-cluster-{digest}",
                    failure_domain=str(failure_domain),
                    evaluation_rule=str(rule),
                    occurrences=int(occurrences),
                    first_seen_at=float(first_seen),
                    last_seen_at=float(last_seen),
                    sample_trace_ids=sample_trace_ids,
                )
            )
        return candidates
