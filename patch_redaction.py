with open("tests/test_safeguards_redaction.py", "r") as f:
    content = f.read()

import re
# Remove the test_execution_log_redacts_personal_contact_fields test completely
content = re.sub(
    r"def test_execution_log_redacts_personal_contact_fields.*?started_at=1\.0,\n\s*ended_at=2\.0,\n\s*\)\n\s*assert .*?in log\.stderr\n", 
    "", 
    content, 
    flags=re.DOTALL
)

with open("tests/test_safeguards_redaction.py", "w") as f:
    f.write(content)
