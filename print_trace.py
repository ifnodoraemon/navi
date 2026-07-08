from navi.trace_store import TraceStore
from pathlib import Path
import json
store = TraceStore(Path(".navi"))
trace = store.get_trace("7480478893734366472")
for event in trace:
    print(json.dumps(event.to_dict(), ensure_ascii=False))
