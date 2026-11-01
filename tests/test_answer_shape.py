"""Answer-shape fixes (CP-102).

Each positive case is a real qwen3:8b output; the other cases must be left as they are.
"""

from __future__ import annotations

from text2sql.sql_guard import SqlGuard
from text2sql.sql_repair import repair_llm_query

VALUES = {
    "v_unit.燃料": ["水", "重油", "天然氣", "輕柴油", "煤"],
    "v_re_generation.能源別": ["地熱", "太陽能", "陸域風力", "離岸風力"],
    "v_unit.電廠": ["卓蘭發電廠", "協和發電廠", "台中發電廠"],
    "v_unit.縣市": ["苗栗縣卓蘭鎮", "基隆市", "台中市龍井區"],
}


def _repair(question: str, sql: str, params: list[object]):
    repair = repair_llm_query(sql, params, column_values=VALUES, max_rows=200, question=question)
    assert SqlGuard().validate(repair.query.sql, repair.query.params).allowed
    return repair


def test_a_count_question_answered_with_a_list_is_counted() -> None:
    repair = _repair(
        "2026年7月20日尖峰出力超過50萬瓩的機組欄位有幾個",
        'SELECT "機組欄位" FROM v_peak WHERE "日期" = ? AND "尖峰出力_萬瓩" > ? LIMIT 1',
        ["2026-07-20", 50],
    )

    assert repair.query.sql == (
        'SELECT COUNT(*) AS "數量" FROM (SELECT "機組欄位" FROM v_peak '
        'WHERE "日期" = ? AND "尖峰出力_萬瓩" > ?) LIMIT 1'
    )
    assert repair.query.params == ("2026-07-20", 50)


def test_counting_plants_counts_the_groups_that_passed_having() -> None:
    repair = _repair(
        "有兩種以上燃料的電廠有幾座",
        'SELECT "電廠", COUNT(DISTINCT "燃料") AS n FROM v_unit GROUP BY "電廠" '
        'HAVING COUNT(DISTINCT "燃料") > 1 LIMIT 100',
        [],
    )

    assert repair.query.sql.startswith('SELECT COUNT(*) AS "數量" FROM (SELECT "電廠"')
    assert "LIMIT 100" not in repair.query.sql


def test_counting_plants_from_a_unit_list_dedupes_first() -> None:
    repair = _repair(
        "有天然氣機組的電廠共幾座",
        'SELECT "電廠" FROM v_unit WHERE "燃料" = ? LIMIT 200',
        ["天然氣"],
    )

    assert '(SELECT DISTINCT "電廠" FROM v_unit' in repair.query.sql


def test_a_count_grouped_by_the_filtered_column_is_already_the_answer() -> None:
    sql = 'SELECT "燃料", COUNT(*) AS n FROM v_unit WHERE "燃料" = ? GROUP BY "燃料" LIMIT 1'
    repair = _repair("燃料是重油的機組有幾部", sql, ["重油"])

    assert repair.query.sql == sql


def test_a_count_per_group_is_not_collapsed() -> None:
    sql = 'SELECT "電廠", COUNT(DISTINCT "燃料") AS n FROM v_unit GROUP BY "電廠" LIMIT 200'

    assert _repair("每座電廠有幾種燃料", sql, []).query.sql == sql


def test_units_and_tens_are_not_counting_words() -> None:
    sql = 'SELECT AVG("裝置容量_萬瓩") AS wkw FROM v_unit WHERE "燃料" = ? LIMIT 1'

    assert _repair("燃煤機組的平均裝置容量是幾萬瓩", sql, ["煤"]).query.sql == sql


def test_each_of_two_listed_plants_gets_its_own_row() -> None:
    repair = _repair(
        "協和發電廠和林口發電廠各有幾部機組",
        'SELECT COUNT(*) AS "機組數" FROM v_unit WHERE "電廠" IN (?, ?) LIMIT 1',
        ["協和發電廠", "林口發電廠"],
    )

    assert repair.query.sql == (
        'SELECT "電廠", COUNT(*) AS "機組數" FROM v_unit WHERE "電廠" IN (?, ?) '
        'GROUP BY "電廠" LIMIT 200'
    )


