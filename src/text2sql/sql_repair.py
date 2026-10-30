"""Mechanical fixes to LLM SQL before the guards see it (CP-097).

`answer_checks` 把錯誤交回模型重寫；但評測的逐次紀錄顯示，本機 qwen3:8b 收到錯誤之後
常常**原樣再送一次**：三次嘗試寫出同一句 `"縣市" = '高雄市'`、同一個 `LIMIT ?`，
題目就這樣耗盡重試。這幾類錯的正確寫法是唯一的、不需要理解題意，所以直接在這裡改好，
不再花一輪模型呼叫：

- `LIMIT ?` 配一個整數參數：SQLite 認，但 SqlGuard 只收整數常數。把參數填回 LIMIT。
- 多送的空字串參數：SQL 沒有對應的 `?`，模型卻送了 `[""]`。全是空白的多餘參數直接拿掉。
- 缺 LIMIT：補上 `LIMIT max_rows`。只會讓結果變少，不會讓查詢範圍變大。
- 封閉集合欄位的 `=` 用了說法而不是值，而且只有一種對法：
  - 資料裡恰好一個值包含在說法裡：`燃料 = '燃煤'` → `'煤'`；
  - 說法是所有相關值的開頭：`縣市 = '高雄市'` → `LIKE '高雄市%'`（v_unit 的縣市含鄉鎮）；
  - 說法是所有相關值的結尾：`能源別 = '風力'` → `LIKE '%風力'`（陸域風力、離岸風力）。
  對法不只一種時不動，交給 `answer_checks` 照舊退回給模型，例如「明潭」同時是「明潭發電廠」的
  開頭與多廠合併欄位的中段 —— 猜錯會重複計算。
- 問句問「縣市」卻在 v_unit 上 `GROUP BY "縣市"`（CP-099）：v_unit 的縣市含鄉鎮，這樣是按鄉鎮分組。
  規則說明寫明要用 `SUBSTR("縣市", 1, 3)`，模型還是照寫；SELECT、GROUP BY、ORDER BY 裡的
  `"縣市"` 一起換掉。問句提到鄉、鎮時不動，那時按鄉鎮分組才是對的。
- 字串字面值（`LIKE '%風力%'`）改成參數：SqlGuard 要求值一律走參數，模型被退回後仍原樣再送。
- GROUP BY 的欄位沒有出現在 SELECT：補在最前面，否則一排數字分不出是哪一組。

只修 LLM 寫的 SQL。修過的 SQL 一樣要通過 SqlGuard、答案檢查與語意守門，
這裡放寬的只是格式，不是安全。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError

from text2sql.llm import GeneratedQuery
from text2sql.scope_guard import _MARKER, ScopeRewriteError, _name_original_placeholders

_INTEGER = re.compile(r"^\d+$")
# 問句提到這些字時，按鄉鎮分組是使用者要的，不改成縣市。
TOWNSHIP_WORDS = ("鄉", "鎮")


@dataclass(frozen=True)
class Repair:
    query: GeneratedQuery
    notes: tuple[str, ...]


def _drop_blank_extras(sql: str, params: Sequence[object]) -> tuple[tuple[object, ...], str | None]:
    expected = sql.count("?")
    if len(params) <= expected:
        return tuple(params), None
    kept = tuple(value for value in params if not (isinstance(value, str) and not value.strip()))
    if len(kept) != expected:
        return tuple(params), None
    return kept, f"拿掉 {len(params) - len(kept)} 個沒有對應 ? 的空白參數"


def _closed_set_fix(value: str, allowed: Sequence[str]) -> tuple[str, str] | None:
    """('eq', 值) 或 ('like', 樣式)；對法不只一種時回 None。"""

    inside = [item for item in allowed if item and item in value]
    if len(inside) == 1:
        return "eq", inside[0]
    related = [item for item in allowed if value in item]
    if not related or inside:
        return None
    if all(item.startswith(value) for item in related):
        return "like", f"{value}%"
    if all(item.endswith(value) for item in related):
        return "like", f"%{value}"
    return None


def _asks_by_county(question: str) -> bool:
    return "縣市" in question and not any(word in question for word in TOWNSHIP_WORDS)


def _county(column: exp.Column) -> exp.Expression:
    return exp.Substring(
        this=column.copy(), start=exp.Literal.number(1), length=exp.Literal.number(3)
    )


def _group_by_county(tree: exp.Select) -> bool:
    """Swap the township-level "縣市" for its first three characters; True when changed."""

    group = tree.args.get("group")
    if group is None or "v_unit" not in {table.name for table in tree.find_all(exp.Table)}:
        return False
    grouped = [item for item in group.expressions if _is_county_column(item)]
    if not grouped:
        return False
    for item in grouped:
        item.replace(_county(item))
    for item in list(tree.expressions):
        if isinstance(item, exp.Alias) and _is_county_column(item.this):
            item.set("this", _county(item.this))
        elif _is_county_column(item):
            item.replace(exp.alias_(_county(item), "縣市", quoted=True))
    order = tree.args.get("order")
    for ordered in order.expressions if order is not None else ():
        if _is_county_column(ordered.this):
            ordered.set("this", _county(ordered.this))
    return True


def _select_missing_group_keys(tree: exp.Select) -> list[str]:
    """Put GROUP BY keys the SELECT left out in front of it; returns their names.

    `SELECT SUM(…) FROM v_unit GROUP BY 縣市` 回 13 個數字，卻沒說哪個數字是哪個縣市。
    """

    group = tree.args.get("group")
    if group is None or any(isinstance(item, exp.Star) for item in tree.expressions):
        return []
    selected = [item.this if isinstance(item, exp.Alias) else item for item in tree.expressions]
    aliases = {item.alias for item in tree.expressions if item.alias}
    added: list[str] = []
    for key in reversed(group.expressions):
        if isinstance(key, exp.Literal) or key in selected:
            continue
        if isinstance(key, exp.Column) and key.name in aliases:
            continue
        columns = list(key.find_all(exp.Column))
        name = columns[0].name if len(columns) == 1 else key.sql(dialect="sqlite")
        item = (
            key.copy() if isinstance(key, exp.Column) else exp.alias_(key.copy(), name, quoted=True)
        )
        tree.set("expressions", [item, *tree.expressions])
        added.insert(0, name)
    return added


def _is_county_column(node: exp.Expression) -> bool:
    return isinstance(node, exp.Column) and node.name == "縣市"


def repair_llm_query(
    sql: str,
    params: Sequence[object],
    *,
    column_values: Mapping[str, Sequence[str]],
    max_rows: int,
    question: str = "",
) -> Repair:
    """Apply the fixes above; anything unparsable is returned untouched for SqlGuard to reject."""

    params, blank_note = _drop_blank_extras(sql, params)
    notes = [blank_note] if blank_note else []
    try:
        tree = parse_one(sql, read="sqlite")
        bindings = _name_original_placeholders(tree, params)
    except (ParseError, ScopeRewriteError):
        return Repair(GeneratedQuery(sql, tuple(params)), tuple(notes))
    if not isinstance(tree, exp.Select):
        return Repair(GeneratedQuery(sql, tuple(params)), tuple(notes))

    limit = tree.args.get("limit")
    if limit is None:
        tree.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
        notes.append(f"補上 LIMIT {max_rows}")
    elif isinstance(limit.expression, exp.Placeholder):
        name = limit.expression.this
        value = bindings.get(name)
        text = str(value).strip() if isinstance(value, int | str) else ""
        if _INTEGER.match(text) and not isinstance(value, bool):
            limit.set("expression", exp.Literal.number(int(text)))
            del bindings[name]
            notes.append(f"LIMIT ? 改成 LIMIT {int(text)}")

    literals = [node for node in tree.find_all(exp.Literal) if node.is_string]
    for index, node in enumerate(literals):
        name = f"__l{index}"
        bindings[name] = node.this
        node.replace(exp.Placeholder(this=name))
    if literals:
        notes.append(f"{len(literals)} 個字串字面值改成參數")

    tables = [table.name for table in tree.find_all(exp.Table)]
    for node in list(tree.find_all(exp.EQ)):
        column, placeholder = node.this, node.expression
        if not (isinstance(column, exp.Column) and isinstance(placeholder, exp.Placeholder)):
            continue
        value = bindings.get(placeholder.this)
        allowed = next(
            (column_values[key] for t in tables if (key := f"{t}.{column.name}") in column_values),
            None,
        )
        if not isinstance(value, str) or allowed is None or value in allowed:
            continue
        fix = _closed_set_fix(value, allowed)
        if fix is None:
            continue
        kind, replacement = fix
        bindings[placeholder.this] = replacement
        if kind == "like":
            node.replace(exp.Like(this=column.copy(), expression=placeholder.copy()))
            notes.append(f"「{column.name}」= {value!r} 改成 LIKE {replacement!r}")
        else:
            notes.append(f"「{column.name}」的 {value!r} 改成資料裡的 {replacement!r}")

    if _asks_by_county(question) and _group_by_county(tree):
        notes.append('按縣市分組：GROUP BY "縣市" 改成 SUBSTR("縣市", 1, 3)')
    missing_keys = _select_missing_group_keys(tree)
    if missing_keys:
        notes.append(f"SELECT 補上分組欄位：{'、'.join(missing_keys)}")

    if not notes:
        return Repair(GeneratedQuery(sql, tuple(params)), ())
    rendered = tree.sql(dialect="sqlite")
    ordered = _MARKER.findall(rendered)
    return Repair(
        GeneratedQuery(_MARKER.sub("?", rendered), tuple(bindings[name] for name in ordered)),
        tuple(notes),
    )


__all__ = ["Repair", "repair_llm_query"]
