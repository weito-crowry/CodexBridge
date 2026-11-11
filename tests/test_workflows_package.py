from __future__ import annotations

import hashlib
import runpy
from importlib.resources import files
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
POC_OWS = ROOT / "poc" / "ows"
SCHEMA_SHA256 = "df3326da907e17e743e9d9e888839481d1557abdf91c592e74f27506ff523f0b"


def test_product_schema_is_the_pinned_ows_v103_resource() -> None:
    product_schema = files("codex_bridge.workflows").joinpath("schema", "workflow.yaml")
    poc_schema = POC_OWS / "schema" / "workflow.yaml"
    product_bytes = product_schema.read_bytes()

    assert hashlib.sha256(product_bytes).hexdigest() == SCHEMA_SHA256
    assert poc_schema.read_bytes() == product_bytes


@pytest.mark.parametrize(
    ("poc_module", "product_module", "symbols"),
    [
        ("interpreter.py", "codex_bridge.workflows.interpreter", ("Interpreter", "OwsError")),
        (
            "worker.py",
            "codex_bridge.workflows.worker",
            ("execute_ows_workflow", "_wait_for_result"),
        ),
        (
            "codex_job_adapter.py",
            "codex_bridge.workflows.codex_job_adapter",
            ("CodexJobAdapter", "CodexJobReconciliationRequiredError"),
        ),
        (
            "job_adapter.py",
            "codex_bridge.workflows.job_adapter",
            ("DurableJobAdapter", "JobReconciliationRequiredError"),
        ),
        (
            "job_service.py",
            "codex_bridge.workflows.job_service",
            ("start_mock_job_service", "MockJobServiceProcess"),
        ),
        (
            "process_identity.py",
            "codex_bridge.workflows.process_identity",
            ("get_process_identity", "terminate_spawned_python"),
        ),
        (
            "codex_http_client.py",
            "codex_bridge.workflows.http_client",
            ("CodexHTTPClient", "CodexPreflightError"),
        ),
    ],
)
def test_poc_modules_forward_to_product_implementation(
    monkeypatch: pytest.MonkeyPatch,
    poc_module: str,
    product_module: str,
    symbols: tuple[str, ...],
) -> None:
    import importlib
    import sys

    monkeypatch.syspath_prepend(str(POC_OWS))
    product = importlib.import_module(product_module)
    forwarded = runpy.run_path(str(POC_OWS / poc_module))

    assert all(forwarded[name] is getattr(product, name) for name in symbols)
    assert "codex_bridge.workflows" in sys.modules
