#!/usr/bin/env python3
"""Extract diagnostic info from /tmp/codex_debug.json"""
import json, sys

try:
    d = json.load(open("/tmp/codex_debug.json"))
except FileNotFoundError:
    print("No debug file yet")
    sys.exit(0)

print(f"raw_tools_count: {d.get('raw_tools_count')}")
print(f"converted_tools_count: {d.get('converted_tools_count')}")

payload = d.get("payload", {})
print(f"payload has tools key: {'tools' in payload}")
print(f"payload tool_choice: {payload.get('tool_choice')}")
print(f"payload tools count: {len(payload.get('tools', []))}")
print(f"payload model: {payload.get('model')}")
print(f"instructions length: {len(payload.get('instructions', ''))}")

# Show first 2 tool names
tools = payload.get("tools", [])
for i, t in enumerate(tools[:5]):
    print(f"  tool[{i}]: type={t.get('type')} name={t.get('name')}")
if len(tools) > 5:
    print(f"  ... and {len(tools)-5} more tools")

# Show raw vs converted sample
print(f"\nraw_tools_sample (first 2):")
for t in d.get("raw_tools_sample", []):
    fn = t.get("function", {})
    print(f"  raw: type={t.get('type')} fn.name={fn.get('name')} fn.keys={list(fn.keys())}")

print(f"\nconverted_tools_sample (first 2):")
for t in d.get("converted_tools_sample", []):
    print(f"  conv: type={t.get('type')} name={t.get('name')} keys={list(t.keys())}")
