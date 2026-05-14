"""ResumeAI — Autonomous Job Application Agent · Streamlit UI"""
import json
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

from modules.database import get_connection, init_db
from modules.profile_manager import ProfileManager

# ──────────────────────────────────────── Bootstrap ──────────────────────────

st.set_page_config(
    page_title="ResumeAI",
    page_icon="assets/icon.png" if Path("assets/icon.png").exists() else ":briefcase:",
    layout="wide",
    initial_sidebar_state="expanded",
)

_ROOT = Path(__file__).parent
SETTINGS_PATH = _ROOT / "data" / "settings.json"

DEFAULT_SETTINGS: dict = {
    "anthropic_api_key":  "",
    "dry_run":            True,
    "max_apps_per_day":   10,
    "expected_salary":    "Open to discussion",
    "notice_period":      "2 weeks",
    "willing_to_relocate": False,
    "sources_indeed":     True,
    "sources_jobbank":    True,
    "sources_linkedin":   False,
    "sources_adzuna":     False,
    "sources_jooble":     False,
    "adzuna_app_id":      "",
    "adzuna_app_key":     "",
    "jooble_api_key":     "",
    "max_search_pages":   2,
    "indeed_email":       "",
    "indeed_password":    "",
    "linkedin_email":     "",
    "linkedin_password":  "",
}

# ──────────────────────────────────────── CSS ─────────────────────────────────

def _inject_css() -> None:
    st.markdown("""
    <style>
    /* ── Sidebar ── */
    section[data-testid="stSidebar"] {
        background: linear-gradient(160deg, #0d1117 0%, #161b22 100%);
        border-right: 1px solid #30363d;
    }
    section[data-testid="stSidebar"] p,
    section[data-testid="stSidebar"] span,
    section[data-testid="stSidebar"] label,
    section[data-testid="stSidebar"] div  { color: #c9d1d9; }
    section[data-testid="stSidebar"] h1,
    section[data-testid="stSidebar"] h2,
    section[data-testid="stSidebar"] h3  { color: #e6edf3 !important; }
    section[data-testid="stSidebar"] hr  { border-color: #30363d; }
    section[data-testid="stSidebar"] .stProgress > div > div > div {
        background: linear-gradient(90deg, #238636, #2ea043);
    }

    /* ── Stat pills in sidebar ── */
    .stat-grid { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin:6px 0 14px; }
    .stat-pill {
        background:#21262d; border:1px solid #30363d; border-radius:10px;
        padding:10px 8px; text-align:center;
    }
    .stat-pill .val { font-size:1.45rem; font-weight:700; color:#58a6ff; line-height:1.1; }
    .stat-pill .lbl { font-size:0.72rem; color:#8b949e; margin-top:3px; }
    .stat-pill.green .val { color:#3fb950; }
    .stat-pill.amber .val { color:#e3b341; }
    .stat-pill.purple .val{ color:#a371f7; }

    /* ── Status badges ── */
    .badge {
        display:inline-block; padding:2px 9px; border-radius:12px;
        font-size:0.75rem; font-weight:600; letter-spacing:.3px;
    }
    .badge-new      { background:#1f6feb22; color:#58a6ff; border:1px solid #1f6feb55; }
    .badge-applied  { background:#23863622; color:#3fb950; border:1px solid #23863655; }
    .badge-pending  { background:#9e6a0322; color:#e3b341; border:1px solid #9e6a0355; }
    .badge-dry_run  { background:#6e40c922; color:#a371f7; border:1px solid #6e40c955; }
    .badge-rejected { background:#da363322; color:#f85149; border:1px solid #da363355; }
    .badge-error    { background:#da363322; color:#f85149; border:1px solid #da363355; }
    .badge-indeed   { background:#2037a522; color:#6fa8dc; border:1px solid #2037a555; }
    .badge-linkedin { background:#0a66c222; color:#5ba4cf; border:1px solid #0a66c255; }

    /* ── Job cards ── */
    .job-card {
        background:#161b22; border:1px solid #30363d; border-radius:10px;
        padding:16px 20px; margin:8px 0;
        transition:border-color .15s;
    }
    .job-card:hover { border-color:#58a6ff44; }
    .job-card .jc-title  { font-size:1.05rem; font-weight:600; color:#e6edf3; }
    .job-card .jc-sub    { font-size:0.85rem; color:#8b949e; margin-top:3px; }
    .job-card .jc-salary { font-size:0.82rem; color:#3fb950; }

    /* ── Score ring ── */
    .score-ring {
        display:inline-flex; align-items:center; justify-content:center;
        width:52px; height:52px; border-radius:50%;
        font-size:1.05rem; font-weight:700; color:#e6edf3;
        border:3px solid #238636;
    }
    .score-ring.mid  { border-color:#e3b341; }
    .score-ring.low  { border-color:#da3633; }

    /* ── Section headers ── */
    .section-title {
        font-size:1.15rem; font-weight:700; color:#e6edf3;
        border-bottom:1px solid #30363d; padding-bottom:6px;
        margin:24px 0 14px;
    }

    /* ── Auto-Apply panel ── */
    .aa-panel {
        background:linear-gradient(135deg,#0d1117 0%,#0d1f0f 100%);
        border:1px solid #238636; border-radius:12px;
        padding:20px 24px; margin:12px 0;
    }
    .aa-panel h4 { color:#3fb950; margin:0 0 4px; font-size:1rem; }
    .aa-panel p  { color:#8b949e; margin:0 0 14px; font-size:0.85rem; }

    /* ── Live log box ── */
    .log-box {
        background:#0d1117; border:1px solid #30363d; border-radius:8px;
        padding:14px 16px; max-height:430px; overflow-y:auto;
        font-family:monospace;
    }

    /* ── Misc ── */
    .main .block-container { padding-top:1.2rem; max-width:1200px; }
    div[data-testid="stExpander"] { border:1px solid #30363d !important; border-radius:8px; }
    </style>
    """, unsafe_allow_html=True)


# ──────────────────────────────────── Settings I/O ───────────────────────────

def _load_settings() -> dict:
    if SETTINGS_PATH.exists():
        try:
            return {**DEFAULT_SETTINGS, **json.loads(SETTINGS_PATH.read_text())}
        except Exception:
            pass
    return DEFAULT_SETTINGS.copy()


def _save_settings(s: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(s, indent=2))


# ──────────────────────────────────── DB helpers ─────────────────────────────

