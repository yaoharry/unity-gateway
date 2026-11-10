"""Cowork plugin hooks that keep Claude informed about Unity Gateway connectors.

Cowork may drop idle MCP connections and report them as disconnected. These hooks
cannot make Cowork reconnect sooner, but they refresh Databricks credentials,
probe upstream MCP servers, and provide recovery guidance. They run on SessionStart
and on UserPromptSubmit after an idle gap.
"""

from __future__ import annotations

from datetime import UTC, datetime

import anyio
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ucode.cowork_hook_runtime import needs_probe
from ucode.databricks import ensure_pat_bearer, get_databricks_token
from ucode.mcp_proxy import (
    CONNECTION_AUTH_REQUIRED_ERROR_CODE,
    URL_ELICITATION_REQUIRED_ERROR_CODE,
    _httpx,
)
from ucode.ug_headers import ug_headers
from ucode.workspace import workspace_headers

SESSION_START_PROBE_TIMEOUT_SECONDS = 15.0
# UserPromptSubmit blocks the prompt and has a 30-second default timeout.
PROMPT_PROBE_TIMEOUT_SECONDS = 10.0
READY = "ready"
NEEDS_SIGN_IN = "needs_sign_in"
SIGN_IN_ERROR_CODES = {CONNECTION_AUTH_REQUIRED_ERROR_CODE, URL_ELICITATION_REQUIRED_ERROR_CODE}


def hook_output(
    payload: object,
    servers: dict[str, str],
    workspace: str,
    profile: str | None,
    *,
    use_pat: bool = False,
    now: datetime | None = None,
) -> dict:
    current = now or datetime.now(UTC)
    if not isinstance(payload, dict) or not needs_probe(payload, servers, current):
        return {}
    event = payload.get("hook_event_name")
    if event == "SessionStart":
        statuses = _warm(
            servers,
            workspace,
            profile,
            use_pat=use_pat,
            timeout=SESSION_START_PROBE_TIMEOUT_SECONDS,
        )
        context = session_start_context(list(servers), statuses, current)
    else:
        statuses = _warm(
            servers, workspace, profile, use_pat=use_pat, timeout=PROMPT_PROBE_TIMEOUT_SECONDS
        )
        context = idle_resume_context(list(servers), statuses, current)
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}


def session_start_context(names: list[str], statuses: dict[str, str], checked_at: datetime) -> str:
    # Readiness goes stale after the first turn, so only the actionable sign-in result is kept.
    context = (
        f"Unity Gateway connectors configured for this session: {_quoted(names)}. Use them for "
        "requests about the user's own resources on those services. Before saying a connector "
        "is unavailable, follow the `unity-gateway-connectors` skill."
    )
    return context + _sign_in_note(names, statuses, checked_at)


def idle_resume_context(names: list[str], statuses: dict[str, str], checked_at: datetime) -> str:
    context = (
        'After an idle break, Cowork may show stale "MCP servers disconnected" notices for '
        "Unity Gateway connectors. Do not say a connector is unavailable based on an earlier "
        "notice; check current tools and follow the `unity-gateway-connectors` skill."
    )
    answered = [name for name in names if statuses.get(name) == READY]
    silent = [name for name in names if name not in statuses]
    if answered:
        context += (
            f" At {_stamp(checked_at)} these connectors answered: {_quoted(answered)}. Their "
            "tools may take a few seconds to reappear."
        )
    if silent and answered:
        context += f" These did not answer: {_quoted(silent)}."
    return context + _sign_in_note(names, statuses, checked_at)


def _sign_in_note(names: list[str], statuses: dict[str, str], checked_at: datetime) -> str:
    needs_sign_in = [name for name in names if statuses.get(name) == NEEDS_SIGN_IN]
    if not needs_sign_in:
        return ""
    return (
        f" At {_stamp(checked_at)}, these connectors needed sign-in: {_quoted(needs_sign_in)}. "
        "Call that connector's `authenticate` tool before using it; if the user has signed in "
        "since, its tools may already work."
    )


def _stamp(checked_at: datetime) -> str:
    return checked_at.strftime("%H:%M UTC")


def _quoted(names: list[str]) -> str:
    return ", ".join(f"`{name}`" for name in names)


def _warm(
    servers: dict[str, str],
    workspace: str,
    profile: str | None,
    *,
    use_pat: bool,
    timeout: float,
) -> dict[str, str]:
    """Probe each connector; absent names did not answer in time or failed for another reason."""
    if use_pat and not ensure_pat_bearer(profile):
        return {}
    try:
        # One refresh here saves every proxy from racing to refresh the same token.
        token = get_databricks_token(workspace, profile)
    except RuntimeError:
        return {}
    return anyio.run(_probe_all, servers, token, timeout)


async def _probe_all(servers: dict[str, str], token: str, timeout: float) -> dict[str, str]:
    statuses: dict[str, str] = {}

    async def probe(name: str, url: str) -> None:
        try:
            await _probe(url, token, timeout)
        except Exception as exc:  # noqa: BLE001 - only sign-in errors are worth reporting
            if _is_sign_in_error(exc):
                statuses[name] = NEEDS_SIGN_IN
            return
        statuses[name] = READY

    with anyio.move_on_after(timeout):
        async with anyio.create_task_group() as tg:
            for name, url in servers.items():
                tg.start_soon(probe, name, url)
    return statuses


def _is_sign_in_error(exc: BaseException) -> bool:
    # MCP SDK 1.x and 2.x name the error class differently; both carry `.error.code`.
    if getattr(getattr(exc, "error", None), "code", None) in SIGN_IN_ERROR_CODES:
        return True
    if getattr(getattr(exc, "response", None), "status_code", None) == 401:
        return True
    return any(_is_sign_in_error(nested) for nested in getattr(exc, "exceptions", ()) or ())


async def _probe(url: str, token: str, timeout: float) -> None:
    httpx = _httpx()
    headers = {**ug_headers(), **workspace_headers(url), "Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
        async with streamable_http_client(url, http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                await session.list_tools()
