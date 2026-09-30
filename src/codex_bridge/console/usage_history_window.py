from __future__ import annotations

import csv
import sqlite3
from collections.abc import Callable, Iterable
from datetime import datetime
from functools import partial
from math import inf
from pathlib import Path
from time import time

from PySide6.QtCharts import (
    QChart,
    QChartView,
    QDateTimeAxis,
    QLegendMarker,
    QLineSeries,
    QScatterSeries,
    QValueAxis,
)
from PySide6.QtCore import QByteArray, QDateTime, QMargins, QSettings, Qt
from PySide6.QtGui import QBrush, QCloseEvent, QCursor, QFont, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QDateTimeEdit,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from .usage_history import (
    UsageHistoryEvent,
    UsageHistorySample,
    get_confirmed_usage_events,
    get_latest_usage_remaining,
    get_usage_samples,
    get_usage_samples_for_display,
)

_DAY = 24 * 60 * 60
_TARGET_POINTS = 4_000
_GEOMETRY_KEY = "console/usageHistory/geometry"
_PRESETS = (("7 days", 7), ("1 month", 30), ("1 year", 365))


def format_sample_tooltip(sample: UsageHistorySample) -> str:
    lines = [datetime.fromtimestamp(sample.captured_at_epoch).strftime("%Y/%m/%d %H:%M:%S")]
    if sample.five_hour_remaining is not None:
        lines.append(f"5h remaining: {sample.five_hour_remaining}%")
    if sample.weekly_remaining is not None:
        lines.append(f"Weekly remaining: {sample.weekly_remaining}%")
    return "\n".join(lines)


def format_reset_tooltip(event: UsageHistoryEvent) -> str:
    increase = event.current_weekly_remaining - event.previous_weekly_remaining
    return "\n".join(
        (
            "Weekly reset candidate",
            datetime.fromtimestamp(event.occurred_at_epoch).strftime("%Y/%m/%d %H:%M:%S"),
            f"{event.previous_weekly_remaining}% → {event.current_weekly_remaining}%",
            f"+{increase} pt",
            "Candidate inferred from an observed weekly quota increase.",
        )
    )


def write_usage_history_csv(
    path: str | Path,
    samples: Iterable[UsageHistorySample],
) -> None:
    with Path(path).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("timestamp", "five_hour_remaining", "weekly_remaining"))
        for sample in samples:
            timestamp = (
                datetime.fromtimestamp(sample.captured_at_epoch)
                .astimezone()
                .isoformat(timespec="seconds")
            )
            writer.writerow(
                (
                    timestamp,
                    "" if sample.five_hour_remaining is None else sample.five_hour_remaining,
                    "" if sample.weekly_remaining is None else sample.weekly_remaining,
                )
            )


def export_usage_history_csv(
    path: str | Path,
    start_epoch: float,
    end_epoch: float,
    *,
    database_path: Path | None = None,
) -> int:
    samples = get_usage_samples(start_epoch, end_epoch, database_path=database_path)
    write_usage_history_csv(path, samples)
    return len(samples)


