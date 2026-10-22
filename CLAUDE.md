# mcp-sql-review 加规则的检查清单

## 加一条规则

- [ ] 规则函数放进 `src/sql_review/rulebook/<场景>.py`（select / dml / type / tx / engine / subquery）
- [ ] **一个函数只对应一个 rule_id** —— 否则 `disable`/`enable` 无法精确到单条
- [ ] 在分册的 `META` 里写：`name` / `category` / `severity` / `why` / `false_positive`
- [ ] `_mk()` 里的 severity **必须与 META 一致**（有测试会校验这条，实测踩过 7 次）
- [ ] 属于特定引擎的，加 `engines: ["mysql"]` 之类的约束
- [ ] 至少 1 个命中样例 + 1 个「不许误报」样例，写进 `tests/test_sql_review.py`
- [ ] 能自动改写的，在 `fix.py` 里加 FIXER；不能的明确标 `manual`

## 误报比漏报更糟

一条乱报的规则会让整套工具失去信任。写规则时先想「什么情况下这个判断是错的」，
把答案填进 `false_positive`，并写成测试用例。

历史踩过的误报：

- `WHERE id = 2` 被报隐式类型转换 → Literal 用 `is_string` 判断，且只对数值列名给 high
- `FROM a, b WHERE a.x = b.x` 被报笛卡尔积 → 要识别等值关联与 IN/EXISTS 半连接

## 改规则后必跑

```bash
python tests/test_sql_review.py     # 含「不许误报」反向用例
```
