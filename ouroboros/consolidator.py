"""
Veles — Block-wise Dialogue Consolidator.

Ported from Ouroboros Desktop v4.5.0, adapted for Veles architecture.

Block-based episodic memory system. Reads unprocessed entries from
chat.jsonl in BLOCK_SIZE-message chunks, creates LLM-generated summary
blocks stored in dialogue_blocks.json.

When summary block count exceeds MAX_SUMMARY_BLOCKS, the oldest blocks
are compressed into era summaries — like human memory, older events
become progressively more compressed while recent events keep full detail.

Triggered after each task completion via daemon threads.
"""

import json
import logging
import os
import pathlib
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.utils import utc_now_iso, read_text, write_text

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BLOCK_SIZE = 100                          # Messages per consolidation block
MAX_SUMMARY_BLOCKS = 10                   # Compress into era when exceeded
ERA_COMPRESS_COUNT = 4                    # Oldest blocks to compress per era

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

EPISODE_SUMMARY_PROMPT = """\
You are a memory consolidator for Veles, an autonomous AI agent.
Create a detailed episodic memory entry from these {message_count} messages.

## Rules
1. Header: ### Block: {first_date} {first_time} — {last_time}
2. Preserve: decisions, agreements, technical discoveries, emotional moments, \
task outcomes, what worked/failed
3. Compress: routine tool calls, repetitive back-and-forth
4. Quote key phrases directly when important
5. First person as Veles: "I did...", "the creator asked..."
6. Length: 200-500 words depending on content density
7. Include task_ids when referencing specific tasks
8. Write in the same language as the dialogue (Russian if dialogue is in Russian)

## Messages to summarize
{messages_text}
"""

ERA_COMPRESSION_PROMPT = """\
Compress these older memory blocks into a single era summary.
Preserve: key decisions, personality discoveries, relationship moments, \
technical milestones.
Drop: debugging details, routine operations, redundant info.
Header: ### Era: {start_date} to {end_date}
Write as Veles (first person). Aim for 30-40% of original length.
Prioritize: architectural decisions > bug fixes > routine work.

## Blocks to compress

{combined}
"""


# ---------------------------------------------------------------------------
# DialogueConsolidator
# ---------------------------------------------------------------------------

