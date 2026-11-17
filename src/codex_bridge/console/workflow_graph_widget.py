from __future__ import annotations

import math
import re
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any, cast

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .i18n import tr

_MAX_NODES = 256
_MAX_EDGES = 1024
_MAX_VIRTUAL_NODES = 64
_MAX_EVENTS = 150
_MAX_EVENT_PAGE = 100
_MAX_EXECUTIONS_PER_NODE = 4096
_MAX_TOTAL_EXECUTIONS = 8192
_MAX_DETAIL_CHARS = 16_000
_MAX_INT = 2**63 - 1
_NODE_ID = re.compile(r"^/(?!/)[^\\?#\x00-\x1f\x7f]{1,2048}$")
_VIRTUAL_ID = re.compile(r"^@scope:/[^\\?#\x00-\x1f\x7f]{0,1024}:(?:entry|exit)$")
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$", re.ASCII)
_VERSION = re.compile(r"^sha256:[0-9a-f]{64}$", re.ASCII)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_SECRET = re.compile(r"(?i)\b(?:authorization|token|api[_-]?key|password|secret)\s*[:=]\s*\S+")
_CREDENTIAL_LIKE = re.compile(
    r"(?i)\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{12,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{12,}|"
    r"AKIA[0-9A-Z]{16})\b"
)
_WINDOWS_PATH = re.compile(r"(?i)(?:\b[A-Z]:[\\/]|\\\\)[^\r\n\"'<>|]+")
_FILE_URI = re.compile(r"(?i)\bfile:[/\\]{2,}[^\r\n\"'<>|]+")
_POSIX_PATH = re.compile(
    r"(?<![A-Za-z0-9])/(?:home|Users|private|tmp|var|etc|mnt|opt|root)/[^\s\"'<>|]+"
)

_WORKFLOW_STATUSES = frozenset(
    {
        "queued",
        "starting",
        "running",
        "succeeded",
        "failed",
        "failed_to_start",
        "needs_reconciliation",
        "unknown",
    }
)
_COVERAGE = frozenset({"complete", "partial", "unavailable", "unknown"})
_NODE_STATES = frozenset(
    {"not_started", "running", "succeeded", "failed", "unknown", "needs_reconciliation"}
)
_EVENT_TYPES = frozenset(
    {
        "task_started",
        "task_completed",
        "task_failed",
        "branch_selected",
        "scope_entered",
        "scope_exited",
        "iteration_started",
        "iteration_completed",
        "workflow_completed",
    }
)
_EDGE_KINDS = frozenset(
    {
        "sequence",
        "branch",
        "jump",
        "loop_body",
        "loop_repeat",
        "loop_exit",
        "scope_entry",
        "scope_exit",
        "scope_return",
        "fallthrough",
        "default",
    }
)
_OPERATIONS = frozenset(
    {"call", "do", "for", "set", "switch", "if", "while", "parallel", "all", "any"}
)
_METADATA_ENUMS: dict[str, frozenset[str]] = {
    "operation": _OPERATIONS,
    "branch_class": frozenset({"continue", "exit", "jump"}),
    "failure_phase": frozenset({"input", "operation", "output", "export"}),
}
_METADATA_NUMBERS = frozenset({"iteration_ordinal", "case_index"})
_EVIDENCE_SOURCES = frozenset(
    {"node_events", "workflow_status", "worker_status", "dbos_status", "codex_job_ledger"}
)
_COVERAGE_REASON_KEYS = frozenset(
    {
        "journal_missing",
        "journal_unavailable",
        "journal_invalid",
        "journal_corrupt",
        "journal_schema_invalid",
        "journal_event_limit",
        "journal_busy",
        "journal_too_large",
        "journal_unsafe_path",
        "graph_invalid",
        "truncated",
        "workflow_not_confirmed",
        "evidence_conflict",
        "ledger_unavailable",
        "ledger_invalid",
        "run_integrity_invalid",
        "identity_invalid",
        "unknown",
    }
)
_EDGE_LABELS = {
    "sequence": "sequence",
    "branch": "branch",
    "jump": "jump",
    "loop_body": "loop body",
    "loop_repeat": "loop repeat",
    "loop_exit": "loop exit",
    "scope_entry": "scope entry",
    "scope_exit": "scope exit",
    "scope_return": "scope return",
    "fallthrough": "fallthrough",
    "default": "default",
}


def _safe_text(
    value: object, limit: int = 256, *, node_id: bool = False, scope: bool = False
) -> str:
    if not isinstance(value, str) or not value:
        return tr("Not available")
    if (
        _CONTROL.search(value)
        or _BEARER.search(value)
        or _SECRET.search(value)
        or _CREDENTIAL_LIKE.search(value)
    ):
        return tr("[redacted]")
    if _WINDOWS_PATH.search(value) or _FILE_URI.search(value) or _POSIX_PATH.search(value):
        return tr("[path hidden]")
    if "<" in value or ">" in value:
        return tr("[redacted]")
    if node_id:
        if not (_NODE_ID.fullmatch(value) or _VIRTUAL_ID.fullmatch(value)):
            return tr("Not available")
    elif scope and _NODE_ID.fullmatch(value):
        pass
    elif "/" in value or "\\" in value:
        return tr("[path hidden]")
    safe = " ".join(value.split())
    if len(safe) > limit:
        return safe[: max(0, limit - 1)] + "…"
    return safe


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and _OPAQUE_ID.fullmatch(value) is not None


