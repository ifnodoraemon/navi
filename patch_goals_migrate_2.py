with open("src/navi/goals.py", "r") as f:
    content = f.read()

import re
new_migration = """    @staticmethod
    def _migrate_goals(conn) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(goals)")}
        if "parent_goal_id" not in columns:
            conn.execute("ALTER TABLE goals ADD COLUMN parent_goal_id TEXT NOT NULL DEFAULT ''")
        if "task_status" not in columns:
            conn.execute("ALTER TABLE goals ADD COLUMN task_status TEXT NOT NULL DEFAULT 'in_progress'")
        if "cron_schedule" not in columns:
            conn.execute("ALTER TABLE goals ADD COLUMN cron_schedule TEXT NOT NULL DEFAULT ''")
        if "next_run_at" not in columns:
            conn.execute("ALTER TABLE goals ADD COLUMN next_run_at REAL NOT NULL DEFAULT 0.0")"""

content = re.sub(
    r"    @staticmethod\n    def _migrate_goals\(conn\) -> None:.*?ALTER TABLE goals ADD COLUMN next_run_at REAL NOT NULL DEFAULT 0\.0\"\)",
    new_migration,
    content,
    flags=re.DOTALL
)

with open("src/navi/goals.py", "w") as f:
    f.write(content)
