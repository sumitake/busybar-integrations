import pytest
from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integrations"))
from calendar_countdown.logic import (
    CalEvent, select_next_event, select_active_event, ascii_safe,
    build_elements, _track_fill_width, _state_for, _title_fits,
    _format_countdown, _text_width_px,
    STATE_NORMAL, STATE_NOTICE, STATE_WARNING, STATE_IN_PROGRESS, STATES,
    BG_GRADIENT, TITLE_COLOR, TRACK_COLOR, TRACK_FILL_GRADIENT,
    TRACK_FILL_IN_PROGRESS, TIME_TEXT_COLOR,
    ENDS_TEXT_COLOR, ENDS_TEXT, DIVIDER_COLOR, DIGIT_COLOR,
    PANEL_WIDTH, PANEL_HEIGHT, CD_TEXT_X, CD_TEXT_MAX_WIDTH, GLYPH_ADVANCE_PX,
    _minutes_left, select_priority, select_led, resolve_led_value, should_chirp, commit_chirped,
    next_sleep_seconds, IMMINENT_LED_COLOR, CHIRP_STOCK_PATH, _chirp_key,
    check_threshold_ordering,
    is_just_started, START_ANIM_ID, CAL_ICON_ID, ICON_EVENT, ICON_REMINDER,
    SCROLL_RATE, SCROLL_DELAY_MS,
)
from busybar.display import (
    PRIORITY_AMBIENT, PRIORITY_AMBIENT_RAISED, PRIORITY_AMBIENT_URGENT,
)

TZ = timezone.utc
NOW = datetime(2026, 8, 3, 13, 37, tzinfo=TZ)
CFG = {"progress_window_minutes": 60, "notice_minutes": 15, "warn_minutes": 5}

# Firmware ink-offset model, reconstructed here (test-only) so the row-5
# buffer rule can be checked structurally from build_elements()'s raw
# geometry, not just eyeballed against the constants. Text ink starts 2px
# below the element's `y` and is as tall as the font's native pixel height;
# rectangles have no offset. Mirrors the table in logic.py's geometry
# comment and the v1.4 spec doc.
TEXT_INK_OFFSET = 2
FONT_PIXEL_HEIGHT = {"small": 5, "large": 9, "bold": 7}


def ev(offset_min: int, title: str = "Standup", dur_min: int = 30, all_day: bool = False) -> CalEvent:
    start = NOW + timedelta(minutes=offset_min)
    return CalEvent(title=title, start=start, end=start + timedelta(minutes=dur_min), all_day=all_day)


def ink_rows(el: dict) -> range:
    """Rows an element's ink actually occupies, per the offset model above."""
    if el["type"] == "text":
        top = el["y"] + TEXT_INK_OFFSET
        height = FONT_PIXEL_HEIGHT[el["font"]]
    else:  # rectangle: no offset
        top = el["y"]
        height = el["height"]
    return range(top, top + height)


def test_selects_earliest_upcoming():
    assert select_next_event([ev(120), ev(23), ev(400)], NOW, 12, False).start == ev(23).start

def test_ignores_past_and_beyond_lookahead():
    assert select_next_event([ev(-10), ev(13 * 60)], NOW, 12, False) is None

def test_all_day_skipped_unless_enabled():
    events = [ev(60, all_day=True)]
    assert select_next_event(events, NOW, 12, False) is None
    assert select_next_event(events, NOW, 12, True) is not None

def test_active_event():
    assert select_active_event([ev(-5, dur_min=30)], NOW) is not None
    assert select_active_event([ev(5)], NOW) is None

def test_ascii_safe():
    assert ascii_safe("Café · Sync") == "Caf Sync"
    assert ascii_safe("日本語") == "event"


# --- state selection ----------------------------------------------------------

def test_state_normal_above_notice():
    assert _state_for(20, notice_minutes=15, warn_minutes=5, in_progress=False) == STATE_NORMAL

def test_state_notice_at_and_below_notice_threshold():
    assert _state_for(15, notice_minutes=15, warn_minutes=5, in_progress=False) == STATE_NOTICE
    assert _state_for(10, notice_minutes=15, warn_minutes=5, in_progress=False) == STATE_NOTICE

def test_state_warning_at_and_below_warn_threshold():
    assert _state_for(5, notice_minutes=15, warn_minutes=5, in_progress=False) == STATE_WARNING
    assert _state_for(0, notice_minutes=15, warn_minutes=5, in_progress=False) == STATE_WARNING
    assert _state_for(-3, notice_minutes=15, warn_minutes=5, in_progress=False) == STATE_WARNING

def test_state_in_progress_overrides_thresholds():
    # Even minutes_left values that would read "normal" by threshold alone
    # must resolve to in_progress once in_progress=True.
    assert _state_for(999, notice_minutes=15, warn_minutes=5, in_progress=True) == STATE_IN_PROGRESS


# --- palette completeness ------------------------------------------------------

def test_every_state_has_a_palette_entry_in_every_state_keyed_table():
    tables = [BG_GRADIENT, TITLE_COLOR, TRACK_FILL_GRADIENT, DIVIDER_COLOR, DIGIT_COLOR]
    for table in tables:
        # TRACK_FILL_GRADIENT intentionally omits in_progress (solid fill instead).
        expected = set(STATES) - {STATE_IN_PROGRESS} if table is TRACK_FILL_GRADIENT else set(STATES)
        assert set(table) == expected


# --- track-fill drain math ------------------------------------------------------

def test_track_fill_full_when_at_or_beyond_window():
    assert _track_fill_width(60, 60) == PANEL_WIDTH
    assert _track_fill_width(90, 60) == PANEL_WIDTH