def _valid_node_id(value: object) -> bool:
    return isinstance(value, str) and (
        _NODE_ID.fullmatch(value) is not None or _VIRTUAL_ID.fullmatch(value) is not None
    )


def _short_node_id(value: str) -> str:
    safe = _safe_text(value, 2048, node_id=True)
    if safe == tr("Not available"):
        return safe
    leaf = safe.rsplit("/", 1)[-1].replace(":entry", " entry").replace(":exit", " exit")
    if len(leaf) > 28:
        leaf = leaf[:12] + "…" + leaf[-12:]
    return leaf


def _known_state(value: object) -> str | None:
    return value if isinstance(value, str) and value in _NODE_STATES else None


def _validate_snapshot(workflow_id: str, payload: object) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    run_id = payload.get("run_id")
    snapshot_version = payload.get("snapshot_version")
    workflow_status = payload.get("workflow_status")
    coverage = payload.get("coverage")
    if (
        type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != 1
        or payload.get("workflow_id") != workflow_id
        or not _valid_id(workflow_id)
        or not _valid_id(run_id)
        or not isinstance(snapshot_version, str)
        or _VERSION.fullmatch(snapshot_version) is None
        or not isinstance(workflow_status, str)
        or workflow_status not in _WORKFLOW_STATUSES
        or not isinstance(coverage, str)
        or coverage not in _COVERAGE
        or type(payload.get("latest_event_id")) is not int
        or not 0 <= cast(int, payload.get("latest_event_id")) <= _MAX_INT
        or type(payload.get("truncated")) is not bool
        or not isinstance(payload.get("nodes"), list)
        or not isinstance(payload.get("edges"), list)
    ):
        return None
    reason = payload.get("coverage_reason")
    if reason is not None and (not isinstance(reason, str) or reason not in _COVERAGE_REASON_KEYS):
        return None
    counts = payload.get("counts")
    if not isinstance(counts, Mapping) or any(
        key not in counts or type(counts[key]) is not int or counts[key] < 0
        for key in ("nodes", "edges", "executions")
    ):
        return None

    nodes: list[dict[str, Any]] = []
    seen_nodes: set[str] = set()
    execution_total = 0
    for raw in payload["nodes"]:
        if not isinstance(raw, Mapping):
            return None
        node_id = raw.get("node_id")
        operation = raw.get("operation")
        parent_scope = raw.get("parent_scope")
        order = raw.get("order")
        executions = raw.get("executions")
        if (
            not _valid_node_id(node_id)
            or cast(str, node_id).startswith("@scope:")
            or node_id in seen_nodes
            or not isinstance(operation, str)
            or not re.fullmatch(r"[\w.-]{1,64}", operation, re.UNICODE)
            or not isinstance(parent_scope, str)
            or len(parent_scope) > 1024
            or _CONTROL.search(parent_scope)
            or type(order) is not int
            or not 0 <= order <= _MAX_INT
            or _known_state(raw.get("state")) is None
            or type(raw.get("completion_observed")) is not bool
            or not isinstance(executions, list)
            or len(executions) > _MAX_EXECUTIONS_PER_NODE
        ):
            return None
        seen_nodes.add(cast(str, node_id))
        state = _known_state(raw.get("state"))
        completion_observed = raw.get("completion_observed")
        clean_executions: list[dict[str, Any]] = []
        for execution in executions:
            if not isinstance(execution, Mapping):
                return None
            if (
                not isinstance(execution.get("execution_id"), str)
                or not execution.get("execution_id")
                or len(cast(str, execution.get("execution_id"))) > 512
                or not isinstance(execution.get("task_identity"), str)
                or len(cast(str, execution.get("task_identity"))) > 2048
                or _known_state(execution.get("state")) is None
                or type(execution.get("completion_observed")) is not bool
                or type(execution.get("observation_complete")) is not bool
                or type(execution.get("evidence_conflict")) is not bool
                or "started_at" not in execution
                or "completed_at" not in execution
                or not isinstance(execution.get("evidence_sources"), list)
                or any(
                    not isinstance(item, str) or item not in _EVIDENCE_SOURCES
                    for item in execution["evidence_sources"]
                )
            ):
                return None
            for time_key in ("started_at", "completed_at"):
                time_value = execution.get(time_key)
                if time_value is not None and (
                    not isinstance(time_value, str)
                    or len(time_value) > 128
                    or _CONTROL.search(time_value)
                ):
                    return None
            for identifier_key in ("job_id", "thread_id", "turn_id"):
                identifier = execution.get(identifier_key)
                if identifier is not None and (
                    not isinstance(identifier, str) or not identifier or len(identifier) > 512
                ):
                    return None
            clean_execution: dict[str, Any] = {
                "execution_id": execution.get("execution_id"),
                "task_identity": execution.get("task_identity"),
                "state": execution.get("state"),
                "started_at": execution.get("started_at"),
                "completed_at": execution.get("completed_at"),
                "completion_observed": execution.get("completion_observed"),
                "observation_complete": execution.get("observation_complete"),
                "evidence_sources": list(execution["evidence_sources"]),
                "evidence_conflict": execution.get("evidence_conflict"),
            }
            for identifier_key in ("job_id", "thread_id", "turn_id"):
                if identifier_key in execution:
                    clean_execution[identifier_key] = execution[identifier_key]
            clean_executions.append(clean_execution)
        node = {
            "node_id": node_id,
            "operation": operation,
            "parent_scope": parent_scope,
            "order": order,
            "state": state,
            "completion_observed": completion_observed,
            "executions": clean_executions,
        }
        nodes.append(node)
        execution_total += len(clean_executions)
        if execution_total > _MAX_TOTAL_EXECUTIONS:
            return None

    edges: list[dict[str, str]] = []
    for raw in payload["edges"]:
        if not isinstance(raw, Mapping):
            return None
        source = raw.get("source")
        target = raw.get("target")
        kind = raw.get("kind")
        if (
            not isinstance(source, str)
            or not _valid_node_id(source)
            or not isinstance(target, str)
            or not _valid_node_id(target)
            or not isinstance(kind, str)
        ):
            return None
        if (
            kind not in _EDGE_KINDS
            or (source not in seen_nodes and _VIRTUAL_ID.fullmatch(source) is None)
            or (target not in seen_nodes and _VIRTUAL_ID.fullmatch(target) is None)
        ):
            return None
        edges.append({"source": source, "target": target, "kind": kind})
    return {
        "schema_version": 1,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "workflow_status": workflow_status,
        "coverage": coverage,
        "coverage_reason": reason,
        "latest_event_id": cast(int, payload.get("latest_event_id")),
        "snapshot_version": snapshot_version,
        "truncated": cast(bool, payload.get("truncated")),
        "counts": {key: counts[key] for key in ("nodes", "edges", "executions")},
        "nodes": nodes,
        "edges": edges,
    }


