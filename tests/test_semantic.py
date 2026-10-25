#!/usr/bin/env python3
"""P4 语义校验层回归测试：确定性 SEM* + 可选 LLM 复核（仅建议，永不阻断）。

不依赖 pytest / 真实数据库 / 任何 LLM SDK。直接运行：

    python tests/test_semantic.py
"""

from __future__ import annotations

import os
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "src"))
for _sib in ("sqltk-core", "sqltk-store"):
    _p = _HERE.parent.parent / _sib / "src"
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ.setdefault("DBT_LOG_LEVEL", "ERROR")
# 强制默认无 LLM provider，避免测试受环境变量影响
os.environ["DBT_LLM_PROVIDER"] = "none"


PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
    else:
        FAILED.append(f"{name}  << {detail[:300] if detail else ''}")


def section(title: str) -> None:
    print(f"\n# {title}")


from sql_review.rulebook import review_sql, semantic_review
from sql_review.rulebook.core import Finding
from sql_review.rulebook.semantic import (  # noqa: E402
    NoProvider,
    OpenAIProvider,
    StubProvider,
    get_default_provider,
)


def sem_ids(sql: str, dialect: str = "mysql", **kw) -> set[str]:
    r = review_sql(sql, dialect, semantic=True, **kw)
    return {f.rule_id for f in r.findings if f.source in ("semantic", "llm")}


def builtin_ids(sql: str, dialect: str = "mysql", **kw) -> set[str]:
    r = review_sql(sql, dialect, semantic=False, **kw)
    return {f.rule_id for f in r.findings if f.source is None}


# ---------------------------------------------------------------------------
# 1. 默认关闭：P0 精度门禁不受影响
# ---------------------------------------------------------------------------
section("默认关闭（semantic=False）不触发任何 SEM/AI")
check("默认关闭时 =NULL 不报 SEM", "SEM001" not in builtin_ids("SELECT * FROM t WHERE col = NULL"))
check("默认关闭时 BETWEEN 不报 SEM",
      "SEM003" not in builtin_ids("SELECT * FROM t WHERE dt BETWEEN '2026-01-01' AND '2026-01-31'"))
check("默认关闭时语义层不影响内置规则", builtin_ids("SELECT a FROM t LIMIT 10") == {"SEL006"})


# ---------------------------------------------------------------------------
# 2. SEM001 = NULL / <> NULL
# ---------------------------------------------------------------------------
section("SEM001 NULL 比较误用")
r = review_sql("SELECT * FROM t WHERE col = NULL", semantic=True)
ids = {f.rule_id for f in r.findings if f.source == "semantic"}
check("col = NULL 触发 SEM001", "SEM001" in ids)
check("SEM001 定位到列", any(f.rule_id == "SEM001" and f.col is not None for f in r.findings))
check("col <> NULL 也触发 SEM001",
      "SEM001" in sem_ids("SELECT * FROM t WHERE col <> NULL"))
check("IS NULL 不触发 SEM001", "SEM001" not in sem_ids("SELECT * FROM t WHERE col IS NULL"))
check("col = 'x' 不触发 SEM001", "SEM001" not in sem_ids("SELECT * FROM t WHERE col = 'x'"))


# ---------------------------------------------------------------------------
# 3. SEM002 LIMIT/OFFSET 无 ORDER BY
# ---------------------------------------------------------------------------
section("SEM002 已并入内置 SEL006（此处验证不重复造规则）")
check("LIMIT 无 ORDER 已由内置 SEL006 覆盖（语义层不再造 SEM002）",
      "SEM002" not in sem_ids("SELECT a FROM t LIMIT 10"))
check("LIMIT 有 ORDER 内置也不报", "SEL006" not in builtin_ids("SELECT a FROM t ORDER BY a LIMIT 10"))


# ---------------------------------------------------------------------------
# 4. SEM003 BETWEEN 日期边界
# ---------------------------------------------------------------------------
section("SEM003 BETWEEN 日期边界 off-by-one")
check("BETWEEN 纯日期上界触发 SEM003",
      "SEM003" in sem_ids("SELECT * FROM t WHERE dt BETWEEN '2026-01-01' AND '2026-01-31'"))
check("BETWEEN 数值不触发 SEM003",
      "SEM003" not in sem_ids("SELECT * FROM t WHERE n BETWEEN 1 AND 10"))
check("BETWEEN 上界带 23:59:59 不触发（有意写法）",
      "SEM003" not in sem_ids("SELECT * FROM t WHERE dt BETWEEN '2026-01-01' AND '2026-01-31 23:59:59'"))


