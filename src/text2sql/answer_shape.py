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

CP-103 再補五種（同樣照已看過題庫的錯誤設計，成績只看 CP-103 新寫的 fresh2）：

- 問「幾座電廠／幾種燃料」，模型自己寫了 `COUNT(*)`：改成 `COUNT(DISTINCT 電廠)`。
- 問「第三多／第二大」，SQL 卻是 `LIMIT 1` 沒有 OFFSET：補上 `OFFSET 名次-1`。
- 問「每座電廠／各燃料…」，SQL 有聚合卻沒分組（常見的是只查了一座）：按那個欄位分組；
  問「每座電廠最近／最早…」卻排序取一列：改成每組 MAX／MIN。
- 問「同時有 A 和 B」，SQL 寫成 `燃料 = A AND 燃料 = B`（永遠 0 列）：改成
  `燃料 IN (A, B) … HAVING COUNT(DISTINCT 燃料) = 2`。
- 名稱欄位（電廠、縣市、發電站…）的條件值在問句裡一個字也沒出現：那是模型編的，拿掉。

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
SUPERLATIVE = re.compile(r"最(?:大|小|多|少|高|低|早|晚|長|短|近|新)")
# 問句自己指定了筆數或名次：「三部」「前五」「第二」「排名」。
_NUMERAL = "[0-9一二三四五六七八九十兩]"
SPECIFIED_RANK = re.compile(rf"[前第]\s*{_NUMERAL}|{_NUMERAL}\s*[部座個筆名家]|排名")

