"""Searches Indeed Canada and LinkedIn Jobs.

Architecture
------------
Indeed  : Indeed RSS feed (ca.indeed.com/rss) — pure HTTP, public, not blockable,
          returns title / company / description / date / URL as XML.
LinkedIn: Playwright (async) running via nest_asyncio, which patches the running
          event loop so asyncio can be nested inside Streamlit on Windows.

Results are deduplicated by URL and persisted to SQLite.
"""
import asyncio
import random
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

# Apply nest_asyncio immediately so Playwright can run inside Streamlit's loop
try:
    import nest_asyncio
    nest_asyncio.apply()
except ImportError:
    pass  # Will warn at runtime if Playwright is actually needed

from playwright.async_api import BrowserContext, Page, async_playwright

from modules.database import get_connection, init_db

# ──────────────────────────────────────── Constants ──────────────────────────

INDEED_BASE   = "https://ca.indeed.com"
LINKEDIN_BASE = "https://www.linkedin.com"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

POPUP_SELECTORS = [
    "button#onetrust-accept-btn-handler",
    "button[data-testid='allow-all-cookies']",
    "button[data-testid='gdpr-consent-accept']",
    "button[aria-label='Dismiss']",
    "button.modal__dismiss",
    "button[data-test='modal-close-btn']",
    "button.contextual-sign-in-modal__modal-dismiss",
    "button[aria-label='Dismiss sign in nudge']",
    "div[data-test='modal-close-btn'] button",
]

# ──────────────────────────────────────── Main class ─────────────────────────

