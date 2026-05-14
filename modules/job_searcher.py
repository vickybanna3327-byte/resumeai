"""Searches multiple job boards for Canadian job postings.

Sources
-------
indeed   — ca.indeed.com/rss  (RSS/XML via urllib with Referer header, no cookies)
jobbank  — jobbank.gc.ca/jobsearch/rss  (Government of Canada, completely public)
adzuna   — api.adzuna.com/v1/api/jobs/ca  (Free JSON API, requires free key)
linkedin — Playwright + nest_asyncio  (handles Streamlit's running event loop)
"""
# nest_asyncio MUST be imported and applied before asyncio so that Playwright
# can run inside Streamlit's already-running event loop on Windows.
import nest_asyncio
nest_asyncio.apply()

import asyncio
import gzip
import random
import re
import ssl
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import quote, quote_plus

import requests
from bs4 import BeautifulSoup

from playwright.async_api import BrowserContext, Page, async_playwright

from modules.database import get_connection, init_db

# ──────────────────────────────────────── Constants ──────────────────────────

INDEED_BASE   = "https://ca.indeed.com"
JOBBANK_BASE  = "https://www.jobbank.gc.ca"
ADZUNA_BASE   = "https://api.adzuna.com/v1/api/jobs/ca"
LINKEDIN_BASE = "https://www.linkedin.com"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

POPUP_SELECTORS = [
    "button#onetrust-accept-btn-handler",
    "button[data-testid='allow-all-cookies']",
    "button[aria-label='Dismiss']",
    "button.modal__dismiss",
    "button.contextual-sign-in-modal__modal-dismiss",
    "button[aria-label='Dismiss sign in nudge']",
]

# ──────────────────────────────────────── Main class ─────────────────────────

