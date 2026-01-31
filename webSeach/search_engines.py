import asyncio
import random
import os
from dataclasses import dataclass
from typing import List, Tuple, Dict, Callable, Optional
from urllib.parse import quote
from playwright.async_api import async_playwright
from utils import human_sleep, captcha_lock, captcha_pause_lock, is_captcha_page, wait_for_captcha_resolution
from state_cache import StateCacheManager

# Debug 控制 (默认关闭，可通过环境变量 DEBUG=true 开启)
DEBUG = os.getenv("DEBUG", "true").lower() == "true"

def _log(*args, **kwargs):
    """仅在 DEBUG 模式下输出日志"""
    if DEBUG:
        print(*args, **kwargs)

@dataclass
class Engine:
    name: str
    build_url: Callable[[str], str]
    result_selector: str
    clean_title: Optional[Callable[[str], str]] = None
    wait_ms_after_nav: int = 1200
    extra_wait_ms: int = 0
    keep_tab_open: bool = True
    pre_goto: Optional[Callable] = None
    post_goto: Optional[Callable] = None


def default_clean_title(s: str) -> str:
    return " ".join(s.split()).strip()


async def baidu_post_goto(page, query: str, eng: Engine) -> None:
    await human_sleep(eng.extra_wait_ms, 500)

async def bing_post_goto(page, query: str, eng: Engine) -> None:
    """模拟人类浏览后的额外操作"""
    await human_sleep(eng.extra_wait_ms, 500)
    try:
        await page.evaluate("window.scrollBy(0, 100)")
        await human_sleep(300, 200)
    except:
        pass

ENGINES: List[Engine] = [
    Engine(
        name="bing",
        build_url=lambda q: f"https://www.bing.com/search?q={quote(q)}",
        result_selector="li.b_algo h2 a",
        clean_title=default_clean_title,
        wait_ms_after_nav=2202,
        extra_wait_ms=800,
        post_goto=bing_post_goto,
    ),
    Engine(
        name="duckduckgo",
        build_url=lambda q: f"https://duckduckgo.com/?q={quote(q)}&ia=web",
        result_selector="a[data-testid='result-title-a']",
        clean_title=default_clean_title,
        wait_ms_after_nav=1344,
    ),
    Engine(
        name="baidu",
        build_url=lambda q: f"https://www.baidu.com/s?wd={quote(q)}",
        result_selector="div#content_left h3 a, div#content_left a",
        clean_title=default_clean_title,
        wait_ms_after_nav=1500,
        extra_wait_ms=1200,
        post_goto=baidu_post_goto,
    ),
]
async def extract_results(page, selector: str, top_k: int) -> List[Tuple[str, str]]:
    results = []
    loc = page.locator(selector)

    try:
        count = await loc.count()
    except Exception:
        return results

    for i in range(min(count, top_k)):
        a = loc.nth(i)
        try:

            await human_sleep(320, 120)
            title = await a.text_content(timeout=3000)
            href = await a.get_attribute("href", timeout=3000)

            if not title or not href:
                continue

            title = title.strip()
            results.append((title, href))

        except Exception:
            continue

    return results