class JobSearcher:
    """
    Searches Indeed (via RSS feed) and LinkedIn (via Playwright).

    Usage:
        searcher = JobSearcher()
        jobs = searcher.search("Data Analyst", "Edmonton, AB", max_pages=3)
        for msg in searcher.errors:
            print(msg)
    """

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._errors: list[str] = []
        init_db()

    @property
    def errors(self) -> list[str]:
        return list(self._errors)

    # ─────────────────────────── Public interface ────────────────────────────

    def search(
        self,
        query: str,
        location: str,
        sources: list[str] | None = None,
        max_pages: int = 3,
    ) -> list[dict]:
        """Synchronous search. Safe to call from Streamlit."""
        self._errors = []
        sources = sources or ["indeed", "linkedin"]
        all_jobs: list[dict] = []

        # ── Indeed via RSS — no browser, not blockable ────────────────────────
        if "indeed" in sources:
            try:
                jobs = self._indeed_search_rss(query, location, max_pages)
                all_jobs.extend(jobs)
                print(f"[indeed-rss] {len(jobs)} jobs")
                if not jobs:
                    self._errors.append(
                        "[indeed] RSS returned 0 jobs. The feed may be empty for this "
                        "query/location combination, or Indeed's RSS is temporarily unavailable."
                    )
            except Exception as exc:
                self._errors.append(
                    f"[indeed] RSS search failed — {type(exc).__name__}: {exc}"
                )

        # ── LinkedIn via Playwright + nest_asyncio ────────────────────────────
        if "linkedin" in sources:
            try:
                jobs = self._run_async(
                    self._playwright_search_async(query, location, ["linkedin"], max_pages)
                )
                all_jobs.extend(jobs)
                print(f"[linkedin-pw] {len(jobs)} jobs")
            except Exception as exc:
                self._errors.append(
                    f"[linkedin] Playwright failed — {type(exc).__name__}: {exc}\n"
                    "Fix: pip install nest_asyncio && playwright install chromium"
                )

        saved = _save_to_db(all_jobs)
        print(f"[db] Saved {len(saved)} jobs total")
        return saved

    def get_job_details(self, url: str) -> dict:
        """Fetch the full job description for a single listing."""
        if "indeed.com" in url:
            return self._fetch_indeed_details_bs4(url)
        # LinkedIn requires Playwright
        try:
            return self._run_async(self._fetch_details_async(url))
        except Exception as exc:
            self._errors.append(f"[details] {type(exc).__name__}: {exc}")
            return {"url": url, "description": ""}

    # ──────────────────── Indeed: RSS feed (primary) ─────────────────────────

    def _indeed_search_rss(self, query: str, location: str, max_pages: int) -> list[dict]:
        """
        Parse Indeed's public RSS feed.  Returns XML — no JavaScript, no CAPTCHA,
        no 403.  Each <item> contains title, link, description (HTML snippet),
        and pubDate.
        """
        session = _make_session()
        jobs: list[dict] = []

        for page_num in range(max_pages):
            url = (
                f"{INDEED_BASE}/rss?"
                f"q={quote_plus(query)}"
                f"&l={quote_plus(location)}"
                f"&sort=date"
                f"&start={page_num * 10}"
            )
            print(f"[indeed-rss] Page {page_num + 1}: {url}")

            try:
                resp = session.get(url, timeout=20)
                print(f"[indeed-rss] HTTP {resp.status_code}, {len(resp.content)} bytes")

                if resp.status_code != 200:
                    self._errors.append(
                        f"[indeed-rss] HTTP {resp.status_code} on page {page_num + 1}"
                    )
                    break

                items = _parse_rss(resp.content)
                print(f"[indeed-rss] {len(items)} items on page {page_num + 1}")

                if not items:
                    break

                for raw in items:
                    job = _rss_item_to_job(raw, location)
                    if job:
                        jobs.append(job)

                # Indeed RSS returns fewer than 10 items on the last page
                if len(items) < 10:
                    break

                time.sleep(random.uniform(1.0, 2.5))

            except Exception as exc:
                self._errors.append(
                    f"[indeed-rss] Page {page_num + 1} error — {type(exc).__name__}: {exc}"
                )
                break

        return jobs

    # ──────────────────── Indeed: description fetch via BS4 ──────────────────

    def _fetch_indeed_details_bs4(self, url: str) -> dict:
        """Fetch a full job description from an Indeed listing page."""
        session = _make_session()
        description = ""
        try:
            resp = session.get(url, timeout=20)
            soup = BeautifulSoup(resp.text, "html.parser")
            desc_el = soup.select_one(
                "#jobDescriptionText, "
                "[data-testid='jobDescriptionText'], "
                "div.jobsearch-JobComponent-description"
            )
            if desc_el:
                description = _clean(desc_el.get_text(separator=" "))
        except Exception as exc:
            print(f"[indeed-details] {exc}")

        if description:
            _update_description(url, description)
        return {"url": url, "description": description}

    # ─────────────────── Playwright: nest_asyncio runner ─────────────────────

    def _run_async(self, coro):
        """
        Run a coroutine safely whether or not an event loop is already running.

        nest_asyncio patches loop.run_until_complete() so it can be called
        from within a running loop (Streamlit on Windows).  Without it,
        get_event_loop().run_until_complete() raises RuntimeError/
        NotImplementedError when called from Streamlit's thread.
        """
        try:
            loop = asyncio.get_running_loop()
            # We're inside Streamlit's loop — nest_asyncio makes this safe
            return loop.run_until_complete(coro)
        except RuntimeError:
            # No running loop — standard path (e.g. CLI / test)
            return asyncio.run(coro)

    # ──────────────────── Playwright: async search core ──────────────────────

    async def _playwright_search_async(
        self,
        query: str,
        location: str,
        sources: list[str],
        max_pages: int,
    ) -> list[dict]:
        all_jobs: list[dict] = []

        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(
                    headless=self.headless,
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
            except Exception as exc:
                self._errors.append(
                    f"Chromium launch failed — {type(exc).__name__}: {exc}. "
                    "Run: playwright install chromium"
                )
                return all_jobs

            context = await _new_context(browser)

            for source in sources:
                try:
                    if source == "linkedin":
                        jobs = await self._scrape_linkedin_pw(context, query, location, max_pages)
                    elif source == "indeed":
                        jobs = await self._scrape_indeed_pw(context, query, location, max_pages)
                    else:
                        continue
                    all_jobs.extend(jobs)
                except Exception as exc:
                    self._errors.append(
                        f"[{source}] Playwright scrape error — {type(exc).__name__}: {exc}"
                    )

            await browser.close()

        return all_jobs

    # ─────────────────────── Playwright: LinkedIn ────────────────────────────

    async def _scrape_linkedin_pw(
        self, context: BrowserContext, query: str, location: str, max_pages: int
    ) -> list[dict]:
        page = await context.new_page()
        await _stealth(page)
        jobs: list[dict] = []

        for page_num in range(max_pages):
            url = (
                f"{LINKEDIN_BASE}/jobs/search/?"
                f"keywords={quote_plus(query)}"
                f"&location={quote_plus(location)}"
                f"&sortBy=DD"
                f"&start={page_num * 25}"
            )
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await _delay(2.5, 4.5)
                await _dismiss_popups(page)

                if any(kw in page.url for kw in ("authwall", "/login", "/checkpoint")):
                    self._errors.append(
                        "[linkedin] Auth wall — LinkedIn requires login. "
                        "Add your credentials in Settings."
                    )
                    break

                await _scroll(page, distance=2500)
                await _delay(1.5, 2.5)

                cards = await _find_first(page, [
                    "ul.jobs-search__results-list > li",
                    "div.base-card",
                    "li[data-occludable-job-id]",
                ])
                if not cards:
                    break

                page_jobs = []
                for card in cards:
                    job = await self._parse_linkedin_card(card)
                    if job:
                        page_jobs.append(job)

                jobs.extend(page_jobs)
                if len(page_jobs) < 20:
                    break
                await _delay(2.5, 4.0)

            except Exception as exc:
                print(f"[linkedin-pw] Page {page_num + 1}: {exc}")
                break

        await page.close()
        return jobs

    async def _parse_linkedin_card(self, card) -> dict | None:
        try:
            url_el = await _first_el(card, [
                "a.base-card__full-link",
                "a[data-tracking-id='srp-super-premium-job-title']",
                "a[href*='/jobs/view/']",
            ])
            if not url_el:
                return None
            raw_url = await url_el.get_attribute("href") or ""
            url = raw_url.split("?")[0]
            if not url:
                return None

            title = await _text(card, [
                "h3.base-search-card__title",
                "span[aria-hidden='true']",
            ]) or _clean(await url_el.inner_text())

            date_el = await _first_el(card, ["time[datetime]", ".job-search-card__listdate"])
            date_posted = ""
            if date_el:
                date_posted = (
                    await date_el.get_attribute("datetime")
                    or _clean(await date_el.inner_text())
                )

            return {
                "title":       _clean(title),
                "company":     await _text(card, ["h4.base-search-card__subtitle", ".base-search-card__subtitle a"]),
                "location":    await _text(card, ["span.job-search-card__location", ".base-search-card__metadata span"]),
                "salary":      "",
                "date_posted": date_posted,
                "url":         url,
                "source":      "linkedin",
                "description": "",
            }
        except Exception:
            return None

    # ─────────────────────── Playwright: Indeed (optional) ───────────────────

    async def _scrape_indeed_pw(
        self, context: BrowserContext, query: str, location: str, max_pages: int
    ) -> list[dict]:
        """Playwright-based Indeed scraper — only used when explicitly requested."""
        page = await context.new_page()
        await _stealth(page)
        jobs: list[dict] = []

        for page_num in range(max_pages):
            url = (
                f"{INDEED_BASE}/jobs?"
                f"q={quote_plus(query)}&l={quote_plus(location)}"
                f"&sort=date&start={page_num * 10}"
            )
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await _delay(2.5, 4.5)
                await _dismiss_popups(page)
                await _scroll(page, distance=1800)
                await _delay(1.0, 2.0)

                cards = await _find_first(page, [
                    "div.job_seen_beacon",
                    "div[data-testid='slider_item']",
                    "li.css-1ac2h1w",
                ])
                if not cards:
                    break

                for card in cards:
                    title_el = await _first_el(card, [
                        "h2[data-testid='jobTitle'] a", "a.jcs-JobTitle", "h2.jobTitle a",
                    ])
                    if not title_el:
                        continue
                    jk   = await card.get_attribute("data-jk") or ""
                    href = await title_el.get_attribute("href") or ""
                    job_url = (
                        f"{INDEED_BASE}/viewjob?jk={jk}" if jk
                        else (f"{INDEED_BASE}{href}" if href.startswith("/") else href)
                    )
                    if not job_url:
                        continue
                    jobs.append({
                        "title":       _clean(await title_el.inner_text()),
                        "company":     await _text(card, ["[data-testid='company-name']", "span.companyName"]),
                        "location":    await _text(card, ["[data-testid='text-location']", "div.companyLocation"]),
                        "salary":      await _text(card, ["[data-testid='attribute_snippet_testid']"]),
                        "date_posted": await _text(card, ["[data-testid='myJobsStateDate']", "span.date"]),
                        "url":         job_url,
                        "source":      "indeed",
                        "description": "",
                    })

                if not await _indeed_has_next(page):
                    break
                await _delay(2.0, 4.0)

            except Exception as exc:
                print(f"[indeed-pw] Page {page_num + 1}: {exc}")
                break

        await page.close()
        return jobs

    # ─────────────────────────── Job detail fetch ────────────────────────────

    async def _fetch_details_async(self, url: str) -> dict:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=self.headless,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            context = await _new_context(browser)
            page    = await context.new_page()
            await _stealth(page)
            description = ""
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await _delay(1.5, 3.0)
                await _dismiss_popups(page)
                if "linkedin.com" in url:
                    description = await _linkedin_description_pw(page)
                elif "indeed.com" in url:
                    description = await _indeed_description_pw(page)
            except Exception as exc:
                print(f"[details-pw] {url}: {exc}")
            finally:
                await browser.close()

        if description:
            _update_description(url, description)
        return {"url": url, "description": description}


# ──────────────────────────── RSS parsing helpers ─────────────────────────────

def _parse_rss(content: bytes) -> list[dict]:
    """
    Parse Indeed's RSS XML and return a list of raw item dicts.
    Uses ElementTree (built-in).  Strips XML namespaces before parsing
    so we don't have to register or handle namespace prefixes.
    """
    # Strip namespace declarations to simplify element lookup
    clean = re.sub(r'\s+xmlns(?::\w+)?="[^"]*"', "", content.decode("utf-8", errors="replace"))
    try:
        root = ET.fromstring(clean)
    except ET.ParseError as exc:
        print(f"[rss] XML parse error: {exc}")
        return []

    channel = root.find("channel")
    if channel is None:
        return []

    items = []
    for item in channel.findall("item"):
        items.append({
            "title":    item.findtext("title", "").strip(),
            "link":     item.findtext("link", "").strip() or item.findtext("guid", "").strip(),
            "desc":     item.findtext("description", "").strip(),
            "pub_date": item.findtext("pubDate", "").strip(),
        })
    return items


def _rss_item_to_job(raw: dict, search_location: str) -> dict | None:
    """Convert a raw RSS item dict into a job dict."""
    title_raw = raw.get("title", "")
    url       = raw.get("link", "")
    if not title_raw or not url:
        return None

    title, company, location = _parse_rss_title(title_raw)

    # Strip HTML from description (Indeed wraps it in HTML tags)
    desc_html = raw.get("desc", "")
    description = ""
    if desc_html:
        description = _clean(
            BeautifulSoup(desc_html, "html.parser").get_text(separator=" ")
        )

    return {
        "title":       title,
        "company":     company,
        "location":    location or search_location,
        "salary":      "",
        "date_posted": raw.get("pub_date", ""),
        "url":         url,
        "source":      "indeed",
        "description": description,
    }


def _parse_rss_title(raw: str) -> tuple[str, str, str]:
    """
    Indeed RSS titles look like: 'Job Title - Company Name (City, Province)'
    Returns (title, company, location).
    """
    location = ""
    m = re.search(r"\(([^)]+)\)\s*$", raw)
    if m:
        location = m.group(1).strip()
        raw = raw[: m.start()].strip()

    parts = raw.split(" - ", 1)
    title   = _clean(parts[0])
    company = _clean(parts[1]) if len(parts) > 1 else ""
    return title, company, location


# ──────────────────────────────────── DB helpers ─────────────────────────────

def _save_to_db(jobs: list[dict]) -> list[dict]:
    saved: list[dict] = []
    with get_connection() as conn:
        for job in jobs:
            if not job.get("title") or not job.get("url"):
                continue
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO jobs
                        (title, company, url, location, salary, date_posted,
                         source, description, status)
                    VALUES
                        (:title, :company, :url, :location, :salary,
                         :date_posted, :source, :description, 'new')
                    """,
                    {k: job.get(k, "") for k in
                     ("title", "company", "url", "location", "salary",
                      "date_posted", "source", "description")},
                )
                saved.append(job)
            except Exception as exc:
                print(f"[db] Insert error {job.get('url')}: {exc}")
    return saved


def _update_description(url: str, description: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE jobs SET description = ? WHERE url = ?",
            (description, url),
        )


# ────────────────────────── Playwright page helpers ──────────────────────────

async def _indeed_description_pw(page: Page) -> str:
    desc_el = await _first_el(page, [
        "#jobDescriptionText",
        "[data-testid='jobDescriptionText']",
        "div.jobsearch-JobComponent-description",
    ])
    return _clean(await desc_el.inner_text()) if desc_el else ""


async def _linkedin_description_pw(page: Page) -> str:
    try:
        btn = await page.query_selector(
            "button.show-more-less-html__button--more, "
            "button[aria-label='Click to see more description']"
        )
        if btn and await btn.is_visible():
            await btn.click()
            await _delay(0.5, 1.0)
    except Exception:
        pass
    desc_el = await _first_el(page, [
        "div.show-more-less-html__markup",
        "div.description__text",
        "article.job-details",
    ])
    return _clean(await desc_el.inner_text()) if desc_el else ""


async def _indeed_has_next(page: Page) -> bool:
    try:
        btn = await page.query_selector(
            "a[data-testid='pagination-page-next'], a[aria-label='Next Page']"
        )
        return btn is not None and await btn.is_visible()
    except Exception:
        return False


# ──────────────────────────────── Browser helpers ────────────────────────────

def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent":                random.choice(USER_AGENTS),
        "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language":           "en-CA,en-US;q=0.7,en;q=0.3",
        "Accept-Encoding":           "gzip, deflate, br",
        "DNT":                       "1",
        "Connection":                "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control":             "max-age=0",
    })
    return s


async def _new_context(browser):
    return await browser.new_context(
        user_agent=random.choice(USER_AGENTS),
        viewport={
            "width":  random.choice([1366, 1440, 1536, 1920]),
            "height": random.choice([768, 900, 960, 1080]),
        },
        locale="en-CA",
        timezone_id="America/Toronto",
        extra_http_headers={
            "Accept-Language": "en-CA,en-GB;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "DNT": "1",
        },
    )


async def _stealth(page: Page) -> None:
    await page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins', {
            get: () => [
                { name: 'Chrome PDF Plugin' },
                { name: 'Chrome PDF Viewer' },
                { name: 'Native Client' },
            ],
        });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-CA', 'en-GB', 'en'] });
        if (!window.chrome) { window.chrome = { runtime: {} }; }
        const _origPerm = window.navigator.permissions.query;
        window.navigator.permissions.query = (p) =>
            p.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : _origPerm(p);
    """)


