from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from codex_bridge.tunnel_profile import (
    TunnelProfileError,
    _render_profile,
    generate_tunnel_profile,
)

PROFILE: dict[str, Any] = {
    "config_version": 1,
    "control_plane": {
        "base_url": "https://api.openai.com",
        "tunnel_id": "tunnel_0123456789abcdef0123456789abcdef",
        "api_key": "env:CONTROL_PLANE_API_KEY",
    },
    "health": {"listen_addr": "127.0.0.1:8080"},
    "admin_ui": {"open_browser": False},
    "log": {"level": "info", "format": "json"},
    "mcp": {"server_urls": [{"channel": "main", "url": "http://127.0.0.1:8000/mcp"}]},
}


class FakeDoctor:
    def __init__(self, *, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self.during_call: Any = None

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((args, kwargs))
        if self.during_call is not None:
            self.during_call(args, kwargs)
        return subprocess.CompletedProcess(args, self.returncode, b"", b"")


def install_doctor(monkeypatch: pytest.MonkeyPatch, doctor: FakeDoctor) -> None:
    monkeypatch.setattr("codex_bridge.tunnel_profile.subprocess.run", doctor)


def test_mapping_renders_exact_client_v1_yaml_shape_and_validates_before_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    target = profile_dir / "codex-bridge.yaml"
    target.write_text("old-valid-profile\n", encoding="utf-8")
    doctor = FakeDoctor()

    def inspect_temp(args: list[str], _: object) -> None:
        assert args[1:] == [
            "doctor",
            "--profile-file",
            args[3],
            "--health.listen-addr",
            "127.0.0.1:0",
        ]
        staged = Path(args[3])
        assert staged.parent == profile_dir
        assert staged != target
        assert target.read_text(encoding="utf-8") == "old-valid-profile\n"
        staged_profile = json.loads(staged.read_text(encoding="utf-8"))
        assert staged_profile == PROFILE
        assert staged_profile["health"]["listen_addr"] == "127.0.0.1:8080"

    doctor.during_call = inspect_temp
    install_doctor(monkeypatch, doctor)

    result = generate_tunnel_profile(
        PROFILE,
        profile_dir=profile_dir,
        profile_name="codex-bridge",
        client_executable="tunnel-client.exe",
    )

    assert result.path == target
    assert json.loads(target.read_text(encoding="utf-8")) == PROFILE
    assert len(doctor.calls) == 1


def test_doctor_rejection_preserves_existing_profile_and_hides_client_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    target = profile_dir / "codex-bridge.yaml"
    old = "previous-profile-with-no-secrets\n"
    target.write_text(old, encoding="utf-8")
    doctor = FakeDoctor(returncode=2)
    monkeypatch.setattr(
        "codex_bridge.tunnel_profile.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args, 2, b"resolved API key must never be surfaced", b"secret output"
        ),
    )

    with pytest.raises(TunnelProfileError) as raised:
        generate_tunnel_profile(
            PROFILE,
            profile_dir=profile_dir,
            profile_name="codex-bridge",
            client_executable="tunnel-client.exe",
        )

    assert "resolved API key" not in str(raised.value)
    assert "secret output" not in str(raised.value)
    assert target.read_text(encoding="utf-8") == old
    assert list(profile_dir.glob("*.tmp")) == []
    assert doctor.returncode == 2


@pytest.mark.parametrize(
    "api_key",
    ["literal-secret-value", "${CONTROL_PLANE_API_KEY}", "env:", "file:"],
)
def test_secret_values_must_be_references_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    api_key: str,
) -> None:
    profile = json.loads(json.dumps(PROFILE))
    profile["control_plane"]["api_key"] = api_key
    doctor = FakeDoctor()
    install_doctor(monkeypatch, doctor)

    with pytest.raises(TunnelProfileError):
        generate_tunnel_profile(
            profile,
            profile_dir=tmp_path,
            profile_name="codex-bridge",
            client_executable="tunnel-client.exe",
        )

    assert doctor.calls == []
    assert list(tmp_path.iterdir()) == []


