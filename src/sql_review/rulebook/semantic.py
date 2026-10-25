"""语义校验层（P4：评测驱动优化）。

评测结论（SQLFlex arXiv 2603.16155 / FSE 2026）提示两条铁律：
1. 纯规则 lint 只查「形态」，漏掉「语义」层面的错误（结果是否正确、是否符合作者意图）。
2. 纯 LLM lint 精确率仅 ~0.307（误报极高），**绝不能当卡口**——否则 CI 会被噪声淹没。

因此本层分两级，默认**都不阻断**（severity 最高 low，绝不为 blocker）：
- 确定性语义检查（SEM*）：纯 AST、零密钥、可穷举、可测试，覆盖形态规则触及不到的
  语义陷阱（如 `= NULL` 恒假、LIMIT 无 ORDER BY 顺序未定义、BETWEEN 日期边界 off-by-one）。
- 可选 LLM 语义复核（AI*）：仅当显式配置了 provider 才跑，产出一律 advisory(info)，
  source 标记为 llm，绝不进入 CI 门禁的阻断判定；即便网络/解析失败也只降级为「无建议」。

设计约束（对齐 docs/ARCHITECTURE.md §5.1 误报治理）：
- 默认 `review_sql(semantic=False)` 完全不触发本层，保证 P0 精度门禁 F1 不变。
- SEM* 只在「判得出这大概率是错的」时才报；把握低的语义问题交给 LLM 层（仍仅建议）。
- 本模块只依赖 dbcore.dialect 与 sqlglot，不反向依赖任何其它分册。
"""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta

from sqlglot import exp

from dbcore.dialect import normalize_dialect, parse_statements
from sql_review.rulebook.core import Finding, _mk


# ---------------------------------------------------------------------------
# 1. 确定性语义检查（SEM*）—— 不需要 LLM，永不阻断
# ---------------------------------------------------------------------------

_DATE_LIT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$")


def _sem_eq_null(ast, ctx) -> list[Finding]:
    """SEM001：`col = NULL` / `col <> NULL` 用比较运算符判 NULL，永远得不到预期结果。

    误报治理：这是「判得出错」的写法——NULL 不参与任何比较（结果恒为 unknown），
    所以 `= NULL` 永远筛选不到行、`<> NULL` 也永远筛选不到行。无合法使用场景。
    """
    out: list[Finding] = []
    for node in ast.find_all(exp.EQ):
        if isinstance(node.expression, exp.Null) or isinstance(node.this, exp.Null):
            col = node.find(exp.Column)
            out.append(_mk("SEM001", "low",
                           "`= NULL` 永远为假（NULL 不等于任何值，包括它自己）；"
                           "判空必须用 `IS NULL`",
                           "把 `col = NULL` 改为 `col IS NULL`",
                           node if col is None else col, source="semantic"))
    for node in ast.find_all(exp.NEQ):
        if isinstance(node.expression, exp.Null) or isinstance(node.this, exp.Null):
            col = node.find(exp.Column)
            out.append(_mk("SEM001", "low",
                           "`<> NULL` 的结果不符合直觉（NULL 不参与比较，结果仍是 unknown，"
                           "WHERE 中恒不为真）；判非空请用 `IS NOT NULL`",
                           "把 `col <> NULL` 改为 `col IS NOT NULL`",
                           node if col is None else col, source="semantic"))
    return out


def _sem_between_datetime(ast, ctx) -> list[Finding]:
    """SEM003：BETWEEN 两端是日期/时间字面量 —— 右边界只到当天 00:00:00，易漏掉当天数据。

    典型 off-by-one：BETWEEN '2026-01-01' AND '2026-01-31' 实际只含 31 号 00:00:00 整点，
    31 号其余时段全部漏掉。改用半开区间 `< 下一天` 才是闭合正确的写法。

    误报治理：仅当**上界是纯日期**（缺时分秒）时才报；若上界已显式带 `23:59:59`
    等精确时刻，视为有意写法，不报。
    """
    out: list[Finding] = []
    for b in ast.find_all(exp.Between):
        low = b.args.get("low")
        high = b.args.get("high")
        if not (isinstance(low, exp.Literal) and isinstance(high, exp.Literal)):
            continue
        if not (low.is_string and high.is_string):
            continue
        ls, hs = low.this, high.this
        if _DATE_LIT_RE.match(ls) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", hs):
            col_node = b.this if isinstance(b.this, exp.Column) else b
            out.append(_mk("SEM003", "low",
                           f"BETWEEN '{ls}' AND '{hs}' 的右边界只到 '{hs}' 00:00:00，"
                           "会漏掉当天其余时段的数据（典型 off-by-one）",
                           f"改用半开区间：{col_node.sql()} >= '{ls}' "
                           f"AND {col_node.sql()} < '{_next_day(hs)}'",
                           col_node, source="semantic"))
    return out