def test_track_fill_scales_within_window():
    assert _track_fill_width(30, 60) == 36   # half window -> half width (72 * 0.5)
    assert _track_fill_width(15, 60) == 18

def test_track_fill_clamped_min_one():
    assert _track_fill_width(0, 60) == 1
    assert _track_fill_width(-5, 60) == 1   # defensive: never negative width

def test_track_fill_window_disabled_defensively_full():
    assert _track_fill_width(5, 0) == PANEL_WIDTH


# --- title fit / scroll decision --------------------------------------------

def test_title_fits_short_string():
    assert _title_fits("Sync", 68) is True

def test_title_does_not_fit_long_string():
    assert _title_fits("A Very Long Meeting Title That Does Not Fit At All", 68) is False


# --- countdown text formatting ------------------------------------------------
#
# _format_countdown feeds the cd_text element (a plain `text` element,
# `large` font as of v1.4, replacing the native `countdown` element -- see
# logic.py's CD_TEXT_X comment for why). Floored to whole minutes; falls
# back to an hour-only form whenever the full "<H>h<MM>m" form's *measured*
# width would overflow the space available past cd_text's x -- a per-string
# check, not a fixed hour cutoff (see _format_countdown's docstring: this
# font's '1' glyph is measurably narrower than every other digit, so a
# fixed "hours >= 10" threshold turned out unsafe for single-digit hours on
# real hardware -- re-verification for v1.4 found several single-digit-hour,
# 5-glyph strings that measure wider than the budget).

def test_countdown_format_under_an_hour():
    assert _format_countdown(0) == "0m"
    assert _format_countdown(1) == "1m"
    assert _format_countdown(54.7) == "54m"   # floored, not rounded

def test_countdown_format_boundary_59_60_minutes():
    assert _format_countdown(59) == "59m"
    assert _format_countdown(59.9) == "59m"   # still floors to 59, not 60
    assert _format_countdown(60) == "1h00m"   # exactly at the width budget; fits

def test_countdown_format_full_form_when_it_fits():
    assert _format_countdown(65) == "1h05m"          # hours digit '1' is narrow enough
    assert _format_countdown(119) == "1h59m"         # fits even with wide minute digits
    assert _format_countdown(9 * 60 + 11) == "9h11m"  # narrow '1's in minutes rescue a wide hours digit

def test_countdown_format_falls_back_to_hour_only_when_full_form_overflows():
    # Single-digit-hour, 5-glyph strings -- the same nominal "shape" a fixed
    # hours>=10 cutoff would have assumed always fits -- but none of these
    # digits happen to include the narrower '1' glyph, so the measured
    # full-form width (34px) exceeds the 33px budget. This is the concrete
    # finding that replaced the fixed cutoff with a per-string width check.
    assert _format_countdown(125) == "2h"              # would-be "2h05m"
    assert _format_countdown(5 * 60 + 55) == "5h"       # would-be "5h55m"
    assert _format_countdown(9 * 60 + 59) == "9h"       # would-be "9h59m"

def test_countdown_format_two_digit_hours_always_hour_only():
    assert _format_countdown(10 * 60) == "10h"
    assert _format_countdown(10 * 60 + 59) == "10h"
    assert _format_countdown(12 * 60) == "12h"   # default lookahead_hours

def test_countdown_format_clamps_negative_to_zero():
    assert _format_countdown(-5) == "0m"

def test_countdown_format_never_overflows_available_width():
    # Sweep a wide range of inputs (including the confirmed-overflow cases
    # above, and the largest 2-digit-hour case) and verify every produced
    # string's *measured* width (not an approximation) fits CD_TEXT_MAX_WIDTH.
    samples_minutes = [0, 1, 30, 59, 60, 65, 119, 125, 5 * 60 + 55,
                       9 * 60 + 11, 9 * 60 + 59, 10 * 60, 12 * 60, 23 * 60 + 59]
    for minutes in samples_minutes:
        text = _format_countdown(minutes)
        assert _text_width_px(text) <= CD_TEXT_MAX_WIDTH, (minutes, text)

def test_countdown_full_form_overflow_is_a_real_per_string_measurement():
    # Documents *why* a per-string check replaced the fixed hour cutoff:
    # two full-form strings of the identical nominal shape (single-digit
    # hour, 5 glyphs) measure differently and land on opposite sides of the
    # budget, purely because of which digits happen to appear.
    assert _text_width_px("1h05m") <= CD_TEXT_MAX_WIDTH   # fits
    assert _text_width_px("2h05m") > CD_TEXT_MAX_WIDTH    # does not
    # The actual rule picks correctly in both cases:
    assert _format_countdown(65) == "1h05m"
    assert _format_countdown(125) == "2h"

def test_glyph_advance_table_covers_every_countdown_glyph():
    # Every character _format_countdown can ever emit (digits, 'h', 'm')
    # must have a table entry, or _text_width_px raises KeyError at draw
    # time instead of failing a test. (v1.5.1 extended the table further,
    # with "~" plus the letters ci_status's running-badge ETA label
    # feature needs -- see GLYPH_ADVANCE_PX's own comment -- so this is a
    # subset check now, not exact equality.)
    assert set("0123456789hm").issubset(set(GLYPH_ADVANCE_PX))


# --- build_elements: upcoming event ------------------------------------------

