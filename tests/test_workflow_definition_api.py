from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace

import httpx2 as httpx
import pytest

from codex_bridge.activity import ActivityStore
from codex_bridge.config import BridgeConfig
from codex_bridge.ui_api import create_ui_app
from codex_bridge.workflows.definitions import WorkflowDefinitionRepository
from tests.test_workflows_api import _workflow_yaml

_TOKEN = "definition-api-test-token"
_BASE = "/ui-api/control/workflow-definitions"
_DEFINITION_ID = "00000000-0000-4000-8000-000000000001"


class FakeBridge:
    app_server_ready = True
    app_server_os_pid = 4567


def _settings(tmp_path, *, token: str | None = _TOKEN) -> BridgeConfig:
    return replace(
        BridgeConfig.from_sources(
            explicit_allowed_roots=(str(tmp_path),),
            environ={},
            config_data={},
        ),
        ui_port=18001,
        control_token=token,
    )


def _app(tmp_path, *, repository=None, token: str | None = _TOKEN):
    return create_ui_app(
        FakeBridge(),
        ActivityStore(),
        _settings(tmp_path, token=token),
        workflow_definitions=repository,
    )


def _transport(app):
    return httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))


def _source(name: str, prompt: str = "definition prompt") -> str:
    return _workflow_yaml(prompt).replace("fixed-codex-job", name)


def _payload(mutation_id: str, source: str) -> dict[str, object]:
    return {
        "mutation_request_id": mutation_id,
        "source_yaml": source,
        "max_live_codex_jobs": 1,
    }


def _auth(**headers: str) -> dict[str, str]:
    return {"authorization": f"Bearer {_TOKEN}", **headers}


@pytest.mark.asyncio
async def test_http_definition_routes_create_read_validate_update_history_and_graph(
    tmp_path,
) -> None:
    state_root = tmp_path / "state" / "workflows"
    repository = WorkflowDefinitionRepository(state_root)
    app = _app(tmp_path, repository=repository)
    source_one = _source("definition-one", "private-definition-prompt")
    source_two = _source("definition-one-updated", "private-updated-prompt")

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        validation = await client.post(
            f"{_BASE}/validate",
            headers=_auth(),
            json={"source_yaml": source_one, "max_live_codex_jobs": 1},
        )
        assert validation.status_code == 200
        assert validation.json()["valid"] is True
        assert (
            validation.json()["source_sha256"]
            == hashlib.sha256(source_one.encode("utf-8")).hexdigest()
        )
        assert isinstance(validation.json()["canonical_sha256"], str)
        assert validation.json()["max_live_codex_jobs"] == 1
        assert "source_yaml" not in validation.json()
        assert not state_root.exists()

        created = await client.post(
            _BASE,
            headers=_auth(),
            json=_payload("http-create-one", source_one),
        )
        assert created.status_code == 201
        first = created.json()
        assert first["revision"] == 1
        assert first["source_sha256"] == hashlib.sha256(source_one.encode("utf-8")).hexdigest()
        assert first["etag"] == created.headers["etag"]
        assert "source_yaml" not in first
        assert "private-definition-prompt" not in created.text
        definition_id = first["definition_id"]

        listed = await client.get(_BASE, headers=_auth())
        assert listed.status_code == 200
        assert listed.json()["definitions"][0]["definition_id"] == definition_id
        assert listed.json()["definitions"][0]["document_name"] == "definition-one"
        assert "source_yaml" not in listed.text
        assert "private-definition-prompt" not in listed.text

        detail = await client.get(f"{_BASE}/{definition_id}", headers=_auth())
        revision_one = await client.get(f"{_BASE}/{definition_id}/revisions/1", headers=_auth())
        assert detail.status_code == revision_one.status_code == 200
        assert detail.headers["etag"] == first["etag"]
        assert detail.json()["source_yaml"] == source_one
        assert revision_one.json()["source_yaml"] == source_one

        graph = await client.get(f"{_BASE}/{definition_id}/graph", headers=_auth())
        assert graph.status_code == 200
        assert graph.headers["etag"] == first["etag"]
        assert graph.json()["definition_id"] == definition_id
        assert graph.json()["revision"] == 1
        assert "private-definition-prompt" in graph.text

        updated = await client.put(
            f"{_BASE}/{definition_id}",
            headers=_auth(**{"if-match": first["etag"]}),
            json=_payload("http-update-one", source_two),
        )
        assert updated.status_code == 200
        assert updated.json()["revision"] == 2
        assert updated.headers["etag"] == updated.json()["etag"]
        assert "source_yaml" not in updated.json()

        old_graph = await client.get(f"{_BASE}/{definition_id}/graph?revision=1", headers=_auth())
        revision_two = await client.get(f"{_BASE}/{definition_id}/revisions/2", headers=_auth())
        assert old_graph.json()["revision"] == 1
        assert revision_two.json()["source_yaml"] == source_two

    database = state_root / "definitions.sqlite"
    assert database.is_file()
    assert not (state_root / "requests.sqlite").exists()


