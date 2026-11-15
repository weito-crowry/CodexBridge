from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from codex_bridge.workflows.graph import project_workflow_graph
from codex_bridge.workflows.interpreter import OwsError, parse_workflow_text

FIXTURES = Path(__file__).parents[1] / "poc" / "ows" / "fixtures"


@pytest.mark.parametrize(
    ("fixture_name", "codex_job_live", "max_live_codex_jobs", "task_count"),
    [
        ("codex_job_workflow.yaml", True, 1, 2),
        ("dataflow_workflow.yaml", False, 1, 2),
        ("workflow.yaml", False, 1, 9),
        ("three_codex_job_loop_workflow.yaml", True, 3, 4),
        ("conditional_items_codex_workflow.yaml", True, 3, 4),
        ("mock_job_workflow.yaml", False, 1, 4),
    ],
)
def test_ows_fixtures_project_to_their_static_task_counts(
    fixture_name: str,
    codex_job_live: bool,
    max_live_codex_jobs: int,
    task_count: int,
) -> None:
    definition = parse_workflow_text((FIXTURES / fixture_name).read_text(encoding="utf-8"))

    graph = project_workflow_graph(
        definition,
        codex_job_live=codex_job_live,
        max_live_codex_jobs=max_live_codex_jobs,
    )

    assert graph["schema_version"] == 1
    assert graph["static_task_count"] == task_count
    assert len(graph["nodes"]) == task_count
    assert all(
        node["operation"] in {"do", "set", "switch", "for", "call"} for node in graph["nodes"]
    )
    json.dumps(graph, ensure_ascii=False, allow_nan=False)


def test_node_ids_escape_json_pointer_names_and_allow_nested_duplicate_names() -> None:
    definition: dict[str, Any] = {
        "document": {
            "dsl": "1.0.3",
            "namespace": "codexbridge",
            "name": "pointer-test",
            "version": "1.0.0",
        },
        "evaluate": {"language": "jq", "mode": "strict"},
        "do": [
            {
                "a/b~c": {
                    "do": [{"a/b~c": {"set": {"value": "nested"}}}],
                }
            },
            {"finish": {"set": {"value": "done"}}},
        ],
    }

    graph = project_workflow_graph(definition)

    assert [node["node_id"] for node in graph["nodes"]] == [
        "/do/0/a~1b~0c",
        "/do/0/a~1b~0c/do/0/a~1b~0c",
        "/do/1/finish",
    ]
    assert graph["nodes"][1]["parent_scope"] == "/do/0/a~1b~0c/do"
    assert graph["scopes"][1]["owner_node"] == "/do/0/a~1b~0c"
    assert graph["static_task_count"] == 3


def test_switch_edges_keep_priority_case_metadata_and_no_match_fallthrough() -> None:
    definition = {
        "document": {
            "dsl": "1.0.3",
            "namespace": "codexbridge",
            "name": "switch-no-default",
            "version": "1.0.0",
        },
        "evaluate": {"language": "jq", "mode": "strict"},
        "do": [
            {
                "choose": {
                    "switch": [
                        {"first": {"when": "${ .first }", "then": "left"}},
                        {"second": {"when": "${ .second }", "then": "right"}},
                    ]
                }
            },
            {"left": {"set": {"value": "left"}, "then": "exit"}},
            {"right": {"set": {"value": "right"}, "then": "finish"}},
            {"finish": {"set": {"value": "finish"}}},
        ],
    }

    graph = project_workflow_graph(definition)
    choose_edges = [edge for edge in graph["edges"] if edge["source"] == "/do/0/choose"]

    assert [(edge["kind"], edge["target"]) for edge in choose_edges] == [
        ("branch", "/do/1/left"),
        ("branch", "/do/2/right"),
        ("fallthrough", "/do/1/left"),
    ]
    assert choose_edges[0]["case_index"] == 0
    assert choose_edges[0]["case_name"] == "first"
    assert choose_edges[0]["when"] == "${ .first }"
    assert choose_edges[0]["then"] == "left"
    assert choose_edges[1]["case_index"] == 1
    assert choose_edges[1]["case_name"] == "second"