def test_rendered_yaml_with_literal_secret_is_rejected_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered = json.dumps(PROFILE, indent=2).replace(
        '"env:CONTROL_PLANE_API_KEY"', '"literal-secret-value"'
    )
    doctor = FakeDoctor()
    install_doctor(monkeypatch, doctor)

    with pytest.raises(TunnelProfileError):
        generate_tunnel_profile(
            rendered,
            profile_dir=tmp_path,
            profile_name="codex-bridge",
            client_executable="tunnel-client.exe",
        )

    assert doctor.calls == []
    assert list(tmp_path.iterdir()) == []


def test_env_and_file_secret_references_are_kept_as_references(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = json.loads(json.dumps(PROFILE))
    profile["control_plane"]["api_key"] = "file:C:/secrets/control-plane.key"
    doctor = FakeDoctor()
    install_doctor(monkeypatch, doctor)

    generate_tunnel_profile(
        profile,
        profile_dir=tmp_path,
        profile_name="codex-bridge",
        client_executable="tunnel-client.exe",
    )

    target = tmp_path / "codex-bridge.yaml"
    assert "file:C:/secrets/control-plane.key" in target.read_text(encoding="utf-8")
    assert "CONTROL_PLANE_API_KEY" not in target.read_text(encoding="utf-8")


def test_replace_failure_preserves_target_and_cleans_staged_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "codex-bridge.yaml"
    old = "previous-valid-profile\n"
    target.write_text(old, encoding="utf-8")
    doctor = FakeDoctor()
    install_doctor(monkeypatch, doctor)

    def fail_replace(source: str | Path, destination: str | Path) -> None:
        assert Path(source).parent == Path(destination).parent
        assert Path(destination) == target
        raise OSError("filesystem detail must not be returned")

    monkeypatch.setattr("codex_bridge.tunnel_profile.os.replace", fail_replace)

    with pytest.raises(TunnelProfileError) as raised:
        generate_tunnel_profile(
            PROFILE,
            profile_dir=tmp_path,
            profile_name="codex-bridge",
            client_executable="tunnel-client.exe",
        )

    assert "filesystem detail" not in str(raised.value)
    assert target.read_text(encoding="utf-8") == old
    assert list(tmp_path.glob(".codex-bridge.*.yaml.tmp")) == []


def test_rendered_yaml_is_validated_by_client_without_exposing_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered = json.dumps(PROFILE, indent=2)
    doctor = FakeDoctor()
    install_doctor(monkeypatch, doctor)

    generate_tunnel_profile(
        rendered,
        profile_dir=tmp_path,
        profile_name="codex-bridge",
        client_executable="tunnel-client.exe",
    )

    assert (tmp_path / "codex-bridge.yaml").read_text(encoding="utf-8") == rendered


def test_config_mcp_headers_are_preserved_through_console_render_and_fake_doctor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codex_bridge.config_file import load_user_config
    from codex_bridge.console.config import ConsoleConfig

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[tunnel.control_plane]\n"
        'base_url = "https://control.example.test"\n'
        'tunnel_id = "dummy-tunnel-id"\n'
        'api_key = "env:TUNNEL_API_KEY"\n'
        "[tunnel.mcp]\n"
        'startup_wait_timeout = "60s"\n'
        "[tunnel.mcp.extra_headers]\n"
        'X-Debug-Mode = "1"\n'
        'Authorization = "env:TUNNEL_MCP_AUTH"\n'
        'Cookie = "file:C:/dummy/mcp-cookie.txt"\n',
        encoding="utf-8",
    )
    loaded = load_user_config(path=config_path)
    console_config = ConsoleConfig.from_sources(environ={"CODEX_BRIDGE_CONFIG": str(config_path)})
    profile = console_config.tunnel_profile_settings
    expected_mcp = {
        "startup_wait_timeout": "60s",
        "extra_headers": {
            "X-Debug-Mode": "1",
            "Authorization": "env:TUNNEL_MCP_AUTH",
            "Cookie": "file:C:/dummy/mcp-cookie.txt",
        },
    }
    assert loaded["tunnel"]["mcp"] == expected_mcp
    assert profile["mcp"] == expected_mcp

    rendered = _render_profile(profile)
    assert json.loads(rendered)["mcp"] == expected_mcp

    doctor = FakeDoctor()

    def inspect_staged_profile(args: list[str], _: object) -> None:
        assert args[0] == "fake-tunnel-client.exe"
        assert json.loads(Path(args[3]).read_text(encoding="utf-8"))["mcp"] == expected_mcp

    doctor.during_call = inspect_staged_profile
    install_doctor(monkeypatch, doctor)
    result = generate_tunnel_profile(
        profile,
        profile_dir=tmp_path / "profiles",
        profile_name="codex-bridge",
        client_executable="fake-tunnel-client.exe",
    )

    assert doctor.calls[0][0][0] == "fake-tunnel-client.exe"
    assert json.loads(result.path.read_text(encoding="utf-8"))["mcp"] == expected_mcp


