"""MCP 服务配置"""
import os
from typing import List

# 搜索引擎配置
DEFAULT_ENGINES: List[str] = ["bing", "duckduckgo", "baidu"]
ALL_ENGINES: List[str] = ["bing", "duckduckgo", "baidu"]

# 默认参数
DEFAULT_TOP_K = 10
DEFAULT_CRAWL_CONCURRENCY = 8
DEFAULT_MAX_CHARS = 5000
DEFAULT_STATE_TTL = 7200  # 2小时

# 运行模式 (默认非 headless，以便用户能看到验证页面)
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"

# Debug 模式 (MCP 服务默认关闭)
DEBUG = os.getenv("DEBUG", "false").lower() == "true"
