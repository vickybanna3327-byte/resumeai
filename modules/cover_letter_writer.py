"""Generates tailored cover letters using Claude AI."""
import anthropic


class CoverLetterWriter:
    def __init__(self, api_key: str):
        self.client = anthropic.Anthropic(api_key=api_key)

    def write(self, resume: dict, job_description: str, company: str) -> str:
        raise NotImplementedError