def _get_stats() -> dict:
    today = datetime.now().date().isoformat()
    week_ago = (datetime.now() - timedelta(days=7)).isoformat()
    try:
        with get_connection() as conn:
            total   = conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
            today_n = conn.execute(
                "SELECT COUNT(*) FROM applications WHERE DATE(applied_at)=?", (today,)
            ).fetchone()[0]
            week_n  = conn.execute(
                "SELECT COUNT(*) FROM applications WHERE applied_at>=?", (week_ago,)
            ).fetchone()[0]
            success_row = conn.execute(
                "SELECT COUNT(*) FROM applications WHERE outcome='applied'"
            ).fetchone()[0]
            rate = round(success_row / total * 100) if total else 0
    except Exception:
        total = today_n = week_n = rate = 0
    return {"total": total, "today": today_n, "week": week_n, "rate": rate}


def _get_applications(status_filter: str = "all") -> list[dict]:
    try:
        with get_connection() as conn:
            q = """
                SELECT a.id, a.applied_at, a.outcome, a.resume_path,
                       a.match_score,
                       j.title, j.company, j.url, j.source, j.location, j.status
                FROM applications a
                LEFT JOIN jobs j ON a.job_id = j.id
                ORDER BY a.applied_at DESC
            """
            rows = conn.execute(q).fetchall()
            apps = [dict(r) for r in rows]
    except Exception:
        apps = []

    if status_filter != "all":
        apps = [a for a in apps if a.get("outcome") == status_filter]
    return apps


def _get_all_jobs() -> list[dict]:
    try:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def _local_match(job: dict, profile: dict) -> int:
    """Keyword overlap score 0-100 — fast, no API call."""
    skills = {s.lower() for s in profile.get("skills", {}).get("technical", [])}
    skills |= {s.lower() for s in profile.get("skills", {}).get("tools", [])}
    text = (
        (job.get("description") or "") + " " +
        (job.get("title") or "") + " " +
        (job.get("company") or "")
    ).lower()
    if not skills or not text.strip():
        return 0
    hits = sum(1 for s in skills if s in text)
    return min(int(hits / len(skills) * 100), 100)


def _badge(text: str, cls: str = "") -> str:
    return f'<span class="badge badge-{cls or text.lower()}">{text}</span>'


def _score_html(score: int) -> str:
    cls = "low" if score < 50 else ("mid" if score < 75 else "")
    return f'<div class="score-ring {cls}">{score}</div>'


# ──────────────────────────────────────── Sidebar ────────────────────────────