def test_switch_default_is_a_branch_and_disables_implicit_fallthrough() -> None:
    definition = {
        "document": {
            "dsl": "1.0.3",
            "namespace": "codexbridge",
            "name": "switch-default",
            "version": "1.0.0",
        },
        "evaluate": {"language": "jq", "mode": "strict"},
        "do": [
            {
                "choose": {
                    "switch": [
                        {"selected": {"when": "${ .selected }", "then": "finish"}},
                        {"fallback": {"then": "fallback"}},
                    ]
                }
            },
            {"fallback": {"set": {"value": "fallback"}}},
            {"finish": {"set": {"value": "finish"}}},
        ],
    }

    graph = project_workflow_graph(definition)
    choose_edges = [edge for edge in graph["edges"] if edge["source"] == "/do/0/choose"]

    assert [(edge["kind"], edge["target"]) for edge in choose_edges] == [
        ("branch", "/do/2/finish"),
        ("branch", "/do/1/fallback"),
    ]
    assert choose_edges[1]["case_default"] is True
    assert "when" not in choose_edges[1]
    assert not any(edge["kind"] == "fallthrough" for edge in choose_edges)


def test_do_and_for_scopes_return_exit_and_loop_without_unrolling() -> None:
    definition = {
        "document": {
            "dsl": "1.0.3",
            "namespace": "codexbridge",
            "name": "scope-control-flow",
            "version": "1.0.0",
        },
        "evaluate": {"language": "jq", "mode": "strict"},
        "do": [
            {
                "container": {
                    "do": [
                        {"inner": {"set": {"value": "one"}, "then": "exit"}},
                        {"unreachable": {"set": {"value": "two"}}},
                    ],
                    "then": "after_do",
                }
            },
            {
                "loop": {
                    "for": {"in": "${ .items }"},
                    "do": [{"body": {"set": {"value": "${ $item }"}, "then": "exit"}}],
                    "then": "after_loop",
                }
            },
            {"after_do": {"set": {"value": "do-return"}}},
            {"after_loop": {"set": {"value": "loop-return"}}},
        ],
    }

    graph = project_workflow_graph(definition)
    edges = graph["edges"]
    do_scope_exit = "@scope:/do/0/container/do:exit"
    loop_scope_entry = "@scope:/do/1/loop/do:entry"
    loop_scope_exit = "@scope:/do/1/loop/do:exit"

    assert {
        "source": "/do/0/container",
        "target": "@scope:/do/0/container/do:entry",
        "kind": "scope_entry",
    } in [{key: edge[key] for key in ("source", "target", "kind")} for edge in edges]
    assert any(
        edge["source"] == do_scope_exit
        and edge["target"] == "/do/2/after_do"
        and edge["kind"] == "scope_return"
        for edge in edges
    )
    assert not any(
        edge["source"] == "/do/0/container" and edge["target"] == "/do/2/after_do" for edge in edges
    )
    assert any(
        edge["source"] == "/do/1/loop"
        and edge["target"] == loop_scope_entry
        and edge["kind"] == "loop_body"
        for edge in edges
    )
    assert any(
        edge["source"] == "/do/1/loop"
        and edge["target"] == "/do/3/after_loop"
        and edge["kind"] == "loop_exit"
        and edge["iteration"] == "zero"
        for edge in edges
    )
    assert any(
        edge["source"] == loop_scope_exit
        and edge["target"] == loop_scope_entry
        and edge["kind"] == "loop_repeat"
        for edge in edges
    )
    assert any(
        edge["source"] == loop_scope_exit
        and edge["target"] == "/do/3/after_loop"
        and edge["kind"] == "loop_exit"
        and edge["iteration"] == "after_body"
        for edge in edges
    )
    assert graph["static_task_count"] == 7


