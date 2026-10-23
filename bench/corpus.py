"""标注评测语料：SQL 静态审查引擎的精度基准（anti-pattern 级）。

每个样本含：
- sql      : 被测 SQL
- dialect  : 方言（默认 mysql；PG 专属规则用 postgres）
- opts     : 透传给 review_sql 的上下文参数（partition_keys / deep_offset_threshold …）
- expect   : 该样本「应当」产出的规则 ID 集合（ground truth）
- note     : 标注理由

三类样本：
1. 正例：应当触发某条/几条规则（覆盖全部 37 条规则至少一次）。
2. FP 陷阱（expect=[]）：表面像违规、但按设计不应报——用来量化「误报率」。
   这些是 dbcore 误报治理（heuristics.py）要压住的：
   - 日期/文本列与字符串比较（TYPE001 跳过）
   - 点写 WHERE id=1（DML002 跳过）
   - 非相关子查询（SUBQ 跳过）
3. 干净负例（expect=[]）：正常业务 SQL，用来给整体误报率施压。

指标口径对齐 SQLFlex (arXiv 2603.16155) 与 FSE 2026：
以「规则 ID」为 anti-pattern 单元，按 TP/FP/FN 算全局与每条规则的 P/R/F1。
"""

# 公共方言
MY = "mysql"
PG = "postgres"

