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
        repository_root=tmp_path,
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
        repository_root=tmp_path,
        platform="win32",
        which=lambda _: str(path_fallback),
    )
    assert candidates == (TunnelCandidate(str(explicit), "explicit"),)

    with pytest.raises(TunnelResolutionError):
        enumerate_candidates(
            {"CODEX_BRIDGE_TUNNEL_EXECUTABLE": str(tmp_path / "missing.exe")},
            repository_root=tmp_path,
            platform="win32",
            which=lambda _: str(path_fallback),
        )


def test_tunnel_resolver_prefers_config_then_checkout_local_then_path(tmp_path: Path) -> None:
    configured = tmp_path / "configured-tunnel.exe"
    configured.write_bytes(b"")
    repo = tmp_path / "repo"
    local = repo / ".tools" / "tunnel-client" / "tunnel-client.exe"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"")
    (repo / "pyproject.toml").write_text("[project]\nname='codexbridge'\n", encoding="utf-8")
    path_candidate = tmp_path / "path-tunnel.exe"
    path_candidate.write_bytes(b"")

    candidates = enumerate_candidates(
        config_executable=str(configured),
        repository_root=repo,
        platform="win32",
        which=lambda _name: str(path_candidate),
    )

    assert candidates == (TunnelCandidate(str(configured), "config"),)


def test_tunnel_resolver_uses_checkout_local_before_path_and_skips_unrelated_project(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    local = repo / ".tools" / "tunnel-client" / "tunnel-client.exe"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"")
    (repo / "pyproject.toml").write_text("[project]\nname='codexbridge'\n", encoding="utf-8")
    unrelated = tmp_path / "other" / ".tools" / "tunnel-client" / "tunnel-client.exe"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"")
    path_candidate = tmp_path / "path-tunnel.exe"
    path_candidate.write_bytes(b"")

    candidates = enumerate_candidates(
        repository_root=repo,
        platform="win32",
        which=lambda _name: str(path_candidate),
    )

    assert candidates[0] == TunnelCandidate(str(local), "local")
    assert all(str(unrelated) != candidate.path for candidate in candidates)


def test_invalid_config_tunnel_path_fails_without_path_fallback(tmp_path: Path) -> None:
    with pytest.raises(TunnelResolutionError, match="Configured Tunnel executable"):
        enumerate_candidates(
            config_executable=str(tmp_path / "missing.exe"),
            platform="win32",
            which=lambda _name: str(tmp_path / "path.exe"),
        )


def test_ps1_is_excluded_from_explicit_and_path_candidates(tmp_path: Path) -> None:
    script = tmp_path / "tunnel-client.ps1"
    script.write_bytes(b"")

    with pytest.raises(TunnelResolutionError):
        enumerate_candidates(
            {"CODEX_BRIDGE_TUNNEL_EXECUTABLE": str(script)},
            repository_root=tmp_path,
            platform="win32",
            which=lambda _: str(script),
        )

    assert (
        enumerate_candidates(
            repository_root=tmp_path,
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

    candidates = enumerate_candidates(repository_root=tmp_path, platform="win32", which=fake_which)

    assert candidates == (TunnelCandidate(str(executable), "path"),)
