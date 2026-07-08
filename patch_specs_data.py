import re

with open("src/navi/specs_data.py", "r") as f:
    content = f.read()

# Remove watch endpoints
content = re.sub(r'\s*"watches": "/v1/watches",', "", content)
content = re.sub(r'\s*"active_watches": "/v1/active/watches",', "", content)
content = re.sub(r'\s*"active_watches_process": "/v1/active/watches/process",', "", content)

# Remove watch.create and watch.delete safeguards
safeguard_to_remove = """        "watch.create": {
            "risk_class": "medium",
            "sensitive_contexts": ["scheduled_activity"],
            "confirmation_required": False,
            "reason_code": "capability_safeguard_watch_create",
        },
        "watch.delete": {
            "risk_class": "high",
            "sensitive_contexts": ["scheduled_activity"],
            "confirmation_required": True,
            "reason_code": "capability_safeguard_watch_delete",
        },"""
content = content.replace(safeguard_to_remove, "")

# Remove execution_watch
exec_watch_to_remove = """    "execution_watch": {
        "title": "Watch Scheduling",
        "capabilities": ["watch.create", "watch.delete"],
    },"""
content = content.replace(exec_watch_to_remove, "")

# Clean up docstrings
content = content.replace("Convert verified task or watch results", "Convert verified task results")
content = content.replace('"Background watch delivery.", ', '')
content = content.replace("notification text to task, watch, or execution", "notification text to task or execution")
content = content.replace('"Watch execution through the actuator protocol.",', '')

with open("src/navi/specs_data.py", "w") as f:
    f.write(content)