class JobSearcher:
    """
    Multi-source job searcher for Canadian jobs.

    Usage:
        searcher = JobSearcher(adzuna_app_id="...", adzuna_app_key="...")
        jobs = searcher.search("Data Analyst", "Edmonton, AB", max_pages=3)
        for msg in searcher.errors:
            print(msg)
    """

    def __init__(
        self,
        headless: bool = True,
        adzuna_app_id: str = "",
        adzuna_app_key: str = "",
    ):
        self.headless       = headless
        self.adzuna_app_id  = adzuna_app_id
        self.adzuna_app_key = adzuna_app_key
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
        """Synchronous multi-source search. Safe to call from Streamlit."""
        self._errors = []
        sources = sources or ["indeed", "jobbank"]
        all_jobs: list[dict] = []

        dispatch = {
            "jobbank": self._jobbank_search_html,
            "adzuna":  self._adzuna_search,
        }

        for source in sources:
            if source == "linkedin":
                continue  # handled separately below (needs async)
            if source == "indeed":
                # Indeed.com returns 403 on every endpoint (HTML, RSS, API).
                # Adzuna aggregates Indeed-sourced listings via its free API.
                self._errors.append(
                    "[indeed] Indeed.com blocks all automated requests (HTTP 403 on "
                    "all endpoints including RSS). Enable Adzuna in Settings — it "
                    "surfaces Indeed-sourced jobs via its free API."
                )
                continue
            handler = dispatch.get(source)
            if not handler:
                continue
            try:
                jobs = handler(query, location, max_pages)
                all_jobs.extend(jobs)
                print(f"[{source}] {len(jobs)} jobs")
                if not jobs:
                    self._errors.append(
                        f"[{source}] 0 results — query may return no matches "
                        "for this location, or the source is temporarily unavailable."
                    )
            except Exception as exc:
                self._errors.append(
                    f"[{source}] Failed — {type(exc).__name__}: {exc}"
                )

        if "linkedin" in sources:
            try:
                jobs = self._run_async(
                    self._playwright_search_async(query, location, ["linkedin"], max_pages)
                )
                all_jobs.extend(jobs)
                print(f"[linkedin] {len(jobs)} jobs")
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
        if "jobbank.gc.ca" in url or "adzuna" in url or "indeed.com" in url:
            return self._fetch_generic_details(url)
        try:
            return self._run_async(self._fetch_details_async(url))
        except Exception as exc:
            self._errors.append(f"[details] {type(exc).__name__}: {exc}")
            return {"url": url, "description": ""}

    # ─────────────────── Canada Job Bank (HTML scraper) ─────────────────────

    def _jobbank_search_html(self, query: str, location: str, max_pages: int) -> list[dict]:
        """
        Scrape Canada Job Bank search results page — Government of Canada data.
        RSS endpoint is 404 (deprecated); the HTML search page returns 200.
        Pagination via &pg= parameter. SSL verification disabled (cert chain issue).
        """
        city = location.split(",")[0].strip()
        jobs: list[dict] = []

        for page_num in range(max_pages):
            url = (
                f"{JOBBANK_BASE}/jobsearch/jobsearch?"
                f"searchstring={quote_plus(query)}"
                f"&locationstring={quote_plus(city)}"
                f"&pg={page_num + 1}"
            )
            print(f"[jobbank-html] Page {page_num + 1}: {url}")

            try:
                content = _fetch_urllib(
                    url,
                    extra_headers={"Referer": f"{JOBBANK_BASE}/jobsearch/jobsearch"},
                    verify_ssl=False,
                )
                soup  = BeautifulSoup(content, "html.parser")
                cards = soup.select("article.action-buttons")
                print(f"[jobbank-html] {len(cards)} cards on page {page_num + 1}")

                if not cards:
                    break

                for card in cards:
                    job = self._parse_jobbank_card(card)
                    if job:
                        jobs.append(job)

                if len(cards) < 10:
                    break

                time.sleep(random.uniform(1.0, 2.0))

            except Exception as exc:
                self._errors.append(f"[jobbank] {type(exc).__name__}: {exc}")
                break

        return jobs

    def _parse_jobbank_card(self, card) -> dict | None:
        try:
            link_el = card.select_one("a.resultJobItem")
            if not link_el:
                return None

            # URL: strip jsessionid from href and build clean canonical URL
            # e.g. /jobsearch/jobposting/49507161;jsessionid=...?source=... → /jobposting/49507161
            href = link_el.get("href", "")
            m = re.search(r"/jobposting/(\d+)", href)
            if not m:
                return None
            url = f"{JOBBANK_BASE}/jobsearch/jobposting/{m.group(1)}"

            title_el = link_el.select_one(".noctitle")
            title = _clean(title_el.get_text()) if title_el else ""
            if not title:
                return None

            company_el = card.select_one("li.business")
            company    = _clean(company_el.get_text()) if company_el else ""

            loc_el  = card.select_one("li.location")
            location = ""
            if loc_el:
                for s in loc_el.select(".wb-inv, .fas, .fa"):
                    s.decompose()
                location = _clean(loc_el.get_text())

            sal_el = card.select_one("li.salary")
            salary = ""
            if sal_el:
                for s in sal_el.select(".wb-inv, .fas, .fa"):
                    s.decompose()
                salary = _clean(sal_el.get_text()).removeprefix("Salary").strip()

            date_el    = card.select_one("li.date")
            date_posted = _clean(date_el.get_text()) if date_el else ""

            return {
                "title":       title,
                "company":     company,
                "location":    location,
                "salary":      salary,
                "date_posted": date_posted,
                "url":         url,
                "source":      "jobbank",
                "description": "",
            }
        except Exception as exc:
            print(f"[jobbank] Card parse error: {exc}")
            return None

    def _fetch_generic_details(self, url: str) -> dict:
        """Generic description fetch via urllib + BeautifulSoup."""
        description = ""
        try:
            content = _fetch_urllib(url)
            soup    = BeautifulSoup(content, "html.parser")
            # Remove nav/footer/header noise
            for el in soup.select("nav, footer, header, script, style"):
                el.decompose()
            main = soup.select_one("main, article, #content, .content, .job-description")
            target = main or soup.body
            if target:
                description = _clean(target.get_text(separator=" "))[:5000]
        except Exception as exc:
            print(f"[generic-details] {url}: {exc}")

        if description:
            _update_description(url, description)
        return {"url": url, "description": description}

    # ──────────────────────────── Adzuna API ─────────────────────────────────

    def _adzuna_search(self, query: str, location: str, max_pages: int) -> list[dict]:
        """
        Adzuna free API — Canadian jobs.
        Register for a free key at: https://developer.adzuna.com/
        """
        if not self.adzuna_app_id or not self.adzuna_app_key:
            self._errors.append(
                "[adzuna] App ID and App Key not set. "
                "Register free at developer.adzuna.com, then add keys in Settings."
            )
            return []

        city = location.split(",")[0].strip()
        jobs: list[dict] = []

        for page_num in range(max_pages):
            url = (
                f"{ADZUNA_BASE}/search/{page_num + 1}?"
                f"app_id={self.adzuna_app_id}"
                f"&app_key={self.adzuna_app_key}"
                f"&results_per_page=20"
                f"&what={quote_plus(query)}"
                f"&where={quote_plus(city)}"
                f"&content-type=application/json"
            )
            print(f"[adzuna] Page {page_num + 1}: {url}")

            try:
                resp = requests.get(url, timeout=20)
                if resp.status_code == 401:
                    self._errors.append(
                        "[adzuna] HTTP 401 — Invalid API keys. "
                        "Check your App ID and App Key in Settings."
                    )
                    break
                resp.raise_for_status()
                data    = resp.json()
                results = data.get("results", [])
                print(f"[adzuna] {len(results)} results on page {page_num + 1}")

                if not results:
                    break

                for item in results:
                    job = _adzuna_item_to_job(item)
                    if job:
                        jobs.append(job)

                # Adzuna returns fewer than 20 results on the last page
                if len(results) < 20:
                    break

                time.sleep(random.uniform(0.5, 1.2))

            except Exception as exc:
                self._errors.append(f"[adzuna] {type(exc).__name__}: {exc}")
                break

        return jobs

    # ─────────────────── Playwright + nest_asyncio (LinkedIn) ────────────────

    def _run_async(self, coro):
        """
        Run a coroutine inside Streamlit's already-running event loop.
        nest_asyncio patches loop.run_until_complete() to allow nesting.
        Falls back to asyncio.run() when no loop is active (CLI / tests).
        """
        try:
            loop = asyncio.get_running_loop()
            return loop.run_until_complete(coro)
        except RuntimeError:
            return asyncio.run(coro)

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
                    args=["--no-sandbox", "--disable-dev-shm-usage",
                          "--disable-blink-features=AutomationControlled"],
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
                        jobs = await self._scrape_linkedin(context, query, location, max_pages)
                    else:
                        continue
                    all_jobs.extend(jobs)
                except Exception as exc:
                    self._errors.append(
                        f"[{source}] Playwright error — {type(exc).__name__}: {exc}"
                    )

            await browser.close()

        return all_jobs

    async def _scrape_linkedin(
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
                f"&sortBy=DD&start={page_num * 25}"
            )
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await _delay(2.5, 4.5)
                await _dismiss_popups(page)

                if any(kw in page.url for kw in ("authwall", "/login", "/checkpoint")):
                    self._errors.append(
                        "[linkedin] Auth wall — LinkedIn requires a logged-in session. "
                        "Add LinkedIn credentials in Settings."
                    )
                    break

                await _scroll(page, 2500)
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
                print(f"[linkedin] Page {page_num + 1}: {exc}")
                break

        await page.close()
        return jobs

    async def _parse_linkedin_card(self, card) -> dict | None:
        try:
            url_el = await _first_el(card, [
                "a.base-card__full-link",
                "a[href*='/jobs/view/']",
            ])
            if not url_el:
                return None
            url = (await url_el.get_attribute("href") or "").split("?")[0]
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
                "location":    await _text(card, ["span.job-search-card__location"]),
                "salary":      "",
                "date_posted": date_posted,
                "url":         url,
                "source":      "linkedin",
                "description": "",
            }
        except Exception:
            return None

    async def _fetch_details_async(self, url: str) -> dict:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=self.headless,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            ctx  = await _new_context(browser)
            page = await ctx.new_page()
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