@pytest.mark.parametrize(
    "extra_headers,secret_value",
    [
        ("not-a-mapping", "dummy-secret-value"),
        (["not-a-mapping"], "dummy-secret-value"),
        ({"X-Count": 3}, "dummy-secret-value"),
        ({"": "dummy-secret-value"}, "dummy-secret-value"),
        ({"X:Bad": "dummy-secret-value"}, "dummy-secret-value"),
        ({"X-é": "dummy-secret-value"}, "dummy-secret-value"),
        ({"X-Debug": "dummy\r\nX-Injected: yes"}, "dummy\r\nX-Injected: yes"),
        ({"X-Debug": "dummy\u0000value"}, "dummy\u0000value"),
        ({"X-Debug": "dummy\u0085value"}, "dummy\u0085value"),
        ({"X-Debug": "dummy\u2028value"}, "dummy\u2028value"),
        ({"X-Debug": "dummy\u2029value"}, "dummy\u2029value"),
        ({"Authorization": "dummy-header-secret"}, "dummy-header-secret"),
        ({"Proxy-Authorization": "dummy-header-secret"}, "dummy-header-secret"),
        ({"Cookie": "dummy-header-secret"}, "dummy-header-secret"),
        ({"Set-Cookie": "dummy-header-secret"}, "dummy-header-secret"),
        ({"X-API-Key": "dummy-header-secret"}, "dummy-header-secret"),
        ({"X-API-Key-Value": "dummy-header-secret"}, "dummy-header-secret"),
        ({"X-Access-Token": "dummy-header-secret"}, "dummy-header-secret"),
        ({"X-Client-Secret": "dummy-header-secret"}, "dummy-header-secret"),
        ({"X-User-Credential": "dummy-header-secret"}, "dummy-header-secret"),
        ({"X-Authentication-Mode": "dummy-header-secret"}, "dummy-header-secret"),
        ({"Authorization": "env:"}, "env:"),
        ({"Cookie": "file:"}, "file:"),
    ],
)
def test_mapping_mcp_extra_headers_are_validated_before_write_or_doctor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra_headers: object,
    secret_value: str,
) -> None:
    profile = json.loads(json.dumps(PROFILE))
    profile["mcp"]["extra_headers"] = extra_headers
    doctor = FakeDoctor()
    install_doctor(monkeypatch, doctor)

    with pytest.raises(TunnelProfileError) as exc_info:
        generate_tunnel_profile(
            profile,
            profile_dir=tmp_path,
            profile_name="codex-bridge",
            client_executable="fake-tunnel-client.exe",
        )

    if secret_value not in {"env:", "file:"}:
        assert secret_value not in str(exc_info.value)
    assert doctor.calls == []
    assert list(tmp_path.iterdir()) == []


def test_mapping_mcp_extra_headers_accept_references_and_noncredential_literals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = json.loads(json.dumps(PROFILE))
    profile["mcp"].update(
        {
            "startup_wait_timeout": "60s",
            "extra_headers": {
                "X-Debug-Mode": "1",
                "X-Monkey-Key": "enabled",
                "Authorization": "env:TUNNEL_MCP_AUTH",
                "Cookie": "file:C:/dummy/mcp-cookie.txt",
            },
        }
    )
    doctor = FakeDoctor()
    install_doctor(monkeypatch, doctor)

    result = generate_tunnel_profile(
        profile,
        profile_dir=tmp_path,
        profile_name="codex-bridge",
        client_executable="fake-tunnel-client.exe",
    )

    assert json.loads(result.path.read_text(encoding="utf-8"))["mcp"] == profile["mcp"]
    assert len(doctor.calls) == 1
    assert doctor.calls[0][0][0] == "fake-tunnel-client.exe"
