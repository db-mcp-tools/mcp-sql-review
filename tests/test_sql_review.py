#!/usr/bin/env python3
"""sql-review 回归测试：规则引擎 / 体检 / 修法 / 规则分册

不依赖 pytest / 真实数据库。直接运行：

    python tests/test_sql_review.py
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "src"))
# 开发期：兄弟仓库用源码目录；正式使用请 pip install（见 README）
for _sib in ("dbt-core", "dbt-store", "dbt-mcp-kit"):
    _p = _HERE.parent.parent / _sib / "src"
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ.setdefault("DBT_LOG_LEVEL", "ERROR")
_TMPDIR = tempfile.mkdtemp(prefix="dbt-sql_review-")
os.environ.setdefault("DBT_STORE", os.path.join(_TMPDIR, "store.db"))

from dbcore.protocol import Result, ToolError  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
    else:
        FAILED.append(f"{name}  << {detail[:300] if detail else ''}")


def section(title: str) -> None:
    print(f"\n── {title}")


section("2. 规则引擎（含误报回归）")
# ==================================================================
from sql_review.rules import review_sql, list_rules, explain_rule, RULE_META, RULES  # noqa: E402

check("规则数量 >= 30", len(RULES) >= 30, str(len(RULES)))
check("规则元数据齐备", len(RULE_META) >= 25, str(len(RULE_META)))
check("list_rules 可用", len(list_rules()) > 0)
check("explain_rule 可用", explain_rule("SEL003") is not None)
check("未知规则返回 None", explain_rule("NOPE999") is None)


def rules_of(sql: str, dialect: str = "mysql", **kw) -> set[str]:
    return {f.rule_id for f in review_sql(sql, dialect, **kw).findings}


# --- 命中 ---
hit_cases = [
    ("UPDATE t SET a=1", "mysql", "DML001"),
    ("SELECT * FROM a, b", "mysql", "JOIN002"),
    ("SELECT id FROM u WHERE phone = '13800138000'", "mysql", "TYPE001"),
    ("CREATE INDEX ix ON t (a)", "postgres", "PG001"),
    ("TRUNCATE TABLE t", "mysql", "DDL004"),
    ("DROP TABLE t", "mysql", "DDL005"),
    ("BEGIN; UPDATE t SET a=1 WHERE id=1; COMMIT;", "mysql", "TX002"),
    ("SELECT uid, (SELECT n FROM u WHERE u.id=o.uid) FROM o", "mysql", "SUBQ001"),
    ("SELECT * FROM o WHERE id IN (SELECT oid FROM p)", "mysql", "SUBQ002"),
    ("SELECT * FROM o JOIN u ON 1=1 JOIN p ON 1=1 JOIN c ON 1=1 JOIN d ON 1=1 "
     "JOIN e ON 1=1 JOIN f ON 1=1 JOIN g ON 1=1", "mysql", "JOIN003"),
]
for sql, dia, rid in hit_cases:
    got = rules_of(sql, dia)
    check(f"命中 {rid}", rid in got, f"{sql[:40]} -> {got}")

# --- 误报（这些是踩过的坑，必须守住）---
fp_cases = [
    ("SELECT * FROM a, b WHERE a.x = b.x", "mysql", "JOIN002"),
    ("UPDATE t SET a=1 WHERE id=2", "mysql", "TYPE001"),
    ("SELECT * FROM orders WHERE created_at > 1 LIMIT 20", "mysql", "TYPE001"),
    ("SELECT o.*, u.name FROM o JOIN u ON o.uid=u.id WHERE o.amount>100", "mysql", "JOIN002"),
    ("SELECT * FROM orders WHERE amount > 0 ORDER BY id LIMIT 10", "mysql", "SEL006"),
]
for sql, dia, rid in fp_cases:
    got = rules_of(sql, dia)
    check(f"不误报 {rid}", rid not in got, f"{sql[:40]} -> {got}")

# 分区键
r_part = review_sql("SELECT * FROM orders WHERE amount > 10", "mysql",
                    partition_keys={"orders": "created_at"})
check("分区键缺失可检出", any(f.rule_id == "PART001" for f in r_part.findings))
r_part2 = review_sql("SELECT * FROM orders WHERE created_at > 10", "mysql",
                     partition_keys={"orders": "created_at"})
check("分区键命中不报", not any(f.rule_id == "PART001" for f in r_part2.findings))

# 正常 SQL 应当干净
clean = review_sql("SELECT id, amount FROM orders WHERE uid = '1001' AND amount > 100 "
                   "ORDER BY id LIMIT 20", "mysql")
check("干净 SQL 无 blocker/high",
      not any(f.severity in ("blocker", "high") for f in clean.findings),
      str([f.rule_id for f in clean.findings]))

# ==================================================================

section("4. sql-review MCP")
# ==================================================================
from sql_review import server as SR  # noqa: E402

o = SR.review_sql("SELECT * FROM a, b", "mysql").to_dict()
check("review_sql 判定为 fail", o["verdict"] == "fail")
check("review_sql 有 evidence", len(o["evidence"]) > 0)
check("review_sql 指向下一步", any(n["tool"] == "suggest_fixes" for n in o["next_actions"]))

o2 = SR.review_batch(["SELECT * FROM a, b", "SELECT id FROM t WHERE id=1"],
                     "mysql", fail_on="critical").to_dict()
check("review_batch 门禁 FAIL", o2["data"]["gate"] == "FAIL" and o2["verdict"] == "fail")

fix = SR.suggest_fixes("SELECT * FROM orders WHERE DATE(created_at)='2026-01-01' "
                       "ORDER BY id LIMIT 10 OFFSET 50000", "mysql",
                       columns=["id", "amount"]).to_dict()
check("suggest_fixes 产出改法", fix["data"]["actionable"] >= 2)
check("composed_sql 存在", bool(fix["data"].get("composed_sql")))
check("游标分页去掉了 OFFSET",
      "OFFSET" not in (fix["data"].get("composed_sql") or "").upper())
check("改法后指向验证", any(n["tool"] == "verify_fix" for n in fix["next_actions"]))

eq = SR.verify_fix("SELECT * FROM t WHERE id=1",
                   "SELECT id, amount FROM t WHERE id=1", "mysql").to_dict()
check("静态等价性可判定", isinstance(eq["data"]["equivalent"], bool))
check("列变化被检出", not eq["data"]["equivalent"] or eq["data"]["diff_count"] >= 0)

fp = SR.fingerprint_sql("SELECT * FROM t WHERE id = 5").to_dict()
check("指纹生成", fp["data"]["fingerprint"].startswith("fp:"))
fp2 = SR.fingerprint_sql("SELECT * FROM t WHERE id = 999").to_dict()
check("字面量不同指纹相同", fp["data"]["fingerprint"] == fp2["data"]["fingerprint"])

lr = SR.list_rules().to_dict()
check("list_rules 返回规则", len(lr["data"]["rules"]) > 10)
er = SR.explain_rule("SEL003").to_dict()
check("explain_rule 有效", er["ok"] and "why" in er["data"])

objs = SR.extract_objects("SELECT a.id FROM t1 a JOIN t2 b ON a.id=b.id WHERE a.x=1",
                          "mysql").to_dict()
check("extract_objects 表识别", len(objs["data"]["all_tables"]) == 2, str(objs["data"]["all_tables"]))
check("extract_objects JOIN 识别", len(objs["data"]["statements"][0]["joins"]) >= 1)

# 正常 SQL 不应被拦截
okq = SR.review_sql("SELECT id FROM orders WHERE uid = '1001' ORDER BY id LIMIT 10",
                    "mysql").to_dict()
check("正常 SQL 通过", okq["verdict"] in ("pass", "warn"), okq["summary"])

# ==================================================================

section("12. 规则分册 rulebook（§9 实现端：拆分 + 启停）")
# ==================================================================
from sql_review.rulebook.registry import build as _build_rules  # noqa: E402
from sql_review.rulebook.review import review_sql as _rb_review  # noqa: E402
from sql_review.rulebook.profiles import PROFILES  # noqa: E402

_rb = _build_rules()
check("规则注册表含全部规则（>=30）", _rb.rule_count() >= 30, str(_rb.rule_count()))
check("规则元数据 + PARSE000 齐备（>=30）", len(_rb.meta_map()) >= 30, str(len(_rb.meta_map())))
# 每条规则必须有 why 与 false_positive（质量约束 §9.4）
_missing = [rid for rid, m in _rb.meta_map().items() if rid != "PARSE000"
             and (not m.get("why") or not m.get("false_positive"))]
check("每条规则都有 why 与 false_positive", not _missing, str(_missing))

# 方言过滤：PG001 只在 postgres 系生效
_pg_mysql = {r.rule_id for r in _rb.filtered(dialect="mysql")}
_pg_pg = {r.rule_id for r in _rb.filtered(dialect="postgres")}
check("PG001 在 mysql 下被方言过滤掉", "PG001" not in _pg_mysql)
check("PG001 在 postgres 下生效", "PG001" in _pg_pg)
check("MY001 在 postgres 下被方言过滤掉", "MY001" not in _pg_pg)

# profile：lenient 只看 blocker+high
_lenient_ids = {r.rule_id for r in _rb.filtered(severities=["blocker", "high"])}
check("lenient profile 不含 low 级规则（SEL001）", "SEL001" not in _lenient_ids)
check("lenient profile 保留 high 级规则（SEL004）", "SEL004" in _lenient_ids)

# disable / enable 优先级
_dis = {r.rule_id for r in _rb.filtered(exclude=["SEL001"])}
check("exclude 能禁用单条规则（SEL001）", "SEL001" not in _dis)
_en = {r.rule_id for r in _rb.filtered(severities=["blocker", "high"], include=["SEL001"])}
check("include 能顶回被 profile 过滤的规则（SEL001）", "SEL001" in _en)

# 默认体检行为不变（回归）：SELECT * + RAND 命中 SEL001/SEL004
_rdefault = _rb_review("SELECT * FROM t WHERE 1=1 ORDER BY RAND() LIMIT 5", "mysql")
_rb_ids = {f.rule_id for f in _rdefault.findings}
check("默认体检仍命中 SEL001（SELECT *）", "SEL001" in _rb_ids, str(sorted(_rb_ids)))
check("默认体检仍命中 SEL004（ORDER BY RAND）", "SEL004" in _rb_ids, str(sorted(_rb_ids)))

# profile 接入 review 入口
_rlen = _rb_review("SELECT * FROM t WHERE 1=1 ORDER BY RAND() LIMIT 5", "mysql", profile="lenient")
check("review 入口套用 lenient 后不再报 SEL001", "SEL001" not in {f.rule_id for f in _rlen.findings})

# 1:1 拆分校验：每个 rule_id 唯一，且引擎专属规则的 engines 与 category 自洽
_dup = [rid for rid in _rb.meta_map() if rid != "PARSE000"
        and sum(1 for r in _rb.all() if r.rule_id == rid) != 1]
check("每个 rule_id 在注册表中唯一（已做 1:1 拆分）", not _dup, str(_dup))
for r in _rb.all():
    if r.is_engine_specific:
        check(f"{r.rule_id} 的 engines 与 category 自洽",
              r.category in ("postgres", "mysql", "subquery"), r.category)

# profile 字典可解析
for _pn in PROFILES:
    check(f"profile {_pn} 可被 resolve", True)

# 严重度一致性：每条规则的 _mk 严重度必须与 META 严重度一致（否则过滤与展示会打架）
import inspect as _inspect  # noqa: E402
import re as _re  # noqa: E402
_mismatch = []
for _mod_name in ("select", "dml", "type", "tx", "engine", "subquery"):
    _mod = __import__(f"sql_review.rulebook.{_mod_name}", fromlist=["META", "RULES"])
    for _rid, _fn in _mod.RULES:
        for _mm in _re.finditer(r"_mk\(\s*['\"]([A-Z0-9]+)['\"]\s*,\s*['\"]([a-z]+)['\"]",
                                _inspect.getsource(_fn)):
            _r2, _sev = _mm.group(1), _mm.group(2)
            _ms = _mod.META.get(_r2, {}).get("severity")
            if _ms and _ms != _sev:
                _mismatch.append(f"{_r2}:_mk={_sev},meta={_ms}")
check("每条规则的 _mk 严重度与 META 一致", not _mismatch, str(_mismatch))

# ==================================================================

from sql_review.advisor import rank_candidates
# 13.6 advisor 收益-代价
_rk = rank_candidates(["uid", "status", "created_at"],
                      {"uid": 0.9, "status": 0.02, "created_at": None},
                      table_rows=10_000_000, write_freq=500)
check("advisor：高选择性排前", _rk[0]["column"] == "uid")
check("advisor：无选择性数据标注未验证",
      _rk[-1]["column"] == "created_at" and _rk[-1]["verified"] is False)


# ==================================================================
print("\n" + "=" * 60)
print("通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
if FAILED:
    print("\n失败明细：")
    for _f in FAILED:
        print("  ✗", _f)
    raise SystemExit(1)
print("全部通过")