def test_build_elements_upcoming_shape():
    e = ev(23)
    els = build_elements(e, NOW, CFG, timeout_s=90, in_progress=False)
    by_id = {el["id"]: el for el in els}
    # v1.4 "airy": no card elements -- time_card and cd_card are gone.
    assert set(by_id) == {"bg", "title", "track", "track_fill", "track_tip", "time", "divider", "cd_text"}
    # Draw order is z-order (first = behind); set-membership alone wouldn't
    # catch a reorder that visually breaks layering.
    assert [el["id"] for el in els] == [
        "bg", "title", "track", "track_fill", "track_tip", "time", "divider", "cd_text",
    ]

    bg = by_id["bg"]
    assert bg["type"] == "rectangle" and bg["x"] == 0 and bg["y"] == 0
    assert bg["width"] == PANEL_WIDTH and bg["height"] == PANEL_HEIGHT
    assert bg["fill"] == "gradient_v" and bg["fill_colors"] == BG_GRADIENT[STATE_NORMAL]
    assert bg["border_width"] == 0

    title = by_id["title"]
    assert title["type"] == "text" and title["font"] == "small"
    assert title["text"] == "STANDUP" and title["color"] == TITLE_COLOR[STATE_NORMAL]
    assert title["x"] == 2 and title["y"] == -2 and title["width"] == 68

    track = by_id["track"]
    assert track["type"] == "rectangle" and track["fill"] == "solid"
    assert track["fill_colors"] == [TRACK_COLOR]
    assert track["x"] == 0 and track["y"] == 6 and track["width"] == PANEL_WIDTH and track["height"] == 1
    # RectangleElement's default 1px white border would swallow this 1px-tall
    # track entirely (the same gotcha found on the v1.1 progress bar) --
    # must be explicitly disabled.
    assert track["border_width"] == 0

    track_fill = by_id["track_fill"]
    assert track_fill["fill"] == "gradient_h"
    assert track_fill["fill_colors"] == TRACK_FILL_GRADIENT[STATE_NORMAL]
    assert track_fill["x"] == 0 and track_fill["y"] == 6 and track_fill["height"] == 1
    assert track_fill["border_width"] == 0

    time_el = by_id["time"]
    assert time_el["type"] == "text" and time_el["font"] == "large"
    assert time_el["color"] == TIME_TEXT_COLOR and time_el["x"] == 2 and time_el["y"] == 5
    assert time_el["text"] == f"{e.start.astimezone():%H:%M}"

    divider = by_id["divider"]
    assert divider["x"] == 34 and divider["y"] == 8 and divider["width"] == 2 and divider["height"] == 7
    assert divider["fill_colors"] == [DIVIDER_COLOR[STATE_NORMAL]]
    assert divider["border_width"] == 0

    cd = by_id["cd_text"]
    assert cd["type"] == "text" and cd["font"] == "large"
    assert cd["text"] == _format_countdown((e.start - NOW).total_seconds() / 60)
    assert cd["color"] == DIGIT_COLOR[STATE_NORMAL]
    assert cd["x"] == CD_TEXT_X == 39 and cd["y"] == 5
    assert "align" not in cd  # align was implicated in the v1.3 render mash; not used

def test_build_elements_state_palettes_by_urgency():
    notice_event = ev(10)   # within notice_minutes=15, outside warn_minutes=5
    els = build_elements(notice_event, NOW, CFG, timeout_s=90, in_progress=False)
    by_id = {el["id"]: el for el in els}
    assert by_id["bg"]["fill_colors"] == BG_GRADIENT[STATE_NOTICE]
    assert by_id["title"]["color"] == TITLE_COLOR[STATE_NOTICE]
    assert by_id["track_fill"]["fill_colors"] == TRACK_FILL_GRADIENT[STATE_NOTICE]
    assert by_id["divider"]["fill_colors"] == [DIVIDER_COLOR[STATE_NOTICE]]
    assert by_id["cd_text"]["color"] == DIGIT_COLOR[STATE_NOTICE]

    warning_event = ev(3)   # within warn_minutes=5
    els = build_elements(warning_event, NOW, CFG, timeout_s=90, in_progress=False)
    by_id = {el["id"]: el for el in els}
    assert by_id["bg"]["fill_colors"] == BG_GRADIENT[STATE_WARNING]
    assert by_id["title"]["color"] == TITLE_COLOR[STATE_WARNING]
    assert by_id["track_fill"]["fill_colors"] == TRACK_FILL_GRADIENT[STATE_WARNING]
    assert by_id["divider"]["fill_colors"] == [DIVIDER_COLOR[STATE_WARNING]]
    assert by_id["cd_text"]["color"] == DIGIT_COLOR[STATE_WARNING]

def test_build_elements_track_fill_width_matches_drain_math():
    e = ev(30)   # half of the 60-min progress_window_minutes
    els = build_elements(e, NOW, CFG, timeout_s=90, in_progress=False)
    by_id = {el["id"]: el for el in els}
    assert by_id["track_fill"]["width"] == _track_fill_width(30, 60) == 36

def test_build_elements_track_fill_clamps_at_full_and_min():
    far_event = ev(120)   # well beyond the window -> full width
    els = build_elements(far_event, NOW, CFG, timeout_s=90, in_progress=False)
    assert {el["id"]: el for el in els}["track_fill"]["width"] == PANEL_WIDTH

    imminent_event = ev(0)   # starting now -> minimum sliver
    els = build_elements(imminent_event, NOW, CFG, timeout_s=90, in_progress=False)
    assert {el["id"]: el for el in els}["track_fill"]["width"] == 1

def test_build_elements_title_is_uppercased():
    e = ev(23, title="Gym pyjama day")
    els = build_elements(e, NOW, CFG, timeout_s=90, in_progress=False)
    title = {el["id"]: el for el in els}["title"]
    # Uppercasing after ascii_safe kills descenders (g, y, p, j, y) -- the
    # root cause of the v1.3 title/track collision (see logic.py TITLE_Y).
    assert title["text"] == "GYM PYJAMA DAY"