def test_a_listing_of_plants_from_unit_rows_is_deduped() -> None:
    repair = _repair(
        "商轉日期只到月份的機組分布在哪些電廠",
        'SELECT "電廠" FROM v_unit WHERE "商轉日期精度" = ? ORDER BY "電廠" LIMIT 5',
        ["month"],
    )

    assert repair.query.sql == (
        'SELECT DISTINCT "電廠" FROM v_unit WHERE "商轉日期精度" = ? ORDER BY "電廠" LIMIT 200'
    )


def test_a_listing_of_units_is_not_deduped_and_a_named_count_keeps_its_limit() -> None:
    sql = 'SELECT "機組名" FROM v_unit ORDER BY "裝置容量_瓩" DESC LIMIT 3'

    assert _repair("裝置容量最大的三部機組是哪些", sql, []).query.sql == sql


def test_a_superlative_keeps_only_the_top_row() -> None:
    repair = _repair(
        "平均每部機組裝置容量最大的電廠",
        'SELECT "電廠", AVG("裝置容量_萬瓩") AS a FROM v_unit GROUP BY "電廠" '
        "ORDER BY a DESC LIMIT 200",
        [],
    )

    assert repair.query.sql.endswith("LIMIT 1")


def test_a_superlative_per_group_or_with_an_offset_is_left_alone() -> None:
    per_group = (
        'SELECT "電廠", MIN("商轉日期") AS d FROM v_unit GROUP BY "電廠" ORDER BY d LIMIT 200'
    )
    second = (
        'SELECT "電廠" FROM v_unit GROUP BY "電廠" '
        'ORDER BY SUM("裝置容量_瓩") DESC LIMIT 1 OFFSET 1'
    )

    assert _repair("每座電廠最早商轉的日期", per_group, []).query.sql == per_group
    assert _repair("總裝置容量排名第二的電廠", second, []).query.sql == second


def test_unasked_quality_filters_are_dropped() -> None:
    repair = _repair(
        "燃氣機組的歲修總共有幾筆",
        'SELECT COUNT(*) AS n FROM v_outage WHERE "燃料" = ? AND "日期狀態" = ? '
        'AND "對齊狀態" = ? LIMIT 1',
        ["燃氣", "valid", "matched"],
    )

    assert repair.query.sql == 'SELECT COUNT(*) AS n FROM v_outage WHERE "燃料" = ? LIMIT 1'
    assert repair.query.params == ("燃氣",)


def test_a_quality_filter_the_question_asks_for_stays() -> None:
    sql = 'SELECT COUNT(*) AS n FROM v_unit WHERE "商轉日期精度" = ? LIMIT 1'

    assert _repair("商轉日期沒有精確到日的機組有幾部", sql, ["month"]).query.sql == sql


def test_the_valid_date_filter_stays_when_the_query_uses_outage_dates() -> None:
    sql = (
        'SELECT "機組名" FROM v_outage WHERE "日期狀態" = ? AND "開始日期" <= ? '
        'AND "結束日期" >= ? LIMIT 100'
    )
    repair = _repair("2026年3月有哪些機組在歲修", sql, ["valid", "2026-03-31", "2026-03-01"])

    assert '"日期狀態" = ?' in repair.query.sql


def test_a_subtype_the_question_did_not_name_is_widened() -> None:
    repair = _repair(
        "裝置容量最大的風力場站是哪一座",
        'SELECT "發電站" FROM v_re_generation WHERE "能源別" = ? '
        'ORDER BY "裝置容量_瓩" DESC LIMIT 1',
        ["陸域風力"],
    )

    assert '"能源別" LIKE ?' in repair.query.sql
    assert repair.query.params == ("%風力",)


def test_a_subtype_the_question_named_is_kept() -> None:
    sql = 'SELECT "發電站" FROM v_re_generation WHERE "能源別" = ? LIMIT 200'

    assert _repair("有哪些陸域風力場站", sql, ["陸域風力"]).query.sql.endswith("LIMIT 200")
    assert _repair("有哪些陸域風力場站", sql, ["陸域風力"]).query.params == ("陸域風力",)


def test_without_a_question_nothing_about_shape_changes() -> None:
    sql = 'SELECT "電廠" FROM v_unit WHERE "燃料" = ? AND "商轉日期精度" = ? LIMIT 5'
    repair = repair_llm_query(sql, ["水", "day"], column_values=VALUES, max_rows=200)

    assert repair.query.sql == sql


