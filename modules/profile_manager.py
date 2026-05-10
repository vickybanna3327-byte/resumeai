"""Manages the user's master profile stored at data/profile.json."""
import json
from pathlib import Path


PROFILE_PATH = Path(__file__).parent.parent / "data" / "profile.json"


def _empty_profile() -> dict:
    return {
        "personal": {
            "full_name": "",
            "email": "",
            "phone": "",
            "linkedin_url": "",
            "github_url": "",
            "location": "",
            "work_authorization": "",
        },
        "experience": [],
        "education": [],
        "skills": {
            "technical": [],
            "soft": [],
            "tools": [],
        },
        "projects": [],
        "certifications": [],
    }


class ProfileManager:
    """CRUD interface for the user's master resume profile."""

    def __init__(self):
        self._profile = self._load()

    # ------------------------------------------------------------------ I/O --

    def _load(self) -> dict:
        if PROFILE_PATH.exists():
            with open(PROFILE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Back-fill any keys added after original creation
            empty = _empty_profile()
            for section, value in empty.items():
                if section not in data:
                    data[section] = value
                elif isinstance(value, dict):
                    for key, default in value.items():
                        data[section].setdefault(key, default)
            return data
        return _empty_profile()

    def save(self) -> None:
        PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(PROFILE_PATH, "w", encoding="utf-8") as f:
            json.dump(self._profile, f, indent=2, ensure_ascii=False)

    def get(self) -> dict:
        return self._profile

    def reset(self) -> None:
        self._profile = _empty_profile()
        self.save()

    # ------------------------------------------------------------ Personal ---

    def update_personal(self, **fields) -> None:
        """Update any subset of personal fields.

        Valid keys: full_name, email, phone, linkedin_url, github_url,
                    location, work_authorization
        """
        valid = set(self._profile["personal"].keys())
        for key, value in fields.items():
            if key in valid:
                self._profile["personal"][key] = value
        self.save()

    def get_personal(self) -> dict:
        return self._profile["personal"]

    # ---------------------------------------------------------- Experience ---

    def add_experience(
        self,
        company: str,
        title: str,
        start_date: str,
        end_date: str,
        location: str,
        bullets: list[str],
    ) -> None:
        """Add one job. Bullets capped at 7 per ATS best practice."""
        self._profile["experience"].append(
            {
                "company": company,
                "title": title,
                "start_date": start_date,
                "end_date": end_date,
                "location": location,
                "bullets": bullets[:7],
            }
        )
        self.save()

    def update_experience(self, index: int, **fields) -> None:
        if 0 <= index < len(self._profile["experience"]):
            self._profile["experience"][index].update(fields)
            self.save()

    def remove_experience(self, index: int) -> None:
        if 0 <= index < len(self._profile["experience"]):
            self._profile["experience"].pop(index)
            self.save()

    def get_experience(self) -> list[dict]:
        return self._profile["experience"]

    # ----------------------------------------------------------- Education ---

    def add_education(
        self,
        degree: str,
        institution: str,
        graduation_year: str,
        gpa: str = "",
        relevant_courses: list[str] | None = None,
    ) -> None:
        self._profile["education"].append(
            {
                "degree": degree,
                "institution": institution,
                "graduation_year": graduation_year,
                "gpa": gpa,
                "relevant_courses": relevant_courses or [],
            }
        )
        self.save()

    def update_education(self, index: int, **fields) -> None:
        if 0 <= index < len(self._profile["education"]):
            self._profile["education"][index].update(fields)
            self.save()

    def remove_education(self, index: int) -> None:
        if 0 <= index < len(self._profile["education"]):
            self._profile["education"].pop(index)
            self.save()

    def get_education(self) -> list[dict]:
        return self._profile["education"]

    # -------------------------------------------------------------- Skills ---

    def update_skills(
        self,
        technical: list[str] | None = None,
        soft: list[str] | None = None,
        tools: list[str] | None = None,
    ) -> None:
        if technical is not None:
            self._profile["skills"]["technical"] = technical
        if soft is not None:
            self._profile["skills"]["soft"] = soft
        if tools is not None:
            self._profile["skills"]["tools"] = tools
        self.save()

    def get_skills(self) -> dict:
        return self._profile["skills"]

    # ------------------------------------------------------------ Projects ---

    def add_project(
        self,
        name: str,
        description: str,
        tech_stack: list[str],
        live_url: str = "",
        github_url: str = "",
    ) -> None:
        self._profile["projects"].append(
            {
                "name": name,
                "description": description,
                "tech_stack": tech_stack,
                "live_url": live_url,
                "github_url": github_url,
            }
        )
        self.save()

    def update_project(self, index: int, **fields) -> None:
        if 0 <= index < len(self._profile["projects"]):
            self._profile["projects"][index].update(fields)
            self.save()

    def remove_project(self, index: int) -> None:
        if 0 <= index < len(self._profile["projects"]):
            self._profile["projects"].pop(index)
            self.save()

    def get_projects(self) -> list[dict]:
        return self._profile["projects"]

    # ------------------------------------------------------- Certifications --

    def add_certification(
        self,
        name: str,
        issuer: str,
        date: str,
        credential_url: str = "",
    ) -> None:
        self._profile["certifications"].append(
            {
                "name": name,
                "issuer": issuer,
                "year": date,
                "credential_url": credential_url,
            }
        )
        self.save()

    def remove_certification(self, index: int) -> None:
        if 0 <= index < len(self._profile["certifications"]):
            self._profile["certifications"].pop(index)
            self.save()

    def get_certifications(self) -> list[dict]:
        return self._profile["certifications"]

    # --------------------------------------------------------- Validation ---

    def completeness_check(self) -> tuple[bool, list[str]]:
        """Return (is_complete, list_of_missing_items).

        Only hard-blocks things the tailor and applicator strictly require.
        """
        missing: list[str] = []
        p = self._profile["personal"]

        for field in ("full_name", "email", "phone", "location", "work_authorization"):
            if not p.get(field, "").strip():
                missing.append(f"Personal → {field.replace('_', ' ')}")

        if not self._profile["experience"]:
            missing.append("Experience → at least one job entry required")

        if not self._profile["education"]:
            missing.append("Education → at least one entry required")

        if not self._profile["skills"]["technical"]:
            missing.append("Skills → technical skills list is empty")

        return len(missing) == 0, missing

    def completeness_percent(self) -> int:
        """Rough profile completeness as a 0-100 integer (for UI progress bars)."""
        score = 0
        p = self._profile["personal"]
        personal_fields = ("full_name", "email", "phone", "linkedin_url",
                           "github_url", "location", "work_authorization")
        filled = sum(1 for f in personal_fields if p.get(f, "").strip())
        score += int(filled / len(personal_fields) * 25)   # 25 pts

        score += min(len(self._profile["experience"]) * 10, 25)   # 25 pts
        score += min(len(self._profile["education"]) * 15, 15)    # 15 pts

        sk = self._profile["skills"]
        if sk["technical"]:
            score += 10
        if sk["tools"]:
            score += 5
        if sk["soft"]:
            score += 5                                             # 20 pts

        score += min(len(self._profile["projects"]) * 5, 10)      # 10 pts
        score += min(len(self._profile["certifications"]) * 5, 5)  # 5 pts

        return min(score, 100)