# ─────────────────────────── urllib fetch helper ─────────────────────────────

def _fetch_urllib(url: str, extra_headers: dict | None = None, verify_ssl: bool = True) -> bytes:
    """
    Fetch a URL with browser-like headers via urllib.
    Fresh connection every call — no session cookies.
    Referer set to google.com so the request looks like organic traffic.
    Set verify_ssl=False for government sites with broken cert chains.
    """
    headers = {
        "User-Agent":      random.choice(USER_AGENTS),
        "Referer":         "https://www.google.com/",
        "Accept":          "application/rss+xml,text/xml,application/xml,text/html,*/*;q=0.8",
        "Accept-Language": "en-CA,en-US;q=0.7,en;q=0.3",
        "Accept-Encoding": "gzip, deflate",
        "DNT":             "1",
        "Connection":      "keep-alive",
    }
    if extra_headers:
        headers.update(extra_headers)

    req = urllib.request.Request(url, headers=headers)

    # SSL context: skip verification for sites with self-signed / broken chains
    if not verify_ssl:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    else:
        ctx = None

    with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return raw


# ──────────────────────────── RSS parsing helpers ─────────────────────────────

def _parse_rss_xml(content: bytes) -> list[dict]:
    """
    Parse an RSS 2.0 feed with stdlib ElementTree.
    Strips XML namespace declarations first to avoid prefix clutter.
    """
    text = content.decode("utf-8", errors="replace")
    text = re.sub(r'\s+xmlns(?::\w+)?="[^"]*"', "", text)  # strip namespaces
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        print(f"[rss] XML parse error: {exc}")
        return []

    channel = root.find("channel")
    if channel is None:
        # Some feeds put items directly under root
        channel = root

    items = []
    for item in channel.findall("item"):
        items.append({
            "title":    item.findtext("title", "").strip(),
            "link":     (item.findtext("guid", "") or item.findtext("link", "")).strip(),
            "desc":     item.findtext("description", "").strip(),
            "pub_date": item.findtext("pubDate", "").strip(),
            "author":   item.findtext("author", "").strip(),
        })
    return items