def _metadata_text(metadata: object) -> str:
    if not isinstance(metadata, Mapping):
        return ""
    parts: list[str] = []
    for key in sorted(key for key in metadata if isinstance(key, str)):
        value = metadata[key]
        if key in _METADATA_ENUMS and isinstance(value, str) and value in _METADATA_ENUMS[key]:
            parts.append(f"{key}={value}")
        elif key in _METADATA_NUMBERS and type(value) is int and 0 <= value <= 1_000_000:
            parts.append(f"{key}={value}")
    return ", ".join(parts)


def _yes_no(value: object) -> str:
    return tr("Yes") if value is True else tr("No")


class _WorkflowNodeItem(QGraphicsObject):
    _WIDTH = 220.0
    _HEIGHT = 76.0

    def __init__(self, node: Mapping[str, Any], activate: Any) -> None:
        super().__init__()
        self.node_id = cast(str, node["node_id"])
        self.operation = cast(str, node["operation"])
        self.state = cast(str, node["state"])
        self._activate = activate
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setToolTip(self._tooltip())

    def boundingRect(self) -> QRectF:
        return QRectF(0.0, 0.0, self._WIDTH, self._HEIGHT)

    def paint(self, painter: QPainter, _option: object, _widget: QWidget | None = None) -> None:
        palette = QApplication.palette()
        dark = palette.color(QPalette.ColorRole.Window).lightness() < 128
        fills = {
            "succeeded": ("#14532d" if dark else "#dcfce7"),
            "failed": ("#7f1d1d" if dark else "#fee2e2"),
            "running": ("#1e3a8a" if dark else "#dbeafe"),
            "not_started": ("#374151" if dark else "#f3f4f6"),
            "unknown": ("#78350f" if dark else "#fef3c7"),
            "needs_reconciliation": ("#701a75" if dark else "#fae8ff"),
        }
        fill = QColor(fills.get(self.state, fills["unknown"]))
        outline = (
            palette.color(QPalette.ColorRole.Highlight)
            if self.isSelected()
            else palette.color(QPalette.ColorRole.Mid)
        )
        text_color = QColor("#ffffff") if dark else palette.color(QPalette.ColorRole.Text)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(outline, 2.0 if self.isSelected() else 1.0))
        painter.setBrush(QBrush(fill))
        painter.drawRoundedRect(self.boundingRect(), 7.0, 7.0)
        painter.setPen(text_color)
        painter.drawText(
            QRectF(9.0, 8.0, self._WIDTH - 18.0, 30.0),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            f"{_safe_text(self.operation, 32)} · {_short_node_id(self.node_id)}",
        )
        painter.drawText(
            QRectF(9.0, 40.0, self._WIDTH - 18.0, 26.0),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            f"{tr('State')}: {self.state}",
        )

    def update_node(self, node: Mapping[str, Any]) -> None:
        self.operation = cast(str, node["operation"])
        self.state = cast(str, node["state"])
        self.setToolTip(self._tooltip())
        self.update()

    def _tooltip(self) -> str:
        return (
            f"{_safe_text(self.operation, 64)} · "
            f"{_safe_text(self.node_id, 2048, node_id=True)} · {self.state}"
        )

    def mousePressEvent(self, event: Any) -> None:
        self._activate(self.node_id)
        super().mousePressEvent(event)


