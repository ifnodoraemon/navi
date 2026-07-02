import sqlite3
import json

db_path = "/home/ifnodoraemon/myagent/navi/.navi/traces.db"
trace_id = "7478290259560560776"

with sqlite3.connect(db_path) as conn:
    events = conn.execute(
        "SELECT id, phase, tool, ok, input_json, output_json, created_at, message FROM trace_events WHERE trace_id = ? ORDER BY created_at ASC",
        (trace_id,)
    ).fetchall()

for ev in events[-50:]:
    id, phase, tool, ok, input_json, output_json, created_at, message = ev
    if phase == "planner.syscall":
        print(f"[{phase}] tool={tool} message={message}")
    elif phase == "loop.decision":
        print(f"[{phase}] tool={tool} message={message}")
    elif "repeated" in message.lower():
        print(f"!!! LOOP DETECTED: {message}")

