# page_crawler.py
import asyncio
import re
from typing import Dict, Optional, List, Tuple
from urllib.parse import urlparse

from playwright.async_api import BrowserContext
from readability import Document
from bs4 import BeautifulSoup

from models import PageResult, CandidateScore
from utils import human_sleep


SCALE = 1.2


def ms(x: int) -> int:
    return int(x * SCALE)


# =========================================================
# Site selectors
# =========================================================
SITE_SELECTORS: Dict[str, List[str]] = {
    # ---- GitHub：README / Issues / PR / Discussions ----
    "github.com": [
        # Issue / PR title (newer DOM often uses <bdi>)
        "h1 bdi",
        "h1.gh-header-title",
        "h1",

        # Issue / PR discussion bucket (newer DOM)
        "#discussion_bucket .js-comment-body",
        "#discussion_bucket .comment-body",
        "#discussion_bucket .markdown-body",
        "#discussion_bucket",

        # Discussion / PR body (legacy-ish)
        "div.js-discussion .js-comment-body",
        "div.js-discussion .comment-body",
        "div.js-discussion .markdown-body",

        # Timeline bodies (broader catch)
        "div.TimelineItem-body .js-comment-body",
        "div.TimelineItem-body .comment-body",
        "div.TimelineItem-body .markdown-body",

        # README
        "article.markdown-body",
        "#readme article",
        "#readme",

        "main",
    ],

    "stackoverflow.com": [
        "#question .s-prose",
        "#answers .s-prose",
        "main",
    ],

    # ---- Discourse family (linux.do / HF forum) ----
    "linux.do": [
        ".topic-body .cooked",
        ".cooked",
        "article",
        "main",
    ],
    "discuss.huggingface.co": [
        ".topic-body .cooked",
        ".cooked",
        "article",
        "main",
    ],

    "huggingface.co": [
        "article",
        "main",
    ],

    "medium.com": [
        "article",
        "main",
    ],

    "dev.to": [
        "article",
        ".crayons-article__main",
        "main",
    ],

    "reddit.com": [
        "shreddit-post",
        "article",
        "main",
    ],

    "readthedocs.io": [
        "div.document",
        "article",
        "main",
    ],

    "docs.google.com": [
        "body",
    ],

    "csdn.net": [
        "article",
        "#article_content",
        ".article_content",
        "main",
    ],
    "cnblogs.com": [
        "#cnblogs_post_body",
        "article",
        "main",
    ],
    "juejin.cn": [
        "article",
        ".article-content",
        "main",
    ],
    "jianshu.com": [
        "article",
        ".note",
        "main",
    ],

    "wikipedia.org": [
        "#mw-content-text",
        "#content",
        "main",
    ],

    "arxiv.org": [
        "#abs",
        "article",
        "main",
    ],

    # ---- Generic fallback ----
    "": [
        "article",
        "main",
        "#content",
        ".content",
    ],
}


# =========================================================
# Text normalization
# =========================================================
def _normalize_text(text: str, *, max_chars: int = 10000) -> str:
    # base normalize
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\r\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n...[TRUNCATED]..."
    return text


def _html_to_clean_text(html: str, *, max_chars: int = 10000) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "canvas", "iframe", "form"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    return _normalize_text(text, max_chars=max_chars)


# =========================================================
# Readability extraction
# =========================================================
def _extract_readability(html: str, *, max_chars: int = 10000) -> Dict[str, Optional[str]]:
    doc = Document(html)
    title = doc.short_title()
    content_html = doc.summary(html_partial=True)
    text = _html_to_clean_text(content_html, max_chars=max_chars)
    return {"title": title, "text": text}


# =========================================================
# Scoring (FIXED + clamp)
# =========================================================
NOISE_PATTERNS = [
    r"登录|注册|隐私|cookie|广告|赞助|推荐|关注|下载|APP|客户端|免责声明|相关文章|阅读更多|展开全文",
    r"sign in|log in|register|cookie|privacy|terms|subscribe|advertis|sponsor|download|continue reading",
]