# ──────────────────────── Source-specific item converters ─────────────────────

def _parse_rss_title(raw: str) -> tuple[str, str, str]:
    """
    Parse 'Job Title - Company Name (City, Province)' into (title, company, location).
    Works for Indeed RSS which embeds company and sometimes location in the title.
    """
    location = ""
    m = re.search(r"\(([^)]+)\)\s*$", raw)
    if m:
        location = m.group(1).strip()
        raw = raw[: m.start()].strip()

    parts   = raw.split(" - ", 1)
    title   = _clean(parts[0])
    company = _clean(parts[1]) if len(parts) > 1 else ""
    return title, company, location


def _indeed_rss_to_job(raw: dict, search_location: str) -> dict | None:
    title_raw = raw.get("title", "")
    url       = raw.get("link", "")
    if not title_raw or not url:
        return None

    title, company, location = _parse_rss_title(title_raw)

    desc_html   = raw.get("desc", "")
    description = _clean(
        BeautifulSoup(desc_html, "html.parser").get_text(separator=" ")
    ) if desc_html else ""

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


def _jobbank_rss_to_job(raw: dict) -> dict | None:
    """
    Canada Job Bank RSS item converter.
    Title is the bare job title.  Description contains structured HTML with
    Employer, Location, Salary fields.
    """
    title = _clean(raw.get("title", ""))
    url   = raw.get("link", "")
    if not title or not url:
        return None

    desc_raw    = raw.get("desc", "")
    desc_text   = _clean(BeautifulSoup(desc_raw, "html.parser").get_text(separator=" ")) if desc_raw else ""

    # Extract structured fields from Job Bank's description text
    company  = _extract_field(desc_text, r"Employer[:\s]+([^;|\n]+)")
    location = _extract_field(desc_text, r"Location[:\s]+([^;|\n]+)")
    salary   = _extract_field(desc_text, r"Salary[:\s]+([^;|\n]+)")

    return {
        "title":       title,
        "company":     company,
        "location":    location,
        "salary":      salary,
        "date_posted": raw.get("pub_date", ""),
        "url":         url,
        "source":      "jobbank",
        "description": desc_text,
    }


