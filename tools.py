"""MCP 工具定义"""
import asyncio
import json
import sys
from typing import List, Literal, Any
from pathlib import Path
from mcp.server import Server
from mcp.types import Tool, TextContent
from playwright.async_api import async_playwright

# 添加 webSeach 模块路径
sys.path.insert(0, str(Path(__file__).parent / "webSeach"))

from search_engines import multi_search_with_context
from page_crawler import crawl_page_content
from models import PageResult
from utils import clean_page_text, move_browser_window_offscreen
from state_cache import StateCacheManager
from config import (
    DEFAULT_TOP_K, DEFAULT_CRAWL_CONCURRENCY,
    DEFAULT_MAX_CHARS, DEFAULT_STATE_TTL, HEADLESS
)

# 固定使用所有搜索引擎
ALL_ENGINES = ["duckduckgo", "baidu"]

# 创建 MCP 服务器
server = Server("webSeach-server")

# 工具定义
TOOLS = [
    Tool(
        name="web_search",
        description="执行网络搜索并抓取页面内容。使用搜索引擎，自动过滤高质量结果并去重。",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索查询内容，尽可能使用核心关键词，关键词最好不超过3个。",
                },
                "top_k": {
                    "type": "integer",
                    "description": "每个搜索引擎用于初始检索的候选 URL 数量（默认 15）。系统会先从各引擎获取一批结果，再进行去重、质量过滤与页面解析，最终返回与查询最相关的前 top_k 条有效内容。由于页面可解析性与内容质量限制，通常只有约 60% 的候选页面能够成功提取有效正文，因此不建议随意调整该参数。",
                    "default": 15,
                    "minimum": 5,
                    "maximum": 20
                },
                "format": {
                    "type": "string",
                    "enum": ["json", "md"],
                    "description": "返回格式（默认: json），参数可选值有 'json' 和 'md'",
                    "default": "json"
                }
            },
            "required": ["query"]
        }
    )
]


@server.list_tools()
async def list_tools() -> List[Tool]:
    """列出可用工具"""
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: Any) -> List[TextContent]:
    """处理工具调用"""
    if name == "web_search":
        return await _web_search(**arguments)
    else:
        raise ValueError(f"Unknown tool: {name}")


async def _web_search(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    crawl_concurrency: int = DEFAULT_CRAWL_CONCURRENCY,
    max_chars: int = DEFAULT_MAX_CHARS,
    format: Literal["json", "md"] = "json"
) -> List[TextContent]:
    """
    执行网络搜索并抓取页面内容
    """
    # 清空 debug.log
    log_file = Path(__file__).parent / ".cache" / "debug.log"
    try:
        log_file.write_text("", encoding="utf-8")
    except Exception:
        pass

    engines = ALL_ENGINES.copy()

    state_manager = StateCacheManager(ttl_seconds=DEFAULT_STATE_TTL)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)

        state = await state_manager.load_merged_state(engines)
        context = await browser.new_context(
            storage_state=state if state else None
        )

        # 保持窗口（非 headless 模式）
        if not HEADLESS:
            await context.new_page()
            await move_browser_window_offscreen(browser)

        # 搜索阶段
        results = await multi_search_with_context(
            context=context,
            query=query,
            engines=engines,
            top_k_each=top_k,
            concurrency_tabs=3,
            state_manager=state_manager,
        )

        # 抓取阶段
        sem = asyncio.Semaphore(crawl_concurrency)
        tasks = [
            crawl_page_content(context, sem, it, max_chars=max_chars)
            for it in results
        ]
        pages_raw = await asyncio.gather(*tasks, return_exceptions=True)

        pages: List[PageResult] = []
        for r in pages_raw:
            if isinstance(r, Exception):
                continue
            r.text = clean_page_text(r.text)
            pages.append(r)

        # 保存状态
        for engine in engines:
            await state_manager.save_context_state(context, engine)

        await browser.close()

    # 过滤 good 结果并去重
    good_results = [p for p in pages if p.is_good]

    # 去重（按 URL）
    seen_urls = set()
    deduped = []
    for p in good_results:
        url = str(p.final_url or p.url)
        if url not in seen_urls:
            seen_urls.add(url)
            deduped.append(p)

    # 格式化返回
    if format == "md":
        result_text = _format_markdown(deduped, query)
    else:
        result_text = _format_json(deduped, query)

    return [TextContent(type="text", text=result_text)]


def _format_json(pages: List[PageResult], query: str) -> str:
    """格式化为 JSON"""
    results = []
    for p in pages:
        results.append({
            "title": p.page_title or p.title,
            "url": str(p.final_url or p.url),
            "engine": p.engine,
            "content": p.text,
            "method": p.method,
            "score": p.score
        })
    return json.dumps({
        "query": query,
        "count": len(results),
        "results": results
    }, ensure_ascii=False, indent=2)


def _format_markdown(pages: List[PageResult], query: str) -> str:
    """格式化为 Markdown"""
    lines = [f"# 搜索结果: {query}\n", f"找到 {len(pages)} 个结果\n"]
    for i, p in enumerate(pages, 1):
        lines.append(f"## {i}. {p.page_title or p.title}")
        lines.append(f"**来源**: {p.engine} | **URL**: {p.final_url or p.url}")
        if p.method:
            lines.append(f"**方法**: {p.method} | **分数**: {p.score}")
        lines.append(f"\n{p.text}\n")
        lines.append("---\n")
    return "\n".join(lines)
