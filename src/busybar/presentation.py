"""Small compatibility layer for complete, expiring display frames.

The firmware still has one canvas owner. Selective cleanup preserves common
elements at layout transitions; it does not replace the scheduled full redraws
that reclaim an evicted canvas and renew element timeouts.
"""

from busybar.client import DrawResult


def modern_display(client) -> bool:
    """Require positive capability evidence; older client adapters remain usable."""
    refresh = getattr(client, "refresh_capabilities", None)
    if refresh is not None:
        refresh()
    return getattr(client, "supports_display_v2", False) is True


def prepare_frame(client, application_name: str, elements: list[dict],
                  priority: int, state: dict | None, *, modern: bool) -> list[dict]:
    """Clean up a layout transition, then return the complete drawing payload.

Never mutate the caller's elements or commit state before the draw succeeds.
Expired/preempted IDs can reject a selective delete; one app-scoped full clear
is sufficient recovery. Its failure leaves other owners untouched and the
ordinary draw result/element TTLs govern recovery, with no retry loop.
"""
    frame = [{**element, "z_index": index * 10} if modern else dict(element)
             for index, element in enumerate(elements)]
    if state is None or state.get("last_shape") is None:
        return frame

    old_shape = state["last_shape"]
    new_shape = frozenset(element["id"] for element in frame)
    old_priority = state.get("last_priority")
    old_types = state.get("last_types", {})
    type_changed = any(element["id"] in old_types
                       and old_types[element["id"]] != element["type"]
                       for element in frame)
    priority_lowered = old_priority is not None and priority < old_priority

    if priority_lowered or type_changed:
        client.clear(application_name)
    elif old_shape != new_shape:
        # Partial cleanup is only useful while some common content survives.
        # An unknown prior priority uses the proven whole-app transition path.
        selective = modern and old_priority == priority and bool(old_shape & new_shape)
        removed = sorted(old_shape - new_shape)
        if not selective:
            client.clear(application_name)
        elif removed and client.remove_elements(application_name, removed) is not True:
            client.clear(application_name)
    return frame


def commit_frame(state: dict | None, elements: list[dict], priority: int,
                 result: DrawResult) -> None:
    """Remember only a positively accepted frame for the next transition."""
    if state is not None and result == DrawResult.DRAWN:
        state["last_shape"] = frozenset(element["id"] for element in elements)
        state["last_types"] = {element["id"]: element["type"] for element in elements}
        state["last_priority"] = priority


_ICON_ROWS = {
    "fail": ("X.....X", ".X...X.", "..X.X..", "...X...", "..X.X..", ".X...X.", "X.....X"),
    "stuck": ("XXXXXXX", ".X...X.", "..X.X..", "...X...", "..X.X..", ".X...X.", "XXXXXXX"),
    "green": (".......", "......X", ".....X.", "X...X..", ".X.X...", "..X....", "......."),
}


def ci_bitmap_accent(elements: list[dict], kind: str, timeout_s: int) -> list[dict]:
    """Add an inline 7x7 status symbol beside the fixed CI header.

These are fixed local assets, not a general image parser. The text remains the
meaningful fallback if the client must use an older/cloud endpoint mid-draw.
"""
    rows = _ICON_ROWS.get(kind)
    if rows is None:
        return elements
    colors = {"fail": "#FFFFFF", "stuck": "#FFCB6B", "green": "#6FFFCF"}
    data = "! XPM2\n7 7 2 1\n. c None\nX c " + colors[kind] + "\n" + "\n".join(rows) + "\n"
    frame = [{**element, "x": 12, "width": 58} if element["id"] == "ci_header" else dict(element)
             for element in elements]
    frame.append({"id": "ci_status_icon", "type": "xpmbitmap", "data": data,
                  "x": 2, "y": 0, "timeout": timeout_s})
    return frame
