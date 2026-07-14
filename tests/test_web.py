"""Web-layer helpers (no server needed)."""
from types import SimpleNamespace
from urllib.parse import parse_qs

from app.web.routes import _watch_prefill_qs


def _form(**overrides) -> dict:
    base = {
        "origin": "CPH", "destination": "BOS", "trip_type": "RT", "cabin": "AB",
        "out_from": "", "out_to": "", "ret_from": "", "ret_to": "",
        "min_stay_days": 3, "max_stay_days": 14, "min_seats": 1,
    }
    return {**base, **overrides}


def _result(*date_pairs: tuple[str, str | None]) -> SimpleNamespace:
    return SimpleNamespace(trips=[
        SimpleNamespace(trip=SimpleNamespace(outbound_date=out, inbound_date=ret))
        for out, ret in date_pairs
    ])


def test_blank_windows_filled_from_result_dates():
    result = _result(
        ("2026-09-10", "2026-09-20"),
        ("2026-08-02", "2026-08-14"),
        ("2026-10-01", "2026-10-09"),
    )
    qs = parse_qs(_watch_prefill_qs(_form(), result), keep_blank_values=True)
    assert qs["out_from"] == ["2026-08-02"]
    assert qs["out_to"] == ["2026-10-01"]
    assert qs["ret_from"] == ["2026-08-14"]
    assert qs["ret_to"] == ["2026-10-09"]
    assert qs["origin"] == ["CPH"]
    assert qs["destination"] == ["BOS"]


def test_user_windows_win_over_result_dates():
    result = _result(("2026-08-02", "2026-08-14"))
    form = _form(out_from="2026-09-01", out_to="2026-09-30",
                 ret_from="2026-09-05", ret_to="2026-10-05")
    qs = parse_qs(_watch_prefill_qs(form, result), keep_blank_values=True)
    assert qs["out_from"] == ["2026-09-01"]
    assert qs["out_to"] == ["2026-09-30"]
    assert qs["ret_from"] == ["2026-09-05"]
    assert qs["ret_to"] == ["2026-10-05"]


def test_no_trips_leaves_windows_blank():
    qs = parse_qs(_watch_prefill_qs(_form(), _result()), keep_blank_values=True)
    assert qs["out_from"] == [""]
    assert qs["ret_to"] == [""]


def test_one_way_has_no_return_window():
    result = _result(("2026-08-02", None), ("2026-08-05", None))
    qs = parse_qs(_watch_prefill_qs(_form(trip_type="OW"), result), keep_blank_values=True)
    assert qs["out_from"] == ["2026-08-02"]
    assert qs["out_to"] == ["2026-08-05"]
    assert qs["ret_from"] == [""]
    assert qs["ret_to"] == [""]
