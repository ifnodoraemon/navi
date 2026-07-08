import re

with open("src/navi/runs/store.py", "r") as f:
    content = f.read()

content = content.replace("        with connect(self.db_path) as conn:\n            check_schema_version(conn, \"runs\", RUN_STORE_SCHEMA_VERSION)\n            conn.execute(RUNS_TABLE.ddl)\n            assert_schema_exact(conn, RUNS_TABLE)\n            conn.execute(EXECUTION_LOGS_TABLE.ddl)\n            conn.execute(TOOL_CALL_LOGS_TABLE.ddl)\n", "        with connect(self.db_path) as conn:\n            check_schema_version(conn, \"runs\", RUN_STORE_SCHEMA_VERSION)\n            conn.execute(RUNS_TABLE.ddl)\n            assert_schema_exact(conn, RUNS_TABLE)\n            conn.execute(TOOL_CALL_LOGS_TABLE.ddl)\n")

with open("src/navi/runs/store.py", "w") as f:
    f.write(content)