def test_counting_plants_with_count_star_counts_distinct_plants() -> None:
    repair = _repair(
        "南投縣一共有幾座電廠",
        'SELECT COUNT(*) AS "電廠數" FROM v_unit WHERE "縣市" LIKE ? LIMIT 1',
        ["南投縣%"],
    )

    assert repair.query.sql == (
        'SELECT COUNT(DISTINCT "電廠") AS "電廠數" FROM v_unit WHERE "縣市" LIKE ? LIMIT 1'
    )


def test_counting_units_or_records_keeps_count_star() -> None:
    units = 'SELECT COUNT(*) AS n FROM v_unit WHERE "燃料" = ? LIMIT 1'
    kinds = 'SELECT COUNT(*) AS n FROM v_unit WHERE "電廠" = ? LIMIT 1'

    assert _repair("燃煤機組有幾部", units, ["煤"]).query.sql == units
    # 「幾種」數的是燃料，問句沒提燃料這個詞就不猜。
    assert _repair("台中發電廠有幾種機組", kinds, ["台中發電廠"]).query.sql == kinds


def test_the_nth_place_gets_an_offset() -> None:
    repair = _repair(
        "機組數第三多的電廠是哪一座",
        'SELECT "電廠", COUNT(*) AS "機組數" FROM v_unit GROUP BY "電廠" '
        'ORDER BY "機組數" DESC LIMIT 1',
        [],
    )

    assert repair.query.sql.endswith("LIMIT 1 OFFSET 2")


def test_the_top_n_is_not_turned_into_the_nth() -> None:
    sql = 'SELECT "電廠" FROM v_unit GROUP BY "電廠" ORDER BY COUNT(*) DESC LIMIT 3'

    assert _repair("機組數前三多的電廠", sql, []).query.sql == sql


def test_the_latest_per_plant_is_a_max_per_group() -> None:
    repair = _repair(
        "每座電廠最近一次有機組商轉是哪一天",
        'SELECT "電廠", "商轉日期" FROM v_unit '
        'WHERE "電廠" IN (SELECT DISTINCT "電廠" FROM v_unit) '
        'ORDER BY "商轉日期" DESC LIMIT 1',
        [],
    )

    assert repair.query.sql == (
        'SELECT "電廠", MAX("商轉日期") AS "商轉日期" FROM v_unit '
        'WHERE "電廠" IN (SELECT DISTINCT "電廠" FROM v_unit) GROUP BY "電廠" LIMIT 200'
    )


def test_a_count_for_each_plant_drops_the_made_up_plant_and_groups() -> None:
    repair = _repair(
        "每座電廠的機組數，由多到少排列",
        'SELECT COUNT(*) AS "機組數" FROM v_unit WHERE "電廠" = ? LIMIT 200',
        ["卓蘭發電廠"],
    )

    assert repair.query.sql == (
        'SELECT "電廠", COUNT(*) AS "機組數" FROM v_unit GROUP BY "電廠" LIMIT 200'
    )
    assert repair.query.params == ()


def test_names_the_question_did_not_say_are_dropped() -> None:
    repair = _repair(
        "2026年7月20日類別為再生的機組欄位有幾個",
        'SELECT COUNT(*) FROM v_unit WHERE "商轉日期" <= ? AND "燃料" = ? AND "縣市" = ? '
        'AND "電廠" = ? LIMIT 1',
        ["2026-07-31", "水", "苗栗縣卓蘭鎮", "卓蘭發電廠"],
    )

    assert repair.query.sql == (
        'SELECT COUNT(*) FROM v_unit WHERE "商轉日期" <= ? AND "燃料" = ? LIMIT 1'
    )
    assert repair.query.params == ("2026-07-31", "水")


def test_a_name_that_is_not_in_the_data_is_left_for_the_answer_check() -> None:
    sql = 'SELECT DISTINCT "電廠" FROM v_unit WHERE "縣市" = ? LIMIT 200'

    assert _repair("新北市有哪些電廠", sql, ["林口"]).query.params == ("林口",)


def test_names_the_question_said_stay() -> None:
    sql = 'SELECT COUNT(*) AS n FROM v_unit WHERE "電廠" = ? AND "縣市" LIKE ? LIMIT 1'
    params = ["協和發電廠", "基隆市%"]

    assert _repair("基隆的協和電廠有幾部機組", sql, params).query.params == tuple(params)