SAMPLES: list[dict] = [
    # ---------------- DDL / 结构变更 ----------------
    {"id": "DDL004-TRUNCATE", "sql": "TRUNCATE TABLE orders", "dialect": MY,
     "expect": ["DDL004"], "note": "TRUNCATE 不可回滚"},
    {"id": "DDL005-DROP", "sql": "DROP TABLE tmp_log", "dialect": MY,
     "expect": ["DDL005", "DDL000"], "note": "DROP 同时触发通用 DDL000"},
    {"id": "DDL005-DROPDB", "sql": "DROP DATABASE scratch", "dialect": MY,
     "expect": ["DDL005", "DDL000"], "note": "DROP DATABASE 同样"},
    {"id": "DDL000-CREATE", "sql": "CREATE TABLE t(a INT, b VARCHAR(10))", "dialect": MY,
     "expect": ["DDL000"], "note": "应用 SQL 出现 DDL"},
    {"id": "DDL006-MODIFY", "sql": "ALTER TABLE users MODIFY COLUMN age BIGINT", "dialect": MY,
     "expect": ["DDL006", "DDL000"], "note": "改列类型 + 通用 DDL000"},
    {"id": "DDL006-CHANGE", "sql": "ALTER TABLE users CHANGE COLUMN a b INT", "dialect": MY,
     "expect": ["DDL006", "DDL000"], "note": "CHANGE COLUMN 改列类型"},
    {"id": "PG001-INDEX", "sql": "CREATE INDEX idx ON t (a)", "dialect": PG,
     "expect": ["PG001", "DDL000"], "note": "PG 建索引未用 CONCURRENTLY + DDL000"},
    {"id": "PG002-VACFULL", "sql": "VACUUM FULL", "dialect": PG,
     "expect": ["PG002"], "note": "PG 独占锁重整整表"},

    # ---------------- DML ----------------
    {"id": "DML001-UPDATE", "sql": "UPDATE users SET status = 'x'", "dialect": MY,
     "expect": ["DML001"], "note": "UPDATE 缺 WHERE → 全表"},
    {"id": "DML001-DELETE", "sql": "DELETE FROM logs", "dialect": MY,
     "expect": ["DML001"], "note": "DELETE 缺 WHERE → 全表"},
    {"id": "DML002-RANGE", "sql": "UPDATE orders SET status='x' WHERE created_at < '2020-01-01'",
     "dialect": MY, "expect": ["DML002"],
     "note": "范围条件、非点写、未分批 → 大事务风险；created_at 日期列不触发 TYPE001"},
    {"id": "DML003-INSERT", "sql": "INSERT INTO users VALUES (1, 'a')", "dialect": MY,
     "expect": ["DML003"], "note": "INSERT 缺列清单"},
    {"id": "DML004-REPLACE", "sql": "REPLACE INTO users (id) VALUES (1)", "dialect": MY,
     "expect": ["DML004"], "note": "REPLACE INTO 先删后插"},
    {"id": "DML005-IGNORE", "sql": "INSERT IGNORE INTO users (id) VALUES (1)", "dialect": MY,
     "expect": ["DML005"], "note": "INSERT IGNORE 静默吞错"},

    # ---------------- SELECT / 查询结构 ----------------
    {"id": "SEL001-STAR", "sql": "SELECT * FROM users", "dialect": MY,
     "expect": ["SEL001"], "note": "SELECT *"},
    {"id": "SEL002-LIKE", "sql": "SELECT id FROM users WHERE name LIKE '%abc'", "dialect": MY,
     "expect": ["SEL002"], "note": "前导通配符 LIKE 必全扫；name 文本列不影响本规则"},
    {"id": "SEL003-FUNC", "sql": "SELECT id FROM users WHERE YEAR(created_at) = 2024", "dialect": MY,
     "expect": ["SEL003"], "note": "WHERE 字段套函数使索引失效"},
    {"id": "SEL004-RAND", "sql": "SELECT id FROM users ORDER BY RAND() LIMIT 10", "dialect": MY,
     "expect": ["SEL004"], "note": "ORDER BY RAND() 全量排序；LIMIT 避免误带 SEL006"},
    {"id": "SEL006-LIMIT", "sql": "SELECT id FROM users LIMIT 10", "dialect": MY,
     "expect": ["SEL006"], "note": "LIMIT 无 ORDER BY"},
    {"id": "SEL008-OR", "sql": "SELECT id FROM users WHERE a = 1 OR b = 2", "dialect": MY,
     "expect": ["SEL008"], "note": "WHERE 存在 OR"},
    {"id": "SEL007-JOINNOON", "sql": "SELECT a.id FROM a INNER JOIN b", "dialect": MY,
     "expect": ["JOIN001", "SEL007", "JOIN002"],
     "note": "显式 INNER JOIN 缺 ON 无关联条件：JOIN001(low)+SEL007(high)+JOIN002(blocker) 三连发"},
    {"id": "JOIN002-COMMA", "sql": "SELECT a.id, b.id FROM a, b", "dialect": MY,
     "expect": ["JOIN001", "JOIN002"], "note": "逗号两表无关联条件 → 笛卡尔积；本版 sqlglot 逗号也产出 Join 节点，故 JOIN001 一并触发"},
    {"id": "JOIN003-DEEP", "sql": "SELECT t1.id FROM t1 JOIN t2 ON t1.id=t2.id "
     "JOIN t3 ON t2.id=t3.id JOIN t4 ON t3.id=t4.id JOIN t5 ON t4.id=t5.id "
     "JOIN t6 ON t5.id=t6.id JOIN t7 ON t6.id=t7.id JOIN t8 ON t7.id=t8.id "
     "ORDER BY t1.id LIMIT 10", "dialect": MY,
     "expect": ["JOIN003"], "note": "8 个 JOIN 超阈值；LIMIT+ORDER BY id 避免误带 AGG002/ORDER001"},
    {"id": "AGG001-COUNT", "sql": "SELECT COUNT(status) FROM users", "dialect": MY,
     "expect": ["AGG001"], "note": "COUNT(列) 不数 NULL"},
    {"id": "AGG003-DISTGROUP", "sql": "SELECT DISTINCT a, COUNT(*) FROM t GROUP BY a", "dialect": MY,
     "expect": ["AGG003"], "note": "DISTINCT 与 GROUP BY 同时用"},
    {"id": "AGG002-BIGRES", "sql": "SELECT a.id, b.name, c.x FROM a JOIN b ON a.id=b.aid "
     "JOIN c ON b.id=c.bid", "dialect": MY,
     "expect": ["AGG002"], "note": "三表 JOIN 无聚合无 LIMIT → 可能海量结果（需 2 个 JOIN 节点）"},
    {"id": "AGG004-UNION", "sql": "SELECT id FROM a UNION SELECT id FROM b", "dialect": MY,
     "expect": ["AGG004"], "note": "UNION 而非 UNION ALL"},
    {"id": "ORDER001-UNSTABLE", "sql": "SELECT id FROM users ORDER BY created_at LIMIT 10",
     "dialect": MY, "expect": ["ORDER001"],
     "note": "排序键 created_at 非唯一且无 id → 分页不稳"},
    {"id": "PART001-NOPART", "sql": "SELECT id FROM orders WHERE user_id = 1", "dialect": MY,
     "opts": {"partition_keys": {"orders": "dt"}},
     "expect": ["PART001"], "note": "分区键 dt 未出现在过滤条件 → 扫全分区"},
    {"id": "LOCK003-FORUPD", "sql": "SELECT id FROM accounts FOR UPDATE", "dialect": MY,
     "expect": ["LOCK003"], "note": "FOR UPDATE 缺等值条件 → 锁扫描行"},
    {"id": "MY001-ALTER", "sql": "ALTER TABLE users ADD INDEX idx_name (name)", "dialect": MY,
     "expect": ["MY001", "DDL000"], "note": "MySQL ALTER 未指定 ALGORITHM + 通用 DDL000"},
    {"id": "SEL005-DEEP", "sql": "SELECT id FROM users ORDER BY id LIMIT 10 OFFSET 5000",
     "dialect": MY, "opts": {"deep_offset_threshold": 1000},
     "expect": ["SEL005"], "note": "深度分页 OFFSET 5000 ≥ 阈值；ORDER BY id 避免误带 ORDER001"},

    # ---------------- 子查询 ----------------
    {"id": "SUBQ001-CORR", "sql": "SELECT id, (SELECT COUNT(*) FROM o WHERE o.uid = u.id) AS cnt "
     "FROM u", "dialect": MY, "expect": ["SUBQ001"],
     "note": "SELECT 列表相关子查询（带表名限定引用外层）"},
    {"id": "SUBQ002-CORR", "sql": "SELECT id FROM u WHERE id IN "
     "(SELECT uid FROM o WHERE o.x = u.x)", "dialect": MY, "expect": ["SUBQ002"],
     "note": "WHERE IN 相关子查询（MySQL 方言）"},
    {"id": "TYPE001-NUM", "sql": "SELECT id FROM u WHERE user_id = '123'", "dialect": MY,
     "expect": ["TYPE001"], "note": "数值列 user_id 与数字形态字符串比较 → high"},
    {"id": "TYPE002-NOTIN", "sql": "SELECT id FROM u WHERE id NOT IN (SELECT id FROM o)",
     "dialect": MY, "expect": ["TYPE002"], "note": "NOT IN 优化器难走索引"},
    {"id": "NULL001-NEQ", "sql": "SELECT id FROM u WHERE status <> 'x'", "dialect": MY,
     "expect": ["NULL001", "TYPE002"], "note": "col <> 'x' 漏 NULL 行（NULL001）+ 否定条件（TYPE002）"},
    {"id": "TX002-BEGIN", "sql": "BEGIN", "dialect": MY,
     "expect": ["TX002"], "note": "应用层事务控制语句"},
    {"id": "TX003-AUTOCOMMIT", "sql": "SET autocommit = 0", "dialect": MY,
     "expect": ["TX003"], "note": "关闭 autocommit 留长事务"},

    # ---------------- FP 陷阱：表面像违规但按设计不应报 ----------------
    {"id": "NEG-DML002-POINT", "sql": "UPDATE orders SET status='x' WHERE id = 123", "dialect": MY,
     "expect": [], "note": "点写（命中键列）→ DML002 必须跳过"},
    {"id": "NEG-TYPE-DATE", "sql": "SELECT id FROM u WHERE created_at > '2024-01-01'", "dialect": MY,
     "expect": [], "note": "日期列与字符串比较是标准写法 → TYPE001 必须跳过"},
    {"id": "NEG-TYPE-TEXT", "sql": "SELECT id FROM u WHERE status = 'active'", "dialect": MY,
     "expect": [], "note": "文本列与字符串比较本来就对 → TYPE001 必须跳过"},
    {"id": "NEG-TYPE-KEY-INT", "sql": "SELECT id FROM u WHERE user_id = 123", "dialect": MY,
     "expect": [], "note": "数值列但字面量是整数（非字符串）→ 不算隐式转换，TYPE001 必须跳过"},
    {"id": "NEG-SUBQ1-UNCORR", "sql": "SELECT id, (SELECT MAX(x) FROM o) AS m FROM u", "dialect": MY,
     "expect": [], "note": "非相关标量子查询只算一次 → SUBQ001 必须跳过"},
    {"id": "NEG-SUBQ2-UNCORR", "sql": "SELECT id FROM u WHERE id IN (SELECT uid FROM o)",
     "dialect": MY, "expect": [], "note": "非相关 IN 子查询 → SUBQ002 必须跳过"},
    {"id": "NEG-DML001-WHERE", "sql": "UPDATE users SET status='x' WHERE id=1", "dialect": MY,
     "expect": [], "note": "有 WHERE 的 UPDATE 不应报 DML001"},
    {"id": "NEG-FUNC-IN-SELECT", "sql": "SELECT UPPER(name) FROM users WHERE id = 1", "dialect": MY,
     "expect": [], "note": "SELECT 列表里套函数不应触发 SEL003（只查 WHERE）"},
    {"id": "NEG-LIKE-SUFFIX", "sql": "SELECT id FROM users WHERE name LIKE 'abc%'", "dialect": MY,
     "expect": [], "note": "后缀通配符可走索引 → SEL002 不应触发"},

    # ---------------- 干净负例：正常业务 SQL，施压整体误报率 ----------------
    {"id": "CLEAN-SIMPLE", "sql": "SELECT id FROM users WHERE id = 1", "dialect": MY,
     "expect": [], "note": "最常见主键点查，零发现"},
    {"id": "CLEAN-JOIN", "sql": "SELECT a.id FROM a JOIN b ON a.id = b.aid WHERE a.x = 1",
     "dialect": MY, "expect": [], "note": "显式 JOIN 带 ON + 主键过滤"},
    {"id": "CLEAN-SELECT", "sql": "SELECT id, name FROM users WHERE id = 1 ORDER BY id LIMIT 20",
     "dialect": MY, "expect": [], "note": "指定列 + 主键 + 稳定排序 + LIMIT"},
    {"id": "CLEAN-INSERT", "sql": "INSERT INTO users (id, name) VALUES (1, 'a')", "dialect": MY,
     "expect": [], "note": "带列清单的 INSERT"},
    {"id": "CLEAN-UPDATE-KEY", "sql": "UPDATE users SET name='x' WHERE user_id = 5", "dialect": MY,
     "expect": [], "note": "点写（整数字面量，非字符串）→ DML002/TYPE001 均不报"},
    {"id": "CLEAN-AGG", "sql": "SELECT u.id, COUNT(*) FROM u JOIN o ON u.id=o.uid GROUP BY u.id",
     "dialect": MY, "expect": [], "note": "规范聚合查询（COUNT(*) 不触发 AGG001），无 AGG002（含聚合）"},
    {"id": "CLEAN-PART", "sql": "SELECT id FROM orders WHERE dt = '2024-01-01' AND user_id = 1",
     "dialect": MY, "opts": {"partition_keys": {"orders": "dt"}},
     "expect": [], "note": "分区键 dt 已命中 → PART001 不报"},
    {"id": "CLEAN-INSERT-SELECT",
     "sql": "INSERT INTO t1 (a, b) SELECT a, b FROM t2 WHERE id > 100", "dialect": MY,
     "expect": [], "note": "带列清单的 INSERT ... SELECT"},
]


def by_category(reg) -> dict:
    """返回 rule_id -> category，便于报告分组（非必需）。"""
    return {r.rule_id: r.category for r in reg.all()}
