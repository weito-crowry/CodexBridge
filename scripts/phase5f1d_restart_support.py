from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any


class AclSnapshotError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def restart_eligible(
    *,
    shutdown_status: int | None,
    parent_stopped: bool,
    descendants_stopped: bool,
    ports_released: bool,
    state_unchanged: bool,
) -> bool:
    return (
        shutdown_status == 202
        and parent_stopped
        and descendants_stopped
        and ports_released
        and state_unchanged
    )


def shutdown_once(
    post_request: Callable[[], tuple[int, Any]],
) -> tuple[int | None, Any | None]:
    try:
        return post_request()
    except Exception:
        return None, None


def write_evidence_once(
    path: Path,
    evidence: Mapping[str, Any],
    *,
    sensitive_values: Sequence[str] = (),
) -> None:
    if path.is_symlink() or path.parent.is_symlink() or not path.parent.is_dir():
        raise OSError("evidence parent is unavailable or a symlink")
    if _contains_sensitive_field(evidence):
        raise ValueError("sensitive evidence field is not permitted")
    encoded = (json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    if any(value and value.encode("utf-8") in encoded for value in sensitive_values):
        raise ValueError("sensitive evidence value is not permitted")
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def replay_history_is_unknown(checks: Any) -> bool:
    if not isinstance(checks, Mapping):
        return False
    replay = checks.get("replay")
    expected = {
        "outcome": "UNKNOWN_NO_RETRY",
        "post_attempted": "UNKNOWN",
        "post_count": None,
        "possible_post_count_range": [0, 1],
        "accepted_http_status": None,
        "retry_count": 0,
        "new_workflow_post_count": 0,
        "new_workflow_count": 0,
        "new_codex_job_count": 0,
        "new_codex_thread_count": 0,
        "api_projection_identical_before_and_after": "UNKNOWN",
        "state_after_reconciliation_unchanged": True,
    }
    return isinstance(replay, Mapping) and all(
        replay.get(key) == value for key, value in expected.items()
    )


def _contains_sensitive_field(value: Any) -> bool:
    sensitive_parts = ("token", "password", "secret", "prompt", "authorization", "api_key")
    safe_status_fields = {"missing_token_http", "malformed_token_http"}
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized_key = str(key).casefold()
            if normalized_key not in safe_status_fields and any(
                part in normalized_key for part in sensitive_parts
            ):
                return True
            if _contains_sensitive_field(child):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_sensitive_field(child) for child in value)
    return False


def acl_scope_isolated(rows: Any) -> bool:
    required = {
        "r1_root",
        "workspace",
        "bridge_config",
        "codex_home",
        "codex_auth",
        "localappdata",
        "workflow_state_root",
        "run_root",
    }
    return (
        isinstance(rows, list)
        and {row.get("name") for row in rows if isinstance(row, dict)} == required
        and all(
            isinstance(row, dict)
            and row.get("owner_matches_current") is True
            and row.get("untrusted_writable_ace_count") == 0
            for row in rows
        )
    )


def snapshot_acl(
    root: Path,
    workspace: Path,
    state_root: Path,
    run_dir: Path,
) -> list[dict[str, Any]]:
    paths = {
        "r1_root": root,
        "workspace": workspace,
        "bridge_config": root / "bridge.toml",
        "codex_home": root / "codex_home",
        "codex_auth": root / "codex_home" / "auth.json",
        "localappdata": root / "localappdata",
        "workflow_state_root": state_root,
        "run_root": run_dir,
    }
    for path in paths.values():
        if (
            path.is_symlink()
            or not path.exists()
            or not path.resolve(strict=True).is_relative_to(root)
        ):
            raise AclSnapshotError("r1_acl_path_scope_invalid")
    powershell = (
        Path(os.environ["SYSTEMROOT"])
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    if not powershell.is_file():
        raise AclSnapshotError("windows_acl_reader_unavailable")
    powershell_items = ",\n".join(
        "@{name='" + name + "';path='" + str(path).replace("'", "''") + "'}"
        for name, path in paths.items()
    )
    command = f"""
$ErrorActionPreference = 'Stop'
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$trustedWriterSids = @($currentSid, 'S-1-5-18', 'S-1-5-32-544', 'S-1-3-4')
$writeMask = [int][Security.AccessControl.FileSystemRights]::Modify -bor
             [int][Security.AccessControl.FileSystemRights]::FullControl -bor
             [int][Security.AccessControl.FileSystemRights]::Write -bor
             [int][Security.AccessControl.FileSystemRights]::Delete -bor
             [int][Security.AccessControl.FileSystemRights]::CreateFiles -bor
             [int][Security.AccessControl.FileSystemRights]::CreateDirectories -bor
             [int][Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles
$items = @(
{powershell_items}
)
$rows = foreach ($item in $items) {{
  if ([System.IO.Directory]::Exists($item.path)) {{
    $acl = [System.IO.DirectoryInfo]::new($item.path).GetAccessControl()
  }} else {{
    $acl = [System.IO.FileInfo]::new($item.path).GetAccessControl()
  }}
  $ownerSid = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
  $untrustedWriters = 0
  foreach ($rule in $acl.Access) {{
    if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) {{
      continue
    }}
    $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
    $hasWriteAccess = ([int]$rule.FileSystemRights -band $writeMask) -ne 0
    if (($sid -notin $trustedWriterSids) -and $hasWriteAccess) {{
      $untrustedWriters += 1
    }}
  }}
  $sddl = $acl.GetSecurityDescriptorSddlForm([Security.AccessControl.AccessControlSections]::All)
  $hasher = [Security.Cryptography.SHA256]::Create()
  $aclHash = [BitConverter]::ToString(
    $hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($sddl))
  ).Replace('-', '').ToLowerInvariant()
  $hasher.Dispose()
  [pscustomobject]@{{
    name = $item.name
    owner_matches_current = ($ownerSid -eq $currentSid)
    untrusted_writable_ace_count = $untrustedWriters
    access_rule_count = @($acl.Access).Count
    acl_sha256 = $aclHash
  }}
}}
ConvertTo-Json -InputObject @($rows) -Compress
"""
    try:
        completed = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        value = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        raise AclSnapshotError("r1_acl_snapshot_failed") from None
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list) and all(isinstance(row, dict) for row in value):
        return value
    raise AclSnapshotError("r1_acl_snapshot_invalid")