def test_both_values_on_one_column_become_a_having() -> None:
    repair = _repair(
        "同時有重油和輕柴油機組的電廠",
        'SELECT "電廠" FROM v_unit WHERE "燃料" = ? AND "燃料" = ? LIMIT 1',
        ["重油", "輕柴油"],
    )

    assert repair.query.sql == (
        'SELECT "電廠" FROM v_unit WHERE "燃料" IN (?, ?) GROUP BY "電廠" '
        'HAVING COUNT(DISTINCT "燃料") = 2 LIMIT 200'
    )
    assert repair.query.params == ("重油", "輕柴油")


def test_two_conditions_without_both_in_the_question_are_left_alone() -> None:
    sql = 'SELECT "電廠" FROM v_unit WHERE "燃料" = ? AND "燃料" = ? LIMIT 200'

    assert _repair("重油或輕柴油機組的電廠", sql, ["重油", "輕柴油"]).query.sql == sql


def test_a_period_the_question_never_mentioned_is_dropped() -> None:
    repair = _repair(
        "起訖日期有誤的歲修有幾筆",
        'SELECT COUNT(*) FROM v_outage WHERE "日期狀態" = ? AND "開始日期" > ? '
        'AND "結束日期" < ? LIMIT 200',
        ["invalid_range", "2025-01-01", "2026-07-31"],
    )

    assert repair.query.sql == 'SELECT COUNT(*) FROM v_outage WHERE "日期狀態" = ? LIMIT 200'
    assert repair.query.params == ("invalid_range",)


def test_a_period_the_question_asked_for_stays() -> None:
    sql = 'SELECT COUNT(*) AS n FROM v_unit WHERE "商轉日期" BETWEEN ? AND ? LIMIT 1'

    assert _repair("1990年代商轉的機組有幾部", sql, ["1990-01-01", "1999-12-31"]).query.sql == sql


def test_fuels_the_question_never_mentioned_are_dropped() -> None:
    repair = _repair(
        "總裝置容量超過200萬瓩的電廠有幾座",
        'SELECT COUNT(DISTINCT "電廠") AS n FROM v_unit WHERE "燃料" IN (?, ?, ?, ?) '
        'AND "裝置容量_萬瓩" > ? LIMIT 200',
        ["煤", "天然氣", "重油", "輕柴油", 200],
    )

    assert '"燃料"' not in repair.query.sql
    assert repair.query.params == (200,)


def test_fuels_the_question_named_or_summed_up_stay() -> None:
    gas = 'SELECT COUNT(*) AS n FROM v_unit WHERE "燃料" = ? LIMIT 1'
    thermal = 'SELECT COUNT(*) AS n FROM v_unit WHERE "燃料" IN (?, ?) LIMIT 1'

    assert _repair("燃氣機組有幾部", gas, ["天然氣"]).query.params == ("天然氣",)
    assert _repair("火力機組有幾部", thermal, ["煤", "天然氣"]).query.sql == thermal


def test_unasked_value_status_is_dropped() -> None:
    repair = _repair(
        "每座風力場站最近一筆發電紀錄是哪一年",
        'SELECT "發電站", MAX("年度") AS y FROM v_re_generation WHERE "能源別" LIKE ? '
        'AND "數值狀態" = ? GROUP BY "發電站" LIMIT 200',
        ["%風力", "ok"],
    )

    assert '"數值狀態"' not in repair.query.sql


def test_the_quality_state_follows_the_question() -> None:
    repair = _repair(
        "對應到多部機組、有歧義的歲修有幾筆",
        'SELECT COUNT(*) AS "筆數" FROM v_outage WHERE "對齊狀態" = ? LIMIT 1',
        ["unmatched"],
    )

    assert repair.query.params == ("ambiguous",)


def test_a_free_text_reason_is_matched_as_contains() -> None:
    repair = _repair(
        "原因寫著大修的歲修有幾筆",
        'SELECT COUNT(*) FROM v_outage WHERE "原因" = ? LIMIT 1',
        ["大修"],
    )
    blank = 'SELECT COUNT(*) FROM v_outage WHERE "原因" = ? LIMIT 1'

    assert repair.query.sql == 'SELECT COUNT(*) FROM v_outage WHERE "原因" LIKE ? LIMIT 1'
    assert repair.query.params == ("%大修%",)
    assert _repair("原因空白的歲修有幾筆", blank, [""]).query.sql == blank


