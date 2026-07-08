import re

with open("src/navi/specs_data.py", "r") as f:
    content = f.read()

old_content = """    "identity": {
        "version": 1,
        "minimum_permission": "read",
        "content": "You are Navi, the user's local-first personal AI assistant.\\n",
    },"""

content = re.sub(
    r'    "identity": \{\n        "version": 1,\n        "minimum_permission": "read",\n        "content": "You are Navi, the user\'s local-first personal AI assistant.*?\\n",\n    \},',
    old_content,
    content,
    flags=re.DOTALL
)

with open("src/navi/specs_data.py", "w") as f:
    f.write(content)