@pytest.mark.asyncio
async def test_http_update_etag_errors_stale_cas_and_original_replay(tmp_path) -> None:
    repository = WorkflowDefinitionRepository(tmp_path / "state")
    app = _app(tmp_path, repository=repository)
    source_one = _source("revision-one")
    source_two = _source("revision-two")
    source_three = _source("revision-three")

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        created = await client.post(
            _BASE, headers=_auth(), json=_payload("create-replay", source_one)
        )
        definition_id = created.json()["definition_id"]
        etag_one = created.headers["etag"]
        first_update = await client.put(
            f"{_BASE}/{definition_id}",
            headers=_auth(**{"if-match": etag_one}),
            json=_payload("update-replay", source_two),
        )
        second_update = await client.put(
            f"{_BASE}/{definition_id}",
            headers=_auth(**{"if-match": first_update.headers["etag"]}),
            json=_payload("update-after-replay", source_three),
        )
        replay = await client.put(
            f"{_BASE}/{definition_id}",
            headers=_auth(**{"if-match": etag_one}),
            json=_payload("update-replay", source_two),
        )
        conflict = await client.put(
            f"{_BASE}/{definition_id}",
            headers=_auth(**{"if-match": etag_one}),
            json=_payload("update-replay", source_three),
        )
        stale = await client.put(
            f"{_BASE}/{definition_id}",
            headers=_auth(**{"if-match": etag_one}),
            json=_payload("new-stale-update", _source("stale")),
        )
        missing = await client.put(
            f"{_BASE}/{definition_id}",
            headers=_auth(),
            json=_payload("missing-if-match", source_three),
        )
        malformed = [
            await client.put(
                f"{_BASE}/{definition_id}",
                headers=_auth(**{"if-match": value}),
                json=_payload(f"bad-etag-{index}", source_three),
            )
            for index, value in enumerate(
                (
                    "W/" + etag_one,
                    "*",
                    etag_one + ", " + etag_one,
                    " " + etag_one,
                    etag_one + " ",
                )
            )
        ]

    assert first_update.status_code == second_update.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["revision"] == 2
    assert replay.headers["etag"] == first_update.headers["etag"]
    assert conflict.status_code == 409
    assert conflict.json() == {"error": "mutation_conflict"}
    assert stale.status_code == 412
    assert stale.json() == {"error": "precondition_failed"}
    assert missing.status_code == 428
    assert all(response.status_code == 400 for response in malformed)


@pytest.mark.asyncio
async def test_http_update_rejects_duplicate_if_match_header_fields(tmp_path) -> None:
    repository = WorkflowDefinitionRepository(tmp_path / "state")
    app = _app(tmp_path, repository=repository)
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        created = await client.post(
            _BASE, headers=_auth(), json=_payload("duplicate-header-create", _source("one"))
        )
        definition_id = created.json()["definition_id"]
        response = await client.put(
            f"{_BASE}/{definition_id}",
            headers=[
                ("authorization", f"Bearer {_TOKEN}"),
                ("if-match", created.headers["etag"]),
                ("if-match", created.headers["etag"]),
            ],
            json=_payload("duplicate-header-update", _source("two")),
        )

    assert response.status_code == 400
    assert response.json() == {"error": "invalid"}
    assert repository.get(definition_id).revision == 1


