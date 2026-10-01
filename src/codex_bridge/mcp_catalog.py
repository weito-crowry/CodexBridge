from __future__ import annotations

import fnmatch
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from mcp import types
from pydantic import ConfigDict

from .config import GitHubMcpConfig


class CatalogError(ValueError):
    """Raised when a public MCP tool catalog cannot be built safely."""


class _ForwardCompatibleTool(types.Tool):
    model_config = ConfigDict(extra="allow")


CatalogListToolsResult = types.ListToolsResult


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    native_tools: tuple[types.Tool, ...]
    remote_tools: tuple[types.Tool, ...]
    upstream_tool_count: int
    serialized_schema_bytes: int
    catalog_sha256: str

    @property
    def native_tool_count(self) -> int:
        return len(self.native_tools)

    @property
    def exposed_remote_tool_count(self) -> int:
        return len(self.remote_tools)

    @property
    def total_tool_count(self) -> int:
        return self.native_tool_count + self.exposed_remote_tool_count

    @property
    def tools(self) -> tuple[types.Tool, ...]:
        return self.native_tools + self.remote_tools


def _wire_tool(tool: types.Tool, *, name: str) -> _ForwardCompatibleTool:
    try:
        data = tool.model_dump(by_alias=True, mode="json", exclude_none=True)
        data["name"] = name
        return _ForwardCompatibleTool.model_validate(data)
    except (TypeError, ValueError) as exc:
        raise CatalogError("upstream tool metadata cannot be serialized safely") from exc


def _canonical_tool(tool: types.Tool) -> dict[str, Any]:
    try:
        data = tool.model_dump(by_alias=True, mode="json", exclude_none=True)
        json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return data
    except (TypeError, ValueError) as exc:
        raise CatalogError("tool metadata cannot be serialized safely") from exc


def build_catalog(
    native_tools: tuple[types.Tool, ...] | list[types.Tool],
    upstream_tools: tuple[types.Tool, ...] | list[types.Tool],
    config: GitHubMcpConfig,
) -> CatalogSnapshot:
    native = tuple(native_tools)
    upstream = tuple(upstream_tools)
    if any(not isinstance(tool.name, str) or not tool.name for tool in native + upstream):
        raise CatalogError("tool metadata contains an invalid name")
    native_names = [tool.name for tool in native]
    if len(native_names) != len(set(native_names)):
        raise CatalogError("native tool name collision")

    selected = (
        [
            tool
            for tool in upstream
            if any(fnmatch.fnmatchcase(tool.name, pattern) for pattern in config.include)
            and not any(fnmatch.fnmatchcase(tool.name, pattern) for pattern in config.exclude)
        ]
        if config.enabled
        else []
    )
    selected.sort(key=lambda item: item.name)

    upstream_names = [tool.name for tool in selected]
    if len(upstream_names) != len(set(upstream_names)):
        raise CatalogError("upstream tool name collision")

    remote = tuple(
        _wire_tool(tool, name=f"{config.prefix}{tool.name}")
        for tool in selected[: config.max_tools or None]
    )
    exposed_names = [tool.name for tool in remote]
    if len(exposed_names) != len(set(exposed_names)):
        raise CatalogError("exposed remote tool name collision")
    if set(native_names) & set(exposed_names):
        raise CatalogError("native and remote tool name collision")

    all_tools = native + remote
    canonical = sorted((_canonical_tool(tool) for tool in all_tools), key=lambda item: item["name"])
    serialized = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return CatalogSnapshot(
        native_tools=native,
        remote_tools=remote,
        upstream_tool_count=len(upstream),
        serialized_schema_bytes=len(serialized),
        catalog_sha256=hashlib.sha256(serialized).hexdigest(),
    )
