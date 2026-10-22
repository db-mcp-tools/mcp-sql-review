"""命令行入口：SQL 体检 MCP：31 条规则分册静态体检、自动修法、修复验证、执行计划对比、索引建议。Tier0 零依赖可用。

两条命令就够用（不需要记每个工具的参数）：
    mcp-sql-review tools                       列出本 MCP 的全部工具
    mcp-sql-review call <tool> --args '{"sql": "SELECT ..."}' [--json]
    mcp-sql-review call <tool> --help-args     查看该工具的参数签名
"""

from __future__ import annotations

import argparse
import inspect
import json
import sys


def _load():
    from sql_review import TOOLS
    return TOOLS


def cmd_tools(args) -> int:
    tools = _load()
    if args.json:
        print(json.dumps({"count": len(tools), "tools": sorted(tools)}, ensure_ascii=False, indent=2))
        return 0
    print(f"{len(tools)} 个工具：")
    for n in sorted(tools):
        fn = tools[n]
        doc = (inspect.getdoc(fn) or "").splitlines()
        print(f"  {n:24s} {doc[0] if doc else ''}")
    return 0


def cmd_call(args) -> int:
    tools = _load()
    fn = tools.get(args.tool)
    if fn is None:
        print(f"没有工具 {args.tool}。可用：{', '.join(sorted(tools))}", file=sys.stderr)
        return 2
    if args.help_args:
        print(f"{args.tool}{inspect.signature(fn)}")
        return 0
    try:
        kwargs = json.loads(args.args) if args.args else {}
    except json.JSONDecodeError as exc:
        print(f"--args 不是合法 JSON：{exc}", file=sys.stderr)
        return 2
    if not isinstance(kwargs, dict):
        print("--args 必须是 JSON 对象", file=sys.stderr)
        return 2
    try:
        raw = fn(**kwargs)
    except TypeError as exc:
        print(f"参数不匹配：{exc}\n提示：{args.tool}{inspect.signature(fn)}", file=sys.stderr)
        return 2
    out = raw.to_dict() if hasattr(raw, "to_dict") else raw
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mcp-sql-review", description="SQL 体检 MCP：31 条规则分册静态体检、自动修法、修复验证、执行计划对比、索引建议。Tier0 零依赖可用。")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("tools", help="列出全部工具")
    t.add_argument("--json", action="store_true")
    t.set_defaults(func=cmd_tools)

    c = sub.add_parser("call", help="调用任意工具")
    c.add_argument("tool")
    c.add_argument("--args", default="{}", help="JSON 参数对象")
    c.add_argument("--help-args", action="store_true", help="只打印参数签名")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_call)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
