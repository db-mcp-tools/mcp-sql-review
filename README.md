# mcp-sql-review

**SQL 体检与修法** · MCP Server

> 不开库、不跑 SQL —— 静态 AST + 规则分册，给出问题、改法、以及**改写后能不能用**的验证。

## 安装（按需，不必装其他 MCP）

```bash
pip install ./mcp-sql-review              # 在工具链根目录下执行
pip install "./mcp-sql-review[mysql]"     # 需要连库时补驱动，或 [postgres]
```

会自动带上它需要的共享库：`dbt-core`（协议/方言/AST/计划）、`dbt-store`（发现写账本）。**不会**带其他 MCP。

## 挂到 MCP 客户端

```json
{
  "mcpServers": {
    "sql-review": {
      "command": "python",
      "args": ["-m", "sql_review"],
      "env": { "DBT_STORE": "~/.dbtoolchain" }
    }
  }
}
```

按你的客户端（Claude Desktop / Cursor / Cline 等）说明，把这段合并到对应的 `mcpServers` 配置里。

命令行调试（不挂客户端也能用）：

```bash
dbt-sql-review tools
dbt-sql-review call <tool> --args '{"sql": "SELECT 1"}'
```

## 工具（14）

| 工具 | 作用 |
|---|---|
| `advise_indexes` | 索引建议。已有重复索引或前缀已覆盖会直接劝退。 |
| `compare_plans` | 对比改写前后的执行计划。**扫描行数反而上升会明确判为不可采用** |
| `explain_rule` | 解释一条规则：禁它的原因、什么情况下会误报、怎么改。 |
| `explain_sql` | 取执行计划并归一化，标注可疑点。 |
| `extract_objects` | 把 SQL 抽成结构化对象，供其它 MCP 直接消费（内部的分 Claim 提取实现在 dbcore.extract）。 |
| `fingerprint_sql` | 生成 SQL 指纹与归一化形式（跨 MCP 关联键之一）。 |
| `list_profiles` | 枚举可用的规则 profile（规则启停配置），用于在 review / review_batch 里套用。 |
| `list_rules` | 规则清单：按分类/严重度/方言过滤，并标出每条是否对当前方言生效。 |
| `review_batch` | 批量体检，输出 PASS/FAIL 门禁结论。fail_on 可选 critical\|high\|medium\|low |
| `review_diff` | 只体检 git diff 中新增/修改的 SQL —— CI 门禁的真实用法。 |
| `review_file` | 扫描文件/目录中的 SQL。支持 .sql / MyBatis XML / Java 注解与字符串常量。 |
| `review_sql` | 静态体检。不需要连库，最适合挂 CI。返回 verdict + findings。 |
| `suggest_fixes` | 对每条问题给出可执行改法，并把改写后的 SQL 重新体检一遍证明改对了。 |
| `verify_fix` | 验证改写是否等价。给 url 用真实行数比对；不给则先做静态结构比对。 |

## 闭环

`review_sql` 体检 → `suggest_fixes` 出改法 → `verify_fix` 验证改写是否等价 → `compare_plans` 量化收益 → `advise_indexes` 索引建议

## Tier（冷启动保证）

全部 `tier0`：零历史、零数据库可用。带 `instance_id` 调用时，发现会自动写入 `dbt-store` 的技术债账本（`open → fixed → verified`）。

## 边界：这个仓不做什么

只做 SQL 本身的好坏判断。方言转换归 `mcp-migration-diff`，变更执行归 `mcp-change-risk`，根因归 `mcp-dbwatch`。
需要下一步时只在结果里写 `next_actions`，**不 import 其他 MCP 的代码**。

## 开发

```bash
python tests/test_sql_review.py      # 不需要 pytest，也不需要数据库
```