def test_leaf_flow_edges_distinguish_sequence_jump_and_scope_exit() -> None:
    definition = {
        "document": {
            "dsl": "1.0.3",
            "namespace": "codexbridge",
            "name": "leaf-flow",
            "version": "1.0.0",
        },
        "evaluate": {"language": "jq", "mode": "strict"},
        "do": [
            {"first": {"set": {"value": "one"}}},
            {"second": {"set": {"value": "two"}, "then": "last"}},
            {"last": {"set": {"value": "three"}, "then": "exit"}},
        ],
    }

    graph = project_workflow_graph(definition)

    edge_pairs = {(edge["source"], edge["target"], edge["kind"]) for edge in graph["edges"]}
    assert ("/do/0/first", "/do/1/second", "sequence") in edge_pairs
    assert ("/do/1/second", "/do/2/last", "jump") in edge_pairs
    assert ("/do/2/last", "@scope:/do:exit", "scope_exit") in edge_pairs


def test_graph_is_deterministic_json_safe_and_does_not_mutate_input() -> None:
    source_a = """document:
  dsl: "1.0.3"
  namespace: codexbridge
  name: stable
  version: "1.0.0"
evaluate: {language: jq, mode: strict}
do:
  - first:
      set:
        value: ${ .value + 1 }
  - finish:
      set: "${ . }"
"""
    source_b = """# style and comments differ
document: {dsl: '1.0.3', namespace: codexbridge, name: stable, version: '1.0.0'}
evaluate:
  language: jq
  mode: strict
do:
  - first: {set: {value: '${ .value + 1 }'}}
  - finish: {set: '${ . }'}
"""
    definition = parse_workflow_text(source_a)
    before = copy.deepcopy(definition)

    first = project_workflow_graph(definition)
    second = project_workflow_graph(parse_workflow_text(source_b))

    assert first == second
    assert definition == before
    assert first["nodes"][0]["attributes"]["set"]["value"] == "${ .value + 1 }"
    json.dumps(first, ensure_ascii=False, allow_nan=False)


def test_graph_rejects_non_json_mapping_keys_instead_of_rewriting_the_ast() -> None:
    definition = {
        "document": {
            "dsl": "1.0.3",
            "namespace": "codexbridge",
            "name": "non-json-key",
            "version": "1.0.0",
            "tags": {1: "value"},
        },
        "evaluate": {"language": "jq", "mode": "strict"},
        "do": [{"finish": {"set": {"value": "done"}}}],
    }

    with pytest.raises(OwsError):
        project_workflow_graph(definition)


@pytest.mark.parametrize(
    "tasks",
    [
        [{"first": {"set": {"value": "one"}, "then": "missing"}}],
        [
            {"first": {"set": {"value": "one"}, "then": "second"}},
            {"second": {"set": {"value": "two"}, "then": "first"}},
        ],
        [{"unsupported": {"emit": {"event": "ignored"}}}],
    ],
)
def test_invalid_jump_cycle_or_unsupported_task_never_returns_partial_graph(
    tasks: list[dict[str, Any]],
) -> None:
    definition = {
        "document": {
            "dsl": "1.0.3",
            "namespace": "codexbridge",
            "name": "invalid-graph",
            "version": "1.0.0",
        },
        "evaluate": {"language": "jq", "mode": "strict"},
        "do": tasks,
    }

    with pytest.raises(OwsError):
        project_workflow_graph(definition)


def test_codex_job_fixture_requires_explicit_live_profile_for_projection() -> None:
    definition = parse_workflow_text(
        (FIXTURES / "codex_job_workflow.yaml").read_text(encoding="utf-8")
    )

    with pytest.raises(OwsError):
        project_workflow_graph(definition)
    graph = project_workflow_graph(
        definition,
        codex_job_live=True,
        max_live_codex_jobs=1,
    )
    assert graph["static_task_count"] == 2
