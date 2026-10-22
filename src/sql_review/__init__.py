"""sql_review —— SQL 静态体检与修法（Tier0 可零依赖运行）。

业务判断全部留在本包；跨 MCP 通用能力来自 dbt-core / dbt-store / dbt-know，
MCP 运行时外壳来自 dbt-core.mcp_kit。本包不 import 任何其他 MCP。
"""

from sql_review import advisor, fix, rules, scanner, verify
from sql_review.server import MCP_NAME, TOOLS, main
__all__ = ["MCP_NAME", "TOOLS", "main", "rules", "fix", "verify", "advisor", "scanner"]

__version__ = "0.2.0"
