from __future__ import annotations

import datetime
from pathlib import Path

from ouroboros.codex_proxy import bootstrap_refresh_missing_access_tokens
from ouroboros.utils import append_jsonl

log = __import__("logging").getLogger(__name__)


def prewarm_codex_accounts(drive_root: Path) -> None:
    try:
        log.warning("DEBUG bootstrap START")
        result = bootstrap_refresh_missing_access_tokens()
        log.warning("DEBUG bootstrap DONE result=%s", result)
        append_jsonl(drive_root / "logs" / "supervisor.jsonl", {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "type": "codex_accounts_bootstrap_refresh",
            **result,
        })
    except Exception as e:
        append_jsonl(drive_root / "logs" / "supervisor.jsonl", {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "type": "codex_accounts_bootstrap_refresh_failed",
            "error": repr(e),
        })
