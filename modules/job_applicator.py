"""Automates job application submission on Indeed Canada and LinkedIn.

Flow
----
1. Detect source (indeed / linkedin) from job URL
2. Log in once per session; reuse saved session state on subsequent runs
3. Navigate to job, click Easy Apply / Apply Now
4. Walk through every modal step:
   - fill text / email / tel / number / textarea fields
   - select dropdowns
   - choose radio buttons
   - tick required checkboxes
   - upload resume PDF
4. dry_run=True  → fill everything, screenshot the final review page, STOP
   dry_run=False → click Submit, screenshot confirmation, record to DB

All screenshots land in  data/screenshots/<company>_<title>_<timestamp>/
Session cookies are cached in  data/sessions/{indeed,linkedin}_session.json
"""

import asyncio
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.async_api import BrowserContext, Page, async_playwright

from modules.database import get_connection

# ─────────────────────────────────────── Paths ───────────────────────────────

_ROOT         = Path(__file__).parent.parent
SCREENSHOTS   = _ROOT / "data" / "screenshots"
SESSIONS      = _ROOT / "data" / "sessions"

INDEED_BASE   = "https://ca.indeed.com"
LINKEDIN_BASE = "https://www.linkedin.com"

# ──────────────────────────────── Stealth user-agents ────────────────────────

_UA = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

# ─────────────────────────────────── Main class ───────────────────────────────

