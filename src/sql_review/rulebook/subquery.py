"""规则分册：子查询风险（SUBQ）。

两条规则都只对**相关子查询**报告 —— 非相关子查询（如
`SELECT (SELECT MAX(x) FROM t2) FROM t1`、`WHERE id IN (SELECT oid FROM p)`）
只算一次或被优化器改写成 semi-join / materialized，不会"每行跑一遍"。
按行数放大去报它们，是这套规则里最贵的一类误报。
"""

from __future__ import annotations

from sqlglot import exp

from sql_review.rulebook.core import _mk, table_of
from sql_review.rulebook.heuristics import is_correlated, outer_relations


def _subqueries_in_select_list(ast: exp.Select) -> list[exp.Expression]:
    out: list[exp.Expression] = []
    for e in ast.expressions or []:
        if isinstance(e, exp.Subquery):
            out.append(e)
        elif isinstance(e, exp.Alias) and e.find(exp.Subquery) is not None:
            out.append(e.find(exp.Subquery))
    return out


def rule_select_subquery(ast, ctx) -> list:
    """SELECT 列表里的**相关**子查询 —— 每行执行一次，行数放大即为灾难。"""
    if not isinstance(ast, exp.Select):
        return []
    outer = outer_relations(ast)
    for e in _subqueries_in_select_list(ast):
        if not is_correlated(e, outer):
            continue
        return [_mk("SUBQ001", "high",
                    "SELECT 列表中的相关子查询：外层每行都要执行一次，复杂度 O(结果行数)",
                    "改写为 JOIN 或「先聚合再 LEFT JOIN」的派生表；"
                    "非相关子查询（只算一次）不受此限", e, table_of(ast))]
    return []


def rule_cross_join_subquery(ast, ctx) -> list:
    """WHERE 里的 **相关** IN (子查询)。非相关时优化器能 semi-join，不报。"""
    if not isinstance(ast, exp.Select):
        return []
    d = ctx.get("_dialect", "")
    if d not in ("mysql", "mariadb"):
        return []
    where = ast.args.get("where")
    if where is None:
        return []
    outer = outer_relations(ast)
    for node in where.walk():
        if isinstance(node, exp.In):
            sub = node.find(exp.Subquery)
            if sub is None or not is_correlated(sub, outer):
                continue
            return [_mk("SUBQ002", "medium",
                        "WHERE ... IN (相关子查询)：可能执行成 DEPENDENT SUBQUERY，"
                        "外层每行重跑一次子查询",
                        "改写为 JOIN；或确认 EXPLAIN 里是 MATERIALIZED / FirstMatch "
                        "而非 DEPENDENT SUBQUERY", node, table_of(ast))]
    return []


META: dict[str, dict] = {
    "SUBQ001": {"name": "SELECT 列表中的相关子查询", "category": "subquery", "severity": "high",
                "why": "相关子查询每行执行一次，复杂度随结果行数线性增长",
                "false_positive": "非相关子查询（不引用外层列，例如 (SELECT MAX(x) FROM t2)）；"
                                  "已被优化器改写为单次计算的标量子查询"},
    "SUBQ002": {"name": "IN (相关子查询)", "category": "subquery", "severity": "medium",
                "why": "MySQL 可能执行成 DEPENDENT SUBQUERY，性能极差",
                "false_positive": "非相关子查询（semi-join / MATERIALIZED 生效）；"
                                  "EXPLAIN 显示 FirstMatch 时",
                "engines": ["mysql", "mariadb"]},
}

RULES: list[tuple[str, object]] = [
    ("SUBQ001", rule_select_subquery),
    ("SUBQ002", rule_cross_join_subquery),
]
