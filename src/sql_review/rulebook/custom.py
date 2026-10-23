"""自定义规则 DSL（P5）：用声明式配置扩展审查规则，不改引擎代码。

对标横向评测中 CloudDM 的差异化能力（规则可自定义），但走**声明式 JSON/YAML**
而不是 DSL 语言——表达力换可验证性：每条自定义规则编译成 5 类 matcher 的组合，
行为可穷举、可测试，不会有 DSL 解释器的隐藏分支。

matcher 一览（同一条规则内多个 matcher 取 AND）：
- statements:       禁止的语句类型（ddl/insert/update/delete/select/... 或节点类名）
- require_where:    必须带 WHERE 的语句类型（update/delete/...）
- forbid_functions: 禁用的函数名（大小写不敏感，如 SLEEP / GET_LOCK）
- forbid_columns:   禁止出现的列名（大小写不敏感，如 password）
- forbid_patterns:  对原始 SQL 文本做正则匹配（IGNORECASE）

每条规则：
    {"id": "CUS101", "name": "...", "severity": "blocker",
     "suggestion": "...", "why": "...", <matcher>: ...}

诚实性约束：自定义规则产出的 finding 一律 fixable=False（不自动改写 SQL），
呼应 FSE 2026「自动修复是弱环」的实证结论。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from sqlglot import exp

from dbcore.dialect import _kind_of

from sql_review.rulebook.core import SEVERITY_ORDER, _mk
from sql_review.rulebook.registry import Rule, build

# 用户可写的语句类型别名 → 额外等价的节点类名（小写）
_KIND_ALIASES: dict[str, tuple[str, ...]] = {
    "truncate": ("truncatetable",),
    "grant": ("command",),   # GRANT/REVOKE 解析为 Command
}


def load_rules(source: str) -> list[dict]:
    """从 JSON/YAML 文件路径或内联文本加载规则清单。

    按扩展名与内容自动识别格式；返回 dict 列表（compile_rules 的输入）。
    """
    p = Path(source)
    text = p.read_text(encoding="utf-8") if p.is_file() else source
    stripped = text.strip()
    if stripped.startswith("[") or stripped.startswith("{"):
        data = json.loads(stripped)
    else:
        try:
            import yaml  # 可选依赖
        except ImportError as e:  # pragma: no cover
            raise ValueError("解析 YAML 需要 pyyaml：pip install pyyaml（或改用 JSON）") from e
        data = yaml.safe_load(stripped)
    if isinstance(data, dict):
        data = data.get("rules", [])
    if not isinstance(data, list):
        raise ValueError("规则清单必须是列表（或含 rules 键的映射）")
    return data


def _validate_spec(spec: dict) -> None:
    rid = spec.get("id")
    if not rid:
        raise ValueError("自定义规则缺少 id")
    if build().get(rid) is not None:
        raise ValueError(f"自定义规则 id 与内置规则冲突：{rid}")
    sev = spec.get("severity")
    if sev not in SEVERITY_ORDER:
        raise ValueError(f"{rid}: severity 必须是 {sorted(SEVERITY_ORDER)} 之一，得到 {sev!r}")
    matchers = [k for k in ("statements", "require_where", "forbid_functions",
                            "forbid_columns", "forbid_patterns") if spec.get(k)]
    if not matchers:
        raise ValueError(f"{rid}: 至少需要一个 matcher"
                         "（statements / require_where / forbid_functions /"
                         " forbid_columns / forbid_patterns）")
    if "forbid_patterns" in spec:
        for pat in spec["forbid_patterns"]:
            try:
                re.compile(pat)
            except re.error as e:
                raise ValueError(f"{rid}: 非法正则 {pat!r}: {e}") from e


def _stmt_kind(ast) -> str:
    return _kind_of(ast)


def _compile_fn(spec: dict):
    rid = spec["id"]
    sev = spec["severity"]
    suggestion = spec.get("suggestion", "按团队规范调整语句")
    stmts = {s.lower() for s in (spec.get("statements") or [])}
    req_where = {s.lower() for s in (spec.get("require_where") or [])}
    funcs = {f.lower() for f in (spec.get("forbid_functions") or [])}
    cols = {c.lower() for c in (spec.get("forbid_columns") or [])}
    pats = [re.compile(p, re.I) for p in (spec.get("forbid_patterns") or [])]

    def fn(ast, ctx) -> list:
        findings = []
        table = spec.get("table")
        kind = _stmt_kind(ast)
        cls_name = type(ast).__name__.lower()
        # 命中判定：kind（select/ddl/...）或节点类名（truncatetable/...），
        # 再叠加别名展开（用户写 truncate 等价于 truncatetable）
        if stmts:
            expanded = set(stmts)
            for a, aliases in _KIND_ALIASES.items():
                if a in stmts:
                    expanded.update(aliases)
            if kind in expanded or cls_name in expanded:
                findings.append(_mk(rid, sev, f"禁止的语句类型：{kind or cls_name}", suggestion, ast, table))
        if req_where and kind in req_where and ast.args.get("where") is None:
            findings.append(_mk(rid, sev, f"{kind.upper()} 缺少 WHERE 条件（全表影响）",
                                suggestion, ast, table))
        if funcs:
            for node in ast.walk():
                names = {type(node).__name__.lower()}
                nm = getattr(node, "name", None)
                if isinstance(nm, str) and nm:
                    names.add(nm.lower())
                if isinstance(node, (exp.Func, exp.Anonymous)) and names & funcs:
                    hit = sorted(names & funcs)[0]
                    findings.append(_mk(rid, sev, f"禁用函数：{hit.upper()}()", suggestion, node, table))
                    break
        if cols:
            for col in ast.find_all(exp.Column):
                if col.name and col.name.lower() in cols:
                    findings.append(_mk(rid, sev, f"禁止访问列：{col.name}", suggestion, col, table))
                    break
        if pats:
            text = ctx.get("_sql_text", "")
            for pat in pats:
                m = pat.search(text)
                if m:
                    findings.append(_mk(rid, sev, f"命中禁止模式：{pat.pattern}", suggestion, ast, table))
                    break
        return findings

    return fn


def compile_rules(specs: list[dict]) -> list[Rule]:
    """把声明式规则清单编译成 Rule 对象（可直接喂 review_sql(custom_rules=...)）。

    校验先行：id 冲突 / severity 合法 / matcher 非空 / 正则可编译，任何一条
    不合法即抛 ValueError（配置错误宁可启动失败，不要静默漏检）。
    """
    out: list[Rule] = []
    seen: set[str] = set()
    for spec in specs:
        _validate_spec(spec)
        rid = spec["id"]
        if rid in seen:
            raise ValueError(f"自定义规则 id 重复：{rid}")
        seen.add(rid)
        out.append(Rule(
            rule_id=rid,
            name=spec.get("name") or rid,
            category="custom",
            severity=spec["severity"],
            fn=_compile_fn(spec),
            why=spec.get("why", "团队自定义规则"),
            false_positive="自定义规则按团队规范设定，不存在通用误报场景",
            engines=tuple(spec.get("engines") or []),
        ))
    return out
