from copy import deepcopy
from unittest.mock import Mock

import pytest

from busybar.client import DrawResult
from busybar.presentation import ci_bitmap_accent, commit_frame, modern_display, prepare_frame


def text_element(name):
    return {"id": name, "type": "text", "text": name, "x": 0, "y": 0, "timeout": 10}


def previous(*names, priority=21):
    state = {}
    commit_frame(state, [text_element(n) for n in names], priority, DrawResult.DRAWN)
    return state


class Canvas:
    """Relevant firmware ownership, priority and missing-ID behavior."""
    supports_display_v2 = True

    def __init__(self, owner="app", priority=21, ids=("common", "old")):
        self.owner, self.priority = owner, priority
        self.elements = {n: text_element(n) for n in ids}
        self.removals, self.clears = [], []

    def clear(self, app):
        self.clears.append(app)
        if self.owner != app:
            return False
        self.elements.clear()
        self.owner = None
        return True

    def remove_elements(self, app, ids):
        self.removals.append((app, ids))
        if app != self.owner or not set(ids) <= self.elements.keys():
            return False
        for name in ids:
            del self.elements[name]
        return True

    def draw(self, app, elements, priority):
        if self.owner and (priority < self.priority or (priority == self.priority and app != self.owner)):
            return DrawResult.REJECTED
        if self.owner != app:
            self.elements.clear()
        self.elements.update({e["id"]: e for e in elements})
        self.owner, self.priority = app, priority
        return DrawResult.DRAWN


def test_selective_transition_preserves_common_content_until_full_upsert():
    canvas = Canvas()
    state = previous("common", "old")
    original = [text_element("common"), text_element("new")]
    frame = prepare_frame(canvas, "app", original, 21, state, modern=True)
    assert canvas.removals == [("app", ["old"])]
    assert canvas.clears == []
    assert set(canvas.elements) == {"common"}
    assert canvas.draw("app", frame, 21) == DrawResult.DRAWN
    assert set(canvas.elements) == {"common", "new"}
    assert [e["z_index"] for e in frame] == [0, 10]
    assert all(e["timeout"] == 10 for e in frame)
    assert all("z_index" not in e for e in original)


def test_adding_only_does_not_clear_modern_canvas():
    c = Canvas(ids=("common",))
    prepare_frame(c, "app", [text_element("common"), text_element("new")],
                  21, previous("common"), modern=True)
    assert not c.removals and not c.clears


@pytest.mark.parametrize("modern", [False, True])
def test_priority_reduction_clears_even_when_ids_are_unchanged(modern):
    c = Canvas(priority=65, ids=("common",))
    frame = prepare_frame(c, "app", [text_element("common")], 20,
                          previous("common", priority=65), modern=modern)
    assert c.clears == ["app"]
    assert c.draw("app", frame, 20) == DrawResult.DRAWN


def test_expired_ids_fall_back_to_one_scoped_clear_then_recover():
    c = Canvas(ids=("common",))
    frame = prepare_frame(c, "app", [text_element("common"), text_element("new")],
                          21, previous("common", "old"), modern=True)
    assert c.removals == [("app", ["old"])]
    assert c.clears == ["app"]
    assert c.draw("app", frame, 21) == DrawResult.DRAWN


def test_preemption_never_clears_other_owner_or_commits_rejected_transition():
    c = Canvas(owner="work-session", priority=90, ids=("busy",))
    state = previous("common", "old")
    before = deepcopy(state)
    frame = prepare_frame(c, "app", [text_element("common"), text_element("new")],
                          21, state, modern=True)
    result = c.draw("app", frame, 21)
    commit_frame(state, frame, 21, result)
    assert result == DrawResult.REJECTED
    assert state == before
    assert c.owner == "work-session" and set(c.elements) == {"busy"}


def test_legacy_layout_change_and_modern_type_change_clear():
    c = Canvas()
    prepare_frame(c, "app", [text_element("common"), text_element("new")],
                  21, previous("common", "old"), modern=False)
    assert c.clears == ["app"] and not c.removals
    c = Canvas(ids=("common",))
    prepare_frame(c, "app", [{"id": "common", "type": "rectangle", "timeout": 10}],
                  21, previous("common"), modern=True)
    assert c.clears == ["app"] and not c.removals


@pytest.mark.parametrize("result", [DrawResult.ERROR, DrawResult.REJECTED, DrawResult.UNREACHABLE])
def test_failed_draw_never_advances_state(result):
    state = previous("old")
    before = deepcopy(state)
    commit_frame(state, [text_element("new")], 65, result)
    assert state == before


def test_capability_needs_positive_evidence():
    assert modern_display(object()) is False
    c = Mock()
    assert modern_display(c) is False
    c.supports_display_v2 = True
    assert modern_display(c) is True


@pytest.mark.parametrize("kind", ["fail", "stuck", "green"])
def test_bitmap_has_transparent_margin_and_does_not_obscure_fallback_text(kind):
    original = [text_element("ci")]
    frame = ci_bitmap_accent(original, kind, 10)
    icon = frame[-1]
    lines = icon["data"].splitlines()
    assert lines[:3] == ["! XPM2", "7 7 2 1", ". c None"]
    assert len(lines[4:]) == 7 and all(len(row) == 7 for row in lines[4:])
    assert icon["x"] + 7 <= frame[0]["x"]
    assert frame[0]["x"] + frame[0]["width"] == 72
    assert icon["timeout"] == 10
    assert original == [text_element("ci")]
