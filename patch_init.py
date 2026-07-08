import re

with open("src/navi/runs/__init__.py", "r") as f:
    content = f.read()

content = content.replace("    ExecutionLog,\n", "")
content = content.replace("    \"ExecutionLog\",\n", "")

with open("src/navi/runs/__init__.py", "w") as f:
    f.write(content)

with open("src/navi/runs/models.py", "r") as f:
    content = f.read()

content = re.sub(r"@dataclass\(frozen=True\)\nclass ExecutionLog:\n(?:    .*\n)*\n", "", content)
content = re.sub(r"@dataclass\(frozen=True\)\nclass Watch:\n(?:    .*\n)*\n", "", content)

with open("src/navi/runs/models.py", "w") as f:
    f.write(content)