@pytest.mark.asyncio
async def test_http_put_unknown_definition_returns_404_with_or_without_database(tmp_path) -> None:
    valid_etag = '"workflow-definition-v1-' + "0" * 64 + '"'
    payload = _payload("unknown-definition-update", _source("unknown"))
    unknown_id = "00000000-0000-4000-8000-000000000001"

    empty_root = tmp_path / "empty-state"
    empty_repository = WorkflowDefinitionRepository(empty_root)
    empty_app = _app(tmp_path, repository=empty_repository)
    async with httpx.AsyncClient(
        transport=_transport(empty_app), base_url="http://localhost"
    ) as client:
        without_database = await client.put(
            f"{_BASE}/{unknown_id}",
            headers=_auth(**{"if-match": valid_etag}),
            json=payload,
        )
    assert without_database.status_code == 404
    assert without_database.json() == {"error": "not_found"}
    assert not empty_root.exists()

    repository = WorkflowDefinitionRepository(tmp_path / "initialized-state")
    created = repository.create(
        mutation_request_id="known-definition-create",
        source_yaml=_source("known"),
        max_live_codex_jobs=1,
    )
    app = _app(tmp_path, repository=repository)
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        with_database = await client.put(
            f"{_BASE}/{unknown_id}",
            headers=_auth(**{"if-match": created.etag}),
            json=payload,
        )
        malformed_id = await client.put(
            f"{_BASE}/not-a-uuid",
            headers=_auth(**{"if-match": created.etag}),
            json=payload,
        )

    assert with_database.status_code == 404
    assert with_database.json() == {"error": "not_found"}
    assert malformed_id.status_code == 400


@pytest.mark.asyncio
async def test_http_put_corrupt_definition_database_returns_503(tmp_path) -> None:
    state_root = tmp_path / "corrupt-put-state"
    state_root.mkdir()
    database = state_root / "definitions.sqlite"
    database.write_bytes(b"not sqlite")
    repository = WorkflowDefinitionRepository(state_root)
    app = _app(tmp_path, repository=repository)

    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        response = await client.put(
            f"{_BASE}/{_DEFINITION_ID}",
            headers=_auth(**{"if-match": '"workflow-definition-v1-' + "0" * 64 + '"'}),
            json=_payload("corrupt-definition-update", _source("corrupt")),
        )

    assert response.status_code == 503
    assert response.json() == {"error": "repository_unavailable"}
    assert database.read_bytes() == b"not sqlite"


@pytest.mark.asyncio
async def test_http_concurrent_updates_use_repository_compare_and_swap(tmp_path) -> None:
    repository = WorkflowDefinitionRepository(tmp_path / "state")
    app = _app(tmp_path, repository=repository)
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        created = await client.post(
            _BASE, headers=_auth(), json=_payload("cas-create", _source("cas-one"))
        )
        definition_id = created.json()["definition_id"]
        etag = created.headers["etag"]
        responses = await asyncio.gather(
            *(
                client.put(
                    f"{_BASE}/{definition_id}",
                    headers=_auth(**{"if-match": etag}),
                    json=_payload(f"cas-update-{number}", _source(f"cas-{number}")),
                )
                for number in (1, 2)
            )
        )

    assert sorted(response.status_code for response in responses) == [200, 412]
    assert repository.get(definition_id).revision == 2


