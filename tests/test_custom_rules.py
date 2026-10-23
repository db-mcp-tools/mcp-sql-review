#!/usr/bin/env python3
"""mcp-sql-review 回归测试：自定义规则 DSL（P5b）。

覆盖五类 matcher、校验（id 冲突/非法 severity/非法正则）、YAML/JSON 加载、
与内置规则协同、review_sql(custom_rules=) 接线。直接运行：

    python tests/test_custom_rules.py
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent
sys.path[:0] = [str(_HERE.parent / "src"),
                str(_HERE.parent.parent / "sqltk-core" / "src"),
                str(_HERE.parent.parent / "sqltk-store" / "src"),
                str(_HERE.parent.parent / "sqltk-know" / "src")]
os.environ.setdefault("DBT_LOG_LEVEL", "ERROR")

from sql_review.rulebook.custom import compile_rules, load_rules  # noqa: E402
from sql_review.rulebook.review import review_sql  # noqa: E402

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def section(title: str) -> None:
    print("\n# " + title)


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
    else:
        FAILED.append((name, detail))
        print("  ✗ FAIL:", name, "::", detail)


def ids_of(r) -> set:
    return {f.rule_id for f in r.findings}


# ==================================================================
section("五类 matcher")
# 1) statements：禁 DDL
_c1 = review_sql("DROP TABLE t", dialect="mysql",
                 custom_rules=[{"id": "CUS001", "name": "禁 DDL", "severity": "blocker",
                                "suggestion": "走工单", "statements": ["ddl"]}])
check("statements: DROP 触发", "CUS001" in ids_of(_c1), str(ids_of(_c1)))
_c1b = review_sql("SELECT 1", dialect="mysql",
                  custom_rules=[{"id": "CUS001", "name": "禁 DDL", "severity": "blocker",
                                 "statements": ["ddl"]}])
check("statements: SELECT 不触发", "CUS001" not in ids_of(_c1b), str(ids_of(_c1b)))
# 别名：truncate → truncatetable
_c1c = review_sql("TRUNCATE TABLE t", dialect="mysql",
                  custom_rules=[{"id": "CUS002", "name": "禁 TRUNCATE", "severity": "blocker",
                                 "statements": ["truncate"]}])
check("statements: TRUNCATE 别名命中", "CUS002" in ids_of(_c1c), str(ids_of(_c1c)))

# 2) require_where：UPDATE/DELETE 必须带 WHERE
_spec3 = [{"id": "CUS010", "name": "写操作必须带 WHERE", "severity": "blocker",
           "suggestion": "补 WHERE 或确认全表意图", "require_where": ["update", "delete"]}]
_c3a = review_sql("DELETE FROM t", dialect="mysql", custom_rules=_spec3)
check("require_where: DELETE 无 WHERE 触发", "CUS010" in ids_of(_c3a), str(ids_of(_c3a)))
_c3b = review_sql("UPDATE t SET a = 1", dialect="mysql", custom_rules=_spec3)
check("require_where: UPDATE 无 WHERE 触发", "CUS010" in ids_of(_c3b), str(ids_of(_c3b)))
_c3c = review_sql("DELETE FROM t WHERE id = 1", dialect="mysql", custom_rules=_spec3)
check("require_where: 带 WHERE 不触发", "CUS010" not in ids_of(_c3c), str(ids_of(_c3c)))

# 3) forbid_functions
_spec4 = [{"id": "CUS020", "name": "禁 SLEEP", "severity": "high",
           "forbid_functions": ["sleep", "get_lock"]}]
_c4a = review_sql("SELECT SLEEP(2)", dialect="mysql", custom_rules=_spec4)
check("forbid_functions: SLEEP() 触发", "CUS020" in ids_of(_c4a), str(ids_of(_c4a)))
_c4b = review_sql("SELECT 1", dialect="mysql", custom_rules=_spec4)
check("forbid_functions: 无关语句不触发", "CUS020" not in ids_of(_c4b), str(ids_of(_c4b)))

# 4) forbid_columns
_spec5 = [{"id": "CUS030", "name": "禁读 password 列", "severity": "high",
           "forbid_columns": ["password"]}]
_c5a = review_sql("SELECT name, password FROM users", dialect="mysql", custom_rules=_spec5)
check("forbid_columns: password 触发", "CUS030" in ids_of(_c5a), str(ids_of(_c5a)))
_c5b = review_sql("SELECT name FROM users", dialect="mysql", custom_rules=_spec5)
check("forbid_columns: 无关列不触发", "CUS030" not in ids_of(_c5b), str(ids_of(_c5b)))

# 5) forbid_patterns（原始文本正则）
_spec6 = [{"id": "CUS040", "name": "禁 SELECT *", "severity": "medium",
           "forbid_patterns": [r"SELECT\s+\*"]}]
_c6a = review_sql("SELECT * FROM t", dialect="mysql", custom_rules=_spec6)
check("forbid_patterns: SELECT * 触发", "CUS040" in ids_of(_c6a), str(ids_of(_c6a)))
_c6b = review_sql("SELECT id FROM t", dialect="mysql", custom_rules=_spec6)
check("forbid_patterns: 具名列不触发", "CUS040" not in ids_of(_c6b), str(ids_of(_c6b)))

# ==================================================================
section("校验：配置错误宁可失败，不要静默漏检")
try:
    compile_rules([{"id": "SEL001", "severity": "high", "statements": ["ddl"]}])
    check("内置 id 冲突报错", False, "未抛错")
except ValueError as e:
    check("内置 id 冲突报错", "冲突" in str(e), str(e))
try:
    compile_rules([{"id": "CUS050", "severity": "fatal", "statements": ["ddl"]}])
    check("非法 severity 报错", False, "未抛错")
except ValueError as e:
    check("非法 severity 报错", "severity" in str(e), str(e))
try:
    compile_rules([{"id": "CUS051", "severity": "high"}])
    check("无 matcher 报错", False, "未抛错")
except ValueError as e:
    check("无 matcher 报错", "matcher" in str(e), str(e))
try:
    compile_rules([{"id": "CUS052", "severity": "high", "forbid_patterns": ["("]}])
    check("非法正则报错", False, "未抛错")
except ValueError as e:
    check("非法正则报错", "正则" in str(e), str(e))
try:
    compile_rules([{"id": "CUS053", "severity": "high", "statements": ["ddl"]},
                   {"id": "CUS053", "severity": "low", "statements": ["ddl"]}])
    check("重复 id 报错", False, "未抛错")
except ValueError as e:
    check("重复 id 报错", "重复" in str(e), str(e))
try:
    compile_rules([{"severity": "high", "statements": ["ddl"]}])
    check("缺 id 报错", False, "未抛错")
except ValueError as e:
    check("缺 id 报错", "id" in str(e), str(e))

# ==================================================================
section("load_rules：YAML / JSON 文件与内联文本")
_yaml = """
rules:
  - id: CUS060
    name: 禁 TRUNCATE
    severity: blocker
    statements: [truncate]
