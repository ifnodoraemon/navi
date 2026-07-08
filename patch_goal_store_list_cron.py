import re

with open("src/navi/goals.py", "r") as f:
    content = f.read()

new_method = """    def list_cron_goals(self) -> typing.List[Goal]:
        with connect(self.db_path) as conn:
            rows = conn.execute(
                \"\"\"
                SELECT id, objective, phase, governance, acceptance, resolution, source, peer_id, sender_id, session_id,
                       workspace, run_id, trace_id, evidence_json, blocked_reason,
                       stop_condition, timeout, max_retries,
                       created_at, updated_at, completed_at,
                       parent_goal_id, task_status, cron_schedule, next_run_at
                FROM goals
                WHERE cron_schedule != ''
                ORDER BY created_at DESC
                \"\"\"
            ).fetchall()
        return [Goal(*row) for row in rows]
"""

content = content.replace("    def due_cron_goals(self", new_method + "\n    def due_cron_goals(self")

with open("src/navi/goals.py", "w") as f:
    f.write(content)
