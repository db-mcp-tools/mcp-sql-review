"""误报治理用的判定工具 —— 「这条 SQL 到底值不值得报」。

背景：规则引擎没有 schema，任何"这列是什么类型/是不是主键"的判断都只能是
命名启发式。既然如此，标准就只能是**只在能判断出「这大概率是错的」时才报**：

  · 判得出错 → 按原严重度报（如 `amount = '100'`、相关子查询）
  · 判不出   → 不报，或降级成 low（提示"需确认"，而不是断言）

这里集中放三类判定，供 dml / subquery / type 三个分册共用：

  1. 列名画像：日期 / 文本 / 键（is_date_like / is_text_like / is_key_like）
  2. WHERE 形态：是不是"点写"（is_point_write）
  3. 子查询相关性：是否引用外层列（outer_relations / is_correlated）

为什么不用 schema：`review_sql` 的定位是"贴一段 SQL 就能用"，不连库。
真连了库的场景，`table_rows` / `partition_keys` 这类已确认的信息通过 ctx 传进来。
"""

from __future__ import annotations

import re

from sqlglot import exp

# --- 列名画像 -------------------------------------------------------
# 日期时间列：与字符串字面量比较是**标准写法**（SQL 没有通用日期字面量），
# 对它们报"隐式转换"纯属噪音。
_DATE_LIKE_RE = re.compile(
    r"(^|_)(at|date|dates|day|time|datetime|timestamp|ts|epoch"
    r"|created|updated|deleted|expired|start|end|begin|finish)$",
    re.I,
)

# 文本列：与字符串比较本来就对，不存在隐式转换。
# 注意 `*_no` / `*_code` 归到这里 —— 它们通常是 varchar 单号，
# 而 core._NUMERIC_COL_RE 把 `no` / `codes?` 当数值特征，会误判成 high。
_TEXT_LIKE_RE = re.compile(
    r"(^|_)(name|code|codes|no|phone|mobile|email|status|state|type|kind"
    r"|level|flag|remark|memo|comment|desc|description|url|uri|path"
    r"|uuid|guid|token|sn|serial|account|username|nickname|address|ip|mac"
    r"|version|tag|tags|category|channel|source|city|country|region)$",
    re.I,
)

# 键：等值命中它时，"影响行数"基本可以认为有界（1 行或个位数）。
_KEY_LIKE_RE = re.compile(
    r"(^|_)(id|uid|pk|uuid|guid|sn|key|no|code)$",
    re.I,
)


def is_date_like(colname: str) -> bool:
    return bool(_DATE_LIKE_RE.search(colname or ""))


def is_text_like(colname: str) -> bool:
    return bool(_TEXT_LIKE_RE.search(colname or ""))


def is_key_like(colname: str) -> bool:
    return bool(_KEY_LIKE_RE.search(colname or ""))


# --- WHERE 形态 -----------------------------------------------------


def _conjuncts(where: exp.Expression | None) -> list[exp.Expression] | None:
    """把 WHERE 拆成 AND 连接的合取项；出现 OR 就返回 None（不是合取）。"""
    if where is None:
        return None
    out: list[exp.Expression] = []

    def walk(node: exp.Expression) -> bool:
        if isinstance(node, exp.Or):
            return False
        if isinstance(node, exp.And):
            return walk(node.this) and walk(node.expression)
        if isinstance(node, exp.Paren):
            return walk(node.this)
        out.append(node)
        return True

    body = where.this if isinstance(where, exp.Where) else where
    return out if walk(body) else None


def is_point_write(where: exp.Expression | None) -> bool:
    """`WHERE id = 1` / `WHERE order_no = 'X' AND tenant_id = 3` 这类点写。

    判据三条，全满足才算：
      1. 只有 AND，没有 OR / 范围 / LIKE / 函数
      2. 每个条件都是等值（或字面量 IN 列表，且不含子查询）
      3. 至少一个等值落在"像键"的列上

    只满足 1、2 而没有任何键列（如 `status = 'x'`）不算点写 ——
    那种语句真的可能改掉半张表。
    """
    parts = _conjuncts(where)
    if not parts:
        return False
    key_hit = False
    for node in parts:
        if isinstance(node, exp.EQ):
            col = node.this if isinstance(node.this, exp.Column) else node.find(exp.Column)
            if col is not None and is_key_like(col.name or ""):
                key_hit = True
        elif isinstance(node, exp.In):
            if node.find(exp.Subquery) is not None:
                return False          # IN (子查询) 的影响面不可知
            col = node.this if isinstance(node.this, exp.Column) else node.find(exp.Column)
            if col is not None and is_key_like(col.name or ""):
                key_hit = True
        elif isinstance(node, exp.Between):
            return False
        else:
            return False
    return key_hit


# --- 子查询相关性 ---------------------------------------------------


def outer_relations(ast: exp.Expression) -> set[str]:
    """外层语句直接引用的表名 / 别名（不含子查询内部定义的表）。"""
    names: set[str] = set()
    frm = ast.args.get("from_") or ast.args.get("from")
    containers: list[exp.Expression] = []
    if frm is not None:
        containers.append(frm)
    containers.extend(ast.args.get("joins") or [])

    for c in containers:
        # 跳过派生表内部：它自己的表名不是"外层"
        for t in c.find_all(exp.Table):
            if t.find_ancestor(exp.Subquery) is not None:
                continue
            if t.alias_or_name:
                names.add(t.alias_or_name.lower())
        for s in c.find_all(exp.Subquery):
            if s.alias:
                names.add(s.alias.lower())
    return names


def is_correlated(sub: exp.Expression, outer: set[str]) -> bool:
    """子查询是否引用外层列（`WHERE u.id = o.uid` 里的 o.uid）。

    只看**带表名限定**的列：不带限定的列在静态分析里无法判断归属，
    宁可漏报也不猜 —— 这条规则是 high 严重度，误报成本高于漏报。
    """
    if not outer:
        return False
    inner = {t.alias_or_name.lower() for t in sub.find_all(exp.Table) if t.alias_or_name}
    for col in sub.find_all(exp.Column):
        tbl = (col.table or "").lower()
        if tbl and tbl in outer and tbl not in inner:
            return True
    return False