def _score_text(text: str) -> float:
    """
    Score is >= 0.
    Prefers longer content with paragraph structure, penalizes noise patterns / too many short lines.
    """
    if not text or len(text) < 120:
        return 0.0

    length = len(text)
    paras = text.count("\n\n") + 1
    lines = text.count("\n") + 1

    noise = sum(len(re.findall(p, text, flags=re.I)) for p in NOISE_PATTERNS)
    short_lines = sum(1 for ln in text.splitlines() if 0 < len(ln.strip()) < 30)

    score = 0.0
    score += min(length, 12000) / 40.0
    score += min(paras, 60) * 1.8
    score += min(lines, 180) * 0.15
    score -= noise * 12.0
    score -= short_lines * 0.35

    return max(score, 0.0)


# =========================================================
# Lightweight per-site "content ready" waits (small changes, big robustness)
# =========================================================
def _host_and_path(url: str) -> Tuple[str, str]:
    u = urlparse(url)
    return (u.netloc.lower(), u.path.lower())


async def _best_effort_wait_content(page, final_url: str) -> None:
    """
    Best-effort waits for dynamic sites.
    - Does NOT change input/output contract.
    - Never raises (always best-effort).
    """
    host, path = _host_and_path(final_url)

    # 1) Try to wait for network to calm a bit (helps JS-heavy sites)
    try:
        await page.wait_for_load_state("networkidle", timeout=ms(6000))
    except Exception:
        pass

    # 2) Per-site key selectors (helps hydration / SPA)
    try:
        if host.endswith("github.com") and ("/issues/" in path or "/pull/" in path or "/discussions/" in path):
            await page.wait_for_selector(
                "#discussion_bucket .js-comment-body, #discussion_bucket, h1 bdi, div.js-discussion",
                timeout=ms(9000),
            )
        elif host.endswith("stackoverflow.com"):
            await page.wait_for_selector("#question .s-prose, #answers .s-prose, main", timeout=ms(7000))
        elif host.endswith("linux.do") or host.endswith("discuss.huggingface.co"):
            await page.wait_for_selector(".cooked, .topic-body .cooked, article, main", timeout=ms(7000))
        elif host.endswith("medium.com") or host.endswith("dev.to") or host.endswith("readthedocs.io"):
            await page.wait_for_selector("article, main", timeout=ms(7000))
        elif host.endswith("reddit.com"):
            await page.wait_for_selector("shreddit-post, article, main", timeout=ms(7000))
        else:
            # generic: wait for any likely content container
            await page.wait_for_selector("article, main, #content, .content, body", timeout=ms(5000))
    except Exception:
        pass


# =========================================================
# GitHub Issue/PR specialized extractor (best for /issues/ and /pull/)
# (small change: selectors made more future-proof)
# =========================================================
async def _extract_github_issue(page, *, max_chars: int = 10000, max_comments: int = 10) -> str:
    # Best-effort wait for discussion DOM to exist (avoid early empty evaluate)
    try:
        await page.wait_for_selector(
            "#discussion_bucket .js-comment-body, #discussion_bucket, h1 bdi, div.js-discussion",
            timeout=ms(9000),
        )
    except Exception:
        pass

    data = await page.evaluate(
        """
        (maxComments) => {
          const q = (sel) => document.querySelector(sel);

          const title =
            (q("h1 bdi")?.innerText || q("h1.gh-header-title")?.innerText || q("h1")?.innerText || "").trim();

          const root = q("#discussion_bucket") || document;

          const bodies = Array.from(root.querySelectorAll(".js-comment-body, .comment-body, .markdown-body"))
            .map(el => (el.innerText || "").trim())
            .filter(t => t.length > 0);

          const mainBody = (bodies[0] || "").trim();
          const comments = (bodies.slice(1) || []).slice(0, maxComments);

          return { title, mainBody, comments };
        }
        """,
        max_comments,
    )

    parts: List[str] = []
    if data.get("title"):
        parts.append(data["title"].strip())
    if data.get("mainBody"):
        parts.append(data["mainBody"].strip())

    for i, c in enumerate(data.get("comments") or [], 1):
        if not c:
            continue
        c = c.strip()
        # skip super short "+1/thanks" style noise
        if len(c) < 30:
            continue
        parts.append(f"--- Comment {i} ---\n{c}")

    text = "\n\n".join([p for p in parts if p]).strip()
    return _normalize_text(text, max_chars=max_chars)


