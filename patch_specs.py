import re

with open("src/navi/specs_data.py", "r") as f:
    content = f.read()

# Add cron_schedule parameter to goal_open
new_param = """                    "cron_schedule": {
                        "type": "string",
                        "description": "Optional cron expression (e.g. '*/5 * * * *') to run this goal repeatedly. If provided, the goal runs periodically.",
                    },
"""
content = content.replace('                    "verification_command": {', new_param + '                    "verification_command": {')

with open("src/navi/specs_data.py", "w") as f:
    f.write(content)
