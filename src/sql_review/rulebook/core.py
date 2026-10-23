"""规则分册共享基础设施。

所有场景分册（select/dml/type/tx/engine/subquery）都从这里取：
严重度序、Finding / ReviewResult 数据类、RuleFn 类型、构造器 _mk、
去重、以及跨分册复用的小工具（_eq_pairs / _literal_of / _NUMERIC_COL_RE）。

设计约束：core 只依赖 dbcore.dialect，不反向依赖任何分册，避免环。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Callable

from sqlglot import exp

from dbcore.dialect import col_of, line_of, normalize_dialect, parse_statements, table_of

SEVERITY_ORDER = {"blocker": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

# 决定「能否上线」的阻断级：仅 blocker。CI 门禁据此判失败。
BLOCKING_SEVERITIES = ("blocker",)

# 数值型列名特征：命中才敢对"列与字符串字面量比较"判 high
_NUMERIC_COL_RE = re.compile(r"(^|_)(id|no|num|count|qty|amount|total|seq|score|coin|codes?)$", re.I)


@dataclass
class Finding:
    rule_id: str
    severity: str
    message: str
    suggestion: str
    line: int | None = None
    table: str | None = None
    col: int | None = None
    auto_fix: str | None = None

    @property
    def fixable(self) -> bool:
        """该问题是否可由确定性规则自动修正（不依赖语义判断）。

        仅当 auto_fix 文本存在时为 True。绝大多数规则返回 False，
        因为 SQL 语义修复风险高（FSE 2026 实证规则修复误修率 5.3%），
        本工具不对此过度承诺——这是对齐评测界「自动修复是弱环」结论的
        产品诚实性选择。
        """
        return self.auto_fix is not None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fixable"] = self.fixable
        return d


@dataclass
class ReviewResult:
    dialect: str
    statement_count: int = 0
    unparsable: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def max_severity(self) -> str | None:
        if not self.findings:
            return None
        return sorted(self.findings, key=lambda f: SEVERITY_ORDER[f.severity])[0].severity

    @property
    def verdict(self) -> str:
        sev = self.max_severity
        return {
            "blocker": "禁止上线",
            "high": "强烈建议修改",
            "medium": "建议修改",
            "low": "可上线",
            "info": "通过",
            None: "通过",
        }[sev]  # type: ignore[index]

    @property
    def blocking_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity in BLOCKING_SEVERITIES]

    @property
    def advisory_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity not in BLOCKING_SEVERITIES]

    @property
    def blocking_count(self) -> int:
        return len(self.blocking_findings)

    @property
    def advisory_count(self) -> int:
        return len(self.advisory_findings)

    @property
    def fixable_count(self) -> int:
        return sum(1 for f in self.findings if f.fixable)

    @property
    def has_blocking(self) -> bool:
        """CI 门禁用：存在任一阻断级 finding 即判失败。"""
        return self.blocking_count > 0

    def to_dict(self) -> dict:
        return {
            "dialect": self.dialect,
            "statement_count": self.statement_count,
            "verdict": self.verdict,
            "max_severity": self.max_severity,
            "summary": {s: sum(1 for f in self.findings if f.severity == s) for s in SEVERITY_ORDER},
            "blocking_count": self.blocking_count,
            "advisory_count": self.advisory_count,
            "fixable_count": self.fixable_count,
            "has_blocking": self.has_blocking,
            "unparsable": self.unparsable,
            "findings": [f.to_dict() for f in self.findings],
        }


RuleFn = Callable[[object, dict], list[Finding]]


def _mk(rule_id: str, severity: str, message: str, suggestion: str,
        node: exp.Expression | None, table: str | None = None,
        auto_fix: str | None = None) -> Finding:
    return Finding(rule_id=rule_id, severity=severity, message=message,
                   suggestion=suggestion, line=line_of(node), col=col_of(node),
                   table=table, auto_fix=auto_fix)


def _dedup_findings(findings: list[Finding]) -> list[Finding]:
    """同一规则在同一行重复报没有意义。"""
    seen: set[tuple] = set()
    out = []
    for f in findings:
        key = (f.rule_id, f.line, f.table)
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out


def _literal_of(node: exp.Expression | None) -> int | None:
    """兼容 sqlglot 各版本的取值：容器节点的 this / expression 都试一遍。"""
    if node is None:
        return None
    for holder in (getattr(node, "expression", None), getattr(node, "this", None)):
        val = getattr(holder, "this", None)
        if val is not None:
            try:
                return int(str(val))
            except (TypeError, ValueError):
                return None
    return None


def _eq_pairs(ast) -> list[list[str]]:
    """找出 WHERE 里跨表的等值条件（a.x = b.y 这种），用于判定"语义上已有连接条件"。"""
    out = []
    for eq in ast.find_all(exp.EQ):
        pair = []
        for side in (eq.this, eq.expression):
            c = side.find(exp.Column) if side is not None else None
            if isinstance(c, exp.Column):
                pair.append(c.table or "")
        if len(pair) == 2 and pair[0] and pair[1] and pair[0] != pair[1]:
            out.append(pair)
    return out


__all__ = [
    "SEVERITY_ORDER", "_NUMERIC_COL_RE", "Finding", "ReviewResult", "RuleFn",
    "_mk", "_dedup_findings", "_literal_of", "_eq_pairs",
    "line_of", "normalize_dialect", "parse_statements", "table_of",
]