async def fetch_engine(
    context,
    sem,
    query,
    eng,
    top_k_each,
    state_manager: StateCacheManager = None,
):
    async with sem:
        await human_sleep(800, 400)

        page = await context.new_page()

        try:
            _log(f"[{eng.name}] 正在访问: {eng.build_url(query)}")
            await human_sleep(180, 60)
            await page.goto(eng.build_url(query), wait_until="domcontentloaded")

            # 获取页面信息用于诊断
            current_url = page.url
            try:
                page_title = await page.title()
                _log(f"[{eng.name}] 当前页面 - URL: {current_url}, Title: {page_title}")
            except:
                _log(f"[{eng.name}] 当前页面 - URL: {current_url}, Title: [无法获取]")

            # CAPTCHA detection and handling
            is_captcha = await is_captcha_page(page)
            _log(f"[{eng.name}] 验证检测结果: {'发现验证' if is_captcha else '无验证'}")

            if is_captcha:
                # Acquire global pause lock BEFORE waiting - this blocks other tasks
                await captcha_pause_lock.acquire()
                _log(f"[{eng.name}] 检测到人机验证，暂停其他任务，等待用户完成...")

                async with captcha_lock:
                    await page.bring_to_front()
                    _log(f"[{eng.name}] 页面已置于前台，URL: {page.url}")
                    try:
                        await wait_for_captcha_resolution(page)
                        # Save state after verification
                        if state_manager:
                            await state_manager.save_context_state(context, eng.name)
                        _log(f"[{eng.name}] 验证完成！")
                    except TimeoutError:
                        _log(f"[{eng.name}] 验证超时")
                        pass
                    finally:
                        # Release global pause lock to let other tasks continue
                        captcha_pause_lock.release()

            await human_sleep(eng.wait_ms_after_nav, 500)

            if eng.post_goto:
                await eng.post_goto(page, query, eng)

            items = await extract_results(page, eng.result_selector, top_k_each)
            _log(f"[{eng.name}] 提取到 {len(items)} 条结果")

            return [
                {
                    "engine": eng.name,
                    "title": eng.clean_title(t) if eng.clean_title else t,
                    "url": u,
                }
                for t, u in items
            ]

        finally:
            if not eng.keep_tab_open:
                await page.close()

async def multi_search_with_context(
    context,
    query: str,
    engines: List[str] = None,
    top_k_each: int = 10,
    concurrency_tabs: int = 3,
    state_manager: StateCacheManager = None,
) -> List[Dict]:
    chosen = set(e.lower() for e in engines) if engines else None
    engine_list = [e for e in ENGINES if (chosen is None or e.name in chosen)]
    if not engine_list:
        raise ValueError("No engines selected.")

    sem = asyncio.Semaphore(concurrency_tabs)

    tasks = [
        fetch_engine(context, sem, query, eng, top_k_each, state_manager=state_manager)
        for eng in engine_list
    ]
    chunks = await asyncio.gather(*tasks)

    results = [x for c in chunks for x in c]

    seen = set()
    deduped = []
    for it in results:
        u = it.get("url")
        if u and u not in seen:
            seen.add(u)
            deduped.append(it)

    return deduped

async def multi_search_gui_async(
    query: str,
    engines: List[str] = None,
    top_k_each: int = 10,
    headless: bool = False,
    concurrency_tabs: int = 3,
    use_state_cache: bool = True,
    state_ttl_hours: float = 2.0,
) -> List[Dict]:

    chosen = set(e.lower() for e in engines) if engines else None
    engine_list = [e for e in ENGINES if chosen is None or e.name in chosen]

    sem = asyncio.Semaphore(concurrency_tabs)

    # 初始化状态管理器
    state_manager = None
    if use_state_cache:
        state_manager = StateCacheManager(ttl_seconds=int(state_ttl_hours * 3600))

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)

        # 加载已保存的状态
        context_args = {}
        if state_manager:
            merged_state = await state_manager.load_merged_state([e.name for e in engine_list])
            if merged_state:
                context_args["storage_state"] = merged_state

        context = await browser.new_context(**context_args)

        tasks = [
            fetch_engine(context, sem, query, eng, top_k_each, state_manager=state_manager)
            for eng in engine_list
        ]

        chunks = await asyncio.gather(*tasks)
        await browser.close()



    results = [x for c in chunks for x in c]

    seen = set()
    deduped = []
    for it in results:
        if it["url"] not in seen:
            seen.add(it["url"])
            deduped.append(it)

    return deduped


if __name__ == "__main__":
    async def _test():
        res = await multi_search_gui_async(
            query="vllm lora update without restart",
            engines=["bing", "duckduckgo", "baidu"],
            top_k_each=10,
            headless=False,
            concurrency_tabs=2,
        )
        for i, r in enumerate(res, 1):
            print(f"{i:02d}. [{r['engine']}] {r['title']}\n    {r['url']}")

    asyncio.run(_test())
