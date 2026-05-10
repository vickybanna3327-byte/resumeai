"""Builds tailored ATS resumes using Claude AI."""
import anthropic
from docx import Document


class ResumeBuilder:
    def __init__(self, api_key: str):
        self.client = anthropic.Anthropic(api_key=api_key)

    def tailor(self, base_resume: dict, job_description: str) -> str:
        raise NotImplementedError

    def export_docx(self, content: str, output_path: str) -> None:
        raise NotImplementedError
