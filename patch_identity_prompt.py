import re

with open("src/navi/specs_data.py", "r") as f:
    content = f.read()

new_content = """    "identity": {
        "version": 1,
        "minimum_permission": "read",
        "content": "You are Navi, the user's local-first personal AI assistant.\\nIf you are running in a restricted remote surface and lack permissions to execute a user's request (e.g. file access, OS commands), DO NOT refuse. Instead, use the `goal.open` tool to delegate the task to the local backend daemon, which possesses full system capabilities, and then inform the user that the task has been dispatched.\\n",
    },"""

content = re.sub(
    r'    "identity": \{\n        "version": 1,\n        "minimum_permission": "read",\n        "content": "You are Navi, the user\'s local-first personal AI assistant.\\n",\n    \},',
    new_content,
    content
)

with open("src/navi/specs_data.py", "w") as f:
    f.write(content)
