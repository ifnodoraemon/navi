with open("src/navi/goals.py", "r") as f:
    content = f.read()

new_migration = """
    @staticmethod
    def _migrate_goals(conn) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(goals)")}
        if "cron_schedule" not in columns:
            conn.execute("ALTER TABLE goals ADD COLUMN cron_schedule TEXT NOT NULL DEFAULT ''")
        if "next_run_at" not in columns:
            conn.execute("ALTER TABLE goals ADD COLUMN next_run_at REAL NOT NULL DEFAULT 0.0")

"""

init_db_replacement = """    def _init_db(self) -> None:
        with connect(self.db_path) as conn:
            self._migrate_goals(conn)
            check_schema_version(conn, "goals", GOAL_STORE_SCHEMA_VERSION)"""

if "def _migrate_goals" not in content:
    content = content.replace("    def _init_db(self) -> None:", new_migration + init_db_replacement)
    with open("src/navi/goals.py", "w") as f:
        f.write(content)
