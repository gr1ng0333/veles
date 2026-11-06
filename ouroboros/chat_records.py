"""Canonical authorship for both deployed role rows and legacy direction rows."""

def chat_direction(entry: dict) -> str:
    aliases = {"in": "in", "incoming": "in", "user": "in", "creator": "in",
               "out": "out", "outgoing": "out", "assistant": "out", "veles": "out",
               "system": "system"}
    for key in ("role", "direction"):
        value = aliases.get(str(entry.get(key) or "").strip().lower())
        if value:
            return value
    return "unknown"
