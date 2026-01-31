import asyncio
import random
import re
import os
from typing import Iterable
from pathlib import Path
from datetime import datetime

# Debug 控制 (默认开启，可通过环境变量 DEBUG=false 关闭)
DEBUG = os.getenv("DEBUG", "true").lower() == "true"

# 日志文件路径
LOG_DIR = Path(__file__).parent.parent / ".cache"
LOG_FILE = LOG_DIR / "debug.log"

# 确保日志目录存在
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _log(*args, **kwargs):
    """仅在 DEBUG 模式下输出日志到文件"""
    if DEBUG:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{timestamp}] ")
                f.write(" ".join(str(arg) for arg in args))
                f.write("\n")
        except Exception as e:
            pass  # 静默失败，避免日志错误影响主逻辑

async def human_sleep(base_ms: int, jitter_ms: int = 400):
    """
    模拟人类随机等待
    """
    delta = random.randint(-jitter_ms, jitter_ms)
    await asyncio.sleep(max(0, base_ms + delta) / 1000)


async def move_browser_window_offscreen(browser, left: int = -20000, top: int = -20000):
    """
    把 Chromium 窗口移动到指定位置（Windows / Linux 可用）
    headless=False 时使用

    :param browser: Playwright Browser 对象
    :param left: 窗口左上角 X 坐标，默认 -20000（屏幕外）
    :param top: 窗口左上角 Y 坐标，默认 -20000（屏幕外）
    """
    try:
        contexts = browser.contexts
        if not contexts:
            return

        pages = contexts[0].pages
        if not pages:
            return

        page = pages[0]
        cdp = await page.context.new_cdp_session(page)

        info = await cdp.send("Browser.getWindowForTarget")
        window_id = info["windowId"]

        await cdp.send(
            "Browser.setWindowBounds",
            {
                "windowId": window_id,
                "bounds": {
                    "left": left,
                    "top": top,
                    "width": 1200,
                    "height": 900,
                },
            },
        )
        _log(f"[info] 浏览器窗口已移至 ({left}, {top})")
    except Exception as e:
        _log("[warn] move window failed:", e)



def clean_page_text(
    text: str,
    *,
    max_chars: int = 10_000,
) -> str:
    """
    轻量级正文清洗（安全版）：
    - 合并被强制换行的正文行
    - 保留代码块 / 列表 / markdown / 短观点
    - 不做“噪声判断”，避免误杀 GitHub / forum 内容
    """

    if not text:
        return ""

    lines = text.splitlines()
    out: list[str] = []
    buffer: list[str] = []

    def flush():
        if buffer:
            out.append(" ".join(buffer).strip())
            buffer.clear()

    for ln in lines:
        ln = ln.rstrip()

        # 空行：段落边界
        if not ln.strip():
            flush()
            continue

        # 代码块 / markdown / 列表：强制独立成段
        if (
            ln.startswith(("    ", "\t", "```"))
            or re.match(r"^\s*[-*•]\s+", ln)
            or re.match(r"^\s*\d+\.\s+", ln)
        ):
            flush()
            out.append(ln)
            continue

        # 看起来像标题（非常保守）
        if len(ln) < 80 and ln.endswith(":"):
            flush()
            out.append(ln.strip())
            continue

        # 普通文本：合并
        buffer.append(ln.strip())

    flush()

    text = "\n\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n...[TRUNCATED]..."

    return text


# ==================== CAPTCHA Handling ====================

# Global lock to ensure only one task waits for CAPTCHA at a time
captcha_lock = asyncio.Lock()

# Global CAPTCHA pause lock - when any task encounters verification, other tasks pause
captcha_pause_lock = asyncio.Lock()