_RANK_DIGITS = {"二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
# 「第三多」「第二大」：取排序後的第 N 名，一列。「第一」就是最…，交給 LIMIT 1。
NTH_RANK = re.compile(
    r"第\s*([2-9]|10|[二三四五六七八九十])\s*(?:多|大|小|少|高|低|早|晚|長|短|名)"
)
# 數的量詞對到哪些名詞：問「幾座」數的是電廠或場站，「幾種」數的是燃料或能源別。
ENTITY_NOUNS = {
    "座": {"電廠": "電廠", "場站": "發電站", "發電站": "發電站"},
    "家": {"電廠": "電廠"},
    "間": {"電廠": "電廠"},
    "種": {"燃料": "燃料", "能源別": "能源別"},
}
# 「每座電廠」「各燃料」：要按哪個欄位分組。縣市另有 SUBSTR 的分組規則（sql_repair），不在這裡。
EACH_NOUN = re.compile(
    r"(?<!平均)(?:每|各)一?(?:座|個|種|部)?(?:風力|太陽能)?(電廠|場站|發電站|燃料|能源別)"
)
EACH_NOUN_COLUMNS = {
    "電廠": "電廠",
    "場站": "發電站",
    "發電站": "發電站",
    "燃料": "燃料",
    "能源別": "能源別",
}
BOTH_QUESTION = re.compile(r"同時|既.+也|都有")
# 名稱欄位：值是專有名詞，問句沒提到就是模型編的。拿掉通用字之後，任兩個連續字出現在問句就算有提。
NAME_COLUMNS = {"電廠", "縣市", "發電站", "機組名", "機組欄位"}
_GENERIC_NAME_PARTS = re.compile(r"發電廠|電廠|分廠|發電站|發電|風力|太陽光電|[%#＃()（）\s]")

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


def _limit_value(tree: exp.Select) -> int | None:
    limit = tree.args.get("limit")
    if limit is None or not isinstance(limit.expression, exp.Literal):
        return None
    return int(limit.expression.this)


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


def _named_in(value: str, question: str) -> bool:
    core = _GENERIC_NAME_PARTS.sub("", value)
    if len(core) < 2:
        return True
    return any(core[start : start + 2] in question for start in range(len(core) - 1))


def drop_unasked_names(
    tree: exp.Select,
    bindings: dict[str, object],
    column_values: Mapping[str, Sequence[str]],
    question: str,
) -> list[str]:
    """`電廠 = '卓蘭發電廠'` when the question never said 卓蘭 → drop the condition.

    只拿掉資料裡**真的有**的值：那是模型從欄位清單抄來、問句沒問的。資料裡沒有的值
    （例如把「林口」填進縣市）交給答案檢查退回重寫，拿掉反而會默默查成全部。
    """

    dropped: list[str] = []
    where = tree.args.get("where")
    if where is None:
        return dropped
    tables = _tables(tree)
    for node in list(where.find_all(exp.EQ, exp.Like)):
        column, placeholder = node.this, node.expression
        if not (isinstance(column, exp.Column) and isinstance(placeholder, exp.Placeholder)):
            continue
        if column.name not in NAME_COLUMNS or node.parent_select is not tree:
            continue
        value = bindings.get(placeholder.this)
        if not isinstance(value, str) or _named_in(value, question):
            continue
        allowed = next(
            (column_values[key] for t in tables if (key := f"{t}.{column.name}") in column_values),
            (),
        )
        core = value.strip("%")
        if not any(item == core or item.startswith(core) for item in allowed):
            continue
        if _drop_condition(node):
            del bindings[placeholder.this]
            dropped.append(f"{column.name}={value!r}")
    return dropped


def group_by_each_noun(tree: exp.Select, question: str, max_rows: int) -> str | None:
    """「每座電廠的機組數」卻只算一個總數（常常還多了一個編出來的電廠）→ GROUP BY 電廠。"""

    match = EACH_NOUN.search(question)
    if match is None or tree.args.get("group") is not None or not _has_aggregate(tree):
        return None
    tables = _tables(tree)
    column = EACH_NOUN_COLUMNS[match.group(1)]
    if len(tables) != 1 or column not in REPEATING_COLUMNS.get(next(iter(tables)), set()):
        return None
    tree.group_by(exp.column(column, quoted=True), copy=False)
    tree.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
    return column


def extreme_per_group(tree: exp.Select, question: str, max_rows: int) -> str | None:
    """「每座電廠最近一次商轉是哪一天」卻排序取了一列 → 每組 MAX／MIN。"""

    match = EACH_NOUN.search(question)
    if match is None or not SUPERLATIVE.search(question):
        return None
    if tree.args.get("group") is not None or _has_aggregate(tree):
        return None
    order = tree.args.get("order")
    columns = _selected_columns(tree)
    if order is None or len(order.expressions) != 1 or columns is None or len(columns) != 2:
        return None
    key = EACH_NOUN_COLUMNS[match.group(1)]
    ordered = order.expressions[0]
    names = [column.name for column in columns]
    if key not in names or not isinstance(ordered.this, exp.Column):
        return None
    value = columns[1 - names.index(key)]
    if ordered.this.name != value.name:
        return None
    function = exp.Max if ordered.args.get("desc") else exp.Min
    item = exp.alias_(function(this=value.copy()), value.name, quoted=True)
    tree.set("expressions", [exp.column(key, quoted=True), item])
    tree.group_by(exp.column(key, quoted=True), copy=False)
    tree.set("order", None)
    tree.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
    return f"問「每…最…」：改成每個「{key}」取 {function.__name__.upper()}(「{value.name}」)"


def both_values(tree: exp.Select, question: str, max_rows: int) -> str | None:
    """「同時有重油和輕柴油的電廠」寫成 `燃料 = ? AND 燃料 = ?` → IN + HAVING COUNT(DISTINCT)。"""

    if not BOTH_QUESTION.search(question) or tree.args.get("group") is not None:
        return None
    where = tree.args.get("where")
    columns = _selected_columns(tree)
    if where is None or not columns or _has_aggregate(tree):
        return None
    by_column: dict[str, list[exp.EQ]] = {}
    for node in where.find_all(exp.EQ):
        if (
            isinstance(node.this, exp.Column)
            and isinstance(node.expression, exp.Placeholder)
            and isinstance(node.parent, exp.And | exp.Where)
        ):
            by_column.setdefault(node.this.name, []).append(node)
    pairs = [nodes for nodes in by_column.values() if len(nodes) >= 2]
    if len(pairs) != 1:
        return None
    nodes = pairs[0]
    column = nodes[0].this
    if column.name in {selected.name for selected in columns}:
        return None
    values = [node.expression.copy() for node in nodes]
    for node in nodes[1:]:
        _drop_condition(node)
    nodes[0].replace(exp.In(this=column.copy(), expressions=values))
    tree.set("distinct", None)
    tree.group_by(*(selected.copy() for selected in columns), copy=False)
    distinct = exp.Count(this=exp.Distinct(expressions=[column.copy()]))
    tree.having(exp.EQ(this=distinct, expression=exp.Literal.number(len(nodes))), copy=False)
    if _limit_value(tree) == 1:
        tree.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
    return f"問「同時有」：「{column.name}」的 {len(nodes)} 個條件改成 IN + HAVING COUNT(DISTINCT)"


def nth_place(tree: exp.Select, question: str) -> str | None:
    """「第三多的電廠」排好序卻取了第一名 → LIMIT 1 OFFSET 2。"""

    match = NTH_RANK.search(question)
    if match is None or EACH_GROUP.search(question) or LISTING_QUESTION.search(question):
        return None
    if tree.args.get("order") is None or tree.args.get("offset") is not None:
        return None
    text = match.group(1)
    rank = int(text) if text.isdigit() else _RANK_DIGITS[text]
    if _limit_value(tree) not in {1, rank}:
        return None
    tree.set("limit", exp.Limit(expression=exp.Literal.number(1)))
    tree.set("offset", exp.Offset(expression=exp.Literal.number(rank - 1)))
    return f"問第 {rank} 名：LIMIT 1 OFFSET {rank - 1}"


def count_distinct_entities(tree: exp.Select, question: str) -> str | None:
    """「南投縣有幾座電廠」寫成 `COUNT(*)` 數到的是機組 → `COUNT(DISTINCT 電廠)`。"""

    match = ENTITY_COUNT_QUESTION.search(question)
    if match is None or EACH_GROUP.search(question) or tree.args.get("group") is not None:
        return None
    tables = _tables(tree)
    if len(tree.expressions) != 1 or len(tables) != 1:
        return None
    item = tree.expressions[0]
    count = item.this if isinstance(item, exp.Alias) else item
    if not isinstance(count, exp.Count) or isinstance(count.this, exp.Distinct):
        return None
    measure = match.group(0)[-1]
    repeating = REPEATING_COLUMNS.get(next(iter(tables)), set())
    candidates = {
        column
        for noun, column in ENTITY_NOUNS.get(measure, {}).items()
        if noun in question and column in repeating
    }
    if len(candidates) != 1:
        return None
    column = candidates.pop()
    count.set("this", exp.Distinct(expressions=[exp.column(column, quoted=True)]))
    return f"問「幾{measure}」：COUNT 改成 COUNT(DISTINCT 「{column}」)"


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
    names = drop_unasked_names(tree, bindings, column_values, question)
    if names:
        notes.append(f"拿掉問句沒提的名稱：{'、'.join(names)}")
    notes.extend(widen_unasked_subtypes(tree, bindings, column_values, question))
    grouped = group_each_listed_value(tree, question, max_rows) or group_by_each_noun(
        tree, question, max_rows
    )
    if grouped:
        notes.append(f"問「各」：按「{grouped}」分組")
    for fix in (extreme_per_group, both_values):
        note = fix(tree, question, max_rows)
        if note:
            notes.append(note)
    notes.extend(dedupe_listing(tree, question, max_rows))
    for fix in (nth_place, keep_only_the_top, count_distinct_entities):
        note = fix(tree, question)
        if note:
            notes.append(note)
    counted = count_instead_of_list(tree, question)
    if counted is not None:
        tree, note = counted
        notes.append(note)
    return tree, notes


__all__ = ["reshape"]
