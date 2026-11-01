"""Make the LLM's SQL return the shape the question asks for (CP-102).

CP-100 的沒調過題庫答對 9/20，剩下的錯幾乎都不是 SQL 寫壞，而是**答案的形狀**不對：
問「有幾座」卻列出清單、問「各有幾部」只回一個總數、問「哪些電廠」回了一部機組一列、
問「最大的電廠」回了 22 列。模型被退回之後多半原樣再送（CP-099），所以和 `sql_repair`
一樣，問句說得夠清楚、改法只有一種的，直接在這裡改：

- 問「幾部／幾座／幾筆…」而沒問「各／每／分別」，SQL 卻沒有算出單一數字：外面包一層
  `SELECT COUNT(*)`。數的是「座／種」這類實體時，裡面先 DISTINCT，免得一部機組算一次。
- 問「各／分別」，SQL 卻只對 `IN (?, ?)` 的幾個值算出一個總數：按那個欄位分組。
- 問「哪些／哪幾」而 SELECT 只有會重複的欄位（電廠、縣市、燃料…）：加上 DISTINCT；
  問句沒指定筆數時，小於上限的 LIMIT 放回上限。
- 問「最大的／最早的…」一個東西（沒說幾個、沒問各組），SQL 卻排好序回了一串：LIMIT 1。
- 模型自己加上問句沒提的資料品質條件（`商轉日期精度 = 'day'`、`對齊狀態 = 'matched'`）：拿掉。
  `日期狀態 = 'valid'` 只在查詢沒用到歲修起訖日時拿掉 —— 規則說它是給期間判斷用的。
- 封閉集合欄位填了問句沒說的細項（問「風力」卻寫 `能源別 = '陸域風力'`）：放寬成問句那個詞
  能對到的所有值。

判斷只看問句的字面，不看資料；改過的 SQL 一樣要過 SqlGuard、答案檢查與語意守門。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from sqlglot import exp

# 數「東西」的問法。「幾萬瓩」「幾成」「幾月」不是在數數，所以量詞要明列。
COUNT_QUESTION = re.compile(r"(?:幾|多少)(?:部|座|個|筆|次|天|種|家|間|台|項)")
# 數的是實體而不是資料列：同一座電廠在 v_unit 有好幾列，要先去重。
ENTITY_COUNT_QUESTION = re.compile(r"(?:幾|多少)(?:座|種|家|間)")
# 「平均每部機組」的「每」不是在問各組。
EACH_GROUP = re.compile(r"各|分別|(?<!平均)每")
LISTING_QUESTION = re.compile(r"哪些|哪幾")
SUPERLATIVE = re.compile(r"最(?:大|小|多|少|高|低|早|晚|長|短)")
# 問句自己指定了筆數或名次：「三部」「前五」「第二」「排名」。
_NUMERAL = "[0-9一二三四五六七八九十兩]"
SPECIFIED_RANK = re.compile(rf"[前第]\s*{_NUMERAL}|{_NUMERAL}\s*[部座個筆名家]|排名")

# 會在多列重複出現的欄位：一列是一部機組、一筆歲修或一個月份，這些欄位只是它的屬性。
REPEATING_COLUMNS = {
    "v_unit": {"電廠", "縣市", "燃料", "商轉日期精度"},
    "v_outage": {"電廠", "燃料", "機組名", "原因", "日期狀態", "對齊狀態"},
    "v_re_generation": {"發電站", "縣市", "能源別", "年度", "月份", "數值狀態", "主檔來源"},
    "v_peak": {"機組欄位", "電廠", "粒度", "類別", "容量來源", "日期"},
}

# 資料品質欄位：問句提到這些字才是使用者要的條件。
QUALITY_FILTERS = {
    "商轉日期精度": ("精度", "精確", "只到", "月份"),
    "對齊狀態": ("對齊", "對不到", "對應", "主檔", "歧義"),
    "日期狀態": ("日期狀態", "日期有誤", "日期錯", "無效", "valid"),
}
OUTAGE_PERIOD_COLUMNS = {"開始日期", "結束日期"}


def _tables(tree: exp.Expression) -> set[str]:
    return {table.name for table in tree.find_all(exp.Table)}


def _has_aggregate(select: exp.Select) -> bool:
    return any(item.find(exp.AggFunc) for item in select.expressions)


def _drop_condition(node: exp.Expression) -> bool:
    """Remove one AND-ed condition; False when it is not a plain conjunct."""

    parent = node.parent
    if isinstance(parent, exp.And):
        other = parent.expression if parent.this is node else parent.this
        parent.replace(other)
        return True
    if isinstance(parent, exp.Where):
        parent.pop()
        return True
    return False


def drop_unasked_quality_filters(tree: exp.Select, question: str) -> list[str]:
    dropped: list[str] = []
    uses_period = any(column.name in OUTAGE_PERIOD_COLUMNS for column in tree.find_all(exp.Column))
    for node in list(tree.find_all(exp.EQ)):
        column = node.this
        if not isinstance(column, exp.Column) or column.name not in QUALITY_FILTERS:
            continue
        if any(word in question for word in QUALITY_FILTERS[column.name]):
            continue
        if column.name == "日期狀態" and uses_period:
            continue
        if _drop_condition(node):
            dropped.append(column.name)
    return dropped


def _longest_part_in(value: str, question: str) -> str | None:
    """The longest piece of `value` (2+ characters, shorter than it) that the question says."""

    for size in range(len(value) - 1, 1, -1):
        for start in range(len(value) - size + 1):
            if value[start : start + size] in question:
                return value[start : start + size]
    return None


def widen_unasked_subtypes(
    tree: exp.Select,
    bindings: dict[str, object],
    column_values: Mapping[str, Sequence[str]],
    question: str,
) -> list[str]:
    """`能源別 = '陸域風力'` when the question only said 風力 → `LIKE '%風力'`."""

    notes: list[str] = []
    tables = _tables(tree)
    for node in list(tree.find_all(exp.EQ)):
        column, placeholder = node.this, node.expression
        if not (isinstance(column, exp.Column) and isinstance(placeholder, exp.Placeholder)):
            continue
        value = bindings.get(placeholder.this)
        allowed = next(
            (column_values[key] for t in tables if (key := f"{t}.{column.name}") in column_values),
            None,
        )
        if not isinstance(value, str) or allowed is None:
            continue
        if value not in allowed or value in question:
            continue
        said = _longest_part_in(value, question)
        if said is None:
            continue
        related = [item for item in allowed if said in item]
        if len(related) < 2:
            continue
        if all(item.endswith(said) for item in related):
            pattern = f"%{said}"
        elif all(item.startswith(said) for item in related):
            pattern = f"{said}%"
        else:
            continue
        bindings[placeholder.this] = pattern
        node.replace(exp.Like(this=column.copy(), expression=placeholder.copy()))
        notes.append(f"問句只說「{said}」：「{column.name}」= {value!r} 放寬成 LIKE {pattern!r}")
    return notes


def group_each_listed_value(tree: exp.Select, question: str, max_rows: int) -> str | None:
    """「A 和 B 各有幾部」寫成 `WHERE 電廠 IN (?, ?)` 卻沒分組 → GROUP BY 電廠。

    原本那句通常帶著 `LIMIT 1`（一個總數本來就只有一列），分組後要放回上限，否則只剩第一組。
    """

    if not EACH_GROUP.search(question) or tree.args.get("group") is not None:
        return None
    if not _has_aggregate(tree):
        return None
    where = tree.args.get("where")
    lists = [
        node
        for node in (where.find_all(exp.In) if where is not None else ())
        if isinstance(node.this, exp.Column) and len(node.expressions) >= 2
    ]
    if len(lists) != 1:
        return None
    column = lists[0].this
    tree.group_by(column.copy(), copy=False)
    tree.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
    return column.name


def _selected_columns(select: exp.Select) -> list[exp.Column] | None:
    columns = []
    for item in select.expressions:
        node = item.this if isinstance(item, exp.Alias) else item
        if not isinstance(node, exp.Column):
            return None
        columns.append(node)
    return columns


def dedupe_listing(tree: exp.Select, question: str, max_rows: int) -> list[str]:
    if not LISTING_QUESTION.search(question) or COUNT_QUESTION.search(question):
        return []
    if tree.args.get("group") is not None or _has_aggregate(tree):
        return []
    notes: list[str] = []
    columns = _selected_columns(tree)
    tables = _tables(tree)
    repeating = set().union(*(REPEATING_COLUMNS.get(name, set()) for name in tables))
    if (
        columns
        and len(tables) == 1
        and not tree.args.get("distinct")
        and all(column.name in repeating for column in columns)
    ):
        tree.set("distinct", exp.Distinct())
        notes.append("問「哪些」：加上 DISTINCT")
    limit = tree.args.get("limit")
    if (
        not SUPERLATIVE.search(question)
        and not SPECIFIED_RANK.search(question)
        and limit is not None
        and isinstance(limit.expression, exp.Literal)
        and int(limit.expression.this) < max_rows
    ):
        limit.set("expression", exp.Literal.number(max_rows))
        notes.append(f"問句沒指定筆數：LIMIT 放回 {max_rows}")
    return notes


def keep_only_the_top(tree: exp.Select, question: str) -> str | None:
    """「裝置容量最大的電廠」排好序卻回了一串 → LIMIT 1。"""

    if not SUPERLATIVE.search(question) or SPECIFIED_RANK.search(question):
        return None
    if EACH_GROUP.search(question) or LISTING_QUESTION.search(question):
        return None
    if tree.args.get("order") is None or tree.args.get("offset") is not None:
        return None
    limit = tree.args.get("limit")
    if limit is None or not isinstance(limit.expression, exp.Literal):
        return None
    if int(limit.expression.this) <= 1:
        return None
    limit.set("expression", exp.Literal.number(1))
    return "問「最…」一個：LIMIT 1"


def count_instead_of_list(tree: exp.Select, question: str) -> tuple[exp.Select, str] | None:
    """「有幾座」卻列出清單或分組結果 → `SELECT COUNT(*) FROM (…)`。"""

    if not COUNT_QUESTION.search(question) or EACH_GROUP.search(question):
        return None
    group = tree.args.get("group")
    if _has_aggregate(tree) and group is None:
        return None
    # 有分組時，包一層數的是「組數」。只有組本身就是要數的東西才對：HAVING 篩過的組，
    # 或問「幾座／幾種」。問「重油機組有幾部」寫成 GROUP BY 燃料，裡面的 COUNT 才是答案。
    if (
        group is not None
        and tree.args.get("having") is None
        and not ENTITY_COUNT_QUESTION.search(question)
    ):
        return None
    inner = tree.copy()
    inner.set("limit", None)
    inner.set("order", None)
    note = "問「幾…」：改成數清單有幾列"
    if ENTITY_COUNT_QUESTION.search(question) and inner.args.get("group") is None:
        inner.set("distinct", exp.Distinct())
        note += "（先 DISTINCT）"
    counted = (
        exp.select(exp.alias_(exp.Count(this=exp.Star()), "數量", quoted=True))
        .from_(exp.Subquery(this=inner))
        .limit(1)
    )
    return counted, note


def reshape(
    tree: exp.Select,
    bindings: dict[str, object],
    *,
    question: str,
    column_values: Mapping[str, Sequence[str]],
    max_rows: int,
) -> tuple[exp.Select, list[str]]:
    """Apply every shape fix above; returns the (possibly new) tree and what changed."""

    notes: list[str] = []
    if not question:
        return tree, notes
    dropped = drop_unasked_quality_filters(tree, question)
    if dropped:
        notes.append(f"拿掉問句沒提的條件：{'、'.join(dropped)}")
    notes.extend(widen_unasked_subtypes(tree, bindings, column_values, question))
    grouped = group_each_listed_value(tree, question, max_rows)
    if grouped:
        notes.append(f"問「各」：按「{grouped}」分組")
    notes.extend(dedupe_listing(tree, question, max_rows))
    top = keep_only_the_top(tree, question)
    if top:
        notes.append(top)
    counted = count_instead_of_list(tree, question)
    if counted is not None:
        tree, note = counted
        notes.append(note)
    return tree, notes


__all__ = ["reshape"]