def test_build_elements_long_title_scrolls_short_title_static():
    long_e = ev(23, title="A Very Long Meeting Title That Will Definitely Not Fit On Screen Width")
    els = build_elements(long_e, NOW, CFG, timeout_s=90, in_progress=False)
    title = {el["id"]: el for el in els}["title"]
    assert title.get("scroll_rate") == 2000
    assert title.get("scroll_start_delay") == 800
    assert title.get("scroll_repeat_delay") == 800

    short_e = ev(23, title="Sync")
    els = build_elements(short_e, NOW, CFG, timeout_s=90, in_progress=False)
    title = {el["id"]: el for el in els}["title"]
    assert "scroll_rate" not in title


# --- build_elements: in-progress event ---------------------------------------

def test_build_elements_in_progress_shape():
    e = ev(-5, dur_min=30, title="Standup")
    els = build_elements(e, NOW, CFG, timeout_s=90, in_progress=True)
    by_id = {el["id"]: el for el in els}
    assert set(by_id) == {"bg", "title", "track", "track_fill", "track_tip", "ends", "divider", "cd_text"}
    assert "time" not in by_id  # no start-time label while in progress
    # Draw order is z-order (first = behind); see the upcoming-shape test
    # for why set-membership alone isn't enough to catch a reorder.
    assert [el["id"] for el in els] == [
        "bg", "title", "track", "track_fill", "track_tip", "ends", "divider", "cd_text",
    ]

    bg = by_id["bg"]
    assert bg["fill_colors"] == BG_GRADIENT[STATE_IN_PROGRESS]

    title = by_id["title"]
    assert title["color"] == TITLE_COLOR[STATE_IN_PROGRESS]
    assert title["text"] == "STANDUP"

    track_fill = by_id["track_fill"]
    assert track_fill["fill"] == "gradient_h"
    assert track_fill["fill_colors"][-1] == TRACK_FILL_IN_PROGRESS
    assert track_fill["width"] == 60   # 25 of 30 minutes remaining

    ends = by_id["ends"]
    assert ends["type"] == "text" and ends["font"] == "bold"
    assert ends["text"] == ENDS_TEXT and ends["color"] == ENDS_TEXT_COLOR
    assert ends["x"] == 2 and ends["y"] == 6

    divider = by_id["divider"]
    assert divider["fill_colors"] == [DIVIDER_COLOR[STATE_IN_PROGRESS]]

    cd = by_id["cd_text"]
    assert cd["text"] == _format_countdown((e.end - NOW).total_seconds() / 60)  # counts down to END
    assert cd["color"] == DIGIT_COLOR[STATE_IN_PROGRESS]

def test_build_elements_in_progress_ignores_urgency_thresholds():
    # An in-progress event must render as in_progress even when minutes-left
    # (to its end) would fall inside the warn/notice bands by coincidence.
    e = ev(-58, dur_min=60, title="Long Meeting")  # 2 min left until end
    els = build_elements(e, NOW, CFG, timeout_s=90, in_progress=True)
    by_id = {el["id"]: el for el in els}
    assert by_id["bg"]["fill_colors"] == BG_GRADIENT[STATE_IN_PROGRESS]
    assert by_id["cd_text"]["color"] == DIGIT_COLOR[STATE_IN_PROGRESS]


# --- row-5 buffer (v1.4) --------------------------------------------------------
#
# v1.4 frees a deliberate blank row (5) between the title and the track by
# moving the track down to y=6. Nothing may ink row 5. `bg` is exempt (the
# gradient background legitimately spans every row) -- this checks every
# *foreground* element's computed ink range, using the same offset model
# documented in logic.py's geometry comment.

def test_no_foreground_element_inks_row_5_upcoming():
    e = ev(23)
    els = build_elements(e, NOW, CFG, timeout_s=90, in_progress=False)
    for el in els:
        if el["id"] == "bg":
            continue
        assert 5 not in ink_rows(el), (el["id"], ink_rows(el))

def test_no_foreground_element_inks_row_5_in_progress():
    e = ev(-5, dur_min=30)
    els = build_elements(e, NOW, CFG, timeout_s=90, in_progress=True)
    for el in els:
        if el["id"] == "bg":
            continue
        assert 5 not in ink_rows(el), (el["id"], ink_rows(el))

def test_no_foreground_element_inks_beyond_panel_rows():
    # Every element's ink must land within the panel's 16 rows (0-15) in
    # both layouts -- guards against a future geometry edit clipping at the
    # bottom edge the way v1.3's "ends" originally did.
    for in_progress in (False, True):
        e = ev(-5, dur_min=30) if in_progress else ev(23)
        els = build_elements(e, NOW, CFG, timeout_s=90, in_progress=in_progress)
        for el in els:
            rows = ink_rows(el)
            assert min(rows) >= 0 and max(rows) <= PANEL_HEIGHT - 1, (el["id"], rows)


# --- v1.5.2 escalation ladder: select_priority ------------------------------------

APPROACH = 30
NOTICE = 15
WARN = 5
IMMINENT = 1

def test_select_priority_normal_beyond_approach():
    assert select_priority(45, APPROACH, NOTICE, in_progress=False) == PRIORITY_AMBIENT

def test_select_priority_approach_tier():
    # Inside approach_minutes, outside notice_minutes.
    assert select_priority(30, APPROACH, NOTICE, in_progress=False) == PRIORITY_AMBIENT_RAISED
    assert select_priority(16, APPROACH, NOTICE, in_progress=False) == PRIORITY_AMBIENT_RAISED

