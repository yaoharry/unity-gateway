"""Measure local shared-state preparation with a synthetic workspace registry.

Run with ``uv run --frozen scripts/benchmark_startup_state.py``. External discovery
is skipped; the real reader, hydrator, and writer operate only on a temporary file.
These are component timings, not live authentication or agent readiness results.
"""

import argparse
import json
import statistics
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch


def measure(workspaces: int, servers: int, runs: int) -> dict:
    from ucode import cli, state

    host = "https://workspace-0.example.invalid"
    payload = {
        "state_version": state.STATE_VERSION,
        "current_workspace": host,
        "workspaces": {
            f"https://workspace-{number}.example.invalid": {
                "profile": "benchmark",
                "claude_models": {"opus": "benchmark-claude"},
                "codex_default_model": "benchmark-gpt",
                "codex_models": ["benchmark-gpt"],
                "self_managed_agents": ["claude"],
                "mcp_servers": [
                    {
                        "name": f"benchmark-server-{index}",
                        "url": f"https://workspace-{number}.example.invalid/mcp/{index}",
                        "clients": ["claude", "codex"],
                    }
                    for index in range(servers)
                ],
            }
            for number in range(workspaces)
        },
    }
    samples = []
    cpu_samples = []
    read_counts = []
    with tempfile.TemporaryDirectory(prefix="ug-state-benchmark-") as directory:
        path = Path(directory) / "state.json"
        with ExitStack() as stack:
            stack.enter_context(patch.object(state, "STATE_PATH", path))
            stack.enter_context(patch.object(state, "APP_DIR", path.parent))
            stack.enter_context(patch.object(cli, "resolve_workspace", lambda value: value))
            reads = stack.enter_context(
                patch.object(state, "read_state_file", wraps=state.read_state_file)
            )
            for iteration in range(runs + 1):
                path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                input_bytes = path.stat().st_size
                reads.reset_mock()
                started = time.perf_counter_ns()
                cpu_started = time.process_time_ns()
                result = cli.configure_shared_state(
                    host, profile="benchmark", tools=["codex"], skip_preflight=True
                )
                elapsed = (time.perf_counter_ns() - started) / 1_000_000
                cpu_elapsed = (time.process_time_ns() - cpu_started) / 1_000_000
                assert result["self_managed_agents"] == ["claude"]
                assert result["codex_models"] == ["benchmark-gpt"]
                assert len(json.loads(path.read_text())["workspaces"]) == workspaces
                if iteration:
                    samples.append(elapsed)
                    cpu_samples.append(cpu_elapsed)
                    read_counts.append(reads.call_count)
    return {
        "workspaces": workspaces,
        "mcp_servers_per_workspace": servers,
        "input_bytes": input_bytes,
        "median_ms": statistics.median(samples),
        "median_cpu_ms": statistics.median(cpu_samples),
        "samples_ms": samples,
        "cpu_samples_ms": cpu_samples,
        "registry_reads": read_counts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-directory", type=Path, default=Path(__file__).resolve().parents[1] / "src"
    )
    parser.add_argument("--runs", type=int, default=11)
    parser.add_argument("--workspaces", type=int, default=100)
    parser.add_argument("--servers", type=int, default=30)
    args = parser.parse_args()
    if args.runs < 1 or args.workspaces < 1 or args.servers < 0:
        parser.error("runs and workspaces must be positive; servers cannot be negative")
    if not (args.source_directory / "ucode" / "cli.py").is_file():
        parser.error("source directory must contain ucode/cli.py")
    sys.path.insert(0, str(args.source_directory.resolve()))
    print(json.dumps(measure(args.workspaces, args.servers, args.runs), indent=2))


if __name__ == "__main__":
    main()