# =========================================================
# Method A: site selectors (FIX: lower threshold + slightly more forgiving waits)
# =========================================================
async def _extract_by_selectors(page, selectors: List[str], *, max_chars: int) -> str:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                continue

            # wait a bit for hydration; also wait for element to be visible-ish
            await human_sleep(ms(140), ms(120))
            try:
                await loc.wait_for(state="attached", timeout=ms(2500))
            except Exception:
                pass

            # dynamic sites sometimes need longer than 2s
            txt = await loc.inner_text(timeout=ms(6500))
            txt = _normalize_text(txt, max_chars=max_chars)
            if len(txt) >= 200:
                return txt
        except Exception:
            continue
    return ""


def _match_site_selectors(url: str) -> List[str]:
    host = urlparse(url).netloc.lower()
    keys = sorted((k for k in SITE_SELECTORS if k), key=len, reverse=True)
    for k in keys:
        if host.endswith(k):
            return SITE_SELECTORS[k]
    return SITE_SELECTORS[""]


# =========================================================
# Method C: main content block heuristic (FIX: lower threshold)
# =========================================================
async def _extract_main_block(page, *, max_chars: int) -> str:
    candidates = await page.evaluate(
        """
        () => {
          const sels = [
            "article","main","#content",".content",".article",".post",
            ".markdown-body",".entry-content",".post-content",
            ".prose",".s-prose",".cooked",
            "#article_content","#cnblogs_post_body",
            "#discussion_bucket", ".TimelineItem-body"
          ];
          const uniq = new Set();
          const els = [];
          for (const sel of sels) {
            document.querySelectorAll(sel).forEach(el => {
              const key = el.tagName + "|" + (el.id||"") + "|" + (el.className||"");
              if (!uniq.has(key)) {
                uniq.add(key);
                els.push(el);
              }
            });
          }
          if (els.length === 0 && document.body) els.push(document.body);

          function linkDensity(el) {
            const t = (el.innerText||"").length || 1;
            let l = 0;
            el.querySelectorAll("a").forEach(a => l += (a.innerText||"").length);
            return l / t;
          }

          return els.map(el => ({
            text: (el.innerText||"").trim(),
            len: (el.innerText||"").trim().length,
            ld: linkDensity(el)
          }))
          .filter(x => x.len >= 180)
          .sort((a,b)=> (b.len/(1+b.ld*2)) - (a.len/(1+a.ld*2)));
        }
        """
    )

    best_text, best_score = "", 0.0
    for c in candidates[:25]:
        ld = float(c.get("ld") or 0.0)
        if ld > 0.45:
            continue
        t = _normalize_text(c.get("text") or "", max_chars=max_chars)
        s = _score_text(t) - ld * 80.0
        if s > best_score:
            best_score, best_text = s, t
    return best_text


