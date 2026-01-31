import asyncio
from typing import List
from playwright.async_api import async_playwright

from search_engines import multi_search_with_context
from page_crawler import crawl_page_content
from models import PageResult
from utils import move_browser_window_offscreen, clean_page_text
from state_cache import StateCacheManager
import time

async def main() -> List[PageResult]:
    query = "小米vs保时捷"

    # 初始化状态管理器
    state_manager = StateCacheManager(ttl_seconds=7200)  # 2小时

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        
        # 加载已保存的状态
        state = await state_manager.load_merged_state(["bing", "yandex", "duckduckgo", "baidu"])
        context = await browser.new_context(
            storage_state=state if state else None
        )

        # 保持一个窗口存在（防止某些平台自动关闭）
        await context.new_page()
        await move_browser_window_offscreen(browser)

        # ========= ① 搜索阶段 =========
        results = await multi_search_with_context(
            context=context,
            query=query,
            engines=["bing", "yandex", "duckduckgo", "baidu"],
            top_k_each=10,
            concurrency_tabs=3,
            state_manager=state_manager,
        )
        print(f"[Search] got {len(results)} urls")
        
        time.sleep(10)  # 等待浏览器稳定下来
        # ========= ② 抓取阶段 =========
        sem = asyncio.Semaphore(8)

        tasks = [
            crawl_page_content(context, sem, it, max_chars=10000)
            for it in results
        ]

        # ⚠️ 关键：不要让一个页面炸掉整个 pipeline
        pages_raw = await asyncio.gather(*tasks, return_exceptions=True)

        pages: List[PageResult] = []
        for r in pages_raw:
            if isinstance(r, Exception):
                continue
            r.text = clean_page_text(r.text)
            pages.append(r)

        # 保存最终状态
        await state_manager.save_context_state(context, "bing")
        await state_manager.save_context_state(context, "yandex")
        await state_manager.save_context_state(context, "duckduckgo")
        await state_manager.save_context_state(context, "baidu")

        await browser.close()

    return pages


if __name__ == "__main__":
    pages = asyncio.run(main())

    good = [p for p in pages if p.is_good]
    bad = [p for p in pages if not p.is_good]

    print(f"\n[Result] good={len(good)} bad={len(bad)}")

    for i, p in enumerate(good[:5], 1):
        print(f"\n=== {i}. {p.page_title or p.title} ===")
        print(p.final_url or p.url)
        print(p.text[:1500])
    
    for p in bad[:20]:
        print("\n--- BAD ---")
        print("url:", p.final_url or p.url)
        print("method:", p.method, "score:", p.score, "len:", len(p.text))
        print("cands:", [(c.method, round(c.score,1), c.len) for c in (p.candidates or [])])
        print("head:", (p.text or "")[:200].replace("\n"," "))