def test_every_group_kept_by_having_is_listed() -> None:
    repair = _repair(
        "同時有煤和天然氣機組的電廠",
        'SELECT "電廠" FROM v_unit WHERE "燃料" IN (?, ?) GROUP BY "電廠" '
        'HAVING COUNT(DISTINCT "燃料") = 2 LIMIT 1',
        ["煤", "天然氣"],
    )

    assert repair.query.sql.endswith("LIMIT 200")


def test_kinds_per_group_count_distinct_instead_of_grouping_by_the_kind() -> None:
    repair = _repair(
        "每個縣市有幾種燃料的機組",
        'SELECT SUBSTRING("縣市", 1, 3) AS "縣市", "燃料", COUNT(*) AS "機組數" FROM v_unit '
        'GROUP BY SUBSTRING("縣市", 1, 3), "燃料" ORDER BY SUBSTRING("縣市", 1, 3) LIMIT 100',
        [],
    )

    assert 'COUNT(DISTINCT "燃料")' in repair.query.sql
    assert 'GROUP BY SUBSTRING("縣市", 1, 3) ORDER' in repair.query.sql
    assert ', "燃料",' not in repair.query.sql


def test_params_sent_with_no_placeholder_at_all_are_dropped() -> None:
    repair = _repair(
        "每個縣市單一機組最大的裝置容量",
        'SELECT SUBSTR("縣市", 1, 3) AS "縣市", MAX("裝置容量_萬瓩") AS m FROM v_unit '
        'GROUP BY SUBSTR("縣市", 1, 3) LIMIT 200',
        ["JSON scalar"],
    )

    assert repair.query.params == ()


def test_every_group_is_listed_when_the_question_asks_for_each() -> None:
    repair = _repair(
        "每個縣市單一機組最大的裝置容量",
        'SELECT SUBSTR("縣市", 1, 3) AS "縣市", MAX("裝置容量_萬瓩") AS m FROM v_unit '
        'GROUP BY SUBSTR("縣市", 1, 3) ORDER BY m DESC LIMIT 1',
        [],
    )
    top = 'SELECT "電廠" FROM v_unit GROUP BY "電廠" ORDER BY COUNT(*) DESC LIMIT 1'

    assert repair.query.sql.endswith("LIMIT 200")
    assert _repair("各電廠中機組數最多的是哪一座", top, []).query.sql == top


def test_the_latest_per_plant_ignores_extra_columns_when_asking_for_the_date() -> None:
    repair = _repair(
        "各電廠最晚商轉的機組是哪一天商轉的",
        'SELECT "機組名", "商轉日期" FROM v_unit WHERE "商轉日期" IS NOT NULL '
        'ORDER BY "商轉日期" DESC LIMIT 1',
        ["2025-01-01"],
    )
    which_unit = (
        'SELECT "機組名", "電廠", "裝置容量_瓩" FROM v_unit ORDER BY "裝置容量_瓩" DESC LIMIT 1'
    )

    assert repair.query.sql == (
        'SELECT "電廠", MAX("商轉日期") AS "商轉日期" FROM v_unit '
        'WHERE NOT "商轉日期" IS NULL GROUP BY "電廠" LIMIT 200'
    )
    assert _repair("各電廠容量最大的是哪一部機組", which_unit, []).query.sql == which_unit


def test_which_few_is_a_listing_not_a_count() -> None:
    sql = 'SELECT DISTINCT "電廠" FROM v_unit WHERE "燃料" = ? LIMIT 200'

    assert _repair("哪幾座電廠有重油機組", sql, ["重油"]).query.sql == sql


def test_the_valid_date_filter_goes_with_an_unasked_period() -> None:
    repair = _repair(
        "沒寫原因的歲修有幾筆",
        'SELECT COUNT(*) FROM v_outage WHERE "原因" IS NULL AND "日期狀態" = ? '
        'AND "開始日期" >= ? AND "結束日期" <= ? LIMIT 1',
        ["valid", "2025-01-01", "2026-07-31"],
    )

    assert repair.query.sql == 'SELECT COUNT(*) FROM v_outage WHERE "原因" IS NULL LIMIT 1'
    assert repair.query.params == ()