# =========================================================
# Main crawl entry
# =========================================================
async def crawl_page_content(
    context: BrowserContext,
    sem: asyncio.Semaphore,
    item: Dict,
    *,
    nav_timeout_ms: int = 25000,
    js_wait_ms: int = 900,
    do_scroll: bool = True,
    max_chars: int = 10000,
) -> PageResult:

    async with sem:
        page = await context.new_page()
        try:
            page.set_default_navigation_timeout(nav_timeout_ms)
            page.set_default_timeout(nav_timeout_ms)

            resp = await page.goto(item["url"], wait_until="domcontentloaded")
            _ = resp.status if resp else None  # keep behavior (status captured previously)

            # base JS wait (unchanged contract) + slightly safer floor for JS-heavy pages
            await human_sleep(ms(max(js_wait_ms, 900)), ms(600))

            # additional best-effort wait for dynamic sites (generic improvement)
            await _best_effort_wait_content(page, page.url)

            if do_scroll:
                await page.evaluate(
                    """
                    async () => {
                      const sleep = ms => new Promise(r=>setTimeout(r,ms));
                      const r = (a,b)=>Math.floor(a+Math.random()*(b-a+1));
                      let last=0;

                      // scroll a bit deeper (helps lazy-loaded comment sections)
                      for(let i=0;i<r(5,10);i++){
                        window.scrollBy(0,r(600,Math.max(900,innerHeight)));
                        await sleep(r(350,800));
                        const h=document.body.scrollHeight;
                        if(h===last)break; last=h;
                      }

                      await sleep(r(250,500));
                      window.scrollTo(0,0);
                    }
                    """
                )

                # small post-scroll settle
                await human_sleep(ms(260), ms(200))

            final_url = page.url
            await human_sleep(ms(250), ms(180))

            candidates: List[Tuple[str, str]] = []

            # ---- GitHub issues/PR special path ----
            host = urlparse(final_url).netloc.lower()
            path = urlparse(final_url).path.lower()
            if host.endswith("github.com") and ("/issues/" in path or "/pull/" in path):
                try:
                    await human_sleep(ms(180), ms(160))
                    tG = await _extract_github_issue(page, max_chars=max_chars, max_comments=10)
                    if tG and len(tG) >= 160:
                        candidates.append(("github_issue", tG))
                except Exception:
                    pass

            # A: selectors
            tA = await _extract_by_selectors(page, _match_site_selectors(final_url), max_chars=max_chars)
            if tA:
                candidates.append(("selectors", tA))

            # C: main block
            tC = await _extract_main_block(page, max_chars=max_chars)
            if tC:
                candidates.append(("main_block", tC))

            # B: readability
            html = await page.content()
            rb = _extract_readability(html, max_chars=max_chars)
            if rb.get("text"):
                candidates.append(("readability", rb["text"]))

            # fallback: body
            if not candidates:
                body = await page.evaluate("() => document.body?.innerText || ''")
                body = _normalize_text(body, max_chars=max_chars)
                if body:
                    candidates.append(("body", body))

            METHOD_PRIOR = {
                "github_issue": 1.55,
                "selectors": 1.20,
                "main_block": 1.10,
                "readability": 1.00,
                "body": 0.90,
            }

            scored: List[Tuple[str, float, str]] = []
            for m, t in candidates:
                s = _score_text(t) * METHOD_PRIOR.get(m, 1.0)
                scored.append((m, s, t))

            scored.sort(key=lambda x: x[1], reverse=True)
            best_method, best_score, best_text = scored[0]

            # final cleanup for printing/LLM
            best_text = _normalize_text(best_text, max_chars=max_chars)

            await human_sleep(ms(360), ms(220))

            return PageResult(
                engine=item.get("engine"),
                url=item.get("url"),
                final_url=final_url,
                title=item.get("title"),
                page_title=rb.get("title"),
                method=best_method,
                score=float(best_score),
                text=best_text,
                candidates=[CandidateScore(method=m, score=float(s), len=len(t)) for m, s, t in scored],
                error=None,
            )

        except Exception as e:
            return PageResult(
                engine=item.get("engine"),
                url=item.get("url"),
                final_url=None,
                title=item.get("title"),
                page_title=None,
                method=None,
                score=None,
                text="",
                candidates=None,
                error=repr(e),
            )
        finally:
            await page.close()