class UsageHistoryWindow(QMainWindow):
    preset_object_names = {
        "7 days": "usagePreset7Days",
        "1 month": "usagePreset1Month",
        "1 year": "usagePreset1Year",
        "Custom": "usagePresetCustom",
    }

    def __init__(
        self,
        database_path: Path | None = None,
        *,
        now: Callable[[], float] | None = None,
        settings: QSettings | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self._database_path = database_path
        self._now = time if now is None else now
        self._settings = settings or QSettings("CodexBridge", "Console")
        self._selected_preset = "1 month"
        self._display_samples: list[UsageHistorySample] = []
        self._events: list[UsageHistoryEvent] = []

        self.setWindowTitle("Usage History · CodexBridge Console")
        self.setMinimumSize(900, 580)
        self._build_ui()
        self._restore_geometry()
        self._apply_preset("1 month", refresh=False)
        self.refresh()

    @property
    def selected_preset(self) -> str:
        return self._selected_preset

    def _build_ui(self) -> None:
        root = QWidget(self)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(20, 16, 20, 18)
        layout.setSpacing(12)

        heading = QHBoxLayout()
        title = QLabel("Usage History")
        title_font = title.font()
        title_font.setPointSize(title_font.pointSize() + 5)
        title_font.setWeight(QFont.Weight.DemiBold)
        title.setFont(title_font)
        heading.addWidget(title)
        heading.addStretch(1)
        self.live_label = QLabel("● Live")
        self.live_label.setObjectName("usageHistoryLive")
        heading.addWidget(self.live_label)
        self.updated_label = QLabel("Updated —")
        self.updated_label.setObjectName("usageHistoryUpdated")
        heading.addWidget(self.updated_label)
        layout.addLayout(heading)

        preset_row = QHBoxLayout()
        preset_row.setSpacing(0)
        self.preset_group = QButtonGroup(self)
        self.preset_group.setExclusive(True)
        self.preset_buttons: dict[str, QToolButton] = {}
        for index, label in enumerate(("7 days", "1 month", "1 year", "Custom")):
            button = QToolButton(self)
            button.setText(label)
            button.setCheckable(True)
            button.setObjectName(self.preset_object_names[label])
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            button.setMinimumHeight(30)
            button.setStyleSheet(
                "QToolButton { border: 1px solid palette(mid); padding: 4px 12px; }"
                "QToolButton:checked { background: palette(highlight); "
                "color: palette(highlighted-text); }"
                "QToolButton:hover { border-color: palette(highlight); }"
            )
            if index == 0:
                button.setStyleSheet(
                    button.styleSheet() + "QToolButton { border-radius: 5px 0 0 5px; }"
                )
            elif index == 3:
                button.setStyleSheet(
                    button.styleSheet() + "QToolButton { border-radius: 0 5px 5px 0; }"
                )
            else:
                button.setStyleSheet(button.styleSheet() + "QToolButton { border-left: 0; }")
            self.preset_group.addButton(button)
            self.preset_buttons[label] = button
            button.clicked.connect(
                lambda checked=False, selected=label: (
                    self._apply_preset(selected) if checked else None
                )
            )
            preset_row.addWidget(button)
        preset_row.addStretch(1)
        layout.addLayout(preset_row)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        controls.addWidget(QLabel("Start"))
        self.start_edit = self._new_datetime_edit("usageHistoryStart")
        controls.addWidget(self.start_edit)
        controls.addWidget(QLabel("End"))
        self.end_edit = self._new_datetime_edit("usageHistoryEnd")
        controls.addWidget(self.end_edit)
        controls.addSpacing(8)
        controls.addWidget(QLabel("Axis"))
        self.axis_mode_combo = QComboBox(self)
        self.axis_mode_combo.setObjectName("usageHistoryAxisMode")
        self.axis_mode_combo.addItems(("Auto", "Date", "Time"))
        controls.addWidget(self.axis_mode_combo)
        controls.addStretch(1)
        self.refresh_button = QPushButton("Refresh", self)
        self.refresh_button.setObjectName("usageHistoryRefresh")
        self.export_button = QPushButton("Export CSV", self)
        self.export_button.setObjectName("usageHistoryExport")
        controls.addWidget(self.refresh_button)
        controls.addWidget(self.export_button)
        layout.addLayout(controls)

        self.range_error_label = QLabel()
        self.range_error_label.setObjectName("usageHistoryRangeError")
        self.range_error_label.setVisible(False)
        layout.addWidget(self.range_error_label)

        self.start_edit.dateTimeChanged.connect(self._on_range_edited)
        self.end_edit.dateTimeChanged.connect(self._on_range_edited)
        self.axis_mode_combo.currentTextChanged.connect(self._update_axis_format)
        self.refresh_button.clicked.connect(lambda: self.refresh())
        self.export_button.clicked.connect(self._export_csv)

        summary = QHBoxLayout()
        summary.setSpacing(10)
        self.summary_labels: dict[str, QLabel] = {}
        for key in ("5h remaining", "Weekly remaining", "Reset candidates"):
            card = QFrame(self)
            card.setObjectName("usageHistoryMetricCard")
            card.setStyleSheet(
                "QFrame#usageHistoryMetricCard { background: palette(alternate-base); "
                "border: 1px solid palette(mid); border-radius: 6px; }"
                "QFrame#usageHistoryMetricCard QLabel { color: palette(text); }"
            )
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(12, 8, 12, 8)
            card_layout.setSpacing(3)
            caption = QLabel(key, card)
            caption.setObjectName("usageHistoryMetricCaption")
            value = QLabel("—", card)
            value.setObjectName("usageHistoryMetricValue")
            value_font = value.font()
            value_font.setPointSize(value_font.pointSize() + 3)
            value_font.setWeight(QFont.Weight.DemiBold)
            value.setFont(value_font)
            card_layout.addWidget(caption)
            card_layout.addWidget(value)
            self.summary_labels[key] = value
            summary.addWidget(card, 1)
        layout.addLayout(summary)

        self.chart, self.time_axis, self.percent_axis = self._create_chart()
        self.five_hour_series = QLineSeries(self.chart)
        self.five_hour_series.setName("5h remaining")
        self.weekly_series = QLineSeries(self.chart)
        self.weekly_series.setName("Weekly remaining")
        self.reset_series = QScatterSeries(self.chart)
        self.reset_series.setName("Reset candidate")
        self.reset_series.setMarkerSize(12)
        self.chart.addSeries(self.five_hour_series)
        self.chart.addSeries(self.weekly_series)
        self.chart.addSeries(self.reset_series)
        self.five_hour_series.attachAxis(self.time_axis)
        self.five_hour_series.attachAxis(self.percent_axis)
        self.weekly_series.attachAxis(self.time_axis)
        self.weekly_series.attachAxis(self.percent_axis)
        self.reset_series.attachAxis(self.time_axis)
        self.reset_series.attachAxis(self.percent_axis)
        self._style_series()
        self._connect_chart_interaction()

        self.chart_view = QChartView(self.chart, self)
        self.chart_view.setObjectName("usageHistoryChart")
        self.chart_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.chart_view.setMinimumHeight(260)
        self.chart_stack = QStackedWidget(self)
        self.chart_stack.setObjectName("usageHistoryChartState")
        self.chart_content = QWidget(self.chart_stack)
        chart_content_layout = QVBoxLayout(self.chart_content)
        chart_content_layout.setContentsMargins(0, 0, 0, 0)
        chart_content_layout.setSpacing(6)
        self.empty_state_label = self._state_label("No usage data for the selected period.")
        self.empty_state_label.setVisible(False)
        chart_content_layout.addWidget(self.empty_state_label)
        chart_content_layout.addWidget(self.chart_view, 1)
        self.chart_stack.addWidget(self.chart_content)
        self.error_state_label = self._state_label("Usage history unavailable.")
        self.chart_stack.addWidget(self.error_state_label)
        layout.addWidget(self.chart_stack, 1)

        history_heading = QHBoxLayout()
        history_title = QLabel("Reset history")
        history_font = history_title.font()
        history_font.setWeight(QFont.Weight.DemiBold)
        history_title.setFont(history_font)
        history_heading.addWidget(history_title)
        history_heading.addStretch(1)
        self.history_count_label = QLabel("—")
        history_heading.addWidget(self.history_count_label)
        layout.addLayout(history_heading)
        self.reset_history_list = QListWidget(self)
        self.reset_history_list.setObjectName("usageHistoryResetList")
        self.reset_history_list.setMinimumHeight(70)
        self.reset_history_list.setMaximumHeight(132)
        self.reset_history_list.setStyleSheet(
            "QListWidget { background: palette(base); border: 1px solid palette(mid); "
            "border-radius: 4px; padding: 3px; color: palette(text); }"
        )
        layout.addWidget(self.reset_history_list)

        self.setCentralWidget(root)
        self._update_axis_format()

    def _new_datetime_edit(self, name: str) -> QDateTimeEdit:
        editor = QDateTimeEdit(self)
        editor.setObjectName(name)
        editor.setCalendarPopup(True)
        editor.setDisplayFormat("yyyy/MM/dd HH:mm")
        editor.setMinimumWidth(154)
        return editor

    @staticmethod
    def _state_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setObjectName("usageHistoryState")
        font = label.font()
        font.setPointSize(font.pointSize() + 2)
        label.setFont(font)
        return label

    def _create_chart(self) -> tuple[QChart, QDateTimeAxis, QValueAxis]:
        palette = self.palette()
        chart = QChart()
        chart.setBackgroundBrush(palette.window())
        chart.setBackgroundPen(QPen(Qt.PenStyle.NoPen))
        chart.setPlotAreaBackgroundVisible(True)
        chart.setPlotAreaBackgroundBrush(palette.alternateBase())
        chart.setMargins(QMargins(12, 8, 12, 4))
        chart.setAnimationOptions(QChart.AnimationOption.NoAnimation)
        chart.legend().setVisible(True)
        chart.legend().setAlignment(Qt.AlignmentFlag.AlignBottom)
        chart.legend().setLabelColor(palette.color(QPalette.ColorRole.WindowText))
        chart.legend().setFont(self.font())

        time_axis = QDateTimeAxis()
        time_axis.setFormat("MM/dd")
        time_axis.setTickCount(6)
        percent_axis = QValueAxis()
        percent_axis.setRange(0, 100)
        percent_axis.setTickCount(6)
        percent_axis.setLabelFormat("%d%%")
        for axis in (time_axis, percent_axis):
            axis.setLabelsColor(palette.color(QPalette.ColorRole.WindowText))
            axis.setLinePen(QPen(palette.color(QPalette.ColorRole.Mid)))
            grid_color = palette.color(QPalette.ColorRole.Mid)
            grid_color.setAlphaF(0.24)
            grid_pen = QPen(grid_color)
            grid_pen.setStyle(Qt.PenStyle.DotLine)
            axis.setGridLinePen(grid_pen)
        chart.addAxis(time_axis, Qt.AlignmentFlag.AlignBottom)
        chart.addAxis(percent_axis, Qt.AlignmentFlag.AlignLeft)
        return chart, time_axis, percent_axis

    def _style_series(self) -> None:
        palette = self.palette()
        for series, role in (
            (self.five_hour_series, QPalette.ColorRole.Link),
            (self.weekly_series, QPalette.ColorRole.Highlight),
        ):
            pen = QPen(palette.color(role))
            pen.setWidthF(2.4)
            series.setPen(pen)
        marker_color = palette.color(QPalette.ColorRole.Link)
        border = QPen(palette.color(QPalette.ColorRole.Window))
        border.setWidthF(1.5)
        self.reset_series.setBrush(marker_color)
        self.reset_series.setPen(border)

    def _connect_chart_interaction(self) -> None:
        self.five_hour_series.hovered.connect(self._on_sample_hover)
        self.weekly_series.hovered.connect(self._on_sample_hover)
        self.reset_series.hovered.connect(self._on_reset_hover)
        for series in (self.five_hour_series, self.weekly_series):
            for marker in self.chart.legend().markers(series):
                marker.clicked.connect(
                    partial(
                        self.toggle_series_visibility,
                        series,
                        marker,
                        QPen(marker.pen()),
                        QBrush(marker.brush()),
                        QBrush(marker.labelBrush()),
                    )
                )

    def toggle_series_visibility(
        self,
        series: QLineSeries,
        marker: QLegendMarker,
        original_pen: QPen,
        original_brush: QBrush,
        original_label_brush: QBrush,
        *_args: object,
    ) -> None:
        visible = not series.isVisible()
        series.setVisible(visible)
        marker.setVisible(True)
        pen = QPen(original_pen)
        brush = QBrush(original_brush)
        label_brush = QBrush(original_label_brush)
        if not visible:
            pen_color = pen.color()
            pen_color.setAlphaF(0.45)
            brush_color = brush.color()
            brush_color.setAlphaF(0.45)
            label_color = label_brush.color()
            label_color.setAlphaF(0.45)
            pen.setColor(pen_color)
            brush.setColor(brush_color)
            label_brush.setColor(label_color)
        marker.setPen(pen)
        marker.setBrush(brush)
        marker.setLabelBrush(label_brush)

    def _on_sample_hover(self, point: object, entered: bool) -> None:
        if not entered or not self._display_samples:
            QToolTip.hideText()
            return
        point_x = getattr(point, "x", lambda: inf)()
        sample = min(
            self._display_samples,
            key=lambda item: abs(item.captured_at_epoch * 1_000 - point_x),
        )
        QToolTip.showText(QCursor.pos(), format_sample_tooltip(sample), self.chart_view)

    def _on_reset_hover(self, point: object, entered: bool) -> None:
        if not entered or not self._events:
            QToolTip.hideText()
            return
        point_x = getattr(point, "x", lambda: inf)()
        event = min(
            self._events,
            key=lambda item: abs(item.occurred_at_epoch * 1_000 - point_x),
        )
        QToolTip.showText(QCursor.pos(), format_reset_tooltip(event), self.chart_view)

    def _set_range(self, start_epoch: float, end_epoch: float) -> None:
        for editor, value in ((self.start_edit, start_epoch), (self.end_edit, end_epoch)):
            editor.blockSignals(True)
            editor.setDateTime(QDateTime.fromSecsSinceEpoch(int(value)))
            editor.blockSignals(False)
        self._update_axis_format()

    def _apply_preset(self, label: str, *, refresh: bool = True) -> None:
        self._selected_preset = label
        button = self.preset_buttons.get(label)
        if button is not None:
            button.setChecked(True)
        if label == "Custom":
            self._update_axis_format()
            if refresh:
                self.refresh()
            return
        days = dict(_PRESETS).get(label)
        if days is None:
            return
        end = self._now()
        self._set_range(end - days * _DAY, end)
        if refresh:
            self.refresh()

    def _on_range_edited(self, _value: QDateTime) -> None:
        self._selected_preset = "Custom"
        self.preset_buttons["Custom"].setChecked(True)
        self._update_axis_format()

    def _update_axis_format(self, _mode: str | None = None) -> None:
        if not hasattr(self, "time_axis"):
            return
        mode = self.axis_mode_combo.currentText() if hasattr(self, "axis_mode_combo") else "Auto"
        duration = (
            abs(
                self.end_edit.dateTime().toSecsSinceEpoch()
                - self.start_edit.dateTime().toSecsSinceEpoch()
            )
            if hasattr(self, "end_edit")
            else 30 * _DAY
        )
        if mode == "Time":
            axis_format = "HH:mm"
        elif mode == "Date":
            axis_format = "yyyy/MM" if duration > 90 * _DAY else "MM/dd"
        elif duration <= _DAY:
            axis_format = "HH:mm"
        elif duration <= 7 * _DAY:
            axis_format = "MM/dd HH:mm"
        elif duration <= 90 * _DAY:
            axis_format = "MM/dd"
        else:
            axis_format = "yyyy/MM"
        self.time_axis.setFormat(axis_format)

    def refresh(self, *, rolling: bool = False) -> bool:
        if rolling and self._selected_preset != "Custom":
            self._apply_preset(self._selected_preset, refresh=False)
        start = self.start_edit.dateTime().toSecsSinceEpoch()
        end = self.end_edit.dateTime().toSecsSinceEpoch()
        if start > end:
            self.range_error_label.setText("Start must be earlier than or equal to End.")
            self.range_error_label.setVisible(True)
            return False
        self.range_error_label.setVisible(False)
        try:
            samples = get_usage_samples_for_display(
                start,
                end,
                max_points=_TARGET_POINTS,
                database_path=self._database_path,
            )
            events = get_confirmed_usage_events(start, end, database_path=self._database_path)
            five_hour, weekly = get_latest_usage_remaining(
                start, end, database_path=self._database_path
            )
        except (OSError, sqlite3.Error):
            self._display_samples = []
            self._events = []
            self._clear_chart()
            self._set_summary(None, None, 0)
            self.reset_history_list.clear()
            self.reset_history_list.addItem("Usage history unavailable.")
            self.history_count_label.setText("—")
            self.chart_stack.setCurrentWidget(self.error_state_label)
            return False

        self._display_samples = samples
        self._events = events
        self._render_chart(samples, events, start, end)
        self._set_summary(five_hour, weekly, len(events))
        self._render_reset_history(events)
        self.empty_state_label.setVisible(not samples)
        self.chart_stack.setCurrentWidget(self.chart_content)
        self.updated_label.setText(
            f"Updated {datetime.fromtimestamp(self._now()).astimezone().strftime('%H:%M')}"
        )
        return True

    def _clear_chart(self) -> None:
        self.five_hour_series.clear()
        self.weekly_series.clear()
        self.reset_series.clear()
        self.time_axis.setRange(
            QDateTime.fromSecsSinceEpoch(0),
            QDateTime.fromSecsSinceEpoch(1),
        )

    def _render_chart(
        self,
        samples: list[UsageHistorySample],
        events: list[UsageHistoryEvent],
        start: int,
        end: int,
    ) -> None:
        self.five_hour_series.clear()
        self.weekly_series.clear()
        self.reset_series.clear()
        for sample in samples:
            timestamp_ms = round(sample.captured_at_epoch * 1_000)
            if sample.five_hour_remaining is not None:
                self.five_hour_series.append(timestamp_ms, sample.five_hour_remaining)
            if sample.weekly_remaining is not None:
                self.weekly_series.append(timestamp_ms, sample.weekly_remaining)
        for event in events:
            self.reset_series.append(
                round(event.occurred_at_epoch * 1_000), event.current_weekly_remaining
            )
        if start == end:
            start -= 60
            end += 60
        self.time_axis.setRange(
            QDateTime.fromSecsSinceEpoch(start),
            QDateTime.fromSecsSinceEpoch(end),
        )
        self._update_axis_format()

    def _set_summary(
        self,
        five_hour: int | None,
        weekly: int | None,
        reset_count: int,
    ) -> None:
        self.summary_labels["5h remaining"].setText("—" if five_hour is None else f"{five_hour}%")
        self.summary_labels["Weekly remaining"].setText("—" if weekly is None else f"{weekly}%")
        self.summary_labels["Reset candidates"].setText(str(reset_count))

    def _render_reset_history(self, events: list[UsageHistoryEvent]) -> None:
        self.reset_history_list.clear()
        self.history_count_label.setText(str(len(events)))
        if not events:
            self.reset_history_list.addItem("No reset candidates in this period.")
            return
        for event in events:
            timestamp = datetime.fromtimestamp(event.occurred_at_epoch).strftime("%Y/%m/%d %H:%M")
            self.reset_history_list.addItem(
                f"{timestamp}    Weekly    {event.previous_weekly_remaining}% → "
                f"{event.current_weekly_remaining}%"
            )

    def _export_csv(self) -> None:
        path_text, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Export Usage History",
            "usage-history.csv",
            "CSV files (*.csv)",
        )
        if not path_text:
            return
        start = self.start_edit.dateTime().toSecsSinceEpoch()
        end = self.end_edit.dateTime().toSecsSinceEpoch()
        if start > end:
            self.range_error_label.setText("Start must be earlier than or equal to End.")
            self.range_error_label.setVisible(True)
            return
        try:
            export_usage_history_csv(
                path_text,
                start,
                end,
                database_path=self._database_path,
            )
        except (OSError, sqlite3.Error) as exc:
            QMessageBox.warning(self, "Export failed", f"Could not export usage history: {exc}")

    def _restore_geometry(self) -> None:
        self.resize(1_040, 700)
        stored = self._settings.value(_GEOMETRY_KEY)
        if stored:
            try:
                geometry = stored if isinstance(stored, QByteArray) else QByteArray(stored)
                if geometry and self.restoreGeometry(geometry):
                    self._keep_on_screen()
                    return
            except (TypeError, RuntimeError):
                pass
        self._keep_on_screen()

    def _keep_on_screen(self) -> None:
        application = QApplication.instance()
        screens = application.screens() if isinstance(application, QApplication) else []
        if not screens:
            return
        frame = self.frameGeometry()
        if any(frame.intersects(screen.availableGeometry()) for screen in screens):
            return
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        self.move(
            available.left() + max(0, (available.width() - self.width()) // 2),
            available.top() + max(0, (available.height() - self.height()) // 2),
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        self._settings.setValue(_GEOMETRY_KEY, self.saveGeometry())
        self._settings.sync()
        super().closeEvent(event)
