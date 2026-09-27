"""Unit tests: date parsing, normalization and surface rendering."""
from backend.utils.dates import (
    days_between,
    find_dates,
    parse_date_expr,
    render_like,
    shift_iso,
    surface_variants,
)


def test_full_chinese_date_with_spaces():
    hit = parse_date_expr("Alpha V2.0 将于 2026 年 9 月 20 日正式发布", default_year=2026)
    assert hit["iso"] == "2026-09-20"
    assert hit["surface"] == "2026 年 9 月 20 日"


def test_month_day():
    hit = parse_date_expr("9 月 27 日上线", default_year=2026)
    assert hit["iso"] == "2026-09-27"


def test_iso_and_short_forms():
    assert parse_date_expr("2026-09-20 发布")["iso"] == "2026-09-20"
    assert parse_date_expr("上线 09-20", default_year=2026)["iso"] == "2026-09-20"
    assert parse_date_expr("2026/10/08", default_year=2026)["iso"] == "2026-10-08"


def test_multiple_dates_ordered():
    found = find_dates("由 9 月 20 日调整至 9 月 27 日", default_year=2026)
    assert [d["iso"] for d in found] == ["2026-09-20", "2026-09-27"]


def test_invalid_date_ignored():
    assert parse_date_expr("13 月 40 日", default_year=2026) is None
    assert parse_date_expr("2027-02-29") is None
    assert parse_date_expr("2028-02-29")["iso"] == "2028-02-29"


def test_month_end_dates_are_validated_by_calendar():
    assert parse_date_expr("2026-04-30")["iso"] == "2026-04-30"
    assert parse_date_expr("2026-04-31") is None


def test_render_like_preserves_style():
    assert render_like("2026 年 9 月 20 日", "2026-09-27") == "2026 年 9 月 27 日"
    assert render_like("9 月 20 日", "2026-09-27") == "9 月 27 日"
    assert render_like("9月20日", "2026-09-27") == "9月27日"
    assert render_like("2026-09-20", "2026-10-08") == "2026-10-08"
    assert render_like("09-20", "2026-09-27") == "09-27"
    assert render_like("2026/09/20", "2026-10-08") == "2026/10/08"


def test_surface_variants_longest_first():
    variants = surface_variants("2026-09-20")
    assert variants[0].startswith("2026")
    assert "9月20日" in variants
    assert "09-20" in variants


def test_day_math():
    assert days_between("2026-09-18", "2026-09-27") == 9
    assert shift_iso("2026-09-27", -7) == "2026-09-20"