class JobApplicator:
    """
    Apply to jobs automatically.

    Usage (sync / quick):
        result = JobApplicator(profile, dry_run=True).apply(job, resume_pdf)

    Usage (async from a Streamlit callback):
        result = await applicator.apply_async(job, resume_pdf)

    Parameters
    ----------
    profile           : dict returned by ProfileManager.get()
    headless          : run browser without a visible window
    dry_run           : fill forms completely but do NOT click Submit
    expected_salary   : free-text answer for salary questions ("85000" or "Open")
    notice_period     : answer for "when can you start?" questions
    willing_to_relocate: answer for relocation yes/no
    indeed_email      : Indeed account email  (required for Easy Apply)
    indeed_password   : Indeed account password
    linkedin_email    : LinkedIn account email
    linkedin_password : LinkedIn account password
    """

    def __init__(
        self,
        profile: dict,
        headless: bool = True,
        dry_run: bool = False,
        expected_salary: str = "Open to discussion",
        notice_period: str = "2 weeks",
        willing_to_relocate: bool = False,
        indeed_email: str = "",
        indeed_password: str = "",
        linkedin_email: str = "",
        linkedin_password: str = "",
    ):
        self.profile            = profile
        self.headless           = headless
        self.dry_run            = dry_run
        self.expected_salary    = expected_salary
        self.notice_period      = notice_period
        self.willing_to_relocate = willing_to_relocate
        self.indeed_email       = indeed_email
        self.indeed_password    = indeed_password
        self.linkedin_email     = linkedin_email
        self.linkedin_password  = linkedin_password

        SCREENSHOTS.mkdir(parents=True, exist_ok=True)
        SESSIONS.mkdir(parents=True, exist_ok=True)

    # ────────────────────────── Public sync interface ─────────────────────────

    def apply(self, job: dict, resume_path: str, cover_letter: str = "") -> dict:
        """Synchronous wrapper — safe to call from non-async code."""
        return asyncio.run(self.apply_async(job, resume_path, cover_letter))

    # ───────────────────────── Public async interface ─────────────────────────

    async def apply_async(self, job: dict, resume_path: str, cover_letter: str = "") -> dict:
        """Main entry point.  Returns an ApplyResult dict."""
        result = _new_result(job, self.dry_run)
        url    = job.get("url", "")

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=self.headless,
                args=["--no-sandbox",
                      "--disable-blink-features=AutomationControlled"],
            )

            if "indeed" in url:
                session_file = SESSIONS / "indeed_session.json"
                context = await _load_context(browser, session_file)
                try:
                    result = await self._apply_indeed(context, job, resume_path, cover_letter, result)
                finally:
                    await _save_context(context, session_file)

            elif "linkedin" in url:
                session_file = SESSIONS / "linkedin_session.json"
                context = await _load_context(browser, session_file)
                try:
                    result = await self._apply_linkedin(context, job, resume_path, cover_letter, result)
                finally:
                    await _save_context(context, session_file)

            else:
                result["error"] = f"Unsupported job source: {url}"

            await browser.close()

        if result["success"] or self.dry_run:
            _record_application(job, resume_path, cover_letter,
                                "dry_run" if self.dry_run else "applied")
            _update_job_status(job.get("url", ""),
                               "dry_run" if self.dry_run else "applied")
        else:
            _update_job_status(job.get("url", ""), "error")

        return result

    # ══════════════════════════════ INDEED ══════════════════════════════════

    async def _apply_indeed(
        self,
        context: BrowserContext,
        job: dict,
        resume_path: str,
        cover_letter: str,
        result: dict,
    ) -> dict:
        page = await context.new_page()
        await _stealth(page)
        slug = _slug(job)

        try:
            # ── 1. Log in if we have credentials and aren't already authed ──
            if self.indeed_email and self.indeed_password:
                await self._indeed_login(page, slug, result)

            # ── 2. Navigate to job page ──────────────────────────────────────
            await page.goto(job["url"], wait_until="domcontentloaded", timeout=30_000)
            await _delay(2, 3)
            await _dismiss_popups(page)
            result["steps"].append("Loaded job page")

            # ── 3. Click Apply / Easy Apply ──────────────────────────────────
            clicked = await self._indeed_click_apply(page)
            if not clicked:
                result["error"] = "Could not find an Apply button on the Indeed job page"
                return result
            result["steps"].append("Clicked Apply button")
            await _delay(2, 3)

            # ── 4. Walk through multi-step modal ─────────────────────────────
            step = 0
            while True:
                step += 1
                await _dismiss_popups(page)

                # Detect current modal root
                modal = await _first(page, [
                    "#ia-container",
                    ".ia-BasePage",
                    "div[data-testid='ia-IndeedApply-modal']",
                    "div[data-testid='IndeedApplyButton-modal']",
                    "form",
                ])
                root = modal or page

                filled = await self._fill_form_step(page, root, resume_path)
                result["fields_filled"].update(filled)

                shot = await _screenshot(page, slug, step)
                result["screenshots"].append(shot)
                result["steps"].append(f"Step {step}: filled {len(filled)} field(s)")

                # Locate action buttons
                submit_btn = await _first(page, [
                    "button[data-testid='submit-application-button']",
                    "button:has-text('Submit your application')",
                    "button:has-text('Submit application')",
                    "button[data-indeedapply-widget='submit']",
                ])
                next_btn = await _first(page, [
                    "button[data-testid='continue-button']",
                    "button:has-text('Continue')",
                    "button:has-text('Next')",
                    "button[type='submit']:not(:has-text('Submit'))",
                ])

                if submit_btn:
                    if self.dry_run:
                        shot = await _screenshot(page, slug, "DRY_RUN_final")
                        result["screenshots"].append(shot)
                        result["steps"].append("DRY RUN — stopping before Submit")
                        result["success"] = True
                    else:
                        await _human_click(page, submit_btn)
                        await _delay(2, 4)
                        shot = await _screenshot(page, slug, "submitted")
                        result["screenshots"].append(shot)
                        result["steps"].append("Clicked Submit")
                        result["success"] = True
                    break

                elif next_btn:
                    await _human_click(page, next_btn)
                    await _delay(1.5, 2.5)

                else:
                    result["error"] = f"No Next or Submit button found at step {step}"
                    break

                if step > 10:
                    result["error"] = "Exceeded 10 steps — possible infinite loop"
                    break

        except Exception as exc:
            result["error"] = str(exc)
            await _screenshot(page, slug, "error")
        finally:
            await page.close()

        return result

    async def _indeed_login(self, page: Page, slug: str, result: dict) -> None:
        """Log in to Indeed.  Skip silently if already authenticated."""
        await page.goto(f"{INDEED_BASE}/account/login", wait_until="domcontentloaded", timeout=20_000)
        await _delay(1.5, 2.5)

        # Already logged in?
        if await page.query_selector("a[href*='/account/view']"):
            result["steps"].append("Indeed: already logged in")
            return

        email_field = await _first(page, ["input[type='email']", "input#emailAddress"])
        if not email_field:
            result["steps"].append("Indeed: login form not found — skipping")
            return

        await _type_humanly(page, email_field, self.indeed_email)
        await _delay(0.5, 1)

        # Click Continue (first step only asks for email on some flows)
        cont = await _first(page, ["button:has-text('Continue')", "button[type='submit']"])
        if cont:
            await _human_click(page, cont)
            await _delay(1.5, 2.5)

        pw_field = await _first(page, ["input[type='password']", "input#password"])
        if pw_field:
            await _type_humanly(page, pw_field, self.indeed_password)
            await _delay(0.5, 1)
            sign_in = await _first(page, [
                "button:has-text('Sign in')",
                "button[type='submit']",
            ])
            if sign_in:
                await _human_click(page, sign_in)
                await _delay(2, 3)

        result["steps"].append("Indeed: login attempted")

    async def _indeed_click_apply(self, page: Page) -> bool:
        btn = await _first(page, [
            "button[data-testid='applyButton']",
            "button.indeedApplyButton",
            "a.indeedApplyButton",
            "button:has-text('Apply now')",
            "button:has-text('Easily apply')",
            "span[data-indeed-apply-widget]",
        ])
        if btn:
            await _human_click(page, btn)
            return True
        return False

    # ══════════════════════════════ LINKEDIN ════════════════════════════════

    async def _apply_linkedin(
        self,
        context: BrowserContext,
        job: dict,
        resume_path: str,
        cover_letter: str,
        result: dict,
    ) -> dict:
        page = await context.new_page()
        await _stealth(page)
        slug = _slug(job)

        try:
            # ── 1. Login ─────────────────────────────────────────────────────
            if self.linkedin_email and self.linkedin_password:
                await self._linkedin_login(page, result)

            # ── 2. Navigate to job ───────────────────────────────────────────
            await page.goto(job["url"], wait_until="domcontentloaded", timeout=30_000)
            await _delay(2, 3)
            await _dismiss_popups(page)
            result["steps"].append("Loaded job page")

            # ── 3. Click Easy Apply ──────────────────────────────────────────
            clicked = await self._linkedin_click_easy_apply(page)
            if not clicked:
                result["error"] = "Easy Apply button not found — job may require external application"
                return result
            result["steps"].append("Clicked Easy Apply")
            await _delay(2, 3)

            # ── 4. Walk through modal steps ──────────────────────────────────
            step = 0
            while True:
                step += 1
                await _dismiss_popups(page)

                modal = await _first(page, [
                    ".jobs-easy-apply-content",
                    "div[data-test-modal]",
                    ".artdeco-modal__content",
                ])
                root = modal or page

                filled = await self._fill_form_step(page, root, resume_path)
                result["fields_filled"].update(filled)

                shot = await _screenshot(page, slug, step)
                result["screenshots"].append(shot)
                result["steps"].append(f"Step {step}: filled {len(filled)} field(s)")

                submit_btn = await _first(page, [
                    "button:has-text('Submit application')",
                    "button[aria-label='Submit application']",
                ])
                review_btn = await _first(page, [
                    "button:has-text('Review')",
                    "button[aria-label='Review your application']",
                ])
                next_btn = await _first(page, [
                    "button:has-text('Next')",
                    "button[aria-label='Continue to next step']",
                ])

                if submit_btn:
                    if self.dry_run:
                        shot = await _screenshot(page, slug, "DRY_RUN_final")
                        result["screenshots"].append(shot)
                        result["steps"].append("DRY RUN — stopping before Submit")
                        result["success"] = True
                    else:
                        await _human_click(page, submit_btn)
                        await _delay(2, 4)
                        shot = await _screenshot(page, slug, "submitted")
                        result["screenshots"].append(shot)
                        result["steps"].append("Clicked Submit application")
                        result["success"] = True
                    break

                elif review_btn:
                    await _human_click(page, review_btn)
                    await _delay(1.5, 2.5)

                elif next_btn:
                    await _human_click(page, next_btn)
                    await _delay(1.5, 2.5)

                else:
                    result["error"] = f"No Next / Review / Submit found at step {step}"
                    break

                if step > 10:
                    result["error"] = "Exceeded 10 steps — possible infinite loop"
                    break

        except Exception as exc:
            result["error"] = str(exc)
            await _screenshot(page, slug, "error")
        finally:
            await page.close()

        return result

    async def _linkedin_login(self, page: Page, result: dict) -> None:
        await page.goto(f"{LINKEDIN_BASE}/login", wait_until="domcontentloaded", timeout=20_000)
        await _delay(1.5, 2)

        # Already logged in?
        if "feed" in page.url or "mynetwork" in page.url:
            result["steps"].append("LinkedIn: already logged in")
            return

        email_field = await _first(page, ["input#username", "input[name='session_key']"])
        pw_field    = await _first(page, ["input#password", "input[name='session_password']"])

        if not email_field or not pw_field:
            result["steps"].append("LinkedIn: login form not found — skipping")
            return

        await _type_humanly(page, email_field, self.linkedin_email)
        await _delay(0.3, 0.7)
        await _type_humanly(page, pw_field, self.linkedin_password)
        await _delay(0.3, 0.7)

        sign_in = await _first(page, [
            "button[type='submit']",
            "button:has-text('Sign in')",
        ])
        if sign_in:
            await _human_click(page, sign_in)
            await _delay(2, 3)

        result["steps"].append("LinkedIn: login attempted")

    async def _linkedin_click_easy_apply(self, page: Page) -> bool:
        btn = await _first(page, [
            "button.jobs-apply-button",
            "button[aria-label*='Easy Apply']",
            "button:has-text('Easy Apply')",
        ])
        if btn and await btn.is_visible():
            await _human_click(page, btn)
            return True
        return False

    # ══════════════════════════ GENERIC FORM FILLER ═════════════════════════

    async def _fill_form_step(self, page: Page, root, resume_path: str) -> dict:
        """Fill all visible fields inside *root* on the current modal step."""
        filled: dict[str, str] = {}

        filled.update(await self._fill_text_inputs(page, root))
        filled.update(await self._fill_textareas(page, root))
        filled.update(await self._fill_selects(page, root))
        filled.update(await self._fill_radios(page, root))
        filled.update(await self._fill_checkboxes(page, root))

        uploaded = await self._upload_resume(page, root, resume_path)
        if uploaded:
            filled["[resume upload]"] = resume_path

        return filled

    # ── Text inputs (type=text / email / tel / number / search) ─────────────

    async def _fill_text_inputs(self, page: Page, root) -> dict:
        filled = {}
        inputs = await root.query_selector_all(
            "input[type='text'], input[type='email'], "
            "input[type='tel'], input[type='number'], input:not([type])"
        )
        for el in inputs:
            try:
                if not await el.is_visible() or not await el.is_enabled():
                    continue
                existing = (await el.input_value()).strip()
                if existing:
                    continue  # already populated (e.g. pre-filled by LinkedIn)
                label = await _resolve_label(page, el)
                value = self._answer_text(label)
                if value:
                    await el.scroll_into_view_if_needed()
                    await el.click()
                    await el.fill(value)
                    await _delay(0.15, 0.4)
                    filled[label or "[unlabelled]"] = value
            except Exception:
                continue
        return filled

    # ── Textareas (cover-letter / additional info) ───────────────────────────

    async def _fill_textareas(self, page: Page, root) -> dict:
        filled = {}
        areas = await root.query_selector_all("textarea")
        for el in areas:
            try:
                if not await el.is_visible() or not await el.is_enabled():
                    continue
                existing = (await el.input_value()).strip()
                if existing:
                    continue
                label = await _resolve_label(page, el)
                value = self._answer_textarea(label)
                if value:
                    await el.scroll_into_view_if_needed()
                    await el.click()
                    await el.fill(value)
                    filled[label or "[textarea]"] = value[:60] + "…"
            except Exception:
                continue
        return filled

    # ── <select> dropdowns ───────────────────────────────────────────────────

    async def _fill_selects(self, page: Page, root) -> dict:
        filled = {}
        selects = await root.query_selector_all("select")
        for el in selects:
            try:
                if not await el.is_visible():
                    continue
                label  = await _resolve_label(page, el)
                option = await self._best_select_option(el, label)
                if option:
                    await el.select_option(label=option)
                    await _delay(0.1, 0.3)
                    filled[label or "[select]"] = option
            except Exception:
                continue
        return filled

    async def _best_select_option(self, el, label: str) -> Optional[str]:
        """Choose the most appropriate <option> for this dropdown."""
        options: list[str] = await el.evaluate(
            "s => [...s.options].map(o => o.text.trim())"
        )
        if not options:
            return None

        preferred = self._answer_select(label, options)
        return preferred

    # ── Radio button groups ───────────────────────────────────────────────────

    async def _fill_radios(self, page: Page, root) -> dict:
        filled = {}
        # Collect all radio groups by name attribute
        all_radios = await root.query_selector_all("input[type='radio']")
        groups: dict[str, list] = {}
        for r in all_radios:
            name = await r.get_attribute("name") or "__unnamed__"
            groups.setdefault(name, []).append(r)

        for name, radios in groups.items():
            try:
                # Find the group label from the first radio's legend/fieldset/label
                group_label = await _radio_group_label(page, radios[0])
                choice = self._answer_yes_no(group_label)

                # Match choice text against visible labels
                selected = False
                for r in radios:
                    r_id    = await r.get_attribute("id") or ""
                    r_val   = (await r.get_attribute("value") or "").strip()
                    r_label = ""
                    if r_id:
                        lbl_el = await page.query_selector(f"label[for='{r_id}']")
                        if lbl_el:
                            r_label = _clean(await lbl_el.inner_text())

                    if _yn_match(choice, r_label or r_val):
                        await r.scroll_into_view_if_needed()
                        await r.click()
                        await _delay(0.1, 0.3)
                        filled[group_label or name] = r_label or r_val
                        selected = True
                        break

                # Fallback: pick first option if nothing matched
                if not selected and radios:
                    await radios[0].click()
                    filled[group_label or name] = "(first option — no match)"
            except Exception:
                continue
        return filled

    # ── Checkboxes ───────────────────────────────────────────────────────────

    async def _fill_checkboxes(self, page: Page, root) -> dict:
        filled = {}
        boxes = await root.query_selector_all("input[type='checkbox']")
        for box in boxes:
            try:
                if not await box.is_visible():
                    continue
                is_checked = await box.is_checked()
                if is_checked:
                    continue
                b_id    = await box.get_attribute("id") or ""
                b_label = ""
                if b_id:
                    lbl = await page.query_selector(f"label[for='{b_id}']")
                    if lbl:
                        b_label = _clean(await lbl.inner_text())

                # Only auto-check required checkboxes or legal agreements
                required  = await box.get_attribute("required") is not None
                is_terms  = any(w in b_label.lower()
                                for w in ("agree", "terms", "policy", "certify", "confirm"))
                if required or is_terms:
                    await box.check()
                    await _delay(0.1, 0.3)
                    filled[b_label or b_id or "[checkbox]"] = "checked"
            except Exception:
                continue
        return filled

    # ── Resume upload ─────────────────────────────────────────────────────────

    async def _upload_resume(self, page: Page, root, resume_path: str) -> bool:
        if not resume_path or not Path(resume_path).exists():
            return False
        file_inputs = await root.query_selector_all("input[type='file']")
        for el in file_inputs:
            try:
                accept = (await el.get_attribute("accept") or "").lower()
                # Only upload to file inputs that accept docs/pdfs (skip avatar uploads)
                if accept and not any(x in accept for x in ("pdf", "doc", ".doc", "application")):
                    continue
                await el.set_input_files(resume_path)
                await _delay(1, 2)
                return True
            except Exception:
                continue
        return False

    # ══════════════════════════ ANSWER LOGIC ════════════════════════════════

    def _answer_text(self, label: str) -> str:
        """Map a field label to a value from the user's profile."""
        p   = self.profile.get("personal", {})
        exp = self.profile.get("experience", [])
        edu = self.profile.get("education", [{}])
        lo  = label.lower()

        # ── Name parts ────────────────────────────────────────────────────────
        full = p.get("full_name", "")
        parts = full.split()
        if any(w in lo for w in ["first name", "given name"]):
            return parts[0] if parts else ""
        if any(w in lo for w in ["last name", "surname", "family name"]):
            return parts[-1] if len(parts) > 1 else ""
        if any(w in lo for w in ["full name", "your name"]):
            return full

        # ── Contact ───────────────────────────────────────────────────────────
        if "email" in lo:
            return p.get("email", "")
        if any(w in lo for w in ["phone", "mobile", "telephone", "cell"]):
            return p.get("phone", "")

        # ── Location ──────────────────────────────────────────────────────────
        if any(w in lo for w in ["city", "location", "address", "where are you"]):
            return p.get("location", "")

        # ── Online presence ───────────────────────────────────────────────────
        if "linkedin" in lo:
            return p.get("linkedin_url", "")
        if any(w in lo for w in ["github", "portfolio", "website", "personal url"]):
            return p.get("github_url", "")

        # ── Experience ────────────────────────────────────────────────────────
        if any(w in lo for w in ["years of experience", "years experience",
                                  "how many years", "years working"]):
            return str(_total_experience_years(exp))

        # ── Compensation ──────────────────────────────────────────────────────
        if any(w in lo for w in ["salary", "compensation", "pay", "rate",
                                  "expected", "desired wage"]):
            return self.expected_salary

        # ── Availability ─────────────────────────────────────────────────────
        if any(w in lo for w in ["start date", "available to start",
                                  "notice period", "when can you start"]):
            return self.notice_period

        # ── Education ────────────────────────────────────────────────────────
        if any(w in lo for w in ["degree", "highest level of education", "gpa"]):
            if edu:
                if "gpa" in lo:
                    return edu[0].get("gpa", "")
                return edu[0].get("degree", "")

        return ""

    def _answer_textarea(self, label: str) -> str:
        lo = label.lower()
        if any(w in lo for w in ["cover letter", "cover_letter", "why do you",
                                  "tell us about", "additional information",
                                  "anything else"]):
            # Return a brief generic statement; cover letters are provided separately
            p    = self.profile.get("personal", {})
            exp  = self.profile.get("experience", [])
            role = exp[0].get("title", "professional") if exp else "professional"
            return (
                f"I am an experienced {role} excited about this opportunity. "
                f"Please refer to my attached resume for full details about my background."
            )
        return ""

    def _answer_select(self, label: str, options: list[str]) -> Optional[str]:
        lo = label.lower()
        p  = self.profile.get("personal", {})
        wa = p.get("work_authorization", "").lower()

        # ── Work authorization / country ──────────────────────────────────────
        if any(w in lo for w in ["authorized", "work authorization",
                                  "eligible to work", "right to work"]):
            for opt in options:
                ol = opt.lower()
                if "yes" in ol or "authorized" in ol or "eligible" in ol:
                    return opt

        # ── Sponsorship ───────────────────────────────────────────────────────
        if "sponsor" in lo:
            for opt in options:
                ol = opt.lower()
                if ("citizen" in wa or "pr" in wa or "permanent" in wa):
                    if "no" in ol or "not require" in ol:
                        return opt
                else:
                    if "yes" in ol or "require" in ol:
                        return opt

        # ── Education level ───────────────────────────────────────────────────
        if any(w in lo for w in ["education", "degree", "highest level"]):
            edu = self.profile.get("education", [])
            if edu:
                degree = edu[0].get("degree", "").lower()
                for opt in options:
                    ol = opt.lower()
                    if "bachelor" in degree and "bachelor" in ol:
                        return opt
                    if "master" in degree and "master" in ol:
                        return opt
                    if "phd" in degree or "doctor" in degree:
                        if "doctor" in ol or "phd" in ol:
                            return opt

        # ── Experience range ──────────────────────────────────────────────────
        if any(w in lo for w in ["years of experience", "experience level"]):
            years = _total_experience_years(self.profile.get("experience", []))
            for opt in options:
                nums = re.findall(r"\d+", opt)
                if len(nums) >= 2:
                    lo_y, hi_y = int(nums[0]), int(nums[1])
                    if lo_y <= years <= hi_y:
                        return opt
                elif nums and int(nums[0]) <= years:
                    return opt

        # ── Yes / No generic ─────────────────────────────────────────────────
        choice = self._answer_yes_no(label)
        for opt in options:
            if _yn_match(choice, opt):
                return opt

        # Default: skip placeholder "Select …" options
        non_empty = [o for o in options if o.strip() and "select" not in o.lower()]
        return non_empty[0] if non_empty else None

    def _answer_yes_no(self, label: str) -> str:
        """Return 'Yes' or 'No' for binary screening questions."""
        lo = label.lower()
        p  = self.profile.get("personal", {})
        wa = p.get("work_authorization", "").lower()

        # Work authorisation
        if any(w in lo for w in ["authorized to work", "eligible to work",
                                  "right to work", "legally permitted"]):
            return "Yes"

        # Sponsorship requirement
        if "sponsorship" in lo and any(w in lo for w in ["require", "need"]):
            return "No" if any(w in wa for w in ["citizen", "pr", "permanent"]) else "Yes"

        # Citizenship
        if "canadian citizen" in lo or "us citizen" in lo:
            return "Yes" if "citizen" in wa else "No"

        # Relocation
        if "relocat" in lo:
            return "Yes" if self.willing_to_relocate else "No"

        # Remote / hybrid
        if any(w in lo for w in ["remote", "work from home", "hybrid"]):
            return "Yes"

        # Veteran
        if "veteran" in lo or "military" in lo:
            return "No"

        # Disability — safest neutral answer
        if "disability" in lo or "disabled" in lo:
            return "No"

        # Background check, drug test
        if any(w in lo for w in ["background check", "drug test", "drug screen"]):
            return "Yes"

        return "Yes"  # safe default for unknown yes/no


