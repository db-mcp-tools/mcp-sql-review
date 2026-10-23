"""SQL 静态审查引擎精度基准（P0：评测驱动优化）。

跑法（从任意目录，脚本自行补齐依赖路径）：
    python mcp-sql-review/bench/run_bench.py
    python mcp-sql-review/bench/run_bench.py --json bench_report.json
    python mcp-sql-review/bench/run_bench.py --fail-under 0.90 --max-fp 3   # CI 门禁

指标口径对齐 SQLFlex (arXiv 2603.16155) 与 FSE 2026：
- 以「规则 ID」为 anti-pattern 单元（一条 SQL 触发某规则与否，忽略重复行）。
- 逐样本算 TP/FP/FN，再全局聚合出 Precision / Recall / F1。
- 同时按规则聚合，定位「哪条规则在贡献误报」，这正是评测界衡量这类工具的核心指标。
- PARSE000（不可解析）不计入评分（它属于解析覆盖，不是质量 anti-pattern）。

FP 陷阱样本（expect=[]）若被触发 → 计为 FP，直接反映误报治理效果。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

# ---- 依赖路径：bench/ 在 mcp-sql-review/bench，向上两级是 meta 仓库根 ----
_HERE = Path(__file__).resolve().parent
_MCP_SRC = _HERE.parent / "src"                       # mcp-sql-review/src
_META = _HERE.parents[1]                              # db-ai-toolchain/
for _p in (
    _MCP_SRC,
    _META / "sqltk-core" / "src",
    _META / "sqltk-store" / "src",
    _META / "sqltk-know" / "src",
):
    sys.path.insert(0, str(_p))

from sql_review.rulebook.review import review_sql  # noqa: E402
from sql_review.rulebook.registry import build      # noqa: E402

from corpus import SAMPLES                           # noqa: E402


def actual_rule_ids(sample: dict) -> set[str]:
    res = review_sql(
        sample["sql"],
        sample.get("dialect", "mysql"),
        profile="default",
        **(sample.get("opts") or {}),
    )
    return {f.rule_id for f in res.findings if f.rule_id != "PARSE000"}


def _safe_div(a: int, b: int) -> float:
    return (a / b) if b else 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description="SQL 审查引擎精度基准")
    ap.add_argument("--json", metavar="PATH", help="把报告写成 JSON")
    ap.add_argument("--fail-under", type=float, default=None,
                    help="全局 F1 低于该值则返回非零退出码（CI 门禁）")
    ap.add_argument("--max-fp", type=int, default=None,
                    help="全局 FP 数超过该值则返回非零退出码（CI 门禁）")
    args = ap.parse_args()

    reg = build()
    known = {r.rule_id for r in reg.all()}

    # 全局聚合
    g_tp = g_fp = g_fn = 0
    # 每条规则聚合
    per_rule = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    # 样本级失配（用于人工复核：区分「我标错」还是「工具真有 FP/FN」）
    mismatches = []

    for s in SAMPLES:
        want = set(s["expect"])
        got = actual_rule_ids(s)
        # 校验语料：expect 里的 ID 必须真实存在，否则是语料错不是工具错
        unknown = want - known
        if unknown:
            print(f"[语料错误] {s['id']} 引用了不存在的规则: {sorted(unknown)}", file=sys.stderr)
        tp = want & got
        fp = got - want
        fn = want - got
        g_tp += len(tp)
        g_fp += len(fp)
        g_fn += len(fn)
        for rid in tp:
            per_rule[rid]["tp"] += 1
        for rid in fp:
            per_rule[rid]["fp"] += 1
        for rid in fn:
            per_rule[rid]["fn"] += 1
        if fp or fn:
            mismatches.append({
                "id": s["id"], "dialect": s.get("dialect", "mysql"),
                "note": s.get("note", ""),
                "expect": sorted(want), "actual": sorted(got),
                "fp": sorted(fp), "fn": sorted(fn),
            })

    precision = _safe_div(g_tp, g_tp + g_fp)
    recall = _safe_div(g_tp, g_tp + g_fn)
    f1 = _safe_div(2 * precision * recall, precision + recall)

    # ---- 报告 ----
    print("=" * 68)
    print("SQL 审查引擎 · 精度基准（anti-pattern 级 P/R/F1）")
    print("=" * 68)
    print(f"样本数            : {len(SAMPLES)}")
    print(f"规则总数          : {reg.rule_count()}")
    print(f"TP / FP / FN      : {g_tp} / {g_fp} / {g_fn}")
    print(f"Precision         : {precision:.3f}")
    print(f"Recall            : {recall:.3f}")
    print(f"F1                : {f1:.3f}")
    print("-" * 68)
    print("按规则明细（FP 降序，最先暴露误报来源）：")
    print(f"  {'RULE':9s} {'TP':>3s} {'FP':>3s} {'FN':>3s} {'P':>5s} {'R':>5s} {'F1':>5s}")
    for rid, c in sorted(per_rule.items(), key=lambda kv: (-kv[1]["fp"], -kv[1]["fn"])):
        p = _safe_div(c["tp"], c["tp"] + c["fp"])
        r = _safe_div(c["tp"], c["tp"] + c["fn"])
        f = _safe_div(2 * p * r, p + r)
        flag = "  <-- 误报" if c["fp"] else ("  <-- 漏报" if c["fn"] else "")
        print(f"  {rid:9s} {c['tp']:3d} {c['fp']:3d} {c['fn']:3d} "
              f"{p:5.2f} {r:5.2f} {f:5.2f}{flag}")

    covered = {rid for rid, c in per_rule.items() if c["tp"] or c["fn"]}
    never = known - covered
    if never:
        print("-" * 68)
        print(f"未被语料覆盖的规则（{len(never)} 条）：{sorted(never)}")

    if mismatches:
        print("=" * 68)
        print(f"样本级失配（{len(mismatches)} 条）：FP=工具多报 / FN=工具漏报")
        print("=" * 68)
        for m in mismatches:
            print(f"\n[{m['id']}] ({m['dialect']}) {m['note']}")
            print(f"  期望: {m['expect']}")
            print(f"  实际: {m['actual']}")
            if m["fp"]:
                print(f"  FP(误报): {m['fp']}")
            if m["fn"]:
                print(f"  FN(漏报): {m['fn']}")
    else:
        print("\n无样本级失配：语料与引擎行为完全一致。")

    # ---- JSON ----
    if args.json:
        report = {
            "samples": len(SAMPLES),
            "rules": reg.rule_count(),
            "tp": g_tp, "fp": g_fp, "fn": g_fn,
            "precision": precision, "recall": recall, "f1": f1,
            "per_rule": {rid: {**c,
                               "precision": _safe_div(c["tp"], c["tp"] + c["fp"]),
                               "recall": _safe_div(c["tp"], c["tp"] + c["fn"]),
                               "f1": _safe_div(2 * _safe_div(c["tp"], c["tp"] + c["fp"]) * _safe_div(c["tp"], c["tp"] + c["fn"]),
                                               _safe_div(c["tp"], c["tp"] + c["fp"]) + _safe_div(c["tp"], c["tp"] + c["fn"]))}
                        for rid, c in per_rule.items()},
            "uncovered_rules": sorted(never),
            "mismatches": mismatches,
        }
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已写入 {args.json}")

    # ---- CI 门禁 ----
    rc = 0
    if args.fail_under is not None and f1 < args.fail_under:
        print(f"\n[FAIL] 全局 F1 {f1:.3f} < --fail-under {args.fail_under}")
        rc = 1
    if args.max_fp is not None and g_fp > args.max_fp:
        print(f"\n[FAIL] 全局 FP {g_fp} > --max-fp {args.max_fp}")
        rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