def _sem_union_distinct(ast, ctx) -> list[Finding]:
    """SEM004：UNION（默认去重）而非 UNION ALL —— 无去重需求时多一次排序/去重，还可能意外丢行。"""
    out: list[Finding] = []
    if isinstance(ast, exp.Union) and ast.args.get("distinct") is True:
        out.append(_mk("SEM004", "info",
                       "使用 UNION（默认去重）会额外做排序+去重；若两侧不会重复或无去重需求，"
                       "应改用 UNION ALL 以减少开销，并避免去重意外合并本应保留的重复行",
                       "如无需去重，把 `UNION` 改为 `UNION ALL`",
                       ast, source="semantic"))
    return out


def _sem_having_no_group(ast, ctx) -> list[Finding]:
    """SEM005：HAVING 没有 GROUP BY —— 退化为「整张表聚合后的单行过滤」，多半是忘了分组。"""
    out: list[Finding] = []
    if not isinstance(ast, exp.Select):
        return out
    if ast.args.get("having") is not None and ast.args.get("group") is None:
        out.append(_mk("SEM005", "low",
                       "HAVING 缺少 GROUP BY：此时 HAVING 作用于「整张表聚合后的单行」，"
                       "通常意味着忘记写 GROUP BY；或本意是对明细行过滤（应改用 WHERE）",
                       "若要对分组后结果过滤 → 补 GROUP BY；若要对明细行过滤 → 改用 WHERE",
                       ast, source="semantic"))
    return out


DETERMINISTIC_CHECKS = [
    _sem_eq_null,
    _sem_between_datetime,
    _sem_union_distinct,
    _sem_having_no_group,
]

SEMANTIC_META = {
    "SEM001": {"name": "NULL 比较误用", "category": "semantic", "severity": "low",
               "why": "`= NULL` / `<> NULL` 永远得不到预期真假值（NULL 不参与比较），是经典语义陷阱",
               "false_positive": "无：只要出现 `= NULL` / `<> NULL` 即为写法错误（判空必须用 IS NULL）"},
    "SEM003": {"name": "BETWEEN 日期边界 off-by-one", "category": "semantic", "severity": "low",
               "why": "BETWEEN 右边界是日期字面量时只到当天 00:00:00，漏掉当天其余时段",
               "false_positive": "若右边界本就是带时分秒的精确时刻（如 '2026-01-31 23:59:59'）则不报"},
    "SEM004": {"name": "UNION 默认去重", "category": "semantic", "severity": "info",
               "why": "无去重需求时用 UNION 会多一次排序去重，且可能意外合并重复行",
               "false_positive": "确实需要去重语义时不应改"},
    "SEM005": {"name": "HAVING 无 GROUP BY", "category": "semantic", "severity": "low",
               "why": "无 GROUP BY 的 HAVING 作用于整表聚合单行，多半是忘了分组",
               "false_positive": "确实想对「整表聚合后单行」做过滤（如 HAVING COUNT(*)>0）时合理"},
}


def _next_day(s: str) -> str:
    s2 = s.strip()
    try:
        if len(s2) == 10:
            return (date.fromisoformat(s2) + timedelta(days=1)).isoformat()
        return (datetime.fromisoformat(s2) + timedelta(seconds=1)).isoformat(sep=" ")
    except ValueError:
        return s2


def run_deterministic(ast, ctx) -> list[Finding]:
    """对单条语句跑全部确定性语义检查。"""
    out: list[Finding] = []
    for chk in DETERMINISTIC_CHECKS:
        out.extend(chk(ast, ctx))
    return out


# ---------------------------------------------------------------------------
# 2. 可选 LLM 语义复核（AI*）—— 仅建议，永不阻断，无配置时干净跳过
# ---------------------------------------------------------------------------


class LLMSemanticProvider(ABC):
    """LLM 语义复核 provider 接口。实现方负责把 SQL 送模型、解析、返回 Finding 列表。"""

    name = "base"

    @abstractmethod
    def review(self, sql: str, dialect: str, prior: list[Finding]) -> list[Finding]:
        """返回 LLM 发现的问题；severity 一律视为 info（调用方也会强制 clamp）。

        prior: 已由确定性检查产出的 finding，供模型避免重复报告。
        """
        raise NotImplementedError


class NoProvider(LLMSemanticProvider):
    """默认 provider：不做任何 LLM 调用（零密钥即可运行）。"""

    name = "none"

    def review(self, sql: str, dialect: str, prior: list[Finding]) -> list[Finding]:
        return []


class StubProvider(LLMSemanticProvider):
    """测试用 provider：返回预设 findings，不联网。"""

    name = "stub"

    def __init__(self, findings: list[Finding] | None = None) -> None:
        self._findings = findings or []

    def review(self, sql: str, dialect: str, prior: list[Finding]) -> list[Finding]:
        return list(self._findings)


