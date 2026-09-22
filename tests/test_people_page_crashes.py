"""Two ways the People page took itself down, both hit while AI runs finished.

Neither is exotic. One needs a table that got shorter since the click that
ticked a row — which is precisely what scoring a batch of people does, because
their new verdict moves them out of the current filter. The other needs a
company with a known email domain, and nothing else at all.

Both are regression tests: they fail on the page as it was, and the fix for
each is one line.
"""

from __future__ import annotations

from streamlit.testing.v1 import AppTest

from .test_resume_llm import PEOPLE_PAGE, _seed_ui_db  # noqa: TID252


def _page(selection: list[int] | None = None) -> AppTest:
    at = AppTest.from_file(str(PEOPLE_PAGE), default_timeout=60)
    if selection is not None:
        # What the browser sends back: ROW POSITIONS from the render that was
        # clicked on, not ids, and not validated against anything.
        at.session_state["people_table"] = {
            "selection": {"rows": selection, "columns": []}
        }
    return at.run()


# ─────────────── a tick box pointing past the end of the table ───────────


def test_a_selection_left_over_from_a_longer_table_does_not_crash(app_root):
    """`IndexError: index 278 is out of bounds for axis 0 with size 278`.

    The owner ticked a row, an AI run finished underneath them, the table
    shrank, and the ticked position no longer existed. The whole page died on
    the next redraw — including the progress and results they were waiting for.
    """
    _seed_ui_db(read=2)
    at = _page(selection=[0, 1, 99])

    assert not at.exception, [repr(e.value) for e in at.exception]


def test_the_positions_that_still_exist_are_kept(app_root):
    """Dropping the stale ones must not drop the good ones with them."""
    _seed_ui_db(read=3)
    at = _page(selection=[0, 42])

    assert not at.exception, [repr(e.value) for e in at.exception]
    # One row survived, so the page is in its single-person state: the detail
    # panel is rendered, and the multi-select caption is not.
    text = " ".join(str(x.value) for x in at.subheader)
    assert "Your call" in text
    assert not any("people selected" in str(c.value) for c in at.caption)


def test_a_selection_that_is_entirely_stale_reads_as_no_selection(app_root):
    _seed_ui_db(read=2)
    at = _page(selection=[7, 8, 9])

    assert not at.exception, [repr(e.value) for e in at.exception]
    assert any("Tick a row above" in str(m.value) for m in at.info)


# ──────────── the person the panel is about, quietly overwritten ─────────


def test_a_company_with_a_known_domain_does_not_break_your_call(app_root):
    """`KeyError: 'status'`.

    The domain list looped `for row in known`, over the same name the person's
    own record was held in. After the loop `row` was a domain, so "Your call"
    asked a domain for its CRM status and the page ended there — one line below
    the Keep and Discard buttons.
    """
    _seed_ui_db(read=1, domain=True)
    at = _page(selection=[0])

    assert not at.exception, [repr(e.value) for e in at.exception]
    text = " ".join(str(m.value) for m in at.markdown)
    assert "alpine-racing.com" in text          # the domain list did render
    assert "Right now:" in text                 # and the person survived it
    assert any(b.label == "⭐ Keep" for b in at.button)