def _render_sidebar(pm: ProfileManager, stats: dict) -> None:
    with st.sidebar:
        st.markdown("## ResumeAI")
        st.markdown("<small style='color:#8b949e'>Autonomous Job Agent</small>",
                    unsafe_allow_html=True)
        st.markdown("---")

        # Profile completeness
        pct = pm.completeness_percent()
        complete, _ = pm.completeness_check()
        bar_color = "#238636" if complete else "#e3b341"
        st.markdown(f"**Profile Completeness**")
        st.progress(pct / 100)
        st.markdown(
            f"<small style='color:{'#3fb950' if complete else '#e3b341'}'>"
            f"{'✓ Ready to apply' if complete else f'{pct}% — complete your profile'}"
            f"</small>",
            unsafe_allow_html=True,
        )
        st.markdown("---")

        # Stats
        st.markdown("**Activity**")
        st.markdown(f"""
        <div class="stat-grid">
            <div class="stat-pill">
                <div class="val">{stats['today']}</div>
                <div class="lbl">Today</div>
            </div>
            <div class="stat-pill">
                <div class="val">{stats['week']}</div>
                <div class="lbl">This Week</div>
            </div>
            <div class="stat-pill green">
                <div class="val">{stats['rate']}%</div>
                <div class="lbl">Success Rate</div>
            </div>
            <div class="stat-pill purple">
                <div class="val">{stats['total']}</div>
                <div class="lbl">Total Apps</div>
            </div>
        </div>
        """, unsafe_allow_html=True)
        st.markdown("---")

        # Quick personal summary
        p = pm.get_personal()
        if p.get("full_name"):
            st.markdown(f"**{p['full_name']}**")
        if p.get("location"):
            st.markdown(f"<small style='color:#8b949e'>{p['location']}</small>",
                        unsafe_allow_html=True)
        if p.get("work_authorization"):
            st.markdown(f"<small style='color:#8b949e'>{p['work_authorization']}</small>",
                        unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
#  TAB 1 — MY PROFILE
# ══════════════════════════════════════════════════════════════════════════════

def _tab_profile(pm: ProfileManager) -> None:

    # ── Personal ─────────────────────────────────────────────────────────────
    st.markdown('<div class="section-title">Personal Information</div>',
                unsafe_allow_html=True)
    p = pm.get_personal()
    with st.form("form_personal"):
        c1, c2 = st.columns(2)
        full_name  = c1.text_input("Full Name *",          value=p.get("full_name", ""))
        email      = c2.text_input("Email *",              value=p.get("email", ""))
        c3, c4 = st.columns(2)
        phone      = c3.text_input("Phone *",              value=p.get("phone", ""))
        location   = c4.text_input("Location *",           value=p.get("location", ""),
                                    placeholder="Toronto, ON")
        c5, c6 = st.columns(2)
        linkedin   = c5.text_input("LinkedIn URL",         value=p.get("linkedin_url", ""),
                                    placeholder="https://linkedin.com/in/yourname")
        github     = c6.text_input("GitHub / Portfolio",   value=p.get("github_url", ""),
                                    placeholder="https://github.com/yourname")
        work_auth  = st.selectbox(
            "Work Authorization *",
            ["", "Canadian Citizen", "Permanent Resident", "Work Permit",
             "Open Work Permit", "Student Visa", "Requires Sponsorship"],
            index=0 if not p.get("work_authorization") else (
                ["", "Canadian Citizen", "Permanent Resident", "Work Permit",
                 "Open Work Permit", "Student Visa", "Requires Sponsorship"]
                .index(p.get("work_authorization"))
                if p.get("work_authorization") in
                ["", "Canadian Citizen", "Permanent Resident", "Work Permit",
                 "Open Work Permit", "Student Visa", "Requires Sponsorship"]
                else 0
            ),
        )
        if st.form_submit_button("Save Personal Info", type="primary"):
            pm.update_personal(
                full_name=full_name, email=email, phone=phone,
                location=location, linkedin_url=linkedin,
                github_url=github, work_authorization=work_auth,
            )
            st.success("Personal info saved.")

    # ── Experience ────────────────────────────────────────────────────────────
    st.markdown('<div class="section-title">Work Experience</div>',
                unsafe_allow_html=True)

    for i, job in enumerate(pm.get_experience()):
        label = f"{job['title']}  ·  {job['company']}  ({job['start_date']} – {job['end_date']})"
        with st.expander(label):
            with st.form(f"form_exp_{i}"):
                r1c1, r1c2 = st.columns(2)
                co  = r1c1.text_input("Company",    value=job["company"],    key=f"exp_co_{i}")
                ti  = r1c2.text_input("Job Title",  value=job["title"],      key=f"exp_ti_{i}")
                r2c1, r2c2, r2c3 = st.columns(3)
                sd  = r2c1.text_input("Start Date", value=job["start_date"], key=f"exp_sd_{i}",
                                       placeholder="January 2020")
                ed  = r2c2.text_input("End Date",   value=job["end_date"],   key=f"exp_ed_{i}",
                                       placeholder="December 2023 or Present")
                lo  = r2c3.text_input("Location",   value=job["location"],   key=f"exp_lo_{i}")
                bl  = st.text_area(
                    "Achievements — one bullet per line (max 7)",
                    value="\n".join(job.get("bullets", [])),
                    height=160, key=f"exp_bl_{i}",
                )
                bc1, bc2 = st.columns([4, 1])
                saved   = bc1.form_submit_button("Save Changes", type="primary")
                deleted = bc2.form_submit_button("Delete")
                if saved:
                    bullets = [b.strip().lstrip("•-").strip()
                               for b in bl.split("\n") if b.strip()]
                    pm.update_experience(i, company=co, title=ti,
                                         start_date=sd, end_date=ed,
                                         location=lo, bullets=bullets[:7])
                    st.success("Saved.")
                    st.rerun()
                if deleted:
                    pm.remove_experience(i)
                    st.rerun()

    with st.expander("+ Add Work Experience"):
        with st.form("form_add_exp"):
            r1c1, r1c2 = st.columns(2)
            co = r1c1.text_input("Company *")
            ti = r1c2.text_input("Job Title *")
            r2c1, r2c2, r2c3 = st.columns(3)
            sd = r2c1.text_input("Start Date", placeholder="January 2020")
            ed = r2c2.text_input("End Date",   placeholder="December 2023 or Present")
            lo = r2c3.text_input("Location",   placeholder="Toronto, ON (Remote)")
            bl = st.text_area(
                "Achievements — one bullet per line (max 7)",
                height=170,
                placeholder=(
                    "Increased revenue by 40% by architecting a real-time data pipeline\n"
                    "Led cross-functional team of 8 engineers to deliver project on schedule\n"
                    "Reduced API latency by 60% through caching strategy"
                ),
            )
            if st.form_submit_button("Add Experience", type="primary"):
                if co and ti:
                    bullets = [b.strip().lstrip("•-").strip()
                               for b in bl.split("\n") if b.strip()]
                    pm.add_experience(co, ti, sd, ed, lo, bullets[:7])
                    st.success("Added!")
                    st.rerun()
                else:
                    st.error("Company and Job Title are required.")

    # ── Education ─────────────────────────────────────────────────────────────
    st.markdown('<div class="section-title">Education</div>', unsafe_allow_html=True)

    for i, edu in enumerate(pm.get_education()):
        label = f"{edu['degree']}  ·  {edu['institution']}  ({edu['graduation_year']})"
        with st.expander(label):
            with st.form(f"form_edu_{i}"):
                ec1, ec2 = st.columns(2)
                deg  = ec1.text_input("Degree",      value=edu["degree"],          key=f"edu_deg_{i}")
                inst = ec2.text_input("Institution", value=edu["institution"],     key=f"edu_ins_{i}")
                ec3, ec4 = st.columns(2)
                yr   = ec3.text_input("Graduation Year", value=edu["graduation_year"], key=f"edu_yr_{i}")
                gpa  = ec4.text_input("GPA (optional)",  value=edu.get("gpa", ""),    key=f"edu_gpa_{i}")
                crse = st.text_input(
                    "Relevant Courses (comma-separated)",
                    value=", ".join(edu.get("relevant_courses", [])),
                    key=f"edu_cr_{i}",
                )
                bc1, bc2 = st.columns([4, 1])
                saved   = bc1.form_submit_button("Save Changes", type="primary")
                deleted = bc2.form_submit_button("Delete")
                if saved:
                    courses = [c.strip() for c in crse.split(",") if c.strip()]
                    pm.update_education(i, degree=deg, institution=inst,
                                        graduation_year=yr, gpa=gpa,
                                        relevant_courses=courses)
                    st.success("Saved.")
                    st.rerun()
                if deleted:
                    pm.remove_education(i)
                    st.rerun()

    with st.expander("+ Add Education"):
        with st.form("form_add_edu"):
            ec1, ec2 = st.columns(2)
            deg  = ec1.text_input("Degree *",      placeholder="Bachelor of Computer Science")
            inst = ec2.text_input("Institution *", placeholder="University of Toronto")
            ec3, ec4 = st.columns(2)
            yr   = ec3.text_input("Graduation Year", placeholder="2021")
            gpa  = ec4.text_input("GPA (optional)")
            crse = st.text_input("Relevant Courses (comma-separated)",
                                  placeholder="Algorithms, Machine Learning, Databases")
            if st.form_submit_button("Add Education", type="primary"):
                if deg and inst:
                    courses = [c.strip() for c in crse.split(",") if c.strip()]
                    pm.add_education(deg, inst, yr, gpa, courses)
                    st.success("Added!")
                    st.rerun()
                else:
                    st.error("Degree and Institution are required.")

    # ── Skills ───────────────────────────────────────────────────────────────
    st.markdown('<div class="section-title">Skills</div>', unsafe_allow_html=True)

    sk = pm.get_skills()
    with st.form("form_skills"):
        sc1, sc2, sc3 = st.columns(3)
        tech = sc1.text_area(
            "Technical Skills",
            value="\n".join(sk.get("technical", [])),
            height=160,
            placeholder="Python\nReact\nPostgreSQL\nDocker\nAWS",
        )
        tools = sc2.text_area(
            "Tools & Platforms",
            value="\n".join(sk.get("tools", [])),
            height=160,
            placeholder="Git\nJira\nFigma\nTerraform\nkubectl",
        )
        soft = sc3.text_area(
            "Soft Skills",
            value="\n".join(sk.get("soft", [])),
            height=160,
            placeholder="Leadership\nCommunication\nAgile\nMentoring",
        )
        if st.form_submit_button("Save Skills", type="primary"):
            pm.update_skills(
                technical=[s.strip() for s in tech.splitlines() if s.strip()],
                tools=[s.strip() for s in tools.splitlines() if s.strip()],
                soft=[s.strip() for s in soft.splitlines() if s.strip()],
            )
            st.success("Skills saved.")

    # ── Projects ─────────────────────────────────────────────────────────────
    st.markdown('<div class="section-title">Projects</div>', unsafe_allow_html=True)

    for i, proj in enumerate(pm.get_projects()):
        with st.expander(proj["name"]):
            with st.form(f"form_proj_{i}"):
                name = st.text_input("Project Name", value=proj["name"], key=f"pr_n_{i}")
                desc = st.text_area("Description",   value=proj["description"],
                                     height=100, key=f"pr_d_{i}")
                pc1, pc2 = st.columns(2)
                live = pc1.text_input("Live URL",   value=proj.get("live_url", ""),  key=f"pr_l_{i}")
                ghub = pc2.text_input("GitHub URL", value=proj.get("github_url", ""), key=f"pr_g_{i}")
                stack = st.text_input(
                    "Tech Stack (comma-separated)",
                    value=", ".join(proj.get("tech_stack", [])),
                    key=f"pr_ts_{i}",
                )
                bc1, bc2 = st.columns([4, 1])
                saved   = bc1.form_submit_button("Save Changes", type="primary")
                deleted = bc2.form_submit_button("Delete")
                if saved:
                    ts = [t.strip() for t in stack.split(",") if t.strip()]
                    pm.update_project(i, name=name, description=desc,
                                       tech_stack=ts, live_url=live, github_url=ghub)
                    st.success("Saved.")
                    st.rerun()
                if deleted:
                    pm.remove_project(i)
                    st.rerun()

    with st.expander("+ Add Project"):
        with st.form("form_add_proj"):
            name = st.text_input("Project Name *")
            desc = st.text_area("Description *", height=100,
                                 placeholder="What it does and what problem it solves")
            pc1, pc2 = st.columns(2)
            live = pc1.text_input("Live URL")
            ghub = pc2.text_input("GitHub URL")
            stack = st.text_input("Tech Stack (comma-separated)",
                                   placeholder="Python, FastAPI, React, PostgreSQL")
            if st.form_submit_button("Add Project", type="primary"):
                if name and desc:
                    ts = [t.strip() for t in stack.split(",") if t.strip()]
                    pm.add_project(name, desc, ts, live, ghub)
                    st.success("Added!")
                    st.rerun()
                else:
                    st.error("Name and Description are required.")

    # ── Certifications ────────────────────────────────────────────────────────
    st.markdown('<div class="section-title">Certifications</div>', unsafe_allow_html=True)

    certs = pm.get_certifications()
    if certs:
        for i, cert in enumerate(certs):
            cc1, cc2, cc3, cc4 = st.columns([3, 2, 2, 1])
            cc1.markdown(f"**{cert['name']}**")
            cc2.markdown(cert["issuer"])
            cc3.markdown(cert.get("year") or cert.get("date", ""))
            if cc4.button("Remove", key=f"del_cert_{i}"):
                pm.remove_certification(i)
                st.rerun()
    else:
        st.caption("No certifications added yet.")

    with st.expander("+ Add Certification"):
        with st.form("form_add_cert"):
            ce1, ce2 = st.columns(2)
            cname  = ce1.text_input("Certification Name *")
            issuer = ce2.text_input("Issuing Organization *")
            ce3, ce4 = st.columns(2)
            date   = ce3.text_input("Date",           placeholder="March 2024")
            cred   = ce4.text_input("Credential URL")
            if st.form_submit_button("Add Certification", type="primary"):
                if cname and issuer:
                    pm.add_certification(cname, issuer, date, cred)
                    st.success("Added!")
                    st.rerun()
                else:
                    st.error("Name and Issuer are required.")


# ══════════════════════════════════════════════════════════════════════════════
#  TAB 2 — SEARCH JOBS
# ══════════════════════════════════════════════════════════════════════════════

def _tab_search(settings: dict, pm: ProfileManager) -> None:
    if not settings.get("anthropic_api_key"):
        st.warning("Set your Anthropic API key in **Settings** to enable resume tailoring.")

    # ── Search form ───────────────────────────────────────────────────────────
    with st.form("form_search"):
        sc1, sc2 = st.columns([3, 2])
        query    = sc1.text_input("Job Title / Keywords *", placeholder="e.g. Data Analyst")
        location = sc2.text_input("Location", value="Edmonton, AB", placeholder="Edmonton, AB")
        fc1, fc2, fc3, fc4, fc5 = st.columns(5)
        use_indeed   = fc1.checkbox("Indeed (RSS)",      value=settings.get("sources_indeed",   True))
        use_jobbank  = fc2.checkbox("Job Bank Canada",   value=settings.get("sources_jobbank",  True))
        use_linkedin = fc3.checkbox("LinkedIn",          value=settings.get("sources_linkedin", False))
        use_adzuna   = fc4.checkbox("Adzuna",            value=settings.get("sources_adzuna",   False))
        use_jooble   = fc5.checkbox("Jooble",            value=settings.get("sources_jooble",   False))
        fp1, fp2 = st.columns(2)
        max_pages = fp1.slider("Pages per source", 1, 5, int(settings.get("max_search_pages", 2)))
        headless  = fp2.checkbox("Headless browser (LinkedIn)", value=True)
        submitted = st.form_submit_button("Search Jobs", type="primary",
                                          use_container_width=True)

    if submitted:
        if not query:
            st.error("Please enter a job title or keywords.")
        else:
            sources = []
            if use_indeed:   sources.append("indeed")
            if use_jobbank:  sources.append("jobbank")
            if use_linkedin: sources.append("linkedin")
            if use_adzuna:   sources.append("adzuna")
            if use_jooble:   sources.append("jooble")
            if not sources:
                st.error("Select at least one job board.")
            else:
                with st.spinner(f"Searching {', '.join(sources)} — this takes 30-90 seconds..."):
                    try:
                        from modules.job_searcher import JobSearcher
                        searcher = JobSearcher(
                            headless=headless,
                            adzuna_app_id=settings.get("adzuna_app_id", ""),
                            adzuna_app_key=settings.get("adzuna_app_key", ""),
                            jooble_api_key=settings.get("jooble_api_key", ""),
                        )
                        jobs = searcher.search(query, location,
                                               sources=sources, max_pages=max_pages)
                        st.session_state["search_results"] = jobs
                        st.session_state["search_query"]   = query
                        if jobs:
                            st.success(f"Found {len(jobs)} jobs.")
                        else:
                            st.warning("Search completed but returned 0 jobs.")
                        # Surface any non-fatal scraper warnings
                        for err_msg in searcher.errors:
                            if "fallback" in err_msg.lower() and "fetched" in err_msg.lower():
                                st.info(err_msg)
                            else:
                                st.warning(err_msg)
                    except Exception as exc:
                        full_tb = traceback.format_exc()
                        st.error(
                            f"Search error — **{type(exc).__name__}**: {exc}"
                        )
                        with st.expander("Full traceback"):
                            st.code(full_tb, language="text")

    # ── Results ───────────────────────────────────────────────────────────────
    jobs = st.session_state.get("search_results", [])
    if not jobs:
        if not submitted:
            all_jobs = _get_all_jobs()
            if all_jobs:
                st.info(f"Showing {len(all_jobs)} previously found jobs from database.")
                jobs = all_jobs
            else:
                st.info("Run a search above to find jobs.")
                return
        else:
            return

    profile = pm.get()
    api_key = settings.get("anthropic_api_key", "")

    # ── Filter bar ────────────────────────────────────────────────────────────
    fc1, fc2 = st.columns(2)
    source_filter = fc1.selectbox(
        "Source", ["All", "indeed", "linkedin"], index=0
    )
    status_filter = fc2.selectbox(
        "Status", ["All", "new", "applied", "dry_run", "low_match", "error"], index=0
    )

    eligible_jobs = [
        j for j in jobs
        if j.get("status", "new") not in ("applied", "dry_run", "low_match")
    ]
    st.markdown(
        f"**{len(jobs)} jobs** — {len(eligible_jobs)} eligible &nbsp;·&nbsp; "
        f"{len(jobs) - len(eligible_jobs)} already processed"
    )

    # ── Auto-Apply panel ─────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown(
        '<div class="aa-panel">'
        "<h4>Auto-Apply Mode</h4>"
        "<p>Automatically fetch descriptions, score against your profile with Claude, "
        "and apply to every job above the threshold — hands-free.</p>"
        "</div>",
        unsafe_allow_html=True,
    )

    aa1, aa2, aa3 = st.columns([3, 2, 3])
    min_score   = aa1.slider(
        "Minimum ATS Match Score", 50, 95, 70, step=5,
        help="Claude scores each job 0-100. Jobs below this threshold are marked "
             "low_match and skipped.",
    )
    today_apps  = _get_stats()["today"]
    remaining   = max(0, settings.get("max_apps_per_day", 10) - today_apps)
    aa2.metric("Remaining Today", remaining,
               delta=f"−{today_apps} used" if today_apps else None,
               delta_color="inverse")
    dry_flag    = settings.get("dry_run", True)
    mode_color  = "#e3b341" if dry_flag else "#f85149"
    aa3.markdown(
        f'<div style="color:{mode_color};padding-top:30px;font-weight:600;'
        f'font-size:0.9rem;">'
        + ("DRY RUN — forms filled, Submit skipped"
           if dry_flag else
           "LIVE MODE — real applications will be submitted")
        + "</div>",
        unsafe_allow_html=True,
    )

    if not api_key:
        st.warning(
            "Set your Anthropic API key in the **Settings** tab to enable Auto-Apply."
        )
    elif remaining == 0:
        st.warning(
            f"Daily limit of {settings.get('max_apps_per_day', 10)} applications "
            f"reached. Resets at midnight."
        )
    elif not eligible_jobs:
        st.info("No new jobs to process — all jobs have already been applied to or skipped.")
    else:
        to_process = min(len(eligible_jobs), remaining)
        dry_prefix = "DRY RUN  " if dry_flag else "START  "
        btn_label  = (
            f"{dry_prefix}AUTO-APPLY"
            f"  —  analyze {to_process} job{'s' if to_process != 1 else ''}, "
            f"apply to matches >= {min_score}%"
        )
        if st.button(btn_label, type="primary", use_container_width=True,
                     key="btn_auto_apply"):
            _run_auto_apply(eligible_jobs, settings, profile, min_score, remaining)
            st.rerun()

    st.markdown("---")

    for i, job in enumerate(jobs):
        if source_filter != "All" and job.get("source", "") != source_filter:
            continue
        if status_filter != "All" and job.get("status", "new") != status_filter:
            continue

        score = _local_match(job, profile)
        score_cls = "low" if score < 50 else ("mid" if score < 75 else "")
        src_badge  = _badge(job.get("source", "—"), job.get("source", ""))
        stat_badge = _badge(job.get("status", "new"))

        with st.container():
            st.markdown(f"""
            <div class="job-card">
                <div class="jc-title">{job.get('title','—')}</div>
                <div class="jc-sub">
                    {job.get('company','—')} &nbsp;·&nbsp; {job.get('location','—')}
                    &nbsp;·&nbsp; {src_badge} &nbsp; {stat_badge}
                </div>
                {"<div class='jc-salary'>" + job.get('salary') + "</div>"
                  if job.get('salary') else ""}
                <div class="jc-sub" style="margin-top:4px">
                    Posted: {job.get('date_posted','—')}
                    &nbsp;|&nbsp; Local match: {score}%
                </div>
            </div>
            """, unsafe_allow_html=True)

            btn_c1, btn_c2, btn_c3, btn_c4 = st.columns([2, 2, 2, 1])
            job_key = (job.get("url") or "").replace("https://", "").replace("/", "_")[:40]

            # View job
            if job.get("url"):
                btn_c1.markdown(
                    f"[View Posting]({job['url']})",
                    unsafe_allow_html=False,
                )

            # Fetch description
            if btn_c2.button("Fetch Description", key=f"fetch_{i}_{job_key}"):
                with st.spinner("Fetching full job description..."):
                    try:
                        from modules.job_searcher import JobSearcher
                        searcher = JobSearcher(headless=True)
                        detail = searcher.get_job_details(job["url"])
                        job["description"] = detail.get("description", "")
                        st.session_state[f"desc_{job_key}"] = job["description"]
                        st.success("Description loaded.")
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))

            # Tailor resume
            tailor_disabled = not api_key or not job.get("description")
            tailor_help = (
                "Add your API key in Settings" if not api_key
                else "Fetch description first" if not job.get("description")
                else "Generate ATS-optimized resume with Claude"
            )
            if btn_c3.button("Tailor Resume", key=f"tailor_{i}_{job_key}",
                              disabled=tailor_disabled, help=tailor_help):
                with st.spinner("Calling Claude to tailor your resume..."):
                    try:
                        from modules.resume_tailor import ResumeTailor
                        tailor = ResumeTailor(api_key=api_key)
                        result = tailor.tailor(
                            profile=profile,
                            job_description=job["description"],
                            company=job.get("company", "Company"),
                            job_title=job.get("title", "Role"),
                        )
                        st.session_state[f"tailor_{job_key}"] = result
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Tailor error: {exc}")

            # Show tailor result if available
            tailor_result = st.session_state.get(f"tailor_{job_key}")
            if tailor_result:
                score_d = tailor_result.get("match_score", {})
                overall = score_d.get("overall", 0)
                st.markdown(
                    f"**ATS Score:** {_score_html(overall)} &nbsp;"
                    f"Keywords: {score_d.get('keyword_match', 0)} &nbsp;"
                    f"Skills: {score_d.get('skills_alignment', 0)} &nbsp;"
                    f"Experience: {score_d.get('experience_relevance', 0)}",
                    unsafe_allow_html=True,
                )
                dl1, dl2 = st.columns(2)
                docx_path = tailor_result.get("docx_path")
                pdf_path  = tailor_result.get("pdf_path")
                if docx_path and Path(docx_path).exists():
                    dl1.download_button(
                        "Download DOCX", data=Path(docx_path).read_bytes(),
                        file_name=f"resume_{job.get('company','')}.docx",
                        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        key=f"dl_docx_{i}_{job_key}",
                    )
                if pdf_path and Path(pdf_path).exists():
                    dl2.download_button(
                        "Download PDF", data=Path(pdf_path).read_bytes(),
                        file_name=f"resume_{job.get('company','')}.pdf",
                        mime="application/pdf",
                        key=f"dl_pdf_{i}_{job_key}",
                    )

                # Apply button
                if btn_c4.button("Apply", key=f"apply_{i}_{job_key}",
                                  type="primary",
                                  help="Submit application via Playwright"):
                    _do_apply(job, tailor_result.get("pdf_path", ""),
                               settings, profile, overall)

            st.markdown("<hr style='border-color:#21262d;margin:2px 0'>",
                        unsafe_allow_html=True)