class _WorkflowGraphView(QGraphicsView):
    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = 1.15 if event.angleDelta().y() > 0 else 1.0 / 1.15
            current_scale = self.transform().m11()
            if 0.25 <= current_scale * factor <= 3.5:
                self.scale(factor, factor)
            event.accept()
            return
        super().wheelEvent(event)


class WorkflowExecutionGraphWidget(QWidget):
    """Bounded, read-only projection of one Workflow task snapshot."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._workflow_id: str | None = None
        self._run_id: str | None = None
        self._snapshot_version: str | None = None
        self._snapshot: dict[str, Any] | None = None
        self._structure: (
            tuple[tuple[tuple[str, str, int], ...], tuple[tuple[str, str, str], ...]] | None
        ) = None
        self._node_records: dict[str, dict[str, Any]] = {}
        self._node_items: dict[str, _WorkflowNodeItem] = {}
        self._edge_items: list[QGraphicsPathItem] = []
        self._virtual_node_items: dict[str, QGraphicsEllipseItem] = {}
        self._scope_labels: list[QGraphicsSimpleTextItem] = []
        self._selected_node_id: str | None = None
        self._events: OrderedDict[int, str] = OrderedDict()
        self._event_cursor = 0
        self._event_latest = 0
        self._older_event_count = 0
        self._event_truncated = False
        self._graph_truncated = False
        self._omitted_graph_count = 0
        self._has_more = False

        layout = QVBoxLayout(self)
        self.metadata_label = QLabel(tr("Select a workflow to view its execution graph."), self)
        self.metadata_label.setWordWrap(True)
        self.graph_status_label = QLabel("", self)
        self.graph_status_label.setWordWrap(True)
        layout.addWidget(self.metadata_label)
        layout.addWidget(self.graph_status_label)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.graph_scene = QGraphicsScene(self)
        self.graph_view = _WorkflowGraphView(self.graph_scene, splitter)
        self.graph_view.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.graph_view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.graph_view.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.graph_view.setMinimumWidth(360)
        splitter.addWidget(self.graph_view)

        details = QWidget(splitter)
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(4, 0, 0, 0)
        details_layout.addWidget(QLabel(tr("Workflow graph nodes"), details))
        self.node_list = QListWidget(details)
        self.node_list.currentItemChanged.connect(self._node_selection_changed)
        details_layout.addWidget(self.node_list, 1)
        details_layout.addWidget(QLabel(tr("Workflow graph node details"), details))
        self.detail_view = QPlainTextEdit(details)
        self.detail_view.setReadOnly(True)
        self.detail_view.setMaximumHeight(230)
        details_layout.addWidget(self.detail_view)
        details_layout.addWidget(QLabel(tr("Workflow graph event history"), details))
        self.event_list = QListWidget(details)
        self.event_list.setMaximumHeight(180)
        details_layout.addWidget(self.event_list)
        splitter.addWidget(details)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

    @property
    def node_items(self) -> dict[str, _WorkflowNodeItem]:
        return self._node_items

    @property
    def edge_items(self) -> list[QGraphicsPathItem]:
        return self._edge_items

    @property
    def virtual_node_items(self) -> dict[str, QGraphicsEllipseItem]:
        return self._virtual_node_items

    @property
    def selected_node_id(self) -> str | None:
        return self._selected_node_id

    @property
    def event_cursor(self) -> int:
        return self._event_cursor

    @property
    def older_event_count(self) -> int:
        return self._older_event_count

    @property
    def omitted_graph_count(self) -> int:
        return self._omitted_graph_count

    @property
    def has_more_events(self) -> bool:
        return self._has_more

    @property
    def snapshot_identity(self) -> tuple[str, str, str] | None:
        if self._workflow_id is None or self._run_id is None or self._snapshot_version is None:
            return None
        return (self._workflow_id, self._run_id, self._snapshot_version)

    def set_snapshot(self, workflow_id: str, payload: object) -> bool:
        normalized = _validate_snapshot(workflow_id, payload)
        if normalized is None:
            self.set_error("Workflow graph response was invalid.")
            return False
        next_workflow = normalized["workflow_id"]
        next_run = normalized["run_id"]
        next_version = normalized["snapshot_version"]
        identity = (next_workflow, next_run, next_version)
        previous = self.snapshot_identity
        if identity == previous:
            self._snapshot = normalized
            self._update_metadata()
            return True

        different_run = self._workflow_id != next_workflow or self._run_id != next_run
        if different_run:
            self._reset_events()
            self._selected_node_id = None
        selected = self._selected_node_id
        records = {cast(str, node["node_id"]): node for node in normalized["nodes"]}
        structure = self._make_structure(normalized)
        same_structure = not different_run and self._structure == structure
        old_transform = self.graph_view.transform()
        old_horizontal = self.graph_view.horizontalScrollBar().value()
        old_vertical = self.graph_view.verticalScrollBar().value()
        if same_structure:
            for node_id, item in self._node_items.items():
                item.update_node(records[node_id])
        else:
            self._rebuild_graph(normalized)
        self._workflow_id = next_workflow
        self._run_id = next_run
        self._snapshot_version = next_version
        self._snapshot = normalized
        self._node_records = records
        self._structure = structure
        self._graph_truncated = normalized["truncated"]
        returned_executions = sum(len(node["executions"]) for node in normalized["nodes"])
        counts = cast(Mapping[str, int], normalized["counts"])
        self._omitted_graph_count = (
            max(0, counts["nodes"] - len(normalized["nodes"]))
            + max(0, counts["edges"] - len(normalized["edges"]))
            + max(0, counts["executions"] - returned_executions)
            + max(0, len(normalized["nodes"]) - len(self._node_items))
            + max(0, len(normalized["edges"]) - len(self._edge_items))
        )
        self._selected_node_id = selected if selected in self._node_items else None
        if same_structure:
            self.graph_view.setTransform(old_transform)
            self.graph_view.horizontalScrollBar().setValue(old_horizontal)
            self.graph_view.verticalScrollBar().setValue(old_vertical)
        self._sync_node_list()
        self._render_selected_node()
        self._update_metadata()
        return True

    @staticmethod
    def _make_structure(
        snapshot: Mapping[str, Any],
    ) -> tuple[tuple[tuple[str, str, int], ...], tuple[tuple[str, str, str], ...]]:
        return (
            tuple(
                sorted(
                    (
                        cast(str, node["node_id"]),
                        cast(str, node["parent_scope"]),
                        cast(int, node["order"]),
                    )
                    for node in snapshot["nodes"]
                )
            ),
            tuple(
                sorted((edge["source"], edge["target"], edge["kind"]) for edge in snapshot["edges"])
            ),
        )

    def _rebuild_graph(self, snapshot: Mapping[str, Any]) -> None:
        self.graph_scene.clear()
        self._node_items.clear()
        self._edge_items.clear()
        self._virtual_node_items.clear()
        self._scope_labels.clear()
        nodes = cast(list[dict[str, Any]], snapshot["nodes"])
        edges = cast(list[dict[str, str]], snapshot["edges"])
        shown_nodes = nodes[:_MAX_NODES]
        node_by_id = {cast(str, item["node_id"]): item for item in shown_nodes}
        coordinates = self._layout_nodes(shown_nodes)
        centers: dict[str, QPointF] = {}

        virtual_edges: list[dict[str, str]] = []
        virtual_seen: set[str] = set()
        for edge in edges[:_MAX_EDGES]:
            source = edge["source"]
            target = edge["target"]
            missing = [
                endpoint
                for endpoint in (source, target)
                if endpoint not in node_by_id and _VIRTUAL_ID.fullmatch(endpoint)
            ]
            invalid_missing = any(
                endpoint not in node_by_id and _VIRTUAL_ID.fullmatch(endpoint) is None
                for endpoint in (source, target)
            )
            if invalid_missing:
                continue
            if len(virtual_seen | set(missing)) > _MAX_VIRTUAL_NODES:
                continue
            virtual_seen.update(missing)
            virtual_edges.append(edge)

        virtual_use: dict[str, tuple[float, float, int]] = {}
        for edge in virtual_edges:
            source, target = edge["source"], edge["target"]
            if source in node_by_id:
                virtual_use.setdefault(
                    target,
                    (
                        coordinates[source][0] + 230.0,
                        coordinates[source][1] + 31.0,
                        1,
                    ),
                )
            elif target in node_by_id:
                virtual_use.setdefault(
                    source,
                    (
                        coordinates[target][0] - 54.0,
                        coordinates[target][1] + 31.0,
                        -1,
                    ),
                )
        virtual_ordinal = 0
        for virtual in sorted(virtual_seen):
            if virtual in virtual_use:
                continue
            scope = virtual[len("@scope:") :].rsplit(":", 1)[0]
            scope_depth = min(8, scope.count("/"))
            side = 1 if virtual.endswith(":exit") else -1
            related_rows = [
                coordinates[node_id][1]
                for node_id, node in node_by_id.items()
                if cast(str, node["parent_scope"]) == scope
                or cast(str, node["parent_scope"]).startswith(scope + "/")
            ]
            y = min(related_rows) + 31.0 if related_rows else virtual_ordinal * 112.0 + 31.0
            virtual_use[virtual] = (scope_depth * 270.0 + (230.0 if side > 0 else -54.0), y, side)
            virtual_ordinal += 1

        for virtual_id, (x, y, _side) in virtual_use.items():
            virtual_item = QGraphicsEllipseItem(QRectF(x, y, 12.0, 12.0))
            virtual_item.setBrush(
                QBrush(QApplication.palette().color(QPalette.ColorRole.Highlight))
            )
            virtual_item.setPen(QPen(QApplication.palette().color(QPalette.ColorRole.Text), 1.0))
            suffix = "entry" if virtual_id.endswith(":entry") else "exit"
            virtual_item.setToolTip(f"{tr('Virtual scope point')}: {tr(suffix)}")
            virtual_item.setData(0, virtual_id)
            self.graph_scene.addItem(virtual_item)
            label = QGraphicsSimpleTextItem(tr(suffix))
            label.setBrush(QBrush(QApplication.palette().color(QPalette.ColorRole.Text)))
            label.setPos(x + 14.0, y - 3.0)
            label.setToolTip(virtual_item.toolTip())
            self.graph_scene.addItem(label)
            self._virtual_node_items[virtual_id] = virtual_item
            centers[virtual_id] = QPointF(x + 6.0, y + 6.0)

        self._add_scope_labels(shown_nodes, coordinates)
        for node_id, node in node_by_id.items():
            x, y = coordinates[node_id]
            node_item = _WorkflowNodeItem(node, self.select_node)
            node_item.setPos(x, y)
            self.graph_scene.addItem(node_item)
            self._node_items[node_id] = node_item
            centers[node_id] = QPointF(x, y)

        visible_kinds: set[str] = set()
        for edge in edges[:_MAX_EDGES]:
            source, target = edge["source"], edge["target"]
            if source not in centers or target not in centers:
                continue
            path = self._edge_path(source, target, centers, node_by_id)
            edge_item = QGraphicsPathItem(path)
            edge_item.setPen(QPen(QApplication.palette().color(QPalette.ColorRole.Mid), 1.4))
            edge_item.setBrush(QBrush(QApplication.palette().color(QPalette.ColorRole.Mid)))
            kind = edge["kind"]
            edge_item.setToolTip(f"{tr('Edge type')}: {tr(_EDGE_LABELS[kind])}")
            edge_item.setZValue(-1.0)
            self.graph_scene.addItem(edge_item)
            self._edge_items.append(edge_item)
            visible_kinds.add(kind)

        bounds = self.graph_scene.itemsBoundingRect()
        if visible_kinds:
            legend = ", ".join(tr(_EDGE_LABELS[kind]) for kind in sorted(visible_kinds))
            label = QGraphicsSimpleTextItem(f"{tr('Graph edge legend')}: {legend}")
            label.setBrush(QBrush(QApplication.palette().color(QPalette.ColorRole.Text)))
            label.setPos(bounds.left(), bounds.bottom() + 20.0)
            self.graph_scene.addItem(label)
        bounds = self.graph_scene.itemsBoundingRect()
        self.graph_scene.setSceneRect(bounds.adjusted(-48.0, -48.0, 64.0, 48.0))
        self.graph_view.fitInView(self.graph_scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    @staticmethod
    def _layout_nodes(nodes: list[dict[str, Any]]) -> dict[str, tuple[float, float]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for node in nodes:
            groups.setdefault(cast(str, node["parent_scope"]), []).append(node)
        positions: dict[str, tuple[float, float]] = {}
        row = 0
        for scope in sorted(groups, key=lambda item: (item.count("/"), item)):
            group = sorted(
                groups[scope],
                key=lambda item: (cast(int, item["order"]), cast(str, item["node_id"])),
            )
            for node in group:
                node_id = cast(str, node["node_id"])
                positions[node_id] = (min(8, scope.count("/")) * 270.0, row * 112.0 + 30.0)
                row += 1
            row += 1
        return positions

    def _add_scope_labels(
        self,
        nodes: list[dict[str, Any]],
        coordinates: Mapping[str, tuple[float, float]],
    ) -> None:
        scopes: dict[str, float] = {}
        for node in nodes:
            scope = cast(str, node["parent_scope"])
            scopes[scope] = min(
                scopes.get(scope, math.inf), coordinates[cast(str, node["node_id"])][1]
            )
        for scope, y in sorted(scopes.items(), key=lambda item: (item[1], item[0])):
            text = _safe_text(scope or "root", 80, scope=True)
            if text == tr("[path hidden]"):
                text = tr("Workflow scope")
            else:
                text = text.replace("/", " › ")
            label = QGraphicsSimpleTextItem(f"{tr('Scope')}: {text}")
            label.setBrush(QBrush(QApplication.palette().color(QPalette.ColorRole.PlaceholderText)))
            label.setPos(0.0, y - 22.0)
            label.setToolTip(text)
            self.graph_scene.addItem(label)
            self._scope_labels.append(label)

    @staticmethod
    def _edge_path(
        source: str,
        target: str,
        centers: Mapping[str, QPointF],
        nodes: Mapping[str, dict[str, Any]],
    ) -> QPainterPath:
        source_center = centers[source]
        target_center = centers[target]
        start = (
            QPointF(source_center.x() + 220.0, source_center.y() + 38.0)
            if source in nodes
            else QPointF(source_center)
        )
        end = (
            QPointF(target_center.x(), target_center.y() + 38.0)
            if target in nodes
            else QPointF(target_center)
        )
        dx = end.x() - start.x()
        control_one = QPointF(start.x() + dx * 0.45, start.y())
        control_two = QPointF(end.x() - dx * 0.45, end.y())
        if dx < 20.0:
            control_one.setX(start.x() + 50.0)
            control_two.setX(end.x() - 50.0)
            lift = -32.0 if target_center.y() < source_center.y() else 32.0
            control_one.setY(start.y() + lift)
            control_two.setY(end.y() + lift)
        angle = math.atan2(end.y() - control_two.y(), end.x() - control_two.x())
        size = 8.0
        left = QPointF(
            end.x() - size * math.cos(angle - math.pi / 6.0),
            end.y() - size * math.sin(angle - math.pi / 6.0),
        )
        right = QPointF(
            end.x() - size * math.cos(angle + math.pi / 6.0),
            end.y() - size * math.sin(angle + math.pi / 6.0),
        )
        path = QPainterPath(start)
        path.cubicTo(control_one, control_two, end)
        path.moveTo(end)
        path.lineTo(left)
        path.lineTo(right)
        path.closeSubpath()
        return path

    def _sync_node_list(self) -> None:
        previous = self.node_list.blockSignals(True)
        try:
            self.node_list.clear()
            for node_id in sorted(
                self._node_records,
                key=lambda item: (
                    cast(str, self._node_records[item]["parent_scope"]),
                    cast(int, self._node_records[item]["order"]),
                    item,
                ),
            ):
                node = self._node_records[node_id]
                text = (
                    f"{_safe_text(node['operation'], 32)} · {_short_node_id(node_id)} · "
                    f"{node['state']}"
                )
                item = QListWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, node_id)
                self.node_list.addItem(item)
                if node_id == self._selected_node_id:
                    self.node_list.setCurrentItem(item)
        finally:
            self.node_list.blockSignals(previous)

    def _node_selection_changed(
        self, current: QListWidgetItem | None, _previous: QListWidgetItem | None
    ) -> None:
        value = current.data(Qt.ItemDataRole.UserRole) if current is not None else None
        self.select_node(value if isinstance(value, str) else None)

    def select_node(self, node_id: str | None) -> None:
        if node_id is not None and node_id not in self._node_records:
            return
        self._selected_node_id = node_id
        for current_id, node_item in self._node_items.items():
            node_item.setSelected(current_id == node_id)
        previous = self.node_list.blockSignals(True)
        try:
            target: QListWidgetItem | None = None
            for index in range(self.node_list.count()):
                list_item = self.node_list.item(index)
                if list_item.data(Qt.ItemDataRole.UserRole) == node_id:
                    target = list_item
                    break
            if target is None:
                self.node_list.setCurrentRow(-1)
            else:
                self.node_list.setCurrentItem(target)
        finally:
            self.node_list.blockSignals(previous)
        self._render_selected_node()

    def _render_selected_node(self) -> None:
        if self._selected_node_id is None or self._snapshot is None:
            self.detail_view.clear()
            return
        node = self._node_records.get(self._selected_node_id)
        if node is None:
            self._selected_node_id = None
            self.detail_view.clear()
            return
        node_label = (
            f"{tr('Workflow node')}: {_safe_text(node['operation'], 64)} · "
            f"{_short_node_id(self._selected_node_id)}"
        )
        parts = [
            node_label,
            f"{tr('State')}: {node['state']}",
            f"{tr('Completion observed')}: {_yes_no(node['completion_observed'])}",
            f"{tr('Execution count')}: {len(node['executions'])}",
        ]
        for index, execution in enumerate(node["executions"][:32], start=1):
            parts.extend(
                [
                    f"{tr('Execution')}: {index}",
                    f"  {tr('Task identity')}: {_safe_text(execution['task_identity'], 160)}",
                    f"  {tr('Execution ID')}: {_safe_text(execution['execution_id'], 128)}",
                    f"  {tr('State')}: {execution['state']}",
                    f"  {tr('Started')}: {_safe_text(execution.get('started_at'), 128)}",
                    f"  {tr('Completed')}: {_safe_text(execution.get('completed_at'), 128)}",
                    f"  {tr('Job ID')}: {_safe_text(execution.get('job_id'), 128)}",
                    f"  {tr('Codex thread ID')}: {_safe_text(execution.get('thread_id'), 128)}",
                    f"  {tr('Turn ID')}: {_safe_text(execution.get('turn_id'), 128)}",
                    f"  {tr('Completion observed')}: {_yes_no(execution['completion_observed'])}",
                    f"  {tr('Observation complete')}: {_yes_no(execution['observation_complete'])}",
                    f"  {tr('Evidence conflict')}: {_yes_no(execution['evidence_conflict'])}",
                    f"  {tr('Evidence sources')}: "
                    f"{', '.join(execution['evidence_sources']) or tr('None')}",
                ]
            )
            if sum(map(len, parts)) > _MAX_DETAIL_CHARS:
                break
        if len(node["executions"]) > 32:
            parts.append(f"{tr('Additional executions omitted')}: {len(node['executions']) - 32}")
        self.detail_view.setPlainText("\n".join(parts)[:_MAX_DETAIL_CHARS])

    def set_events(self, payload: object) -> bool:
        if not isinstance(payload, Mapping) or self._snapshot is None:
            return False
        coverage = payload.get("coverage")
        if (
            type(payload.get("schema_version")) is not int
            or payload.get("schema_version") != 1
            or payload.get("workflow_id") != self._workflow_id
            or payload.get("run_id") != self._run_id
            or not isinstance(payload.get("events"), list)
            or len(cast(list[Any], payload.get("events"))) > _MAX_EVENT_PAGE
            or type(payload.get("next_after_id")) is not int
            or not self._event_cursor <= cast(int, payload.get("next_after_id")) <= _MAX_INT
            or type(payload.get("has_more")) is not bool
            or type(payload.get("truncated")) is not bool
            or not isinstance(coverage, str)
            or coverage not in _COVERAGE
            or type(payload.get("latest_event_id")) is not int
            or not 0 <= cast(int, payload.get("latest_event_id")) <= _MAX_INT
        ):
            return False
        reason = payload.get("coverage_reason")
        if reason is not None and (
            not isinstance(reason, str) or reason not in _COVERAGE_REASON_KEYS
        ):
            return False
        events = cast(list[Any], payload["events"])
        normalized: list[tuple[int, str]] = []
        event_ids: set[int] = set()
        for event in events:
            if not isinstance(event, Mapping):
                return False
            event_id = event.get("event_id")
            event_type = event.get("event_type")
            node_id = event.get("node_id")
            occurred_at = event.get("occurred_at")
            source = event.get("source")
            if (
                type(event_id) is not int
                or not 1 <= event_id <= _MAX_INT
                or event_id in event_ids
                or not isinstance(event_type, str)
                or event_type not in _EVENT_TYPES
                or (event_type == "workflow_completed" and node_id is not None)
                or (event_type != "workflow_completed" and node_id is None)
                or (node_id is not None and not _valid_node_id(node_id))
                or not isinstance(occurred_at, str)
                or len(occurred_at) > 128
                or _CONTROL.search(occurred_at)
                or source != "interpreter"
                or not isinstance(event.get("metadata", {}), Mapping)
            ):
                return False
            event_ids.add(event_id)
            node_text = _short_node_id(node_id) if isinstance(node_id, str) else tr("Workflow")
            line = (
                f"#{event_id} · {event_type} · {node_text} · "
                f"{_safe_text(occurred_at, 128)} · {source}"
            )
            metadata = _metadata_text(event.get("metadata"))
            if metadata:
                line += f" · {metadata}"
            normalized.append((event_id, line[:512]))
        next_after_id = cast(int, payload["next_after_id"])
        latest_event_id = cast(int, payload["latest_event_id"])
        if any(event_id > next_after_id for event_id, _line in normalized):
            return False
        if latest_event_id < next_after_id and coverage != "unavailable":
            return False
        if cast(bool, payload["has_more"]) != (next_after_id < latest_event_id):
            return False
        for event_id, line in sorted(normalized):
            if event_id <= self._event_cursor or event_id in self._events:
                continue
            self._events[event_id] = line
        self._event_cursor = next_after_id
        self._event_latest = latest_event_id
        self._has_more = cast(bool, payload["has_more"])
        self._event_truncated = self._event_truncated or cast(bool, payload["truncated"])
        while len(self._events) > _MAX_EVENTS:
            self._events.popitem(last=False)
            self._older_event_count += 1
        self.event_list.clear()
        for line in self._events.values():
            self.event_list.addItem(line)
        self._snapshot["coverage"] = coverage
        if reason is not None:
            self._snapshot["coverage_reason"] = reason
        self._snapshot["latest_event_id"] = latest_event_id
        self._update_metadata()
        return True

    def set_error(self, message: str) -> None:
        self.graph_status_label.setText(message)

    def clear_data(self) -> None:
        self._selected_node_id = None
        previous_signals_blocked = self.node_list.blockSignals(True)
        try:
            self.node_list.clear()
        finally:
            self.node_list.blockSignals(previous_signals_blocked)
        self._node_items.clear()
        self._edge_items.clear()
        self._virtual_node_items.clear()
        self._scope_labels.clear()
        self.graph_scene.clear()
        self.event_list.clear()
        self.detail_view.clear()
        self._workflow_id = None
        self._run_id = None
        self._snapshot_version = None
        self._snapshot = None
        self._structure = None
        self._node_records.clear()
        self._reset_events()
        self._omitted_graph_count = 0
        self._graph_truncated = False
        self.metadata_label.setText(tr("Select a workflow to view its execution graph."))
        self.graph_status_label.clear()

    def _reset_events(self) -> None:
        self._events.clear()
        self._event_cursor = 0
        self._event_latest = 0
        self._older_event_count = 0
        self._event_truncated = False
        self._has_more = False
        self.event_list.clear()

    def _update_metadata(self) -> None:
        if self._snapshot is None:
            return
        workflow_status = cast(str, self._snapshot["workflow_status"])
        coverage = cast(str, self._snapshot["coverage"])
        reason = self._snapshot.get("coverage_reason")
        coverage_text = f"{tr('Coverage')}: {coverage}"
        if reason:
            coverage_text += f" ({reason})"
        if coverage in {"partial", "unavailable", "unknown"}:
            coverage_text += f" · {tr('Task coverage is incomplete or unavailable')}"
        omitted = self._omitted_graph_count + self._older_event_count
        if self._graph_truncated or self._event_truncated:
            omitted = max(1, omitted)
        truncation = f" · {tr('Truncated items')}: {omitted}" if omitted else ""
        more = f" · {tr('More events available')}" if self._has_more else ""
        self.metadata_label.setText(
            f"{tr('Workflow status')}: {workflow_status} · {coverage_text} · "
            f"{tr('Event cursor')}: {self._event_cursor}/{self._event_latest}{more}{truncation}"
        )
        self.graph_status_label.clear()