def test_select_priority_notice_tier():
    assert select_priority(15, APPROACH, NOTICE, in_progress=False) == PRIORITY_AMBIENT_URGENT
    assert select_priority(6, APPROACH, NOTICE, in_progress=False) == PRIORITY_AMBIENT_URGENT

def test_select_priority_warn_tier_same_priority_as_notice():
    # warn and notice share PRIORITY_AMBIENT_URGENT -- they differ visually
    # (palette) and in LED (select_led), not in priority.
    assert select_priority(5, APPROACH, NOTICE, in_progress=False) == PRIORITY_AMBIENT_URGENT
    assert select_priority(1, APPROACH, NOTICE, in_progress=False) == PRIORITY_AMBIENT_URGENT

def test_select_priority_in_progress_always_baseline():
    # Even with minutes_left inside every elevated window, in_progress
    # forces the baseline priority -- see the module docstring for why.
    assert select_priority(0.5, APPROACH, NOTICE, in_progress=True) == PRIORITY_AMBIENT
    assert select_priority(-5, APPROACH, NOTICE, in_progress=True) == PRIORITY_AMBIENT

def test_select_priority_boundary_exact_approach_minutes():
    assert select_priority(APPROACH, APPROACH, NOTICE, in_progress=False) == PRIORITY_AMBIENT_RAISED
    assert select_priority(APPROACH + 0.01, APPROACH, NOTICE, in_progress=False) == PRIORITY_AMBIENT

def test_select_priority_boundary_exact_notice_minutes():
    assert select_priority(NOTICE, APPROACH, NOTICE, in_progress=False) == PRIORITY_AMBIENT_URGENT
    assert select_priority(NOTICE + 0.01, APPROACH, NOTICE, in_progress=False) == PRIORITY_AMBIENT_RAISED


# --- select_led (v1.5.2 revision: returns a bool "should be on", not a
# color -- resolve_led_value below decides the actual value to send) --------------

def test_select_led_false_outside_imminent_window():
    assert select_led(2, IMMINENT, in_progress=False) is False
    assert select_led(15, IMMINENT, in_progress=False) is False

def test_select_led_true_inside_imminent_window():
    assert select_led(1, IMMINENT, in_progress=False) is True
    assert select_led(0.1, IMMINENT, in_progress=False) is True

def test_select_led_never_true_in_progress():
    # Even with minutes_left well inside the imminent window (e.g. a
    # negative value from an event that's technically started), in_progress
    # forces False.
    assert select_led(0.5, IMMINENT, in_progress=True) is False
    assert select_led(-1, IMMINENT, in_progress=True) is False

def test_select_led_boundary_exact_imminent_minutes():
    assert select_led(IMMINENT, IMMINENT, in_progress=False) is True
    assert select_led(IMMINENT + 0.01, IMMINENT, in_progress=False) is False

def test_select_led_only_notice_and_warn_have_no_led_outside_imminent():
    # warn_minutes=5 is well outside imminent_minutes=1 by default -- no LED
    # in the warn tier itself, only inside the (much narrower) imminent one.
    assert select_led(WARN, IMMINENT, in_progress=False) is False


# --- resolve_led_value: explicit off-transition, not a bare omission -------------

def test_resolve_led_value_should_be_on_returns_imminent_color():
    assert resolve_led_value(led_should_be_on=True, led_was_on=False) == IMMINENT_LED_COLOR
    assert resolve_led_value(led_should_be_on=True, led_was_on=True) == IMMINENT_LED_COLOR

def test_resolve_led_value_off_transition_is_explicit_not_omitted():
    from calendar_countdown.logic import LED_OFF_COLOR
    assert resolve_led_value(led_should_be_on=False, led_was_on=True) == LED_OFF_COLOR

def test_resolve_led_value_already_off_omits_field():
    assert resolve_led_value(led_should_be_on=False, led_was_on=False) is None


# --- palette stays 4-way: approach adds NO new visual state -----------------------

def test_approach_window_uses_normal_palette_not_a_new_state():
    # v1.5.2: the approach tier changes priority only -- build_elements
    # (and _state_for underneath it) never sees approach_minutes at all,
    # so an event at e.g. 20 minutes out (inside approach, outside notice)
    # still renders with STATE_NORMAL colors.
    e = CalEvent("Standup", NOW + timedelta(minutes=20), NOW + timedelta(minutes=50), False)
    els = build_elements(e, NOW, CFG, timeout_s=90, in_progress=False)
    bg = next(el for el in els if el["id"] == "bg")
    assert bg["fill_colors"] == BG_GRADIENT[STATE_NORMAL]


# --- should_chirp / commit_chirped: once-per-event, edge-detected ----------------
# (reuses the module's own `ev(offset_min, ...)` helper defined near the top)

def test_should_chirp_false_while_upcoming():
    event = ev(2)
    state = {}
    assert should_chirp(event, in_progress=False, now=NOW, chirp_state=state, chirp_enabled=True) is False
    assert _chirp_key(event) in state["seen_upcoming"]

def test_should_chirp_true_on_transition_edge():
    event = ev(2)
    state = {}
    # Poll while upcoming (records seen_upcoming).
    should_chirp(event, in_progress=False, now=NOW, chirp_state=state, chirp_enabled=True)
    # Next poll: event has started.
    assert should_chirp(event, in_progress=True, now=NOW, chirp_state=state, chirp_enabled=True) is True

