from PySide6.QtWidgets import QApplication

from codex_bridge.console.activity_pane import ActivityPane
from codex_bridge.console.thread_list_pane import ThreadListPane


def test_saved_turn_is_visible_in_selected_status_and_list_without_running_group():
    app = QApplication.instance() or QApplication([])
    assert app is not None
    outcome = {
        "turn_id": "last",
        "status": "interrupted",
        "reason": "unknown",
        "evidence": "persisted",
        "needs_attention": True,
    }
    pane = ActivityPane()
    pane.set_snapshot({"state": "not_loaded", "last_turn": outcome})
    assert "interrupted" in pane.last_turn_label.text()
    assert "attention required" in pane.last_turn_label.text()
    listing = ThreadListPane()
    listing.set_threads([{"id": "t", "name": "Title", "cwd": "D:/project"}])
    listing.set_last_turn("t", outcome)
    group = listing.list_widget.topLevelItem(0)
    assert group.text(0) != "Currently running"
    assert "Interrupted" in group.child(0).text(0)
    assert "persisted" in group.child(0).toolTip(0)
    pane.set_loading_state("Loading another thread")
    assert pane.last_turn_label.text() == ""