async def _delay(min_s: float = 1.0, max_s: float = 3.0) -> None:
    await asyncio.sleep(random.uniform(min_s, max_s))


async def _scroll(page: Page, distance: int = 1500) -> None:
    scrolled = 0
    while scrolled < distance:
        step = random.randint(180, 420)
        await page.mouse.wheel(0, step)
        scrolled += step
        await asyncio.sleep(random.uniform(0.08, 0.35))


async def _dismiss_popups(page: Page) -> None:
    for sel in POPUP_SELECTORS:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                await btn.click()
                await asyncio.sleep(random.uniform(0.3, 0.7))
        except Exception:
            pass


# ─────────────────────────── Element query helpers ───────────────────────────

async def _find_first(page_or_card, selector_list: list[str]) -> list:
    for sel in selector_list:
        try:
            results = await page_or_card.query_selector_all(sel)
            if results:
                return results
        except Exception:
            continue
    return []


async def _first_el(page_or_card, selector_list: list[str]):
    for sel in selector_list:
        try:
            el = await page_or_card.query_selector(sel)
            if el:
                return el
        except Exception:
            continue
    return None


async def _text(page_or_card, selector_list: list[str]) -> str:
    el = await _first_el(page_or_card, selector_list)
    if el:
        try:
            return _clean(await el.inner_text())
        except Exception:
            pass
    return ""


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()