def _adzuna_item_to_job(item: dict) -> dict | None:
    title = _clean(item.get("title", ""))
    url   = item.get("redirect_url", "") or item.get("url", "")
    if not title or not url:
        return None

    company  = item.get("company", {}).get("display_name", "")
    location = item.get("location", {}).get("display_name", "")
    sal_min  = item.get("salary_min")
    sal_max  = item.get("salary_max")
    salary   = ""
    if sal_min and sal_max:
        salary = f"${int(sal_min):,} – ${int(sal_max):,} / year"
    elif sal_min:
        salary = f"${int(sal_min):,}+ / year"

    return {
        "title":       title,
        "company":     _clean(company),
        "location":    _clean(location),
        "salary":      salary,
        "date_posted": item.get("created", "")[:10],
        "url":         url,
        "source":      "adzuna",
        "description": _clean(item.get("description", "")),
    }


def _extract_field(text: str, pattern: str) -> str:
    m = re.search(pattern, text, re.IGNORECASE)
    return _clean(m.group(1)) if m else ""


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
                print(f"[db] {job.get('url')}: {exc}")
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
            "DNT": "1",
        },
    )


async def _stealth(page: Page) -> None:
    await page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins', {
            get: () => [{ name: 'Chrome PDF Plugin' }, { name: 'Chrome PDF Viewer' }],
        });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-CA', 'en'] });
        if (!window.chrome) { window.chrome = { runtime: {} }; }
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


# ───────────────────────────── Source connectivity test ──────────────────────

def _test_sources(query: str = "data analyst", location: str = "Edmonton, AB") -> None:
    """
    Quick connectivity check for every HTTP job source.
    Prints a one-line result per source so you can see which ones work.
    Run from the project root:  python -c "from modules.job_searcher import _test_sources; _test_sources()"
    """
    sep = "-" * 60
    print(f"\n{sep}")
    print(f"  ResumeAI source test  |  query={query!r}  location={location!r}")
    print(sep)

    searcher = JobSearcher()

    # ── Indeed (expected: blocked) ────────────────────────────────────────────
    try:
        req = urllib.request.Request(
            f"{INDEED_BASE}/rss?q={quote_plus(query)}&l={quote(location, safe='')}",
            headers={"User-Agent": USER_AGENTS[0], "Referer": "https://www.google.com/"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            print(f"  [indeed]       UNEXPECTED OK — HTTP {r.status} (blocked expected)")
    except urllib.error.HTTPError as e:
        print(f"  [indeed]       BLOCKED — HTTP {e.code} (use Adzuna for Indeed-sourced jobs)")
    except Exception as e:
        print(f"  [indeed]       FAIL — {type(e).__name__}: {e}")

    # ── Canada Job Bank (HTML scraper) ────────────────────────────────────────
    try:
        city = location.split(",")[0].strip()
        url  = (
            f"{JOBBANK_BASE}/jobsearch/jobsearch?"
            f"searchstring={quote_plus(query)}&locationstring={quote_plus(city)}&pg=1"
        )
        content = _fetch_urllib(url, verify_ssl=False)
        soup    = BeautifulSoup(content, "html.parser")
        cards   = soup.select("article.action-buttons")
        if cards:
            title = _clean(cards[0].select_one(".noctitle").get_text()) if cards[0].select_one(".noctitle") else "?"
            print(f"  [jobbank-html] OK  — {len(cards)} cards  |  first: {title!r}")
        else:
            print(f"  [jobbank-html] WARN — 0 cards (no results for this query/location)")
    except Exception as e:
        print(f"  [jobbank-html] FAIL — {type(e).__name__}: {e}")

    # ── Adzuna API ────────────────────────────────────────────────────────────
    try:
        url  = (
            f"{ADZUNA_BASE}/search/1?"
            f"app_id=TEST&app_key=TEST"
            f"&results_per_page=5"
            f"&what={quote_plus(query)}"
            f"&where={quote_plus(location.split(',')[0])}"
            f"&content-type=application/json"
        )
        resp = requests.get(url, timeout=10)
        if resp.status_code == 401:
            print(f"  [adzuna]       OK  — API reachable (401 expected w/ test keys; add real keys in Settings)")
        elif resp.status_code == 200:
            n = len(resp.json().get("results", []))
            print(f"  [adzuna]       OK  — {n} results")
        else:
            print(f"  [adzuna]       WARN — HTTP {resp.status_code}")
    except Exception as e:
        print(f"  [adzuna]       FAIL — {type(e).__name__}: {e}")

    print(sep + "\n")