# ─────────────────────────────── DB helpers ──────────────────────────────────

def _record_application(job: dict, resume_path: str, cover_letter: str, outcome: str) -> None:
    with get_connection() as conn:
        job_row = conn.execute(
            "SELECT id FROM jobs WHERE url = ?", (job.get("url", ""),)
        ).fetchone()
        job_id = job_row["id"] if job_row else None

        conn.execute(
            """
            INSERT INTO applications (job_id, resume_path, cover_letter, outcome)
            VALUES (?, ?, ?, ?)
            """,
            (job_id, resume_path, cover_letter, outcome),
        )


def _update_job_status(url: str, status: str) -> None:
    if not url:
        return
    with get_connection() as conn:
        conn.execute("UPDATE jobs SET status = ? WHERE url = ?", (status, url))


# ────────────────────────── Browser / context helpers ────────────────────────

async def _load_context(browser, session_file: Path) -> BrowserContext:
    """Load a saved auth session if one exists, otherwise start fresh."""
    if session_file.exists():
        context = await browser.new_context(
            storage_state=str(session_file),
            user_agent=random.choice(_UA),
            viewport={"width": random.choice([1366, 1440, 1920]),
                      "height": random.choice([768, 900, 1080])},
            locale="en-CA",
            timezone_id="America/Toronto",
        )
    else:
        context = await browser.new_context(
            user_agent=random.choice(_UA),
            viewport={"width": random.choice([1366, 1440, 1920]),
                      "height": random.choice([768, 900, 1080])},
            locale="en-CA",
            timezone_id="America/Toronto",
        )
    return context


