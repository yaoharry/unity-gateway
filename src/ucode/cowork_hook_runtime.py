"""Entry point for the Cowork plugin's hooks.

Cowork runs ``ug cowork-hook`` on SessionStart and before every prompt, and the prompt waits for
it. Most prompts follow a recent reply and need no connector probe, so that decision uses only the
standard library. Only a probe loads the MCP, HTTP and credential modules in
``ucode.agents.desktop.cowork_hooks``.
"""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

# Cowork was observed dropping idle connectors after about five minutes.
IDLE_GAP_SECONDS = 180.0
TRANSCRIPT_TAIL_BYTES = 256 * 1024


def last_assistant_time(transcript_path: object) -> datetime | None:
    # The current prompt may already be recorded, so measure from Claude's last reply.
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    try:
        with Path(transcript_path).open("rb") as transcript:
            transcript.seek(0, 2)
            transcript.seek(max(0, transcript.tell() - TRANSCRIPT_TAIL_BYTES))
            lines = transcript.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        try:
            stamp = datetime.fromisoformat(str(entry["timestamp"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
    return None


def needs_probe(payload: object, servers: dict[str, str], now: datetime) -> bool:
    """Whether this hook event probes connectors: every SessionStart, and a prompt after idling."""
    if not isinstance(payload, dict) or not servers:
        return False
    event = payload.get("hook_event_name")
    if event == "SessionStart":
        return True
    if event == "UserPromptSubmit":
        last_reply = last_assistant_time(payload.get("transcript_path"))
        return last_reply is not None and (now - last_reply).total_seconds() >= IDLE_GAP_SECONDS
    return False


def run_hook(
    payload_text: str,
    host: str,
    server_entries: list[str],
    profile: str | None,
    *,
    use_pat: bool,
    workspace_id: str | None,
    stdout: TextIO,
) -> None:
    """Write the hook's JSON response to ``stdout``, or nothing when there is nothing to add."""
    try:
        payload = json.loads(payload_text or "{}")
    except ValueError:
        return
    servers = dict(entry.split("=", 1) for entry in server_entries if "=" in entry)
    now = datetime.now(UTC)
    if not needs_probe(payload, servers, now):
        return
    from ucode.agents.desktop.cowork_hooks import hook_output
    from ucode.workspace import pin

    if workspace_id:
        pin(host, workspace_id)
    with redirect_stdout(sys.stderr):  # stdout is reserved for the hook's JSON
        output = hook_output(payload, servers, host, profile, use_pat=use_pat, now=now)
    if output:
        stdout.write(json.dumps(output))


def main(argv: list[str]) -> None:
    # Mirrors the hidden `ug cowork-hook` command's options; `--help` goes to the full CLI.
    parser = argparse.ArgumentParser(prog="ug cowork-hook", add_help=False)
    parser.add_argument("--host", required=True)
    parser.add_argument("--server", action="append", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--use-pat", action="store_true")
    parser.add_argument("--workspace-id")
    args = parser.parse_args(argv)
    run_hook(
        sys.stdin.read(),
        args.host,
        args.server,
        args.profile,
        use_pat=args.use_pat,
        workspace_id=args.workspace_id,
        stdout=sys.stdout,
    )
