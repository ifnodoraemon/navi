import time
import json
from pathlib import Path
from src.navi.trace import TraceStore
store = TraceStore(Path("/home/ifnodoraemon/myagent/navi/.navi"))
start = time.time()
runs = store.list_run_views("7478290259560560776", limit=100000)
json_data = json.dumps([r.to_dict() for r in runs])
print(f"Runs: {len(runs)}, JSON length: {len(json_data)}, Time: {time.time() - start:.2f}s")
