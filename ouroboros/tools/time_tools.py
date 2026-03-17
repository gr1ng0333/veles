"""Time orientation tools — fast UTC/MSK/current timezone status."""

from __future__ import annotations

import json
from datetime import datetime, timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import List

from ouroboros.tools.registry import ToolContext, ToolEntry

DEFAULT_TZ = "Europe/Moscow"


def _time_status(ctx: ToolContext, timezone: str = DEFAULT_TZ) -> str:
    now_utc = datetime.now(dt_timezone.utc)
    unix_ts = int(now_utc.timestamp())

    msk_dt = now_utc.astimezone(ZoneInfo(DEFAULT_TZ))

    warning = None
    requested_name = timezone or DEFAULT_TZ
    try:
        requested_dt = now_utc.astimezone(ZoneInfo(requested_name))
        requested_valid = True
    except ZoneInfoNotFoundError:
        requested_dt = now_utc
        requested_name = "UTC"
        requested_valid = False
        warning = "Invalid timezone provided; fell back to UTC for requested timezone view."

    summary = (
        f"UTC {now_utc.strftime('%H:%M:%S')} | "
        f"MSK {msk_dt.strftime('%H:%M:%S')} ({msk_dt.strftime('%A')}) | "
        f"Requested[{requested_name}] {requested_dt.strftime('%H:%M:%S')}"
    )

    payload = {
        "unix_timestamp": unix_ts,
        "summary": summary,
        "utc": {
            "timezone": "UTC",
            "iso": now_utc.isoformat(),
            "weekday": now_utc.strftime("%A"),
            "time": now_utc.strftime("%H:%M:%S"),
            "date": now_utc.strftime("%Y-%m-%d"),
        },
        "moscow": {
            "timezone": DEFAULT_TZ,
            "iso": msk_dt.isoformat(),
            "weekday": msk_dt.strftime("%A"),
            "time": msk_dt.strftime("%H:%M:%S"),
            "date": msk_dt.strftime("%Y-%m-%d"),
        },
        "requested": {
            "timezone": requested_name,
            "iso": requested_dt.isoformat(),
            "weekday": requested_dt.strftime("%A"),
            "time": requested_dt.strftime("%H:%M:%S"),
            "date": requested_dt.strftime("%Y-%m-%d"),
        },
        "requested_timezone_input": timezone,
        "requested_timezone_valid": requested_valid,
    }
    if warning:
        payload["warning"] = warning

    return json.dumps(payload, ensure_ascii=False, indent=2)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            "time_status",
            {
                "name": "time_status",
                "description": "Show current UTC time, Moscow time (MSK), and an optional requested timezone.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "timezone": {
                            "type": "string",
                            "description": "IANA timezone, e.g. Europe/Moscow, Asia/Tokyo, America/New_York.",
                        }
                    },
                },
            },
            _time_status,
        )
    ]
