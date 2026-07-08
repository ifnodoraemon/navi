import re

with open("src/navi/daemon.py", "r") as f:
    content = f.read()

replacement = """        # 2. Process Cron Goals
        from .cron import next_cron_time
        from .goals import GoalStore
        from .loop_control_service import LoopControlService
        from .loop_contracts import OpenGoalRequest
        import time
        import logging

        now = time.time()
        goal_store = GoalStore(self.home)
        service = LoopControlService(self.home)
        
        due_goals = goal_store.due_cron_goals(now)
        for g in due_goals:
            try:
                request = OpenGoalRequest(
                    objective=g.objective,
                    source="cron",
                    peer_id=g.peer_id,
                    sender_id=g.sender_id,
                    workspace=g.workspace,
                )
                service.open_goal(request)
                
                next_time = next_cron_time(g.cron_schedule, now=now)
                goal_store.update_cron_run(g.id, next_time)
                created.append({"cron_goal_id": g.id, "triggered": True})
            except Exception as e:
                logging.getLogger("navi.daemon").error(f"Failed to process cron goal {g.id}: {e}", exc_info=True)

        return created"""

content = content.replace("        # TODO: Rebuild cron watches using V2 Goal loops\n        return created", replacement)

with open("src/navi/daemon.py", "w") as f:
    f.write(content)
