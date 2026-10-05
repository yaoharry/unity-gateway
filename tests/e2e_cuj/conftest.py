"""Fixtures for dedicated-workspace CUJs."""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

_INTEGRATION = Path(__file__).parents[1] / "integration"
_ROOT = _INTEGRATION.parents[1]
_SUITE = Path(__file__).parent
for _path in (_ROOT, _INTEGRATION, _SUITE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from base import BaseCujTest  # noqa: E402
from utils.harness import UserSession  # noqa: E402


def _ug_binary() -> Path:
    raw = os.environ.get("UG_INTEGRATION_BIN", "").strip()
    if raw:
        path = Path(raw).expanduser()
        if not path.is_file():
            pytest.fail(f"UG_INTEGRATION_BIN is not a file: {path}", pytrace=False)
        return path

    checkout_binary = _ROOT / ".venv" / ("Scripts/ug.exe" if os.name == "nt" else "bin/ug")
    if checkout_binary.is_file():
        return checkout_binary
    pytest.fail(
        f"No checkout ug found at {checkout_binary}; set UG_INTEGRATION_BIN to an installed ug.",
        pytrace=False,
    )


@pytest.fixture(scope="session")
def installed_binary():
    return _ug_binary()


@pytest.fixture
def session(request, installed_binary):
    case = re.sub(r"[^a-zA-Z0-9_.-]", "_", request.node.name)
    with tempfile.TemporaryDirectory(prefix="ug-cuj-run-", dir=_ROOT) as root_name:
        root = Path(root_name)
        project_parent = root if os.name == "nt" else None
        with tempfile.TemporaryDirectory(
            prefix="ug-cuj-project-", dir=project_parent
        ) as project_name:
            user = UserSession(
                root,
                Path(project_name),
                installed_binary,
                root / "artifacts" / case,
            )
            if os.name == "nt":
                app_data = user.home / "AppData/Roaming"
                local_app_data = user.home / "AppData/Local"
                case_temp = local_app_data / "Temp"
                for path in (app_data, local_app_data, case_temp):
                    path.mkdir(parents=True, exist_ok=True)
                user.env.update(
                    {
                        "APPDATA": str(app_data),
                        "LOCALAPPDATA": str(local_app_data),
                        "TEMP": str(case_temp),
                        "TMP": str(case_temp),
                        "TMPDIR": str(case_temp),
                    }
                )
            try:
                yield user
            finally:
                if any(
                    (user.home / ".ucode" / name).is_file()
                    for name in ("state.json", "managed-backups/manifest.json")
                ):
                    if os.name == "posix":
                        from utils.terminal import TerminalProcess

                        with TerminalProcess(
                            user, "ug", [str(user.binary), "revert"], "cleanup-revert"
                        ) as terminal:
                            terminal.finish()
                    else:
                        user.run("revert", timeout=120)


@pytest.fixture
def live_session(request, session):
    test = request.instance
    if not isinstance(test, BaseCujTest):
        pytest.fail("live_session requires a BaseCujTest instance.", pytrace=False)

    for binary in ("databricks", "claude", "codex"):
        if not shutil.which(binary, path=session.env["PATH"]):
            pytest.fail(f"Required CUJ binary is missing: {binary}", pytrace=False)

    has_sp_credentials = all(
        os.environ.get(name, "").strip()
        for name in ("UG_CUJ_SP_CLIENT_ID", "UG_CUJ_SP_CLIENT_SECRET")
    )
    if has_sp_credentials:
        authorization = test.workspace.config.authenticate().get("Authorization", "")
        bearer = authorization.removeprefix("Bearer ").strip()
    else:
        bearer = os.environ.get("DATABRICKS_BEARER", "").strip()
    if not bearer:
        pytest.fail("Workspace authentication returned no bearer token.", pytrace=False)
    session.env["DATABRICKS_BEARER"] = bearer
    return session