@pytest.mark.asyncio
async def test_http_definition_routes_reject_invalid_input_and_enforce_auth(tmp_path) -> None:
    missing_root = tmp_path / "missing-state"
    repository = WorkflowDefinitionRepository(missing_root)
    app = _app(tmp_path, repository=repository)
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        empty = await client.get(_BASE, headers=_auth())
        invalid_limit = await client.get(_BASE + "?limit=0", headers=_auth())
        duplicate_limit = await client.get(_BASE + "?limit=1&limit=2", headers=_auth())
        invalid_cursor = await client.get(_BASE + "?cursor=not-a-cursor", headers=_auth())
        duplicate_json = await client.post(
            _BASE,
            headers=_auth(**{"content-type": "application/json"}),
            content=b'{"mutation_request_id":"x","mutation_request_id":"y"}',
        )
        nan = await client.post(
            _BASE,
            headers=_auth(**{"content-type": "application/json"}),
            content=b'{"mutation_request_id":"x","source_yaml":NaN}',
        )
        extra_field = await client.post(
            _BASE,
            headers=_auth(),
            json={**_payload("extra", _source("extra")), "unexpected": True},
        )
        missing_profile = await client.post(
            _BASE,
            headers=_auth(),
            json={
                "mutation_request_id": "missing-profile",
                "source_yaml": _source("missing-profile"),
            },
        )
        large_body = await client.post(
            _BASE,
            headers=_auth(**{"content-type": "application/json"}),
            content=b" " * (128 * 1024 + 1),
        )
        large_yaml = await client.post(
            _BASE,
            headers=_auth(),
            json=_payload("too-large-source", "x" * (64 * 1024 + 1)),
        )
        invalid_id = await client.get(f"{_BASE}/not-a-uuid", headers=_auth())
        missing_id = await client.get(f"{_BASE}/{_DEFINITION_ID}", headers=_auth())
        invalid_revision = await client.get(
            f"{_BASE}/{_DEFINITION_ID}/revisions/0", headers=_auth()
        )
        missing_revision = await client.get(
            f"{_BASE}/{_DEFINITION_ID}/revisions/1", headers=_auth()
        )
        invalid_graph_revision = await client.get(
            f"{_BASE}/{_DEFINITION_ID}/graph?revision=01", headers=_auth()
        )
        forbidden = await client.get(_BASE, headers={"authorization": "Bearer wrong"})
        bad_origin = await client.get(
            _BASE,
            headers=_auth(origin="http://evil.example:18001"),
        )
        bad_host = await client.get(
            _BASE,
            headers=_auth(host="evil.example"),
        )
        no_token_app = _app(tmp_path / "no-token", repository=repository, token=None)

    assert empty.status_code == 200
    assert empty.json() == {"definitions": [], "limit": 50, "next_cursor": None}
    assert not missing_root.exists()
    assert all(
        response.status_code == 400
        for response in (
            invalid_limit,
            duplicate_limit,
            invalid_cursor,
            duplicate_json,
            nan,
            extra_field,
            missing_profile,
        )
    )
    assert large_body.status_code == large_yaml.status_code == 413
    assert invalid_id.status_code == invalid_revision.status_code == 400
    assert missing_id.status_code == missing_revision.status_code == 404
    assert invalid_graph_revision.status_code == 400
    assert forbidden.status_code == bad_origin.status_code == 403
    assert bad_host.status_code == 400
    async with httpx.AsyncClient(
        transport=_transport(no_token_app), base_url="http://localhost"
    ) as client:
        not_registered = await client.get(_BASE, headers=_auth())
    assert not_registered.status_code == 404


@pytest.mark.asyncio
async def test_http_get_preserves_database_and_classifies_corruption_and_large_response(
    tmp_path, monkeypatch
) -> None:
    import codex_bridge.workflows.definition_api as api_module

    state_root = tmp_path / "state"
    repository = WorkflowDefinitionRepository(state_root)
    app = _app(tmp_path, repository=repository)
    async with httpx.AsyncClient(transport=_transport(app), base_url="http://localhost") as client:
        created = await client.post(
            _BASE, headers=_auth(), json=_payload("read-only-create", _source("read-only"))
        )
        database = state_root / "definitions.sqlite"
        original_bytes = database.read_bytes()
        detail = await client.get(f"{_BASE}/{created.json()['definition_id']}", headers=_auth())
        assert detail.status_code == 200
        assert database.read_bytes() == original_bytes

        monkeypatch.setattr(api_module, "_MAX_HTTP_RESPONSE_BYTES", 1)
        oversized = await client.get(f"{_BASE}/{created.json()['definition_id']}", headers=_auth())

    assert oversized.status_code == 413
    assert oversized.json() == {"error": "response_too_large"}

    corrupt_root = tmp_path / "corrupt"
    corrupt_root.mkdir()
    corrupt_db = corrupt_root / "definitions.sqlite"
    corrupt_db.write_bytes(b"not sqlite")
    corrupt_app = _app(tmp_path, repository=WorkflowDefinitionRepository(corrupt_root))
    async with httpx.AsyncClient(
        transport=_transport(corrupt_app), base_url="http://localhost"
    ) as client:
        unavailable = await client.get(_BASE, headers=_auth())
    assert unavailable.status_code == 503
    assert unavailable.json() == {"error": "repository_unavailable"}
    assert corrupt_db.read_bytes() == b"not sqlite"
