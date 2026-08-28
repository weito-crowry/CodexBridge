from __future__ import annotations

from pathlib import Path

import pytest

from codex_bridge.console.tunnel_resolver import (
    TunnelCandidate,
    TunnelResolutionError,
    enumerate_candidates,
)


def test_candidates_prefer_path_tunnel_client_exe_then_bare_command(tmp_path: Path) -> None:
    executable = tmp_path / "tunnel-client.exe"
    bare = tmp_path / "tunnel-client"
    executable.write_bytes(b"")
    bare.write_bytes(b"")

    candidates = enumerate_candidates(
        platform="win32",
        which=lambda name: {"tunnel-client.exe": str(executable), "tunnel-client": str(bare)}.get(
            name
        ),
    )

    assert candidates == (
        TunnelCandidate(str(executable), "path"),
        TunnelCandidate(str(bare), "path"),
    )


def test_explicit_tunnel_executable_has_priority_and_invalid_value_fails_closed(
    tmp_path: Path,
) -> None:
    explicit = tmp_path / "configured-tunnel.exe"
    path_fallback = tmp_path / "tunnel-client.exe"
    explicit.write_bytes(b"")
    path_fallback.write_bytes(b"")

    candidates = enumerate_candidates(
        {"CODEX_BRIDGE_TUNNEL_EXECUTABLE": str(explicit)},
        platform="win32",
        which=lambda _: str(path_fallback),
    )
    assert candidates == (TunnelCandidate(str(explicit), "explicit"),)

    with pytest.raises(TunnelResolutionError):
        enumerate_candidates(
            {"CODEX_BRIDGE_TUNNEL_EXECUTABLE": str(tmp_path / "missing.exe")},
            platform="win32",
            which=lambda _: str(path_fallback),
        )


def test_ps1_is_excluded_from_explicit_and_path_candidates(tmp_path: Path) -> None:
    script = tmp_path / "tunnel-client.ps1"
    script.write_bytes(b"")

    with pytest.raises(TunnelResolutionError):
        enumerate_candidates(
            {"CODEX_BRIDGE_TUNNEL_EXECUTABLE": str(script)},
            platform="win32",
            which=lambda _: str(script),
        )

    assert (
        enumerate_candidates(
            platform="win32",
            which=lambda _: str(script),
        )
        == ()
    )


def test_windows_path_candidates_are_deduplicated_case_insensitively(tmp_path: Path) -> None:
    executable = tmp_path / "tunnel-client.exe"
    executable.write_bytes(b"")

    def fake_which(name: str) -> str | None:
        return str(executable) if name == "tunnel-client.exe" else str(executable).upper()

    candidates = enumerate_candidates(platform="win32", which=fake_which)

    assert candidates == (TunnelCandidate(str(executable), "path"),)
