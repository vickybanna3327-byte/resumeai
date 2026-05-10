"""Searches Indeed Canada and LinkedIn Jobs using Playwright.

Human-like browser behaviour (random delays, incremental scrolling, stealth JS)
minimises bot-detection. Both scrapers handle pagination automatically.
Results are deduplicated by URL and persisted to SQLite.
"""
import asyncio
import random
import re
from urllib.parse import quote_plus

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

# Popup / cookie banner dismiss selectors (Indeed + LinkedIn)
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
    Scrapes Indeed Canada and LinkedIn Jobs.

    Usage (sync):
        searcher = JobSearcher(headless=True)
        jobs = searcher.search("Python Developer", "Toronto, ON", max_pages=3)

    Usage (async, e.g. from an async Streamlit callback):
        jobs = await searcher.search_async("Python Developer", "Toronto, ON")
    """

    def __init__(self, headless: bool = True):
        self.headless = headless
        init_db()

    # ─────────────────────────── Public sync interface ───────────────────────

    def search(
        self,
        query: str,
        location: str,
        sources: list[str] | None = None,
        max_pages: int = 3,
    ) -> list[dict]:
        """Search job boards and return saved job dicts."""
        return asyncio.run(
            self.search_async(query, location, sources, max_pages)
        )

    def get_job_details(self, url: str) -> dict:
        """Fetch the full job description text from a listing URL."""
        return asyncio.run(self._fetch_details_async(url))

    # ─────────────────────────── Public async interface ──────────────────────

    async def search_async(
        self,
        query: str,
        location: str,
        sources: list[str] | None = None,
        max_pages: int = 3,
    ) -> list[dict]:
        sources = sources or ["indeed", "linkedin"]
        all_jobs: list[dict] = []

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=self.headless,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context = await _new_context(browser)

            for source in sources:
                try:
                    if source == "indeed":
                        jobs = await self._scrape_indeed(context, query, location, max_pages)
                    elif source == "linkedin":
                        jobs = await self._scrape_linkedin(context, query, location, max_pages)
                    else:
                        continue
                    print(f"[{source}] Found {len(jobs)} jobs")
                    all_jobs.extend(jobs)
                except Exception as exc:
                    print(f"[{source}] Scraper error: {exc}")

            await browser.close()

        saved = _save_to_db(all_jobs)
        print(f"[db] Saved/updated {len(saved)} jobs total")
        return saved

    # ─────────────────────────── Indeed Canada ───────────────────────────────

    async def _scrape_indeed(
        self,
        context: BrowserContext,
        query: str,
        location: str,
        max_pages: int,
    ) -> list[dict]:
        page = await context.new_page()
        await _stealth(page)
        jobs: list[dict] = []

        for page_num in range(max_pages):
            url = (
                f"{INDEED_BASE}/jobs?"
                f"q={quote_plus(query)}"
                f"&l={quote_plus(location)}"
                f"&sort=date"
                f"&start={page_num * 10}"
            )
            print(f"[indeed] Page {page_num + 1}: {url}")

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
                    print(f"[indeed] No cards found on page {page_num + 1}, stopping")
                    break

                page_jobs = []
                for card in cards:
                    job = await self._parse_indeed_card(card)
                    if job:
                        page_jobs.append(job)

                print(f"[indeed] Page {page_num + 1}: extracted {len(page_jobs)} jobs")
                jobs.extend(page_jobs)

                if not await _indeed_has_next(page):
                    print("[indeed] No next page, done")
                    break

                await _delay(2.0, 4.0)

            except Exception as exc:
                print(f"[indeed] Page {page_num + 1} error: {exc}")
                break

        await page.close()
        return jobs

    async def _parse_indeed_card(self, card) -> dict | None:
        try:
            # ── Title + URL ──────────────────────────────────────────────────
            title_el = await _first_el(card, [
                "h2[data-testid='jobTitle'] a",
                "a.jcs-JobTitle",
                "h2.jobTitle a",
                "a[id^='jobTitle']",
            ])
            if not title_el:
                return None
            title = _clean(await title_el.inner_text())

            href = await title_el.get_attribute("href") or ""
            # Prefer the data-jk job key for a stable canonical URL
            jk = await card.get_attribute("data-jk") or ""
            if jk:
                url = f"{INDEED_BASE}/viewjob?jk={jk}"
            elif href:
                url = f"{INDEED_BASE}{href}" if href.startswith("/") else href
            else:
                return None

            # ── Company ──────────────────────────────────────────────────────
            company = await _text(card, [
                "[data-testid='company-name']",
                "span.companyName",
                ".css-63koeb",
            ])

            # ── Location ─────────────────────────────────────────────────────
            location = await _text(card, [
                "[data-testid='text-location']",
                "div.companyLocation",
                ".css-1p0sjhy",
            ])

            # ── Salary ───────────────────────────────────────────────────────
            salary = await _text(card, [
                "[data-testid='attribute_snippet_testid']",
                "div.salary-snippet-container",
                ".metadataContainer .attribute_snippet",
            ])

            # ── Date posted ──────────────────────────────────────────────────
            date_posted = await _text(card, [
                "[data-testid='myJobsStateDate']",
                "span.date",
                ".css-qvloho",
            ])

            return {
                "title":       title,
                "company":     company,
                "location":    location,
                "salary":      salary,
                "date_posted": date_posted,
                "url":         url,
                "source":      "indeed",
                "description": "",
            }
        except Exception as exc:
            print(f"[indeed] Card parse error: {exc}")
            return None

    # ─────────────────────────── LinkedIn ────────────────────────────────────

    async def _scrape_linkedin(
        self,
        context: BrowserContext,
        query: str,
        location: str,
        max_pages: int,
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
            print(f"[linkedin] Page {page_num + 1}: {url}")

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await _delay(2.5, 4.5)
                await _dismiss_popups(page)

                # Bail if redirected to an auth wall
                current = page.url
                if any(kw in current for kw in ("authwall", "/login", "/checkpoint")):
                    print("[linkedin] Auth wall detected — LinkedIn requires a session for full results")
                    break

                await _scroll(page, distance=2500)
                await _delay(1.5, 2.5)

                # LinkedIn lazy-loads more cards after scrolling
                cards = await _find_first(page, [
                    "ul.jobs-search__results-list > li",
                    "div.base-card",
                    "li[data-occludable-job-id]",
                ])
                if not cards:
                    print(f"[linkedin] No cards found on page {page_num + 1}, stopping")
                    break

                page_jobs = []
                for card in cards:
                    job = await self._parse_linkedin_card(card)
                    if job:
                        page_jobs.append(job)

                print(f"[linkedin] Page {page_num + 1}: extracted {len(page_jobs)} jobs")
                jobs.extend(page_jobs)

                # LinkedIn pagination: if fewer than 25 results, we're on the last page
                if len(page_jobs) < 20:
                    break

                await _delay(2.5, 4.0)

            except Exception as exc:
                print(f"[linkedin] Page {page_num + 1} error: {exc}")
                break

        await page.close()
        return jobs

    async def _parse_linkedin_card(self, card) -> dict | None:
        try:
            # ── URL ──────────────────────────────────────────────────────────
            url_el = await _first_el(card, [
                "a.base-card__full-link",
                "a[data-tracking-id='srp-super-premium-job-title']",
                "a[href*='/jobs/view/']",
            ])
            if not url_el:
                return None
            raw_url = await url_el.get_attribute("href") or ""
            # Strip tracking query params — keep the clean /jobs/view/{id} path
            url = raw_url.split("?")[0] if raw_url else ""
            if not url:
                return None

            # ── Title ────────────────────────────────────────────────────────
            title = await _text(card, [
                "h3.base-search-card__title",
                "span[aria-hidden='true']",
                ".base-card__full-link",
            ])
            if not title:
                title = _clean(await url_el.inner_text())

            # ── Company ──────────────────────────────────────────────────────
            company = await _text(card, [
                "h4.base-search-card__subtitle",
                ".base-search-card__subtitle a",
                "a[data-tracking-id='srp-super-premium-company-name']",
            ])

            # ── Location ─────────────────────────────────────────────────────
            location = await _text(card, [
                "span.job-search-card__location",
                ".base-search-card__metadata span",
            ])

            # ── Date posted ─────────────────────────────────────────────────
            date_el = await _first_el(card, ["time[datetime]", ".job-search-card__listdate"])
            date_posted = ""
            if date_el:
                date_posted = (
                    await date_el.get_attribute("datetime")
                    or _clean(await date_el.inner_text())
                )

            return {
                "title":       _clean(title),
                "company":     _clean(company),
                "location":    _clean(location),
                "salary":      "",  # LinkedIn rarely shows salary publicly
                "date_posted": date_posted,
                "url":         url,
                "source":      "linkedin",
                "description": "",
            }
        except Exception as exc:
            print(f"[linkedin] Card parse error: {exc}")
            return None

    # ─────────────────────────── Job detail fetch ────────────────────────────

    async def _fetch_details_async(self, url: str) -> dict:
        """Fetch full job description text from an individual listing page."""
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
                    description = await _indeed_description(page)
                elif "linkedin.com" in url:
                    description = await _linkedin_description(page)

            except Exception as exc:
                print(f"[details] Error fetching {url}: {exc}")
            finally:
                await browser.close()

        # Persist description to DB if the job exists
        if description:
            _update_description(url, description)

        return {"url": url, "description": description}


# ──────────────────────────────────── DB helpers ─────────────────────────────

def _save_to_db(jobs: list[dict]) -> list[dict]:
    """INSERT OR IGNORE by URL; returns all jobs that were accepted."""
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
                print(f"[db] Insert error for {job.get('url')}: {exc}")
    return saved


def _update_description(url: str, description: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE jobs SET description = ? WHERE url = ?",
            (description, url),
        )


# ──────────────────────────────── Page-level scrapers ────────────────────────

async def _indeed_description(page: Page) -> str:
    desc_el = await _first_el(page, [
        "#jobDescriptionText",
        "[data-testid='jobDescriptionText']",
        "div.jobsearch-JobComponent-description",
    ])
    return _clean(await desc_el.inner_text()) if desc_el else ""


async def _linkedin_description(page: Page) -> str:
    # Click "Show more" if present
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
    """Return True if Indeed shows a Next Page button."""
    try:
        btn = await page.query_selector(
            "a[data-testid='pagination-page-next'], "
            "a[aria-label='Next Page']"
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
    """Mask Playwright's automation fingerprint."""
    await page.add_init_script("""
        // Hide webdriver flag
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

        // Fake plugin array (empty in headless)
        Object.defineProperty(navigator, 'plugins', {
            get: () => [
                { name: 'Chrome PDF Plugin' },
                { name: 'Chrome PDF Viewer' },
                { name: 'Native Client' },
            ],
        });

        // Real language array
        Object.defineProperty(navigator, 'languages', {
            get: () => ['en-CA', 'en-GB', 'en'],
        });

        // Chrome runtime stub (absent in headless)
        if (!window.chrome) {
            window.chrome = { runtime: {} };
        }

        // Permissions API stub
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) =>
            parameters.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : originalQuery(parameters);
    """)


async def _delay(min_s: float = 1.0, max_s: float = 3.0) -> None:
    await asyncio.sleep(random.uniform(min_s, max_s))


async def _scroll(page: Page, distance: int = 1500) -> None:
    """Scroll down in small random steps to simulate human reading."""
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
    """Try each selector in order; return the first non-empty result list."""
    for sel in selector_list:
        try:
            results = await page_or_card.query_selector_all(sel)
            if results:
                return results
        except Exception:
            continue
    return []


async def _first_el(page_or_card, selector_list: list[str]):
    """Return the first matching element across a list of selectors, or None."""
    for sel in selector_list:
        try:
            el = await page_or_card.query_selector(sel)
            if el:
                return el
        except Exception:
            continue
    return None


async def _text(page_or_card, selector_list: list[str]) -> str:
    """Extract inner text from the first matching selector."""
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
