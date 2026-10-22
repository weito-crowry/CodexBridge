from __future__ import annotations

import os

from codex_bridge.console import main_window as main_window_module
from codex_bridge.console.main_window import MainWindow
from tests.console_main_window_test_support import (
    FakeClient,
    FakeCodexProbe,
    FakeCodexUpdateProbe,
    StableTunnel,
    _application,
    _config,
)


def _window(client: FakeClient) -> MainWindow:
    return MainWindow(
        _config(),
        api_client=client,
        codex_probe=FakeCodexProbe(),
        codex_update_probe=FakeCodexUpdateProbe(),
        tunnel_supervisor=StableTunnel(),
        tray_available=False,
    )


def test_main_window_json_methods_delegate_to_dispatcher(monkeypatch) -> None:
    _application()
    client = FakeClient()
    window = _window(client)
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        window._api_result_dispatcher,
        "apply_result",
        lambda key, payload: calls.append(("result", key, payload)),
    )
    monkeypatch.setattr(
        window._api_result_dispatcher,
        "apply_error",
        lambda key, message: calls.append(("error", key, message)),
    )

    window.apply_json_result("threads", {"threads": []})
    window._apply_json_error("health", "offline")

    assert calls == [
        ("result", "threads", {"threads": []}),
        ("error", "health", "offline"),
    ]
    window.close()


def test_dispatcher_uses_main_window_project_name_callback(monkeypatch, tmp_path) -> None:
    _application()
    client = FakeClient()
    window = _window(client)
    calls: list[None] = []
    cwd = str(tmp_path / "workspace")
    key = os.path.normcase(os.path.normpath(cwd))
    monkeypatch.setattr(
        main_window_module,
        "read_local_project_names",
        lambda: calls.append(None) or {key: "Project"},
    )

    client.result("threads", {"threads": [{"id": "thread-a", "cwd": cwd}]})

    assert calls == [None]
    assert window.thread_pane.list_widget.topLevelItem(0).text(0) == "Project — workspace"
    window.close()


def test_dispatcher_updates_main_window_runtime_thread_state() -> None:
    _application()
    client = FakeClient()
    window = _window(client)

    window._api_result_dispatcher.apply_result(
        "threads",
        {
            "threads": [
                {
                    "id": "thread-a",
                    "runtime_status": {"type": "active", "active_flags": ["waitingOnApproval"]},
                }
            ]
        },
    )

    assert window._thread_turn_statuses == {"thread-a": "needs_approval"}
    assert window._active_thread_ids == {"thread-a"}
    window.close()


def test_api_json_signals_keep_single_main_window_compatibility_slot() -> None:
    _application()
    client = FakeClient()
    window = _window(client)

    dispatcher = window._api_result_dispatcher
    succeeded_slots = [
        slot for slot in client.json_succeeded._slots if getattr(slot, "__self__", None) is window
    ]
    failed_slots = [
        slot for slot in client.json_failed._slots if getattr(slot, "__self__", None) is window
    ]
    assert len(succeeded_slots) == 1
    assert succeeded_slots[0].__func__ is MainWindow.apply_json_result
    assert len(failed_slots) == 1
    assert failed_slots[0].__func__ is MainWindow._apply_json_error
    assert all(
        getattr(slot, "__self__", None) is not dispatcher for slot in client.json_succeeded._slots
    )
    assert all(
        getattr(slot, "__self__", None) is not dispatcher for slot in client.json_failed._slots
    )
    window.close()