def test_should_chirp_does_not_refire_after_commit():
    event = ev(2)
    state = {}
    should_chirp(event, in_progress=False, now=NOW, chirp_state=state, chirp_enabled=True)
    assert should_chirp(event, in_progress=True, now=NOW, chirp_state=state, chirp_enabled=True) is True
    commit_chirped(event, state)
    # Multiple subsequent in_progress polls must never re-fire.
    assert should_chirp(event, in_progress=True, now=NOW, chirp_state=state, chirp_enabled=True) is False
    assert should_chirp(event, in_progress=True, now=NOW, chirp_state=state, chirp_enabled=True) is False

def test_should_chirp_restart_mid_event_does_not_fire():
    # Fresh chirp_state (as if the process just restarted) that never saw
    # this event as upcoming -- the documented restart-safety edge case.
    event = ev(-2)   # already started
    state = {}
    assert should_chirp(event, in_progress=True, now=NOW, chirp_state=state, chirp_enabled=True) is False

def test_should_chirp_disabled_never_fires():
    event = ev(2)
    state = {}
    should_chirp(event, in_progress=False, now=NOW, chirp_state=state, chirp_enabled=False)
    assert should_chirp(event, in_progress=True, now=NOW, chirp_state=state, chirp_enabled=False) is False

def test_should_chirp_disabled_still_tracks_seen_upcoming():
    # Observation happens regardless of chirp_enabled, so toggling chirp on
    # mid-run doesn't lose the edge-detection precondition it needs.
    event = ev(2)
    state = {}
    should_chirp(event, in_progress=False, now=NOW, chirp_state=state, chirp_enabled=False)
    assert _chirp_key(event) in state["seen_upcoming"]

def test_should_chirp_new_event_fires_independently():
    event_a = ev(2)
    event_b = ev(5, dur_min=15)
    state = {}
    should_chirp(event_a, in_progress=False, now=NOW, chirp_state=state, chirp_enabled=True)
    assert should_chirp(event_a, in_progress=True, now=NOW, chirp_state=state, chirp_enabled=True) is True
    commit_chirped(event_a, state)
    # A different event (different start) is entirely independent.
    should_chirp(event_b, in_progress=False, now=NOW, chirp_state=state, chirp_enabled=True)
    assert should_chirp(event_b, in_progress=True, now=NOW, chirp_state=state, chirp_enabled=True) is True

def test_should_chirp_same_start_different_title_do_not_collide():
    # Two distinct events sharing the exact same start timestamp (e.g. two
    # all-day events both effectively at midnight, or two calendars firing
    # something simultaneously) must be tracked independently -- keyed by
    # (start, title), not start alone -- so chirping/committing one never
    # silently marks the other as already handled.
    start = NOW + timedelta(minutes=2)
    event_a = CalEvent("Standup", start, start + timedelta(minutes=30), False)
    event_b = CalEvent("Retro", start, start + timedelta(minutes=15), False)
    state = {}
    should_chirp(event_a, in_progress=False, now=NOW, chirp_state=state, chirp_enabled=True)
    assert should_chirp(event_a, in_progress=True, now=NOW, chirp_state=state, chirp_enabled=True) is True
    commit_chirped(event_a, state)
    # event_b shares event_a's start but was never itself observed
    # upcoming -- must NOT be considered already-chirped just because
    # event_a (same start, different title) was.
    assert should_chirp(event_b, in_progress=True, now=NOW, chirp_state=state, chirp_enabled=True) is False
    # Once event_b is properly observed upcoming first, it chirps on its
    # own, independently of event_a's already-committed chirp.
    should_chirp(event_b, in_progress=False, now=NOW, chirp_state=state, chirp_enabled=True)
    assert should_chirp(event_b, in_progress=True, now=NOW, chirp_state=state, chirp_enabled=True) is True

def test_commit_chirped_not_called_means_retry_next_poll():
    # Mirrors the DRAWN-gated commit discipline: if the caller doesn't
    # call commit_chirped (e.g. because play_audio failed), the very next
    # poll must still see should_chirp return True.
    event = ev(2)
    state = {}
    should_chirp(event, in_progress=False, now=NOW, chirp_state=state, chirp_enabled=True)
    assert should_chirp(event, in_progress=True, now=NOW, chirp_state=state, chirp_enabled=True) is True
    # (caller's play_audio "failed" -- commit_chirped deliberately not called)
    assert should_chirp(event, in_progress=True, now=NOW, chirp_state=state, chirp_enabled=True) is True

def test_chirp_state_prunes_old_entries():
    event = ev(2)
    state = {}
    should_chirp(event, in_progress=False, now=NOW, chirp_state=state, chirp_enabled=True)
    assert _chirp_key(event) in state["seen_upcoming"]
    much_later = NOW + timedelta(hours=48)
    # A call for an unrelated, far-future event 48h later should prune the
    # old entry out of seen_upcoming.
    other = ev(48 * 60 + 2)
    should_chirp(other, in_progress=False, now=much_later, chirp_state=state, chirp_enabled=True)
    assert _chirp_key(event) not in state["seen_upcoming"]

def test_chirp_stock_path_is_a_firmware_stock_sound_not_an_uploaded_asset():
    # Documents the design choice (v1.5.2): no asset generation/upload
    # needed -- see logic.py's module comment for why this is a
    # firmware-shipped stock sound, not a repo-committed binary.
    assert CHIRP_STOCK_PATH == "shared/calendar_event_starts.snd"

def test_chirp_stock_path_is_snd_not_wav_regression():
    # Regression, v1.5.2.1: the runtime filename is .snd -- the firmware
    # build pipeline converts .wav SOURCE files to .snd at packaging time,
    # invisible from the source tree or the OpenAPI spec. The original
    # ".wav" stock_path returned 200 from /api/audio/play (a silently
    # accepted but wrong filename -- see play_audio's docstring for why a
    # 200 doesn't prove audible playback) and produced no sound at all,
    # confirmed by operator ear-testing. This test pins the extension
    # directly so a future edit can't silently regress back to .wav.
    assert CHIRP_STOCK_PATH.endswith(".snd")
    assert not CHIRP_STOCK_PATH.endswith(".wav")