class OpenAIProvider(LLMSemanticProvider):
    """OpenAI 兼容provider（OpenAI 官方 / DeepSeek / 通义 / 本地 llama 等均可）。

    延迟导入 openai：未安装时构造即报错，绝不在导入期强制依赖。
    """

    name = "openai"

    def __init__(self, model: str | None = None, api_key: str | None = None,
                 base_url: str | None = None) -> None:
        try:
            from openai import OpenAI  # 可选依赖，延迟导入
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "使用 LLM 语义复核需要安装 openai：pip install openai；"
                "不想装则保持默认（仅确定性语义检查，无需密钥）") from exc
        self.model = model or os.environ.get("DBT_LLM_MODEL", "gpt-4o-mini")
        self.client = OpenAI(api_key=api_key or os.environ.get("DBT_LLM_API_KEY"),
                             base_url=base_url or os.environ.get("DBT_LLM_BASE_URL"))

    def review(self, sql: str, dialect: str, prior: list[Finding]) -> list[Finding]:
        prior_s = "\n".join(f"- [{f.rule_id}] {f.message}" for f in prior) or "（无）"
        system = (
            "你是 SQL 语义复核助手。给定一段 SQL 与方言，找出「语法正确但语义上大概率错误」"
            "的问题（结果不符合作者意图），例如：逻辑恒真/恒假条件、聚合语义误解、"
            "JOIN 条件写错导致笛卡尔积或漏关联、子查询相关性错误、分区/时间范围边界写错等。"
            "只报你有较高把握的；把握低的不要报。不要重复下面已由规则引擎报告的条目。"
            "只输出 JSON 对象，形如 {\"findings\": [{\"message\":\"...\",\"suggestion\":\"...\"}]}；"
            "severity 一律视为 info（仅建议）。不要输出任何额外文字。"
        )
        user = f"方言: {dialect}\n已由规则引擎报告:\n{prior_s}\n\nSQL:\n{sql}"
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                temperature=0,
                response_format={"type": "json_object"},
            )
            content = resp.choices[0].message.content or "{}"
            data = json.loads(content)
            items = data.get("findings") if isinstance(data, dict) else data
            if not isinstance(items, list):
                items = []
        except Exception as exc:  # 网络/解析失败：降级为「无 LLM 建议」，绝不影响主流程
            return [Finding(rule_id="AI000", severity="info",
                            message=f"LLM 语义复核不可用（已跳过，不影响其它结论）：{exc}",
                            suggestion="检查 DBT_LLM_* 配置或网络连通性", source="llm")]
        out: list[Finding] = []
        for i, it in enumerate(items[:10], start=1):
            if not isinstance(it, dict):
                continue
            out.append(Finding(
                rule_id=f"AI{i:03d}", severity="info",
                message=str(it.get("message", ""))[:500],
                suggestion=str(it.get("suggestion", ""))[:500],
                source="llm",
            ))
        return out


def get_default_provider() -> LLMSemanticProvider:
    """按环境变量 DBT_LLM_PROVIDER 选择 provider；默认 none（零密钥）。"""
    which = os.environ.get("DBT_LLM_PROVIDER", "none").lower()
    if which in ("none", "", "off", "false"):
        return NoProvider()
    if which in ("openai", "openai-compatible", "deepseek", "qwen"):
        return OpenAIProvider()
    raise ValueError(f"未知的 DBT_LLM_PROVIDER={which!r}（支持 none / openai 等 OpenAI 兼容值）")


# ---------------------------------------------------------------------------
# 3. 编排：确定性 + （可选）LLM
# ---------------------------------------------------------------------------


def semantic_review(sql: str, dialect: str | None = None,
                    provider: LLMSemanticProvider | None = None,
                    opts: dict | None = None) -> dict:
    """跑语义校验层，返回 {findings, ran_llm, provider, dialect}。

    - 确定性 SEM* 检查永远执行（无需密钥）。
    - LLM 仅在 provider 非 None 且非 NoProvider 时执行（如显式传入或环境变量已配置）。
    - 任何 LLM 异常都被层内吞掉，绝不影响主流程与 CI 判定。
    """
    d = normalize_dialect(dialect)
    findings: list[Finding] = []
    ctx = dict(opts or {})
    ctx["_dialect"] = d
    for st in parse_statements(sql, d):
        ctx["_sql_text"] = st.sql
        if not st.parsable:
            continue
        findings.extend(run_deterministic(st.ast, ctx))

    ran_llm = False
    provider_name = "none"
    if provider is None:
        provider = get_default_provider()
    if not isinstance(provider, NoProvider):
        ran_llm = True
        provider_name = provider.name
        llm_findings = provider.review(sql, d, findings)
        # 保底：LLM 复核只作建议，无论 provider 返回什么 severity 都强制 clamp 到 info，
        # 杜绝任何来源把 LLM 建议升级为阻断级（对齐 FSE：LLM 精确率仅 ~0.307）。
        for f in llm_findings:
            if f.source == "llm" and f.severity != "info":
                f.severity = "info"
        findings.extend(llm_findings)

    return {"findings": findings, "ran_llm": ran_llm,
            "provider": provider_name, "dialect": d}


__all__ = [
    "SEMANTIC_META", "DETERMINISTIC_CHECKS", "run_deterministic",
    "semantic_review", "LLMSemanticProvider", "NoProvider", "StubProvider",
    "OpenAIProvider", "get_default_provider",
]