async def _save_context(context: BrowserContext, session_file: Path) -> None:
    try:
        await context.storage_state(path=str(session_file))
    except Exception:
        pass
    finally:
        await context.close()


async def _stealth(page: Page) -> None:
    await page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'plugins',   { get: () => [1, 2, 3] });
        Object.defineProperty(navigator, 'languages', { get: () => ['en-CA', 'en'] });
        if (!window.chrome) window.chrome = { runtime: {} };
    """)


async def _dismiss_popups(page: Page) -> None:
    for sel in [
        "button#onetrust-accept-btn-handler",
        "button[data-testid='allow-all-cookies']",
        "button.artdeco-modal__dismiss",
        "button[aria-label='Dismiss']",
        "button[data-test='modal-close-btn']",
        "button.contextual-sign-in-modal__modal-dismiss",
        "button[aria-label='Dismiss sign in nudge']",
    ]:
        try:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                await btn.click()
                await asyncio.sleep(0.4)
        except Exception:
            pass


async def _human_click(page: Page, el) -> None:
    await el.scroll_into_view_if_needed()
    await asyncio.sleep(random.uniform(0.2, 0.5))
    await el.hover()
    await asyncio.sleep(random.uniform(0.1, 0.3))
    await el.click()


async def _type_humanly(page: Page, el, text: str) -> None:
    await el.click()
    await el.fill("")
    for char in text:
        await el.type(char, delay=random.randint(40, 140))


async def _delay(lo: float = 1.0, hi: float = 2.5) -> None:
    await asyncio.sleep(random.uniform(lo, hi))


async def _first(page_or_root, selectors: list[str]):
    """Return the first visible element matching any selector."""
    for sel in selectors:
        try:
            el = await page_or_root.query_selector(sel)
            if el and await el.is_visible():
                return el
        except Exception:
            continue
    return None


async def _screenshot(page: Page, slug: str, step) -> str:
    folder = SCREENSHOTS / slug
    folder.mkdir(parents=True, exist_ok=True)
    path = str(folder / f"step_{step}.png")
    try:
        await page.screenshot(path=path, full_page=False)
    except Exception:
        pass
    return path


# ─────────────────────────── Label resolution ────────────────────────────────

async def _resolve_label(page: Page, el) -> str:
    """Find the human-readable label for a form element."""
    # 1. aria-label
    v = await el.get_attribute("aria-label")
    if v:
        return _clean(v)

    # 2. aria-labelledby
    v = await el.get_attribute("aria-labelledby")
    if v:
        labelled = await page.query_selector(f"#{v}")
        if labelled:
            return _clean(await labelled.inner_text())

    # 3. <label for="id">
    el_id = await el.get_attribute("id")
    if el_id:
        lbl = await page.query_selector(f"label[for='{el_id}']")
        if lbl:
            return _clean(await lbl.inner_text())

    # 4. Wrapping <label>
    parent_label = await el.evaluate(
        "e => e.closest('label')?.innerText || ''"
    )
    if parent_label:
        return _clean(parent_label)

    # 5. Nearest preceding label sibling
    preceding = await el.evaluate("""e => {
        let s = e.previousElementSibling;
        while (s) {
            if (s.tagName === 'LABEL') return s.innerText;
            s = s.previousElementSibling;
        }
        return '';
    }""")
    if preceding:
        return _clean(preceding)

    # 6. placeholder fallback
    v = await el.get_attribute("placeholder")
    return _clean(v) if v else ""


async def _radio_group_label(page: Page, radio) -> str:
    """Find the fieldset legend or nearest group label for a radio button."""
    legend = await radio.evaluate("""r => {
        const fs = r.closest('fieldset');
        return fs?.querySelector('legend')?.innerText || '';
    }""")
    if legend:
        return _clean(legend)

    # Try aria-labelledby on the role="group" ancestor
    group_label = await radio.evaluate("""r => {
        const g = r.closest('[role="group"]');
        if (!g) return '';
        const id = g.getAttribute('aria-labelledby');
        return id ? (document.getElementById(id)?.innerText || '') : '';
    }""")
    if group_label:
        return _clean(group_label)

    return await _resolve_label(page, radio)


# ────────────────────────────── Utility functions ────────────────────────────

def _total_experience_years(experience: list[dict]) -> int:
    total = 0
    for job in experience:
        start = _parse_date(job.get("start_date", ""))
        end_s = job.get("end_date", "").lower().strip()
        end   = datetime.now() if end_s in ("present", "current", "now", "") \
                else _parse_date(end_s)
        if start and end:
            months = (end.year - start.year) * 12 + (end.month - start.month)
            total += max(0, months)
    return max(0, total // 12)


def _parse_date(s: str) -> Optional[datetime]:
    for fmt in ("%B %Y", "%b %Y", "%Y-%m", "%Y", "%m/%Y", "%b. %Y"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue
    return None


def _yn_match(choice: str, option_text: str) -> bool:
    """Return True if the option text matches the yes/no choice."""
    lo = option_text.lower()
    if choice.lower() == "yes":
        return lo in ("yes", "y", "true") or lo.startswith("yes")
    else:
        return lo in ("no", "n", "false") or lo.startswith("no")


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _slug(job: dict) -> str:
    raw = (
        f"{job.get('company', 'company')}_{job.get('title', 'role')}"
        f"_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )
    return re.sub(r"[^a-zA-Z0-9_-]", "_", raw)[:60]


def _new_result(job: dict, dry_run: bool) -> dict:
    return {
        "success":        False,
        "dry_run":        dry_run,
        "source":         job.get("source", ""),
        "job_url":        job.get("url", ""),
        "job_title":      job.get("title", ""),
        "company":        job.get("company", ""),
        "steps":          [],
        "fields_filled":  {},
        "screenshots":    [],
        "error":          None,
    }