"""
_tmpdir = tempfile.mkdtemp()
_yaml_path = os.path.join(_tmpdir, "rules.yaml")
with open(_yaml_path, "w", encoding="utf-8") as fh:
    fh.write(_yaml)
specs = load_rules(_yaml_path)
check("YAML 文件加载", isinstance(specs, list) and specs[0]["id"] == "CUS060", str(specs)[:80])
_cy = review_sql("TRUNCATE TABLE t", dialect="mysql", custom_rules=specs)
check("YAML 规则生效", "CUS060" in ids_of(_cy), str(ids_of(_cy)))

_json_path = os.path.join(_tmpdir, "rules.json")
import json as _json  # noqa: E402
with open(_json_path, "w", encoding="utf-8") as fh:
    _json.dump([{"id": "CUS061", "name": "禁 SLEEP", "severity": "high",
                 "forbid_functions": ["sleep"]}], fh)
_cj = review_sql("SELECT SLEEP(1)", dialect="mysql", custom_rules=load_rules(_json_path))
check("JSON 文件加载并生效", "CUS061" in ids_of(_cj), str(ids_of(_cj)))

_inline = load_rules("- id: CUS062\n  severity: low\n  forbid_patterns: ['BORROW']\n")
check("内联 YAML 加载", isinstance(_inline, list) and _inline[0]["id"] == "CUS062", str(_inline))

# ==================================================================
section("与内置规则协同 + fixable 诚实性")
_cmix = review_sql("SELECT * FROM users WHERE SLEEP(1)", dialect="mysql",
                   custom_rules=[{"id": "CUS070", "name": "禁 SLEEP", "severity": "blocker",
                                  "forbid_functions": ["sleep"]}])
rids = ids_of(_cmix)
check("自定义与内置规则同时生效", "CUS070" in rids and any(r.startswith("SEL") for r in rids),
      str(rids))
cus = [f for f in _cmix.findings if f.rule_id == "CUS070"][0]
check("自定义 finding fixable=False（不承诺自动修复）", cus.fixable is False and cus.auto_fix is None,
      f"fixable={cus.fixable}")
check("自定义 finding 定位到行", cus.line == 1, str(cus.line))
check("自定义 finding 归入 custom 分类", compile_rules(
    [{"id": "CUS071", "severity": "low", "statements": ["ddl"]}])[0].category == "custom")

# engines 过滤：限定方言的自定义规则在其它方言不生效
_ceng = review_sql("DROP TABLE t", dialect="postgres",
                   custom_rules=[{"id": "CUS080", "name": "MySQL 专属", "severity": "blocker",
                                  "statements": ["ddl"], "engines": ["mysql"]}])
check("engines 限定后 postgres 不触发", "CUS080" not in ids_of(_ceng), str(ids_of(_ceng)))
_ceng2 = review_sql("DROP TABLE t", dialect="mysql",
                    custom_rules=[{"id": "CUS080", "name": "MySQL 专属", "severity": "blocker",
                                   "statements": ["ddl"], "engines": ["mysql"]}])
check("engines 限定后 mysql 触发", "CUS080" in ids_of(_ceng2), str(ids_of(_ceng2)))

# ==================================================================
print("\n" + "=" * 60)
print("通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
if FAILED:
    for name, detail in FAILED:
        print("  -", name, "::", detail)
    raise SystemExit(1)
