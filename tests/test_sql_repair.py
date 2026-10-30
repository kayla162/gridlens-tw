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


def test_a_county_question_grouped_by_township_is_grouped_by_county() -> None:
    sql = (
        'SELECT "縣市", SUM("裝置容量_瓩") AS kw FROM v_unit '
        'GROUP BY "縣市" ORDER BY "縣市" LIMIT 100'
    )

    repair = repair_llm_query(
        sql, [], column_values=VALUES, max_rows=200, question="各縣市的總裝置容量（瓩）"
    )

    assert repair.query.sql == (
        'SELECT SUBSTRING("縣市", 1, 3) AS "縣市", SUM("裝置容量_瓩") AS kw FROM v_unit '
        'GROUP BY SUBSTRING("縣市", 1, 3) ORDER BY SUBSTRING("縣市", 1, 3) LIMIT 100'
    )


def test_township_questions_and_other_views_keep_their_grouping() -> None:
    township = 'SELECT "縣市", COUNT(*) FROM v_unit GROUP BY "縣市" LIMIT 200'
    renewable = 'SELECT "縣市", SUM("發電量_度") FROM v_re_generation GROUP BY "縣市" LIMIT 200'

    def notes(sql: str, question: str) -> tuple[str, ...]:
        return repair_llm_query(
            sql, [], column_values=VALUES, max_rows=200, question=question
        ).notes

    assert notes(township, "各鄉鎮有幾部機組") == ()
    assert notes(renewable, "各縣市的再生能源發電量") == ()
    assert notes(township, "") == (), "沒有問句就不猜"


def test_string_literals_become_parameters_in_text_order() -> None:
    sql = (
        'SELECT SUM("發電量_度") FROM v_re_generation '
        'WHERE "縣市" = ? AND "能源別" LIKE \'%風力%\' AND "年度" = ? LIMIT 20'
    )

    repair = _repair(sql, ["澎湖縣", 2025])

    assert "'" not in repair.query.sql
    assert repair.query.params == ("澎湖縣", "%風力%", 2025)
    assert SqlGuard().validate(repair.query.sql, repair.query.params).allowed


def test_group_keys_left_out_of_the_select_are_added_in_front() -> None:
    repair = _repair('SELECT SUM("裝置容量_瓩") FROM v_unit GROUP BY "燃料" LIMIT 50', [])

    assert (
        repair.query.sql == 'SELECT "燃料", SUM("裝置容量_瓩") FROM v_unit GROUP BY "燃料" LIMIT 50'
    )


def test_group_keys_already_selected_or_aliased_are_not_repeated() -> None:
    for sql in (
        'SELECT "燃料", COUNT(*) FROM v_unit GROUP BY "燃料" LIMIT 50',
        'SELECT "燃料" AS f, COUNT(*) FROM v_unit GROUP BY "燃料" LIMIT 50',
        'SELECT SUBSTR("日期", 1, 7), AVG("尖峰負載_萬瓩") FROM v_system '
        'GROUP BY SUBSTR("日期", 1, 7) LIMIT 30',
    ):
        assert _repair(sql, []).notes == (), sql
