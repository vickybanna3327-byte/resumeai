"""Searches Indeed Canada and LinkedIn Jobs.

Architecture
------------
- Indeed   : requests + BeautifulSoup (PRIMARY — no asyncio, works inside Streamlit)
- LinkedIn : Playwright running in a dedicated ThreadPoolExecutor thread with its
             own event loop (avoids NotImplementedError from Streamlit's event loop)

Results are deduplicated by URL and persisted to SQLite.
"""
import asyncio
import concurrent.futures
import random
import re
import time
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup
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
    Scrapes Indeed Canada (requests+BS4) and LinkedIn (Playwright in a thread).

    Usage:
        searcher = JobSearcher(headless=True)
        jobs = searcher.search("Data Analyst", "Edmonton, AB", max_pages=3)
        print(searcher.errors)   # any warnings / non-fatal messages
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
        """Search job boards synchronously. Safe to call from Streamlit."""
        self._errors = []
        sources = sources or ["indeed", "linkedin"]
        all_jobs: list[dict] = []

        # ── PRIMARY: requests+BS4 for Indeed ─────────────────────────────────
        if "indeed" in sources:
            try:
                jobs = self._indeed_search_bs4(query, location, max_pages)
                all_jobs.extend(jobs)
                print(f"[indeed-bs4] {len(jobs)} jobs found")
                if not jobs:
                    self._errors.append(
                        "[indeed] 0 jobs returned — Indeed may be blocking the request "
                        "or the selectors changed. Check the terminal for details."
                    )
            except Exception as exc:
                self._errors.append(
                    f"[indeed] requests+BS4 failed — {type(exc).__name__}: {exc}"
                )

        # ── OPTIONAL: Playwright in isolated thread for LinkedIn ──────────────
        if "linkedin" in sources:
            try:
                jobs = self._playwright_threaded(query, location, ["linkedin"], max_pages)
                all_jobs.extend(jobs)
                print(f"[linkedin-playwright] {len(jobs)} jobs found")
            except Exception as exc:
                self._errors.append(
                    f"[linkedin] Playwright failed — {type(exc).__name__}: {exc}\n"
                    "Fix: run  playwright install chromium  in your terminal."
                )

        saved = _save_to_db(all_jobs)
        print(f"[db] Saved {len(saved)} jobs total")
        return saved

    def get_job_details(self, url: str) -> dict:
        """Fetch the full job description text. Uses BS4 for Indeed, Playwright for LinkedIn."""
        if "indeed.com" in url:
            return self._fetch_indeed_details_bs4(url)
        try:
            return self._playwright_threaded_details(url)
        except Exception as exc:
            self._errors.append(f"[details] Playwright failed: {exc}")
            return {"url": url, "description": ""}

    # ──────────────────── Indeed: requests + BeautifulSoup ───────────────────

    def _indeed_search_bs4(self, query: str, location: str, max_pages: int) -> list[dict]:
        """Primary Indeed scraper — fully synchronous, no event loop dependency."""
        session = self._make_session()
        jobs: list[dict] = []

        # Warm up the session with a homepage visit so cookies are set
        try:
            session.get(INDEED_BASE, timeout=15)
            time.sleep(random.uniform(1.0, 2.0))
        except Exception:
            pass

        for page_num in range(max_pages):
            url = (
                f"{INDEED_BASE}/jobs?"
                f"q={quote_plus(query)}"
                f"&l={quote_plus(location)}"
                f"&sort=date"
                f"&start={page_num * 10}"
            )
            print(f"[indeed-bs4] Page {page_num + 1}: {url}")

            try:
                resp = session.get(url, timeout=25)
                print(f"[indeed-bs4] HTTP {resp.status_code}, {len(resp.text)} chars")

                if resp.status_code == 403:
                    self._errors.append(
                        "[indeed] HTTP 403 — request was blocked. "
                        "Indeed may require a real browser. Try disabling headless mode."
                    )
                    break

                soup = BeautifulSoup(resp.text, "html.parser")

                # Detect CAPTCHA / bot wall
                if soup.select_one("div#captcha-container, div.icl-Card--captcha, #recaptcha"):
                    self._errors.append(
                        "[indeed] CAPTCHA detected — Indeed is blocking automated requests. "
                        "Open ca.indeed.com in your browser to solve it, then retry."
                    )
                    break

                cards = soup.select(
                    "div.job_seen_beacon, "
                    "div[data-testid='slider_item'], "
                    "li.css-1ac2h1w, "
                    "div.jobCard_mainContent"
                )
                print(f"[indeed-bs4] Found {len(cards)} cards")

                if not cards:
                    # Dump a snippet to help diagnose selector drift
                    snippet = soup.get_text()[:500].replace("\n", " ")
                    print(f"[indeed-bs4] Page snippet: {snippet}")
                    self._errors.append(
                        f"[indeed] No job cards found on page {page_num + 1}. "
                        "Indeed's HTML structure may have changed."
                    )
                    break

                for card in cards:
                    job = self._parse_indeed_card_bs4(card)
                    if job:
                        jobs.append(job)

                print(f"[indeed-bs4] Extracted {len(jobs)} jobs so far")

                # Check for next page
                next_btn = soup.select_one(
                    "a[data-testid='pagination-page-next'], "
                    "a[aria-label='Next Page'], "
                    "a[aria-label='Next']"
                )
                if not next_btn:
                    break

                time.sleep(random.uniform(2.0, 4.0))

            except requests.RequestException as exc:
                self._errors.append(f"[indeed] Request error on page {page_num + 1}: {exc}")
                break

        return jobs

    def _parse_indeed_card_bs4(self, card) -> dict | None:
        try:
            title_el = card.select_one(
                "h2[data-testid='jobTitle'] a, "
                "a.jcs-JobTitle, "
                "h2.jobTitle a, "
                "a[id^='jobTitle'], "
                "h2 a[data-jk]"
            )
            if not title_el:
                return None
            title = _clean(title_el.get_text())

            jk = card.get("data-jk", "") or title_el.get("data-jk", "")
            href = title_el.get("href", "")
            if jk:
                job_url = f"{INDEED_BASE}/viewjob?jk={jk}"
            elif href:
                job_url = f"{INDEED_BASE}{href}" if href.startswith("/") else href
            else:
                return None

            company_el = card.select_one(
                "[data-testid='company-name'], span.companyName, "
                ".css-63koeb, [data-testid='inlineHeader-companyName']"
            )
            loc_el = card.select_one(
                "[data-testid='text-location'], div.companyLocation, "
                ".css-1p0sjhy, [data-testid='job-location']"
            )
            salary_el = card.select_one(
                "[data-testid='attribute_snippet_testid'], "
                "div.salary-snippet-container, "
                ".metadataContainer .attribute_snippet"
            )
            date_el = card.select_one(
                "[data-testid='myJobsStateDate'], span.date, "
                ".css-qvloho, span[class*='date']"
            )

            return {
                "title":       title,
                "company":     _clean(company_el.get_text()) if company_el else "",
                "location":    _clean(loc_el.get_text()) if loc_el else "",
                "salary":      _clean(salary_el.get_text()) if salary_el else "",
                "date_posted": _clean(date_el.get_text()) if date_el else "",
                "url":         job_url,
                "source":      "indeed",
                "description": "",
            }
        except Exception as exc:
            print(f"[indeed-bs4] Card parse error: {exc}")
            return None

    def _fetch_indeed_details_bs4(self, url: str) -> dict:
        """Fetch a single Indeed job description via requests."""
        session = self._make_session()
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
            print(f"[indeed-details-bs4] Error: {exc}")

        if description:
            _update_description(url, description)
        return {"url": url, "description": description}

    @staticmethod
    def _make_session() -> requests.Session:
        """Build a requests Session that looks like a real browser."""
        s = requests.Session()
        s.headers.update({
            "User-Agent":                random.choice(USER_AGENTS),
            "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language":           "en-CA,en-US;q=0.7,en;q=0.3",
            "Accept-Encoding":           "gzip, deflate, br",
            "DNT":                       "1",
            "Connection":                "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest":            "document",
            "Sec-Fetch-Mode":            "navigate",
            "Sec-Fetch-Site":            "none",
            "Sec-Fetch-User":            "?1",
            "Cache-Control":             "max-age=0",
        })
        return s

    # ──────────────────── Playwright in isolated thread ──────────────────────

    def _playwright_threaded(
        self,
        query: str,
        location: str,
        sources: list[str],
        max_pages: int,
    ) -> list[dict]:
        """Run async Playwright in a dedicated thread with its own event loop.

        This is the correct pattern when the calling thread (Streamlit) already
        owns an event loop — calling asyncio.run() from there raises
        NotImplementedError on Windows.
        """
        result: list[dict] = []
        thread_errors: list[str] = []

        def _thread_target():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(
                    self._playwright_search_async(query, location, sources, max_pages)
                )
            except Exception as exc:
                thread_errors.append(f"{type(exc).__name__}: {exc}")
                return []
            finally:
                loop.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_thread_target)
            try:
                result = future.result(timeout=300)
            except concurrent.futures.TimeoutError:
                thread_errors.append("Playwright timed out after 5 minutes.")
            except Exception as exc:
                thread_errors.append(f"{type(exc).__name__}: {exc}")

        for err in thread_errors:
            self._errors.append(f"[playwright-thread] {err}")

        return result

    def _playwright_threaded_details(self, url: str) -> dict:
        """Fetch job description via Playwright in an isolated thread."""
        result: dict = {"url": url, "description": ""}

        def _thread_target():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(self._fetch_details_async(url))
            finally:
                loop.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            try:
                result = pool.submit(_thread_target).result(timeout=60)
            except Exception as exc:
                self._errors.append(f"[details-playwright] {type(exc).__name__}: {exc}")

        return result

    # ──────────────────────── Playwright async core ──────────────────────────

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
                    if source == "indeed":
                        jobs = await self._scrape_indeed_pw(context, query, location, max_pages)
                    elif source == "linkedin":
                        jobs = await self._scrape_linkedin_pw(context, query, location, max_pages)
                    else:
                        continue
                    all_jobs.extend(jobs)
                except Exception as exc:
                    self._errors.append(
                        f"[{source}] Playwright scrape error — {type(exc).__name__}: {exc}"
                    )

            await browser.close()

        return all_jobs

    # ─────────────────────── Playwright: Indeed ──────────────────────────────

    async def _scrape_indeed_pw(
        self, context: BrowserContext, query: str, location: str, max_pages: int
    ) -> list[dict]:
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
                    job = await self._parse_indeed_card_pw(card)
                    if job:
                        jobs.append(job)

                if not await _indeed_has_next(page):
                    break
                await _delay(2.0, 4.0)

            except Exception as exc:
                print(f"[indeed-pw] Page {page_num + 1} error: {exc}")
                break

        await page.close()
        return jobs

    async def _parse_indeed_card_pw(self, card) -> dict | None:
        try:
            title_el = await _first_el(card, [
                "h2[data-testid='jobTitle'] a", "a.jcs-JobTitle",
                "h2.jobTitle a", "a[id^='jobTitle']",
            ])
            if not title_el:
                return None
            title = _clean(await title_el.inner_text())
            href  = await title_el.get_attribute("href") or ""
            jk    = await card.get_attribute("data-jk") or ""
            url   = (
                f"{INDEED_BASE}/viewjob?jk={jk}" if jk
                else (f"{INDEED_BASE}{href}" if href.startswith("/") else href)
            )
            if not url:
                return None

            return {
                "title":       title,
                "company":     await _text(card, ["[data-testid='company-name']", "span.companyName"]),
                "location":    await _text(card, ["[data-testid='text-location']", "div.companyLocation"]),
                "salary":      await _text(card, ["[data-testid='attribute_snippet_testid']", "div.salary-snippet-container"]),
                "date_posted": await _text(card, ["[data-testid='myJobsStateDate']", "span.date"]),
                "url":         url,
                "source":      "indeed",
                "description": "",
            }
        except Exception:
            return None

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
                f"keywords={quote_plus(query)}&location={quote_plus(location)}"
                f"&sortBy=DD&start={page_num * 25}"
            )
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await _delay(2.5, 4.5)
                await _dismiss_popups(page)

                if any(kw in page.url for kw in ("authwall", "/login", "/checkpoint")):
                    self._errors.append(
                        "[linkedin] Auth wall detected — LinkedIn requires login for full results. "
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
                    job = await self._parse_linkedin_card_pw(card)
                    if job:
                        page_jobs.append(job)

                jobs.extend(page_jobs)
                if len(page_jobs) < 20:
                    break
                await _delay(2.5, 4.0)

            except Exception as exc:
                print(f"[linkedin-pw] Page {page_num + 1} error: {exc}")
                break

        await page.close()
        return jobs

    async def _parse_linkedin_card_pw(self, card) -> dict | None:
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
                "h3.base-search-card__title", "span[aria-hidden='true']",
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

    # ─────────────────────────── Job detail fetch ────────────────────────────

    async def _fetch_details_async(self, url: str) -> dict:
        """Fetch full description from a single job page via Playwright."""
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=self.headless,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            context = await _new_context(browser)
            page = await context.new_page()
            await _stealth(page)
            description = ""
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await _delay(1.5, 3.0)
                await _dismiss_popups(page)
                if "indeed.com" in url:
                    description = await _indeed_description_pw(page)
                elif "linkedin.com" in url:
                    description = await _linkedin_description_pw(page)
            except Exception as exc:
                print(f"[details-pw] {url}: {exc}")
            finally:
                await browser.close()

        if description:
            _update_description(url, description)
        return {"url": url, "description": description}


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
                        (title, company, url, location, salary, date_posted, source, description, status)
                    VALUES
                        (:title, :company, :url, :location, :salary, :date_posted, :source, :description, 'new')
                    """,
                    {
                        "title":       job.get("title", ""),
                        "company":     job.get("company", ""),
                        "url":         job["url"],
                        "location":    job.get("location", ""),
                        "salary":      job.get("salary", ""),
                        "date_posted": job.get("date_posted", ""),
                        "source":      job.get("source", ""),
                        "description": job.get("description", ""),
                    },
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