# --- next_sleep_seconds: T-0 chirp precision --------------------------------------

def test_next_sleep_seconds_normal_when_no_upcoming_event():
    assert next_sleep_seconds(10, None) == 10

def test_next_sleep_seconds_normal_when_event_far_out():
    assert next_sleep_seconds(10, 25.0) == 10   # >= poll_seconds, not imminent enough

def test_next_sleep_seconds_shortens_when_start_is_sooner():
    assert next_sleep_seconds(10, 3.0) == 3.0

def test_next_sleep_seconds_normal_when_already_started():
    assert next_sleep_seconds(10, 0) == 10
    assert next_sleep_seconds(10, -5.0) == 10

def test_next_sleep_seconds_boundary_exact_poll_seconds():
    # Strictly less than poll_seconds triggers shortening; exactly equal
    # does not (falls through to the normal interval, same value either way).
    assert next_sleep_seconds(10, 10.0) == 10


# --- _minutes_left -------------------------------------------------------------

def test_minutes_left_upcoming_uses_start():
    e = ev(23)
    assert _minutes_left(e, NOW, in_progress=False) == 23.0

def test_minutes_left_in_progress_uses_end():
    e = ev(-5, dur_min=30)   # started 5 min ago, 30 min long -> ends in 25 min
    assert _minutes_left(e, NOW, in_progress=True) == 25.0


# --- check_threshold_ordering: v1.5.2 config sanity warning ----------------------

SANE_CFG = {"approach_minutes": 30, "notice_minutes": 15, "warn_minutes": 5, "imminent_minutes": 1}

def test_check_threshold_ordering_sane_config_no_warning():
    assert check_threshold_ordering(SANE_CFG) is None

def test_check_threshold_ordering_approach_not_greater_than_notice():
    cfg = {**SANE_CFG, "approach_minutes": 15}   # == notice_minutes
    msg = check_threshold_ordering(cfg)
    assert msg is not None
    assert "approach_minutes" in msg and "notice_minutes" in msg

def test_check_threshold_ordering_notice_not_greater_than_warn():
    cfg = {**SANE_CFG, "notice_minutes": 5}   # == warn_minutes
    msg = check_threshold_ordering(cfg)
    assert msg is not None
    assert "notice_minutes" in msg and "warn_minutes" in msg

def test_check_threshold_ordering_warn_less_than_imminent():
    cfg = {**SANE_CFG, "warn_minutes": 0, "imminent_minutes": 1}
    msg = check_threshold_ordering(cfg)
    assert msg is not None
    assert "warn_minutes" in msg and "imminent_minutes" in msg

def test_check_threshold_ordering_warn_equal_imminent_is_fine():
    # warn_minutes >= imminent_minutes is the assumed (non-strict) bound.
    cfg = {**SANE_CFG, "warn_minutes": 1, "imminent_minutes": 1}
    assert check_threshold_ordering(cfg) is None

def test_check_threshold_ordering_missing_keys_returns_none():
    # An old-style cfg dict predating the v1.5.2 keys -- nothing to check,
    # must not KeyError.
    assert check_threshold_ordering({"notice_minutes": 15, "warn_minutes": 5}) is None


# --- v1.6 stock-animation accents: is_just_started, priority, icons, takeover ----
#
# NOW2 (not the module's own `NOW`) is deliberately a distinct local
# constant here -- reassigning the module-level `NOW` at this point in the
# file would retroactively change the reference time every test ABOVE this
# point sees at call time (Python resolves a function's globals when it
# RUNS, not when it's defined), since pytest collects and runs the whole
# module in one process. `_ev`/`_cfg` are local helpers distinct from the
# top-of-file `ev`/`CFG` for the same reason -- this section is self-
# contained and must not perturb anything above it.

def _ev(start):  # 30-min event
    return CalEvent(title="Standup", start=start, end=start + timedelta(minutes=30), all_day=False)

def _cfg(**over):
    base = {"poll_seconds": 10, "lookahead_hours": 12, "warn_minutes": 5, "notice_minutes": 15,
            "approach_minutes": 30, "imminent_minutes": 1, "progress_window_minutes": 60,
            "escalation_icons": True, "start_animation": "meeting_72x16", "start_window_seconds": 60}
    base.update(over); return base

NOW2 = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)

def test_is_just_started_window():
    ev = _ev(NOW2 - timedelta(seconds=30))   # started 30s ago
    assert is_just_started(ev, NOW2, True, 60, "meeting_72x16") is True
    ev2 = _ev(NOW2 - timedelta(seconds=90))  # started 90s ago
    assert is_just_started(ev2, NOW2, True, 60, "meeting_72x16") is False
    assert is_just_started(ev, NOW2, True, 60, "") is False       # disabled
    assert is_just_started(ev, NOW2, False, 60, "meeting_72x16") is False  # not in progress

def test_priority_just_started_is_urgent():
    assert select_priority(0.0, 30, 15, True, just_started=True) == PRIORITY_AMBIENT_URGENT
    assert select_priority(0.0, 30, 15, True, just_started=False) == PRIORITY_AMBIENT  # unchanged