# ---------------------------------------------------------------------------
# 5. SEM004 UNION 默认去重
# ---------------------------------------------------------------------------
section("SEM004 UNION 默认去重")
check("UNION 触发 SEM004", "SEM004" in sem_ids("SELECT a FROM t UNION SELECT a FROM t2"))
check("UNION ALL 不触发 SEM004",
      "SEM004" not in sem_ids("SELECT a FROM t UNION ALL SELECT a FROM t2"))


# ---------------------------------------------------------------------------
# 6. SEM005 HAVING 无 GROUP BY
# ---------------------------------------------------------------------------
section("SEM005 HAVING 无 GROUP BY")
check("HAVING 无 GROUP BY 触发 SEM005",
      "SEM005" in sem_ids("SELECT a, COUNT(*) FROM t HAVING COUNT(*) > 1"))
check("HAVING 有 GROUP BY 不触发 SEM005",
      "SEM005" not in sem_ids("SELECT a, COUNT(*) FROM t GROUP BY a HAVING COUNT(*) > 1"))


# ---------------------------------------------------------------------------
# 7. 语义层永不阻断门禁
# ---------------------------------------------------------------------------
section("语义层仅建议，绝不进入阻断判定")
r = review_sql("SELECT a FROM t WHERE col = NULL", semantic=True)
check("有 SEM 发现", len(r.semantic_findings) > 0)
check("SEM 仅 low/info，不为 blocker", all(f.severity in ("low", "info") for f in r.semantic_findings))
check("has_blocking 在纯语义发现时为 False", r.has_blocking is False)
check("blocking_count 为 0", r.blocking_count == 0)
check("semantic_findings 仅含语义来源", all(f.source in ("semantic", "llm") for f in r.semantic_findings))
check("review_result.semantic_meta 透出", r.semantic_meta is not None and r.semantic_meta["ran_llm"] is False)
check("to_dict 含 semantic 元信息", "semantic" in r.to_dict())


# ---------------------------------------------------------------------------
# 8. 多语句：每条独立检查
# ---------------------------------------------------------------------------
section("多语句分别检查")
ids = sem_ids("SELECT a FROM t LIMIT 1; SELECT b FROM u WHERE c = NULL")
check("多语句中 LIMIT 不报 SEM002（已由 SEL006 覆盖）", "SEM002" not in ids)
check("多语句中 =NULL 触发 SEM001", "SEM001" in ids)


# ---------------------------------------------------------------------------
# 9. LLM 复核层（离线可测）
# ---------------------------------------------------------------------------
section("LLM 复核层（离线）")
# 无 provider（默认）时不跑 LLM
res_none = semantic_review("SELECT a FROM t", "mysql", provider=NoProvider())
check("NoProvider 不跑 LLM", res_none["ran_llm"] is False)
check("NoProvider 无 AI finding", all(f.source != "llm" for f in res_none["findings"]))

# StubProvider 模拟 LLM 返回；severity 强制为 info，source=llm
stub_findings = [Finding(rule_id="AI001", severity="high",  # 即便模型给 high 也要被 clamp
                         message="疑似笛卡尔积", suggestion="检查 JOIN 条件",
                         source="llm")]
res_stub = semantic_review("SELECT * FROM a, b", "mysql", provider=StubProvider(stub_findings))
check("StubProvider 跑 LLM", res_stub["ran_llm"] is True)
check("LLM finding 来源为 llm", any(f.source == "llm" for f in res_stub["findings"]))
llm_f = [f for f in res_stub["findings"] if f.source == "llm"][0]
check("LLM finding severity 被强制为 info（绝不阻断）", llm_f.severity == "info")

# 通过 review_sql 透传 provider 实例（用干净 SQL，避免内置 blocker 干扰断言）
r2 = review_sql("SELECT a FROM t", semantic={"provider": StubProvider(stub_findings)})
check("review_sql 透传 provider 触发 LLM", r2.semantic_meta["ran_llm"] is True)
check("review_sql 中 LLM finding 不进阻断", r2.has_blocking is False)

# get_default_provider 在 DBT_LLM_PROVIDER=none 时返回 NoProvider
check("默认 provider 为 NoProvider", isinstance(get_default_provider(), NoProvider))

# OpenAIProvider 未装 openai 时构造即报错（不静默）
try:
    OpenAIProvider()
    _oa_err = False
except RuntimeError:
    _oa_err = True
except ImportError:
    _oa_err = True
check("OpenAIProvider 缺依赖时构造报错（不静默）", _oa_err)


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
print("\n" + "=" * 56)
print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
if FAILED:
    print("\n失败项：")
    for f in FAILED:
        print("  ✗", f)
    raise SystemExit(1)
print("全部通过")
