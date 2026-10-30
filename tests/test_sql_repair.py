"""Mechanical fixes to LLM SQL (each input is a real CP-097 failure of qwen3:8b)."""

from __future__ import annotations

from text2sql.sql_guard import SqlGuard
from text2sql.sql_repair import repair_llm_query

VALUES = {
    "v_unit.縣市": ["新北市林口區", "新北市新店區", "高雄市小港區", "基隆市"],
    "v_unit.燃料": ["水", "重油", "天然氣", "輕柴油", "煤"],
    "v_re_generation.能源別": ["地熱", "太陽能", "陸域風力", "離岸風力"],
    "v_peak.電廠": ["明潭發電廠", "桂山發電廠|明潭發電廠", "台中發電廠"],
}


def _repair(sql: str, params: list[object]):
    return repair_llm_query(sql, params, column_values=VALUES, max_rows=200)


def test_a_parameterized_limit_becomes_a_constant() -> None:
    repair = _repair('SELECT "電廠" FROM v_unit GROUP BY "電廠" LIMIT ?', ["10"])

    assert repair.query.sql == 'SELECT "電廠" FROM v_unit GROUP BY "電廠" LIMIT 10'
    assert repair.query.params == ()
    assert SqlGuard().validate(repair.query.sql, repair.query.params).allowed


def test_a_limit_parameter_keeps_the_other_parameters_in_order() -> None:
    repair = _repair(
        'SELECT "發電站" FROM v_re_generation WHERE "能源別" = ? '
        'ORDER BY "裝置容量_瓩" DESC LIMIT ?',
        ["陸域風力", 5],
    )

    assert repair.query.sql.endswith("LIMIT 5")
    assert repair.query.params == ("陸域風力",)


def test_a_limit_parameter_that_is_not_an_integer_is_left_for_the_guard() -> None:
    repair = _repair('SELECT "電廠" FROM v_unit LIMIT ?', ["ten"])

    assert repair.notes == ()
    assert not SqlGuard().validate(repair.query.sql, repair.query.params).allowed


def test_blank_parameters_without_a_placeholder_are_dropped() -> None:
    repair = _repair('SELECT "機組名" FROM v_unit ORDER BY "商轉日期" LIMIT 2', [""])

    assert repair.query.params == ()
    assert SqlGuard().validate(repair.query.sql, repair.query.params).allowed


def test_a_missing_limit_is_added_at_the_row_cap() -> None:
    repair = _repair('SELECT COUNT(*) FROM v_unit WHERE "燃料" = ?', ["煤"])

    assert repair.query.sql.endswith("LIMIT 200")
    assert repair.query.params == ("煤",)


def test_a_county_name_becomes_a_prefix_match() -> None:
    repair = _repair('SELECT SUM("裝置容量_萬瓩") FROM v_unit WHERE "縣市" = ? LIMIT 1', ["高雄市"])

    assert '"縣市" LIKE ?' in repair.query.sql
    assert repair.query.params == ("高雄市%",)


def test_a_wording_that_contains_exactly_one_value_becomes_that_value() -> None:
    repair = _repair('SELECT "電廠" FROM v_unit WHERE "燃料" = ? LIMIT 50', ["燃煤"])

    assert '"燃料" = ?' in repair.query.sql
    assert repair.query.params == ("煤",)


def test_a_shared_ending_becomes_a_suffix_match() -> None:
    repair = _repair('SELECT * FROM v_re_generation WHERE "能源別" = ? LIMIT 20', ["風力"])

    assert '"能源別" LIKE ?' in repair.query.sql
    assert repair.query.params == ("%風力",)


def test_an_ambiguous_wording_is_left_for_the_model() -> None:
    """「明潭」是一個值的開頭、另一個合併欄位的中段：猜 LIKE 會重複計算。"""

    sql = 'SELECT * FROM v_peak WHERE "電廠" = ? LIMIT 5'

    assert _repair(sql, ["明潭"]).notes == ()
    assert _repair('SELECT * FROM v_unit WHERE "縣市" = ? LIMIT 5', ["林口"]).notes == ()


def test_values_already_in_the_set_and_open_columns_are_untouched() -> None:
    sql = 'SELECT * FROM v_unit WHERE "縣市" = ? AND "機組名" = ? LIMIT 5'

    repair = _repair(sql, ["基隆市", "協和#1"])

    assert repair.notes == ()
    assert repair.query.sql == sql


def test_unparsable_sql_is_passed_through_for_the_guard_to_reject() -> None:
    repair = _repair("SELEC nonsense", [])

    assert repair.query.sql == "SELEC nonsense"