async def is_captcha_page(page) -> bool:
    """
    Detect if the current page is a CAPTCHA/challenge page.
    """
    url = page.url.lower()
    _log(f"[CAPTCHA] 开始检测，URL: {url}")

    # 0. 先检查页面是否有基本内容
    try:
        body_html = await page.evaluate("() => document.body?.innerHTML || ''")
        _log(f"[CAPTCHA] body HTML 长度: {len(body_html)}")
        if '.captcha' in body_html:
            _log(f"[CAPTCHA] HTML 中发现 .captcha 字符串")
    except Exception as e:
        _log(f"[CAPTCHA] 获取 HTML 异常: {e}")

    # 1. URL detection
    if any(p in url for p in ["captcha", "challenge", "verify", "recaptcha", "hcaptcha", "cf-chl", "__cf_chl_", "turnstile"]):
        _log(f"[CAPTCHA] URL匹配: {url}")
        return True

    # 2. DOM element detection with text content check - 等待动态加载
    _log(f"[CAPTCHA] 等待 10 秒让页面渲染...")
    await asyncio.sleep(10.0)

    try:
        # 使用 query_selector_all 直接检查
        _log(f"[CAPTCHA] 开始 DOM 元素检测...")

        # 检查外层 .captcha 容器（Bing/Cloudflare）
        captcha_elements = await page.query_selector_all('.captcha')
        _log(f"[CAPTCHA] .captcha 元素数: {len(captcha_elements)}")
        if len(captcha_elements) > 0:
            _log(f"[CAPTCHA] 找到 .captcha 容器，判定为验证页面")
            return True

        # 检查 .captcha_header 元素
        header_elements = await page.query_selector_all('.captcha_header')
        _log(f"[CAPTCHA] .captcha_header 元素数: {len(header_elements)}")
        if len(header_elements) > 0:
            try:
                header_text = await page.evaluate('(el) => el.textContent', header_elements[0])
                _log(f"[CAPTCHA] .captcha_header text: {repr(header_text)}")
                _log(f"[CAPTCHA] 找到 .captcha_header，判定为验证页面")
                return True
            except:
                _log(f"[CAPTCHA] 找到 .captcha_header，判定为验证页面")
                return True

        # 检查 .captcha_text 元素
        text_elements = await page.query_selector_all('.captcha_text')
        _log(f"[CAPTCHA] .captcha_text 元素数: {len(text_elements)}")
        if len(text_elements) > 0:
            try:
                text_content = await page.evaluate('(el) => el.textContent', text_elements[0])
                _log(f"[CAPTCHA] .captcha_text text: {repr(text_content)}")
                _log(f"[CAPTCHA] 找到 .captcha_text，判定为验证页面")
                return True
            except:
                _log(f"[CAPTCHA] 找到 .captcha_text，判定为验证页面")
                return True

        # 检查 #turnstile-widget
        turnstile_elements = await page.query_selector_all('#turnstile-widget')
        _log(f"[CAPTCHA] #turnstile-widget 元素数: {len(turnstile_elements)}")
        if len(turnstile_elements) > 0:
            _log(f"[CAPTCHA] 找到 #turnstile-widget，判定为验证页面")
            return True

        # 通用验证元素检测（包括 Cloudflare Turnstile）
        generic_selectors = [
            '[data-sitekey]',
            'iframe[src*="microsoft"]',
            'iframe[src*="recaptcha"]',
            'iframe[src*="challenges.cloudflare.com"]',
            'iframe[src*="cf-chl"]',
            'input[name="cf-turnstile-response"]',
            '.captcha-box'
        ]
        for sel in generic_selectors:
            elements = await page.query_selector_all(sel)
            if len(elements) > 0:
                _log(f"[CAPTCHA] 通用验证元素匹配: {sel} (count: {len(elements)})")
                return True

    except Exception as e:
        _log(f"[CAPTCHA] DOM检测异常: {e}")

    # 3. Page content detection (作为后备方案)
    try:
        body_text = await page.evaluate("() => document.body?.innerText || ''")
        _log(f"[CAPTCHA] body_text length: {len(body_text) if body_text else 0}")
        if body_text:
            body_text_lower = body_text.lower()
            keywords = ["请解决以下难题", "最后一步", "确认您是真人", "人机验证", "安全验证", "verify you are human", "prove you're not a robot", "just a moment", "challenge platform"]
            for keyword in keywords:
                if keyword in body_text_lower:
                    _log(f"[CAPTCHA] 内容匹配: {keyword}")
                    return True
    except Exception as e:
        _log(f"[CAPTCHA] 内容检测异常: {e}")

    _log(f"[CAPTCHA] 未检测到验证 (URL: {url})")
    return False


async def wait_for_captcha_resolution(page, timeout_ms: int = 120000) -> None:
    """
    Wait for user to complete CAPTCHA verification.
    :param page: Playwright Page object
    :param timeout_ms: Timeout in milliseconds, default 120 seconds
    :raises: TimeoutError if timeout is exceeded
    """
    start_time = asyncio.get_event_loop().time()

    while True:
        # Timeout check
        elapsed = (asyncio.get_event_loop().time() - start_time) * 1000
        if elapsed > timeout_ms:
            raise TimeoutError(f"CAPTCHA resolution timeout after {timeout_ms}ms")

        # Check if CAPTCHA is completed
        if not await is_captcha_page(page):
            # Wait a bit to ensure page is fully loaded
            await asyncio.sleep(0.5)
            return

        # Check every second
        await asyncio.sleep(1)