def _do_apply(job: dict, pdf_path: str, settings: dict, profile: dict, match_score: int) -> None:
    dry = settings.get("dry_run", True)
    label = "DRY RUN" if dry else "APPLYING"
    with st.spinner(f"{label}: {job.get('title')} at {job.get('company')}..."):
        try:
            from modules.job_applicator import JobApplicator
            applicator = JobApplicator(
                profile=profile,
                headless=True,
                dry_run=dry,
                expected_salary=settings.get("expected_salary", "Open to discussion"),
                notice_period=settings.get("notice_period", "2 weeks"),
                willing_to_relocate=settings.get("willing_to_relocate", False),
                indeed_email=settings.get("indeed_email", ""),
                indeed_password=settings.get("indeed_password", ""),
                linkedin_email=settings.get("linkedin_email", ""),
                linkedin_password=settings.get("linkedin_password", ""),
            )
            result = applicator.apply(job, pdf_path)
            if result.get("success"):
                verb = "Dry-run complete" if dry else "Applied"
                st.success(f"{verb}! Steps: {len(result['steps'])}")
                if result.get("screenshots"):
                    st.image(result["screenshots"][-1], caption="Final screenshot", width=600)
            else:
                st.error(f"Application failed: {result.get('error')}")
            # Persist match score
            with get_connection() as conn:
                row = conn.execute(
                    "SELECT id FROM applications WHERE job_id = "
                    "(SELECT id FROM jobs WHERE url=?) ORDER BY applied_at DESC LIMIT 1",
                    (job.get("url"),)
                ).fetchone()
                if row:
                    conn.execute("UPDATE applications SET match_score=? WHERE id=?",
                                 (match_score, row[0]))
        except Exception as exc:
            st.error(f"Applicator error: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
#  AUTO-APPLY PIPELINE
# ══════════════════════════════════════════════════════════════════════════════

def _mark_job_low_match(url: str) -> None:
    if not url:
        return
    with get_connection() as conn:
        conn.execute("UPDATE jobs SET status='low_match' WHERE url=?", (url,))


def _format_log(lines: list[tuple[str, str]]) -> str:
    """Render the live log as a coloured HTML block."""
    palette = {
        "applied": ("#3fb950", "&#10003;"),
        "skip":    ("#e3b341", "&#8250;&#8250;"),
        "error":   ("#f85149", "&#10007;"),
        "info":    ("#8b949e", "&rarr;"),
    }
    rows = []
    for level, msg in lines:
        color, icon = palette.get(level, ("#8b949e", "&rarr;"))
        rows.append(
            f'<div style="color:{color};font-size:0.82rem;padding:2px 0;">'
            f'{icon}&nbsp; {msg}</div>'
        )
    return '<div class="log-box">' + "".join(rows) + "</div>"


def _run_auto_apply(
    jobs: list[dict],
    settings: dict,
    profile: dict,
    min_score: int = 70,
    daily_remaining: int = 10,
) -> None:
    """
    Full autonomous pipeline:
      1. Fetch description  (JobSearcher)
      2. Tailor + score     (ResumeTailor / Claude)
      3. If score >= min_score → apply (JobApplicator)
         Else                 → mark low_match, skip
    Live progress is streamed into Streamlit empty() placeholders.
    """
    from modules.job_searcher import JobSearcher
    from modules.resume_tailor import ResumeTailor
    from modules.job_applicator import JobApplicator

    api_key  = settings.get("anthropic_api_key", "")
    dry_run  = settings.get("dry_run", True)
    cap      = min(len(jobs), max(0, daily_remaining))

    if cap == 0:
        st.warning("No jobs to process or daily limit already reached.")
        return

    # ── Live UI placeholders ─────────────────────────────────────────────────
    header_ph   = st.empty()
    progress_ph = st.progress(0)
    status_ph   = st.empty()
    log_ph      = st.empty()
    summary_ph  = st.empty()

    applied = skipped = errors = 0
    log_lines: list[tuple[str, str]] = []

    def _push(level: str, msg: str) -> None:
        log_lines.append((level, msg))
        log_ph.markdown(_format_log(log_lines[-30:]), unsafe_allow_html=True)

    # ── Initialise modules once ──────────────────────────────────────────────
    try:
        searcher   = JobSearcher(headless=True)
        tailor     = ResumeTailor(api_key=api_key)
        applicator = JobApplicator(
            profile=profile,
            headless=True,
            dry_run=dry_run,
            expected_salary=settings.get("expected_salary", "Open to discussion"),
            notice_period=settings.get("notice_period", "2 weeks"),
            willing_to_relocate=settings.get("willing_to_relocate", False),
            indeed_email=settings.get("indeed_email", ""),
            indeed_password=settings.get("indeed_password", ""),
            linkedin_email=settings.get("linkedin_email", ""),
            linkedin_password=settings.get("linkedin_password", ""),
        )
    except Exception as exc:
        st.error(f"Failed to initialise modules: {exc}")
        return

    # ── Job loop ─────────────────────────────────────────────────────────────
    for i, job in enumerate(jobs[:cap]):
        title   = job.get("title",   "Unknown Role")
        company = job.get("company", "Unknown Co.")
        idx     = f"{i + 1}/{cap}"

        header_ph.markdown(
            f"### Analyzing job {i + 1} of {cap}"
            f"&nbsp;&nbsp;·&nbsp;&nbsp;{company}"
        )
        progress_ph.progress((i + 1) / cap)

        # ── Step 1: description ──────────────────────────────────────────────
        if not job.get("description"):
            status_ph.info(
                f"**{idx}** &nbsp; Fetching description: **{title}** at **{company}**…"
            )
            try:
                detail = searcher.get_job_details(job["url"])
                job["description"] = detail.get("description", "")
            except Exception as exc:
                _push("error", f"{idx}  [{company}]  Fetch failed: {exc}")
                errors += 1
                continue

        if not job.get("description"):
            _push("skip", f"{idx}  [{company}]  {title}  — no description, skipping")
            _mark_job_low_match(job.get("url", ""))
            skipped += 1
            continue

        # ── Step 2: tailor + score ───────────────────────────────────────────
        status_ph.info(
            f"**{idx}** &nbsp; Scoring with Claude: **{title}** at **{company}**…"
        )
        try:
            tailor_result = tailor.tailor(
                profile=profile,
                job_description=job["description"],
                company=company,
                job_title=title,
            )
            score = tailor_result.get("match_score", {}).get("overall", 0)
        except Exception as exc:
            _push("error", f"{idx}  [{company}]  Claude error: {exc}")
            errors += 1
            continue

        # ── Step 3: gate on score ────────────────────────────────────────────
        if score < min_score:
            _mark_job_low_match(job.get("url", ""))
            _push("skip",
                  f"{idx}  [{company}]  {title}  —  Score: {score}%  "
                  f"(below {min_score}% threshold)")
            skipped += 1
            continue

        # ── Step 4: apply ────────────────────────────────────────────────────
        verb = "DRY RUN" if dry_run else "APPLYING"
        status_ph.success(
            f"**{idx}** &nbsp; {verb}: **{title}** at **{company}** &nbsp; Score: **{score}%**"
        )
        try:
            apply_result = applicator.apply(
                job=job,
                resume_path=tailor_result.get("pdf_path", ""),
            )
            if apply_result.get("success"):
                outcome = "DRY RUN" if dry_run else "APPLIED"
                _push("applied",
                      f"{idx}  [{company}]  {title}  —  Score: {score}%  →  {outcome}")
                applied += 1
                # Persist match score
                with get_connection() as conn:
                    row = conn.execute(
                        "SELECT id FROM applications "
                        "WHERE job_id=(SELECT id FROM jobs WHERE url=?) "
                        "ORDER BY applied_at DESC LIMIT 1",
                        (job.get("url"),),
                    ).fetchone()
                    if row:
                        conn.execute(
                            "UPDATE applications SET match_score=? WHERE id=?",
                            (score, row[0]),
                        )
            else:
                _push("error",
                      f"{idx}  [{company}]  {title}  —  Apply failed: "
                      f"{apply_result.get('error', 'unknown error')}")
                errors += 1
        except Exception as exc:
            _push("error", f"{idx}  [{company}]  {title}  —  Exception: {exc}")
            errors += 1

        # Respect daily cap mid-loop
        if applied >= daily_remaining:
            _push("info",
                  f"Daily limit of {settings.get('max_apps_per_day', 10)} reached — stopping.")
            break

    # ── Final summary ────────────────────────────────────────────────────────
    header_ph.markdown("### Auto-Apply Complete")
    progress_ph.progress(1.0)
    status_ph.empty()
    summary_ph.success(
        f"Finished! &nbsp;"
        f"Applied: **{applied}** &nbsp;|&nbsp; "
        f"Skipped (low match): **{skipped}** &nbsp;|&nbsp; "
        f"Errors: **{errors}** &nbsp;|&nbsp; "
        f"Total analyzed: **{applied + skipped + errors}**"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  TAB 3 — APPLICATIONS DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

def _tab_dashboard() -> None:
    stats = _get_stats()

    # ── Summary metrics ───────────────────────────────────────────────────────
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Applications", stats["total"])
    m2.metric("Applied Today",      stats["today"])
    m3.metric("This Week",          stats["week"])
    m4.metric("Success Rate",       f"{stats['rate']}%")
    st.markdown("---")

    # ── Filters ───────────────────────────────────────────────────────────────
    fc1, fc2 = st.columns([2, 4])
    status_filter = fc1.selectbox(
        "Filter by outcome",
        ["all", "applied", "dry_run", "pending", "rejected", "error"],
    )

    apps = _get_applications(status_filter if status_filter != "all" else "all")

    if not apps:
        st.info("No applications yet. Search for jobs and click Apply to get started.")
        return

    # ── Table ─────────────────────────────────────────────────────────────────
    rows = []
    for a in apps:
        applied_at = a.get("applied_at", "")
        if applied_at:
            try:
                applied_at = datetime.fromisoformat(applied_at).strftime("%b %d, %Y %H:%M")
            except Exception:
                pass

        outcome = a.get("outcome") or "pending"
        badge   = f'<span class="badge badge-{outcome}">{outcome.replace("_"," ").title()}</span>'
        source  = a.get("source") or "—"
        src_b   = f'<span class="badge badge-{source}">{source.title()}</span>' if source != "—" else "—"
        score   = a.get("match_score") or 0
        score_d = _score_html(score) if score else "—"

        rows.append({
            "Date":      applied_at,
            "Company":   a.get("company") or "—",
            "Role":      a.get("title")   or "—",
            "Source":    src_b,
            "Status":    badge,
            "ATS Score": score_d,
            "Resume":    (
                f"[Download]({a['resume_path']})"
                if a.get("resume_path") and Path(a["resume_path"]).exists()
                else "—"
            ),
        })

    df = pd.DataFrame(rows)
    st.markdown(
        df.to_html(escape=False, index=False,
                   classes="dataframe", border=0),
        unsafe_allow_html=True,
    )

    # ── Charts ────────────────────────────────────────────────────────────────
    if len(apps) > 1:
        st.markdown("---")
        ch1, ch2 = st.columns(2)

        # Outcome breakdown
        outcome_counts = {}
        for a in apps:
            k = (a.get("outcome") or "pending").replace("_", " ").title()
            outcome_counts[k] = outcome_counts.get(k, 0) + 1
        ch1.subheader("Outcome Breakdown")
        ch1.bar_chart(pd.DataFrame.from_dict(
            {"Outcome": list(outcome_counts.keys()),
             "Count":   list(outcome_counts.values())}).set_index("Outcome")
        )

        # Source breakdown
        src_counts = {}
        for a in apps:
            k = (a.get("source") or "unknown").title()
            src_counts[k] = src_counts.get(k, 0) + 1
        ch2.subheader("Source Breakdown")
        ch2.bar_chart(pd.DataFrame.from_dict(
            {"Source": list(src_counts.keys()),
             "Count":  list(src_counts.values())}).set_index("Source")
        )


# ══════════════════════════════════════════════════════════════════════════════
#  TAB 4 — SETTINGS
# ══════════════════════════════════════════════════════════════════════════════

def _tab_settings(settings: dict) -> dict:
    st.markdown("Changes are saved immediately when you click **Save Settings**.")

    with st.form("form_settings"):
        # ── Claude API ────────────────────────────────────────────────────────
        st.markdown('<div class="section-title">Anthropic API</div>',
                    unsafe_allow_html=True)
        api_key = st.text_input(
            "API Key",
            value=settings.get("anthropic_api_key", ""),
            type="password",
            help="Get your key at console.anthropic.com",
            placeholder="sk-ant-…",
        )
        if settings.get("anthropic_api_key"):
            st.success("API key is set.")

        # ── Application behaviour ─────────────────────────────────────────────
        st.markdown('<div class="section-title">Application Behaviour</div>',
                    unsafe_allow_html=True)
        bc1, bc2 = st.columns(2)
        dry_run = bc1.toggle(
            "Dry Run Mode",
            value=settings.get("dry_run", True),
            help="Fill forms completely but stop before clicking Submit. "
                 "Disable only when you are confident the flow works.",
        )
        max_apps = bc2.slider(
            "Max Applications per Day",
            min_value=1, max_value=50,
            value=int(settings.get("max_apps_per_day", 10)),
        )
        if dry_run:
            st.info("Dry run is ON — the agent will screenshot the completed form "
                    "but will NOT submit. Safe for testing.")
        else:
            st.warning("Dry run is OFF — the agent will submit real applications. "
                       "Make sure your resume and profile are complete.")

        # ── Compensation ─────────────────────────────────────────────────────
        st.markdown('<div class="section-title">Compensation & Availability</div>',
                    unsafe_allow_html=True)
        cc1, cc2, cc3 = st.columns(3)
        salary   = cc1.text_input("Expected Salary",
                                    value=settings.get("expected_salary", "Open to discussion"),
                                    placeholder="$95,000 or 'Open to discussion'")
        notice   = cc2.text_input("Notice Period / Start Date",
                                    value=settings.get("notice_period", "2 weeks"),
                                    placeholder="2 weeks")
        relocate = cc3.toggle("Willing to Relocate",
                               value=settings.get("willing_to_relocate", False))

        # ── Job board credentials ─────────────────────────────────────────────
        st.markdown('<div class="section-title">Job Board Credentials</div>',
                    unsafe_allow_html=True)
        st.caption("Required for Easy Apply (Indeed) and Easy Apply (LinkedIn). "
                   "Stored locally in data/settings.json — never sent anywhere else.")

        jc1, jc2 = st.columns(2)
        jc1.markdown("**Indeed Canada**")
        indeed_email    = jc1.text_input("Indeed Email",    value=settings.get("indeed_email", ""),
                                          key="s_ie")
        indeed_password = jc1.text_input("Indeed Password", value=settings.get("indeed_password", ""),
                                          type="password", key="s_ip")

        jc2.markdown("**LinkedIn**")
        li_email    = jc2.text_input("LinkedIn Email",    value=settings.get("linkedin_email", ""),
                                      key="s_le")
        li_password = jc2.text_input("LinkedIn Password", value=settings.get("linkedin_password", ""),
                                      type="password", key="s_lp")

        # ── Search defaults ───────────────────────────────────────────────────
        st.markdown('<div class="section-title">Search Sources</div>',
                    unsafe_allow_html=True)
        sd1, sd2, sd3, sd4, sd5 = st.columns(5)
        src_indeed   = sd1.checkbox("Indeed (RSS)",    value=settings.get("sources_indeed",   True))
        src_jobbank  = sd2.checkbox("Job Bank Canada", value=settings.get("sources_jobbank",  True))
        src_linkedin = sd3.checkbox("LinkedIn",        value=settings.get("sources_linkedin", False))
        src_adzuna   = sd4.checkbox("Adzuna",          value=settings.get("sources_adzuna",   False))
        src_jooble   = sd5.checkbox("Jooble",          value=settings.get("sources_jooble",   False))
        max_pages    = st.slider("Pages per source", 1, 5, int(settings.get("max_search_pages", 2)))

        st.markdown('<div class="section-title">Adzuna API (Free)</div>',
                    unsafe_allow_html=True)
        st.caption(
            "Free API for Canadian job listings — no credit card required. "
            "Get your free API key at [developer.adzuna.com](https://developer.adzuna.com/) "
            "— takes 2 minutes."
        )
        az1, az2 = st.columns(2)
        adzuna_app_id  = az1.text_input("Adzuna App ID",  value=settings.get("adzuna_app_id",  ""), key="s_az_id")
        adzuna_app_key = az2.text_input("Adzuna App Key", value=settings.get("adzuna_app_key", ""),
                                         type="password", key="s_az_key")

        st.markdown('<div class="section-title">Jooble API (Free)</div>',
                    unsafe_allow_html=True)
        st.caption(
            "Aggregates Indeed, LinkedIn, and more into one free API — no credit card required. "
            "Get your free API key at [jooble.org/api](https://jooble.org/api) "
            "— takes 2 minutes."
        )
        jooble_api_key = st.text_input(
            "Jooble API Key",
            value=settings.get("jooble_api_key", ""),
            type="password",
            key="s_jooble_key",
        )

        if st.form_submit_button("Save Settings", type="primary", use_container_width=True):
            new_settings = {
                "anthropic_api_key":  api_key,
                "dry_run":            dry_run,
                "max_apps_per_day":   max_apps,
                "expected_salary":    salary,
                "notice_period":      notice,
                "willing_to_relocate": relocate,
                "indeed_email":       indeed_email,
                "indeed_password":    indeed_password,
                "linkedin_email":     li_email,
                "linkedin_password":  li_password,
                "sources_indeed":     src_indeed,
                "sources_jobbank":    src_jobbank,
                "sources_linkedin":   src_linkedin,
                "sources_adzuna":     src_adzuna,
                "sources_jooble":     src_jooble,
                "adzuna_app_id":      adzuna_app_id,
                "adzuna_app_key":     adzuna_app_key,
                "jooble_api_key":     jooble_api_key,
                "max_search_pages":   max_pages,
            }
            _save_settings(new_settings)
            st.session_state["settings"] = new_settings
            st.success("Settings saved.")
            st.rerun()

    return st.session_state.get("settings", settings)


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    _inject_css()
    init_db()

    # ── Session state bootstrap ───────────────────────────────────────────────
    if "settings" not in st.session_state:
        st.session_state["settings"] = _load_settings()
    if "pm" not in st.session_state:
        st.session_state["pm"] = ProfileManager()

    settings: dict         = st.session_state["settings"]
    pm: ProfileManager     = st.session_state["pm"]
    stats: dict            = _get_stats()

    # ── Sidebar ───────────────────────────────────────────────────────────────
    _render_sidebar(pm, stats)

    # ── Tabs ──────────────────────────────────────────────────────────────────
    tab1, tab2, tab3, tab4 = st.tabs([
        "My Profile",
        "Search Jobs",
        "Applications Dashboard",
        "Settings",
    ])

    with tab1:
        _tab_profile(pm)

    with tab2:
        _tab_search(settings, pm)

    with tab3:
        _tab_dashboard()

    with tab4:
        _tab_settings(settings)


if __name__ == "__main__":
    main()