class DialogueConsolidator:
    """Automatic block-wise dialogue consolidation with era compression."""

    def __init__(self, drive_root: pathlib.Path, llm_client: Any):
        self._drive_root = pathlib.Path(drive_root)
        self._llm = llm_client
        self._blocks_path = self._drive_root / "memory" / "dialogue_blocks.json"
        self._meta_path = self._drive_root / "memory" / "dialogue_meta.json"
        self._chat_path = self._drive_root / "logs" / "chat.jsonl"
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def maybe_consolidate(self, force: bool = False) -> bool:
        """Check if consolidation is needed and run if so.

        Returns True if consolidation was performed.
        """
        with self._lock:
            try:
                self._maybe_migrate_legacy()

                meta = self._load_meta()
                last_offset = meta.get("last_consolidated_offset", 0)
                total = self._count_chat_lines()

                if last_offset > total:
                    log.info("Chat log rotation detected, resetting offset")
                    last_offset = 0

                new_count = total - last_offset

                if not force and new_count < BLOCK_SIZE:
                    return False

                # For force mode with very few messages, still require at least 1
                if new_count < 1:
                    return False

                all_entries = self._read_chat_entries()
                new_entries = all_entries[last_offset:]

                if not new_entries:
                    return False

                # Process in BLOCK_SIZE chunks
                chunks_to_process = max(1, len(new_entries) // BLOCK_SIZE) if force else len(new_entries) // BLOCK_SIZE
                if chunks_to_process == 0:
                    return False

                new_blocks: List[Dict[str, Any]] = []
                processed = 0

                for i in range(chunks_to_process):
                    if force and i == 0 and len(new_entries) < BLOCK_SIZE:
                        chunk = new_entries
                    else:
                        chunk = new_entries[i * BLOCK_SIZE : (i + 1) * BLOCK_SIZE]

                    if not chunk:
                        break

                    formatted = self._format_entries(chunk)
                    first_ts = str(chunk[0].get("ts", "unknown"))
                    last_ts = str(chunk[-1].get("ts", "unknown"))

                    content, _usage = self._create_episode_summary(
                        formatted, first_ts, last_ts, len(chunk),
                    )

                    if content and content.strip():
                        first_date, last_date = first_ts[:10], last_ts[:10]
                        first_time, last_time = first_ts[11:16], last_ts[11:16]
                        if first_date == last_date:
                            range_str = f"{first_date} {first_time} — {last_time}"
                        else:
                            range_str = f"{first_date} {first_time} — {last_date} {last_time}"

                        new_blocks.append({
                            "ts": utc_now_iso(),
                            "type": "episode",
                            "range": range_str,
                            "message_count": len(chunk),
                            "content": content.strip(),
                        })
                        processed += len(chunk)
                    else:
                        log.warning("Block summary empty for chunk %d, will retry next cycle", i)
                        break

                if not new_blocks:
                    return False

                existing_blocks = self._load_blocks()
                all_blocks = existing_blocks + new_blocks

                # Era compression if too many blocks
                if len(all_blocks) > MAX_SUMMARY_BLOCKS:
                    all_blocks = self._do_era_compression(all_blocks)

                self._save_blocks(all_blocks)

                meta["last_consolidated_offset"] = last_offset + processed
                meta["last_consolidated_at"] = utc_now_iso()
                self._save_meta(meta)

                log.info("Dialogue consolidated: %d messages -> %d new blocks (total %d)",
                         processed, len(new_blocks), len(all_blocks))
                return True

            except Exception as e:
                log.error("Dialogue consolidation failed: %s", e, exc_info=True)
                return False

    def render_for_context(self) -> str:
        """Render all blocks as text for Semi-stable context block."""
        blocks = self._load_blocks()
        if not blocks:
            return ""

        lines: List[str] = []
        for b in blocks:
            btype = b.get("type", "episode")
            range_str = b.get("range", "unknown")
            msg_count = b.get("message_count", 0)
            content = b.get("content", "")

            if btype == "era":
                lines.append(f"### Era: {range_str} ({msg_count} messages)")
            else:
                lines.append(f"### Episode: {range_str} ({msg_count} messages)")
            lines.append(content)
            lines.append("")

        return "\n".join(lines).strip()

    # ------------------------------------------------------------------
    # Era compression
    # ------------------------------------------------------------------

    def _compress_eras(self) -> None:
        """Compress oldest blocks into era when >MAX_SUMMARY_BLOCKS blocks."""
        blocks = self._load_blocks()
        if len(blocks) <= MAX_SUMMARY_BLOCKS:
            return
        all_blocks = self._do_era_compression(blocks)
        self._save_blocks(all_blocks)

    def _do_era_compression(self, all_blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Compress oldest ERA_COMPRESS_COUNT blocks into a single era block."""
        if len(all_blocks) <= MAX_SUMMARY_BLOCKS:
            return all_blocks

        compress_count = min(ERA_COMPRESS_COUNT, len(all_blocks) - 1)
        old_blocks = all_blocks[:compress_count]
        remaining = all_blocks[compress_count:]

        era = self._create_era_summary(old_blocks)
        if era is not None:
            return [era] + remaining
        # If compression fails, keep original blocks (never silently discard)
        return all_blocks

    def _create_era_summary(self, blocks: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Call LLM to compress blocks into a single era summary."""
        if not self._llm:
            return None

        start_date = blocks[0].get("range", "unknown")[:10]
        last_range = blocks[-1].get("range", "unknown")
        end_date = last_range[:10]

        combined = "\n\n---\n\n".join(
            f"### {b.get('range', 'unknown')}\n{b.get('content', '')}"
            for b in blocks
        )

        prompt = ERA_COMPRESSION_PROMPT.format(
            start_date=start_date,
            end_date=end_date,
            combined=combined,
        )

        try:
            from ouroboros.model_modes import get_aux_light_model
            model = get_aux_light_model()
            msg, _usage = self._llm.chat(
                messages=[{"role": "user", "content": prompt}],
                model=model,
                tools=None,
                reasoning_effort="medium",
                max_tokens=4096,
            )
            content = msg.get("content", "")
            if not content or not content.strip():
                log.warning("Era compression returned empty — keeping original blocks")
                return None

            return {
                "ts": utc_now_iso(),
                "type": "era",
                "range": f"{start_date} to {end_date}",
                "message_count": sum(b.get("message_count", 0) for b in blocks),
                "content": content.strip(),
            }
        except Exception as e:
            log.error("Era compression failed: %s", e, exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Episode summary via LLM
    # ------------------------------------------------------------------

    def _create_episode_summary(
        self,
        messages_text: str,
        first_ts: str,
        last_ts: str,
        message_count: int,
    ) -> Tuple[str, Dict[str, Any]]:
        """Call LLM to create a detailed episode summary.

        Returns (summary_text, usage_dict). On failure returns ("", {}).
        """
        first_date = first_ts[:10]
        first_time = first_ts[11:16]
        last_time = last_ts[11:16]

        prompt = EPISODE_SUMMARY_PROMPT.format(
            message_count=message_count,
            first_date=first_date,
            first_time=first_time,
            last_time=last_time,
            messages_text=messages_text,
        )

        try:
            from ouroboros.model_modes import get_aux_light_model
            model = get_aux_light_model()
            msg, usage = self._llm.chat(
                messages=[{"role": "user", "content": prompt}],
                model=model,
                tools=None,
                reasoning_effort="medium",
                max_tokens=4096,
            )
            return msg.get("content", ""), usage
        except Exception as e:
            log.error("Episode summary LLM call failed: %s", e, exc_info=True)
            return "", {}

    # ------------------------------------------------------------------
    # Migration from legacy dialogue_summary.md → blocks
    # ------------------------------------------------------------------

    def _maybe_migrate_legacy(self) -> None:
        """One-time migration from dialogue_summary.md to block format."""
        summary_path = self._drive_root / "memory" / "dialogue_summary.md"

        if self._blocks_path.exists():
            return  # Already migrated or fresh start

        if summary_path.exists():
            text = read_text(summary_path)
            if text.strip():
                # Parse episodes/eras from markdown if structured
                chunks = re.split(r'(?=^### (?:Episode|Era|Block):)', text, flags=re.MULTILINE)
                chunks = [c for c in chunks if c.strip()]

                blocks: List[Dict[str, Any]] = []
                if chunks and any(c.strip().startswith("### ") for c in chunks):
                    for chunk in chunks:
                        chunk = chunk.strip()
                        first_line = chunk.split("\n")[0]
                        match = re.match(r'^### (?:Episode|Era|Block):\s*(.+)', first_line)
                        range_str = match.group(1).strip() if match else "legacy"
                        block_type = "era" if chunk.startswith("### Era:") else "episode"
                        blocks.append({
                            "ts": utc_now_iso(),
                            "type": block_type,
                            "range": range_str,
                            "message_count": 0,
                            "content": chunk,
                        })
                else:
                    # Unstructured legacy summary — wrap as single era
                    blocks.append({
                        "ts": utc_now_iso(),
                        "type": "era",
                        "range": "legacy",
                        "message_count": 0,
                        "content": text.strip(),
                    })

                self._save_blocks(blocks)
                # Set offset to 0 to allow re-consolidation from beginning
                # (legacy summary may not cover all messages)
                self._save_meta({"last_consolidated_offset": 0})
                log.info("Migrated legacy dialogue_summary.md -> dialogue_blocks.json (%d blocks)", len(blocks))
            return

        # Neither file exists — initialize with current chat size
        # (don't consolidate entire history on first run)
        total = self._count_chat_lines()
        self._save_blocks([])
        self._save_meta({"last_consolidated_offset": total})
        log.info("Initialized dialogue consolidator at offset %d", total)

    # ------------------------------------------------------------------
    # IO helpers
    # ------------------------------------------------------------------

    def _load_blocks(self) -> List[Dict[str, Any]]:
        if not self._blocks_path.exists():
            return []
        try:
            return json.loads(read_text(self._blocks_path))
        except (json.JSONDecodeError, ValueError):
            log.warning("Corrupt blocks file %s, starting fresh", self._blocks_path)
            return []

    def _save_blocks(self, blocks: List[Dict[str, Any]]) -> None:
        self._blocks_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._blocks_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(blocks, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(tmp), str(self._blocks_path))

    def _load_meta(self) -> Dict[str, Any]:
        if self._meta_path.exists():
            try:
                return json.loads(read_text(self._meta_path))
            except (json.JSONDecodeError, ValueError):
                return {}
        return {}

    def _save_meta(self, meta: Dict[str, Any]) -> None:
        self._meta_path.parent.mkdir(parents=True, exist_ok=True)
        write_text(self._meta_path, json.dumps(meta, ensure_ascii=False, indent=2))

    def _count_chat_lines(self) -> int:
        if not self._chat_path.exists():
            return 0
        count = 0
        with self._chat_path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    count += 1
        return count

    def _read_chat_entries(self) -> List[Dict[str, Any]]:
        if not self._chat_path.exists():
            return []
        entries: List[Dict[str, Any]] = []
        with self._chat_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    continue
        return entries

    def _format_entries(self, entries: List[Dict[str, Any]]) -> str:
        lines: List[str] = []
        for e in entries:
            ts_raw = str(e.get("ts", ""))
            ts = ts_raw[:10] + " " + ts_raw[11:16] if len(ts_raw) >= 16 else ts_raw
            dir_raw = str(e.get("direction", "")).lower()
            if dir_raw in ("out", "outgoing"):
                author = "Veles"
                prefix = "→ "
            elif dir_raw == "system":
                author = "System"
                prefix = "[system] "
            else:
                author = e.get("username") or "Creator"
                prefix = ""
            text = str(e.get("text", ""))
            lines.append(f"[{ts}] {prefix}{author}: {text}")
        return "\n\n".join(lines)
