from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def set_frozen(obj, name, value):
    """Settings is a frozen dataclass; tests poke it directly."""
    object.__setattr__(obj, name, value)


class FakeReddit:
    """Stands in for the stealth browser: maps URLs to saved pages and records every fetch."""

    def __init__(self, pages: dict[str, tuple[int, str]] | None = None, default: tuple[int, str] | None = None):
        self.pages = dict(pages or {})
        self.default = default
        self.calls: list[str] = []

    def __call__(self, url: str, wait_selector: str | None = None) -> tuple[int, str]:
        self.calls.append(url)
        if url in self.pages:
            return self.pages[url]
        for key, value in self.pages.items():
            if key.endswith("*") and url.startswith(key[:-1]):
                return value
        if self.default is not None:
            return self.default
        return 404, "<html><title>page gone</title></html>"


@pytest.fixture(autouse=True)
def fast_settings():
    from reddit_reviews.config import settings

    before = settings.retry_delay_s
    set_frozen(settings, "retry_delay_s", 0)
    yield
    set_frozen(settings, "retry_delay_s", before)
