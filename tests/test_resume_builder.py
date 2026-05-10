import pytest
from unittest.mock import MagicMock, patch
from modules.resume_builder import ResumeBuilder


@pytest.fixture
def builder():
    with patch("modules.resume_builder.anthropic.Anthropic"):
        return ResumeBuilder(api_key="test-key")


def test_builder_instantiation(builder):
    assert builder is not None
