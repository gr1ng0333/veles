"""Time orientation tools — fast UTC/MSK/current timezone status."""

from __future__ import annotations

import json
from datetime import datetime, timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import List

from ouroboros.tools.registry import ToolContext, ToolEntry

DEFAULT_TZ = "Europe/Moscow"


def _to_view(dt: datetime, tz_name: str) -> dict:
    return {
        "timezone": tz_name,
        "iso": dt.isoformat(),
        "weekday": dt.strftime("%A"),
        "time": dt.strftime("%H:%M:%S"),
        "date": dt.strftime("%Y-%m-%d"),
    }


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
        "utc": _to_view(now_utc, "UTC"),
        "moscow": _to_view(msk_dt, DEFAULT_TZ),
        "requested": _to_view(requested_dt, requested_name),
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
