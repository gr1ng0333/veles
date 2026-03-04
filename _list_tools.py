#!/usr/bin/env python3
import json
d = json.load(open("/tmp/codex_debug.json"))
tools = d["payload"]["tools"]
for t in tools:
    print(t["name"])
