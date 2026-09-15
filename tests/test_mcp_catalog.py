from __future__ import annotations

import pytest
from mcp import types

from codex_bridge.config import GitHubMcpConfig
from codex_bridge.mcp_catalog import CatalogError, build_catalog


def github_config(**overrides) -> GitHubMcpConfig:
    values = {"enabled": True, "pat": "test-pat"}
    values.update(overrides)
    return GitHubMcpConfig(**values)


def tool(name: str, *, description: str | None = None) -> types.Tool:
    return types.Tool(
        name=name,
        title=f"Title for {name}",
        description=description or f"Description for {name}",
        inputSchema={"type": "object", "properties": {"value": {"type": "string"}}},
        outputSchema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
        annotations=types.ToolAnnotations(readOnlyHint=True),
        _meta={"source": "upstream", "name": name},
    )


def test_catalog_changes_only_tool_name_and_preserves_metadata() -> None:
    upstream = tool("get_file_contents")

    snapshot = build_catalog((), (upstream,), github_config())

    exposed = snapshot.remote_tools[0]
    assert exposed.name == "github_get_file_contents"
    assert exposed.title == upstream.title
    assert exposed.description == upstream.description
    assert exposed.input_schema == upstream.input_schema
    assert exposed.output_schema == upstream.output_schema
    assert exposed.annotations == upstream.annotations
    assert exposed.icons == upstream.icons
    assert exposed.meta == upstream.meta


def test_catalog_filter_sort_and_max_tools_are_deterministic() -> None:
    upstream = tuple(tool(name) for name in ["repo_z", "get_a", "repo_a", "repo_secret"])
    settings = github_config(include=("repo_*", "get_*"), exclude=("repo_secret",), max_tools=2)

    snapshot = build_catalog((), upstream, settings)

    assert [item.name for item in snapshot.remote_tools] == ["github_get_a", "github_repo_a"]
    assert snapshot.upstream_tool_count == 4
    assert snapshot.exposed_remote_tool_count == 2


def test_catalog_max_25_is_subset_of_max_50() -> None:
    upstream = tuple(tool(f"tool_{index:02d}") for index in range(60))

    small = build_catalog((), upstream, github_config(max_tools=25))
    large = build_catalog((), upstream, github_config(max_tools=50))

    assert set(item.name for item in small.remote_tools) <= {
        item.name for item in large.remote_tools
    }


def test_disabled_catalog_does_not_expose_upstream_tools() -> None:
    snapshot = build_catalog(
        (),
        (tool("get_file_contents"),),
        GitHubMcpConfig(enabled=False),
    )

    assert snapshot.upstream_tool_count == 1
    assert snapshot.remote_tools == ()


def test_catalog_detects_native_collision() -> None:
    with pytest.raises(CatalogError, match="collision"):
        build_catalog(
            (tool("github_get_file_contents"),),
            (tool("get_file_contents"),),
            github_config(),
        )


def test_catalog_rejects_invalid_tool_name() -> None:
    invalid = types.Tool.model_construct(name="", input_schema={"type": "object"})

    with pytest.raises(CatalogError, match="metadata"):
        build_catalog((), (invalid,), github_config())


def test_catalog_fingerprint_is_stable_and_measures_serialized_catalog() -> None:
    upstream = (tool("b"), tool("a"))

    first = build_catalog((), upstream, github_config())
    second = build_catalog((), tuple(reversed(upstream)), github_config())

    assert first.catalog_sha256 == second.catalog_sha256
    assert first.serialized_schema_bytes == second.serialized_schema_bytes
    assert first.serialized_schema_bytes > 0
    assert first.total_tool_count == 2


def test_github_pat_is_not_in_config_repr() -> None:
    settings = github_config()

    assert "test-pat" not in repr(settings)
