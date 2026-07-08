import re

with open("src/navi/goals.py", "r") as f:
    content = f.read()

# Add due_cron_goals and mark_cron_run methods to GoalStore
new_methods = """

    def due_cron_goals(self, now: float) -> typing.List[Goal]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                \"\"\"
                SELECT id, objective, phase, governance, acceptance, resolution, source, peer_id, sender_id, session_id,
                       workspace, run_id, trace_id, evidence_json, blocked_reason,
                       stop_condition, timeout, max_retries,
                       created_at, updated_at, completed_at,
                       parent_goal_id, task_status, cron_schedule, next_run_at
                FROM goals
                WHERE cron_schedule != '' AND next_run_at <= ? AND phase != ?
                ORDER BY next_run_at ASC
                \"\"\",
                (now, Phase.ENDED)
            ).fetchall()
        return [Goal(*row) for row in rows]

    def update_cron_run(self, goal_id: str, next_run_at: float) -> None:
        now = time.time()
        with connect(self.db_path) as conn:
            conn.execute(
                "UPDATE goals SET next_run_at = ?, updated_at = ? WHERE id = ?",
                (next_run_at, now, goal_id),
            )
"""
# Insert before GOALS_TABLE
content = content.replace("\nGOALS_TABLE = Table(", new_methods + "\nGOALS_TABLE = Table(")

with open("src/navi/goals.py", "w") as f:
    f.write(content)
