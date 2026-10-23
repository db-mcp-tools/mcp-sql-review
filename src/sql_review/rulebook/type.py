"""规则分册：数据类型与 NULL 语义（TYPE / NULL）。"""

from __future__ import annotations

import re

from sqlglot import exp

from sql_review.rulebook.core import (_NUMERIC_COL_RE, _mk, table_of)
from sql_review.rulebook.heuristics import is_date_like, is_text_like

# 字面量是不是"数字形态"：纯整数或小数（含正负号）。
# `'2026-01-01'`、`'abc'` 都不算 —— 对数值列来说那是类型错误，
# 不是隐式转换，报"加引号导致索引失效"帮不上忙。
_NUM_LITERAL_RE = re.compile(r"^[+-]?\d+(\.\d+)?$")


def rule_implicit_cast(ast, ctx) -> list:
    """数值列与字符串字面量比较 —— 数据库对**列**做 CAST，索引失效。

    误报治理（见 heuristics.py 顶部）：只有三类列会被报

      · 日期时间列（`*_at` / `*_date` / `*_ts` …）→ 直接跳过。
        带引号写日期是标准写法，SQL 没有通用日期字面量。
      · 文本列（`*_name` / `*_code` / `*_no` / `status` …）→ 直接跳过。
        与字符串比较本来就对；`order_no = '12345'` 不是问题。
      · 其余列：字面量是数字形态且列名带数值特征（`*_id` / `amount` / `count`…）
        → high；否则 low（"需确认"，不断言）。
    """
    out: list = []
    where = ast.args.get("where") if isinstance(ast, (exp.Select, exp.Update, exp.Delete)) else None
    if where is None:
        return out
    for node in where.walk():
        if not isinstance(node, (exp.EQ, exp.In, exp.GTE, exp.LTE, exp.GT, exp.LT)):
            continue
        col = node.find(exp.Column)
        lit = node.find(exp.Literal)
        if col is None or lit is None:
            continue
        # 只有字符串字面量才可能是隐式转换
        if not getattr(lit, "is_string", False):
            continue
        raw = getattr(lit, "this", None)
        if not isinstance(raw, str):
            continue

        colname = col.name or ""
        if is_date_like(colname) or is_text_like(colname):
            continue
        numeric_literal = bool(_NUM_LITERAL_RE.match(raw.strip()))
        numeric_name = bool(_NUMERIC_COL_RE.search(colname))
        severity = "high" if (numeric_name and numeric_literal) else "low"
        out.append(_mk("TYPE001", severity,
                       f"列 `{colname}` 与字符串字面量 '{raw}' 比较，"
                       f"若该列是数值类型会触发隐式转换使索引失效",
                       "去掉引号：让字面量类型与列类型一致；"
                       "MySQL/PG 都会对列做 CAST 而不是对常量做 CAST",
                       node, table_of(ast)))
    return out


def rule_negation(ast, ctx) -> list:
    out: list = []
    where = ast.args.get("where") if isinstance(ast, (exp.Select, exp.Update, exp.Delete)) else None
    if where is None:
        return out
    for node in where.walk():
        if isinstance(node, exp.NEQ):
            col = node.find(exp.Column)
            if col is None:
                continue
            out.append(_mk("TYPE002", "low",
                           f"`{col.name} != 值` 这类否定条件通常无法有效利用索引（选择性差）",
                           "如果绝大多数行都满足该否定条件，考虑改写为正向枚举或用覆盖索引",
                           node, table_of(ast)))
            break
        if isinstance(node, exp.Not):
            inner = node.this
            if isinstance(inner, exp.In):
                out.append(_mk("TYPE002", "low",
                               "使用了 NOT IN：优化器通常无法走索引，且子查询返回 NULL 时结果为空集",
                               "改写为 NOT EXISTS，并显式处理 NULL 语义", node, table_of(ast)))
                break
    return out


def rule_not_null_safe(ast, ctx) -> list:
    """NULL 语义陷阱：col <> 'x' 会漏掉 col IS NULL 的行。"""
    out: list = []
    where = ast.args.get("where") if isinstance(ast, (exp.Select, exp.Update, exp.Delete)) else None
    if where is None:
        return out
    for node in where.walk():
        if isinstance(node, exp.NEQ):
            rhs = node.expression
            if isinstance(rhs, exp.Literal):
                col = node.find(exp.Column)
                if col is not None:
                    out.append(_mk("NULL001", "medium",
                                   f"条件 `{col.name} <> ...` 不会包含 `{col.name} IS NULL` 的行",
                                   "如果你希望 NULL 行也被包含，必须显式写 `OR col IS NULL`",
                                   node, table_of(ast)))
                    break
    return out


# 本分册的 type 规则依赖 _NUMERIC_COL_RE
META: dict[str, dict] = {
    "TYPE001": {"name": "隐式类型转换", "category": "type", "severity": "high",
                "why": "数值列与字符串比较，数据库会对列做 CAST，索引直接失效",
                "false_positive": "日期时间列（'2026-01-01' 是标准写法）；"
                                  "文本列（*_name / *_code / *_no / status 等）；"
                                  "非数字字面量（那是类型用错，不是隐式转换）"},
    "TYPE002": {"name": "否定条件", "category": "type", "severity": "low",
                "why": "!= / NOT IN 的选择性通常很差，索引收益有限",
                "false_positive": "列只有极少数值且分布极度倾斜时"},
    "NULL001": {"name": "NULL 语义陷阱", "category": "semantic", "severity": "medium",
                "why": "col <> 'x' 不会匹配 col IS NULL 的行，导致数据静默丢失",
                "false_positive": "明确知道该列 NOT NULL 时可忽略"},
}

RULES: list[tuple[str, object]] = [
    ("TYPE001", rule_implicit_cast),
    ("TYPE002", rule_negation),
    ("NULL001", rule_not_null_safe),
]
