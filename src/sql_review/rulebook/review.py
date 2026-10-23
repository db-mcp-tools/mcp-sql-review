"""可配置的 SQL 静态体检入口。

在保持旧行为（profile=default 时与拆分前逐条一致）的前提下，新增按
profile / disable / enable / only_categories / only_severities 的启停能力。
"""

from __future__ import annotations

from typing import Any

from sql_review.rulebook.core import (_dedup_findings, normalize_dialect, parse_statements)
from sql_review.rulebook.profiles import resolve
from sql_review.rulebook.registry import Rule, RuleRegistry, build


def review_sql(sql: str, dialect: str | None = None, *,
               profile: str | dict[str, Any] = "default",
               disable: list[str] | None = None,
               enable: list[str] | None = None,
               only_categories: list[str] | None = None,
               only_severities: list[str] | None = None,
               custom_rules: list[dict] | list[Rule] | None = None,
               **opts) -> Any:
    """对一段 SQL（可多句）做静态体检。

    参数
    ----
    profile: 命名配置名（"default"/"lenient"/"ci-gate"/...）或自定义 dict。
    disable: 本次额外禁用的规则 ID 列表（在 profile 之上叠加）。
    enable:  本次强制启用的规则 ID 列表（优先级最高）。
    only_categories / only_severities: 临时把生效集收窄到指定分类/严重度。
    custom_rules: 自定义规则（P5 DSL）。传 dict 列表时自动 compile_rules 校验编译，
        传 Rule 对象列表则原样使用；与内置规则一起执行，id 冲突会直接报错。
    **opts: 透传给规则上下文，如 partition_keys / deep_offset_threshold / table_rows。
    """
    from sql_review.rulebook.core import Finding, ReviewResult  # 延迟避免环

    d = normalize_dialect(dialect)
    result = ReviewResult(dialect=d)
    reg: RuleRegistry = build()

    prof = resolve(profile) if isinstance(profile, str) else (profile or {})
    # profile 里若已含 categories/severities，与显式参数合并
    cats = list(only_categories or prof.get("categories") or [])
    sevs = list(only_severities or prof.get("severities") or [])
    inc = list(enable or prof.get("include") or [])
    exc = list(disable or prof.get("exclude") or [])

    active: list[Rule] = reg.filtered(dialect=d, categories=cats or None,
                                      severities=sevs or None,
                                      include=inc or None, exclude=exc or None)

    if custom_rules:
        from sql_review.rulebook.custom import compile_rules
        extra = [r for r in custom_rules if isinstance(r, Rule)]
        dicts = [r for r in custom_rules if isinstance(r, dict)]
        if dicts:
            extra.extend(compile_rules(dicts))
        # 自定义规则不受 categories/severities 收窄影响（团队显式给出的就是生效集），
        # 但 engines 方言限定照常生效：限定 mysql 的规则不该在 postgres 上触发
        for r in extra:
            if r.engines and d not in [e.lower() for e in r.engines]:
                continue
            active = list(active) + [r]

    stmts = parse_statements(sql, d)
    result.statement_count = len(stmts)
    ctx = dict(opts)
    ctx["_dialect"] = d

    for stmt in stmts:
        ctx["_sql_text"] = stmt.sql
        if not stmt.parsable:
            result.unparsable.append(stmt.sql[:200])
            result.findings.append(
                Finding(
                    rule_id="PARSE000",
                    severity="info",
                    message="该语句无法被解析器识别，跳过静态检查",
                    suggestion="人工复核；若确为合法方言写法，请提 issue 补充方言支持",
                )
            )
            continue
        for rule in active:
            result.findings.extend(rule.fn(stmt.ast, ctx))

    result.findings = _dedup_findings(result.findings)
    result.findings.sort(key=lambda f: _severity_key(f.severity))
    return result


def _severity_key(sev: str) -> int:
    from sql_review.rulebook.core import SEVERITY_ORDER
    return SEVERITY_ORDER.get(sev, 9)


# -- 兼容旧 sql_review.rules 的 list_rules / explain_rule / rule_categories -----


def list_rules(category: str | None = None, severity: str | None = None,
               dialect: str | None = None) -> list[dict]:
    reg = build()
    meta = reg.meta_map()
    out = []
    for rid, m in meta.items():
        if category and m.get("category") != category:
            continue
        if severity and m.get("severity") != severity:
            continue
        if dialect:
            engines = m.get("engines") or []
            cat = m.get("category")
            if cat in ("postgres", "gaussdb", "kingbase") and dialect.lower() not in \
                    [e.lower() for e in engines] and dialect.lower() not in ("postgres", "gaussdb", "kingbase"):
                continue
            if cat == "mysql" and dialect.lower() not in [e.lower() for e in engines] and \
                    dialect.lower() not in ("mysql", "mariadb", "oceanbase", "tidb"):
                continue
        out.append(m)
    return sorted(out, key=lambda r: (_severity_key(r["severity"]), r["rule_id"]))


def explain_rule(rule_id: str) -> dict | None:
    reg = build()
    r = reg.get(rule_id)
    if r is not None:
        return {"rule_id": r.rule_id, "name": r.name, "category": r.category,
                "severity": r.severity, "why": r.why, "false_positive": r.false_positive,
                "engines": list(r.engines)}
    m = reg.meta_map().get(rule_id.upper())
    return m


def rule_categories() -> list[str]:
    reg = build()
    cats = reg.categories()
    if "parse" not in cats:
        cats.append("parse")
    return sorted(cats)