def test_warn_stage_adds_event_icon():
    ev = _ev(NOW2 + timedelta(minutes=4))   # 4m out -> warn, > imminent
    els = build_elements(ev, NOW2, _cfg(), 15, in_progress=False)
    icon = next(e for e in els if e["id"] == CAL_ICON_ID)
    assert icon["type"] == "animation" and icon["stock_path"] == f"shared/{ICON_EVENT}.anim"
    assert icon["x"] == 0 and icon["y"] == 0
    assert any(e["id"] == "title" for e in els)   # title still present at warn

def test_icon_present_drops_time_element():
    # The icon is 16x16 at (0,0) -- it sits directly on top of the `time`
    # element (start-time text at TIME_X=2/TIME_Y=5), so `time` must be
    # excluded whenever the icon shows, in both the warn (icon still
    # ICON_EVENT, title present) and imminent (icon ICON_REMINDER, title
    # dropped) sub-stages. cd_text (x=39) already clears the icon and
    # stays present as the sole countdown readout.
    warn_ev = _ev(NOW2 + timedelta(minutes=4))   # 4m out -> warn, > imminent
    els = build_elements(warn_ev, NOW2, _cfg(), 15, in_progress=False)
    assert not any(e["id"] == "time" for e in els)
    assert any(e["id"] == CAL_ICON_ID for e in els)
    assert any(e["id"] == "cd_text" for e in els)
    # `divider` (id "divider", at DIVIDER_X) sits to the right of `time`'s
    # old position -- with `time` dropped and `title` narrowed, the divider
    # is orphaned and must be dropped too whenever the icon shows (CODE-BUG,
    # minor fix wave item 2).
    assert not any(e["id"] == "divider" for e in els)

    imminent_ev = _ev(NOW2 + timedelta(seconds=30))  # 0.5m out -> imminent
    els = build_elements(imminent_ev, NOW2, _cfg(), 15, in_progress=False)
    assert not any(e["id"] == "time" for e in els)
    assert any(e["id"] == CAL_ICON_ID for e in els)
    assert any(e["id"] == "cd_text" for e in els)
    assert not any(e["id"] == "divider" for e in els)

    # Normal (no-icon) upcoming display is unaffected -- the divider must
    # still be present when there's no icon to orphan it.
    no_icon_els = build_elements(_ev(NOW2 + timedelta(minutes=4)), NOW2,
                                 _cfg(escalation_icons=False), 15, in_progress=False)
    assert any(e["id"] == "divider" for e in no_icon_els)


def test_warning_title_uses_full_reserved_lane_and_scrolls_only_when_needed():
    event = _ev(NOW2 + timedelta(minutes=4))
    frame = {e["id"]: e for e in build_elements(event, NOW2, _cfg(), 15, False)}
    assert frame["title"]["text"] == "STANDUP"
    assert (frame["title"]["x"], frame["title"]["width"]) == (18, 52)
    assert "scroll_rate" not in frame["title"]
    event.title = "Quarterly planning review"
    frame = {e["id"]: e for e in build_elements(event, NOW2, _cfg(), 15, False)}
    assert frame["title"]["scroll_rate"] == SCROLL_RATE
    assert frame["title"]["scroll_start_delay"] == SCROLL_DELAY_MS


def test_imminent_keeps_event_context_and_reads_less_than_one_minute():
    event = _ev(NOW2 + timedelta(seconds=30))
    frame = {e["id"]: e for e in build_elements(event, NOW2, _cfg(), 15, False)}
    assert frame[CAL_ICON_ID]["stock_path"] == f"shared/{ICON_REMINDER}.anim"
    assert frame["title"]["text"] == "STANDUP"
    assert frame["starts_label"]["text"] == "IN"
    assert frame["cd_text"]["text"] == "<1m"
    assert _format_countdown(.5) == "0m"  # shared CI formatting is unchanged


def test_just_started_returns_takeover_animation():
    ev = _ev(NOW2 - timedelta(seconds=10))
    els = build_elements(ev, NOW2, _cfg(), 15, in_progress=True, just_started=True)
    anim = next(e for e in els if e["id"] == START_ANIM_ID)
    assert anim["type"] == "animation" and anim["stock_path"] == "shared/meeting_72x16.anim"
    assert not any(e["id"] in ("cd_text", "ends") for e in els)   # takeover replaces the countdown

def test_escalation_icons_off_is_unchanged():
    ev = _ev(NOW2 + timedelta(minutes=4))
    els = build_elements(ev, NOW2, _cfg(escalation_icons=False), 15, in_progress=False)
    assert not any(e["id"] == CAL_ICON_ID for e in els)


@pytest.mark.parametrize("minutes", [0, .01, .5, 1, 4, 15, 60, 90])
def test_progress_highlight_and_warning_lane_stay_within_track(minutes):
    event = _ev(NOW2 + timedelta(minutes=minutes))
    frame = {e["id"]: e for e in build_elements(event, NOW2, _cfg(), 15, False)}
    track, fill, tip = (frame[k] for k in ("track", "track_fill", "track_tip"))
    assert track["x"] <= fill["x"] <= tip["x"]
    assert tip["x"] + tip["width"] <= fill["x"] + fill["width"] <= track["x"] + track["width"] <= 72
    assert tip["height"] == 1 and tip["border_width"] == 0
    assert all(e["timeout"] == 15 for e in frame.values())
    if CAL_ICON_ID in frame:
        assert track["x"] >= 18 and frame["title"]["x"] >= 18


def test_active_event_progress_drains_and_handles_invalid_duration():
    event = ev(-15, dur_min=30)
    frame = {e["id"]: e for e in build_elements(event, NOW, CFG, 15, True)}
    assert frame["track_fill"]["width"] == 36
    event.end = event.start
    frame = {e["id"]: e for e in build_elements(event, NOW, CFG, 15, True)}
    assert frame["track_fill"]["width"] == 72
