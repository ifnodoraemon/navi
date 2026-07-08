import re

with open("src/navi/goals.py", "r") as f:
    content = f.read()

# Update GOAL_STORE_SCHEMA_VERSION to 4
content = content.replace("GOAL_STORE_SCHEMA_VERSION = 3", "GOAL_STORE_SCHEMA_VERSION = 4")

# Update create() signature and insertion
create_sig_old = """        timeout: float = 0.0,
        max_retries: int = 0,
        parent_goal_id: str = "",
        task_status: str = "in_progress",
    ) -> Goal:"""
create_sig_new = """        timeout: float = 0.0,
        max_retries: int = 0,
        parent_goal_id: str = "",
        task_status: str = "in_progress",
        cron_schedule: str = "",
        next_run_at: float = 0.0,
    ) -> Goal:"""
content = content.replace(create_sig_old, create_sig_new)

create_obj_old = """            completed_at=0.0,
            parent_goal_id=parent_goal_id,
            task_status=task_status,
        )"""
create_obj_new = """            completed_at=0.0,
            parent_goal_id=parent_goal_id,
            task_status=task_status,
            cron_schedule=cron_schedule,
            next_run_at=next_run_at,
        )"""
content = content.replace(create_obj_old, create_obj_new)

insert_sql_old = """                INSERT INTO goals(
                    id, objective, phase, governance, acceptance, resolution, source, peer_id, sender_id, session_id,
                    workspace, run_id, trace_id, evidence_json, blocked_reason,
                    stop_condition, timeout, max_retries,
                    created_at, updated_at, completed_at,
                    parent_goal_id, task_status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
insert_sql_new = """                INSERT INTO goals(
                    id, objective, phase, governance, acceptance, resolution, source, peer_id, sender_id, session_id,
                    workspace, run_id, trace_id, evidence_json, blocked_reason,
                    stop_condition, timeout, max_retries,
                    created_at, updated_at, completed_at,
                    parent_goal_id, task_status, cron_schedule, next_run_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
content = content.replace(insert_sql_old, insert_sql_new)

insert_args_old = """                    goal.completed_at,
                    goal.parent_goal_id,
                    goal.task_status,
                ),"""
insert_args_new = """                    goal.completed_at,
                    goal.parent_goal_id,
                    goal.task_status,
                    goal.cron_schedule,
                    goal.next_run_at,
                ),"""
content = content.replace(insert_args_old, insert_args_new)

# Update all SELECT queries!
select_old = """SELECT id, objective, phase, governance, acceptance, resolution, source, peer_id, sender_id, session_id,
                       workspace, run_id, trace_id, evidence_json, blocked_reason,
                       stop_condition, timeout, max_retries,
                       created_at, updated_at, completed_at,
                       parent_goal_id, task_status"""
select_new = """SELECT id, objective, phase, governance, acceptance, resolution, source, peer_id, sender_id, session_id,
                       workspace, run_id, trace_id, evidence_json, blocked_reason,
                       stop_condition, timeout, max_retries,
                       created_at, updated_at, completed_at,
                       parent_goal_id, task_status, cron_schedule, next_run_at"""
content = content.replace(select_old, select_new)

# Update GOALS_TABLE
table_old = """        Column("parent_goal_id", "TEXT", nullable=False),
        Column("task_status", "TEXT", nullable=False),
    ],"""
table_new = """        Column("parent_goal_id", "TEXT", nullable=False),
        Column("task_status", "TEXT", nullable=False),
        Column("cron_schedule", "TEXT", nullable=False),
        Column("next_run_at", "REAL", nullable=False),
    ],"""
content = content.replace(table_old, table_new)

with open("src/navi/goals.py", "w") as f:
    f.write(content)
