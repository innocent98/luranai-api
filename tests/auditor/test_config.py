from app.core.config import settings


def test_auditor_settings_defaults():
    assert settings.AUDITOR_PUBLIC_MAX_PAGES == 10
    assert settings.AUDITOR_DASHBOARD_DEFAULT_MAX_PAGES == 500
    assert settings.AUDITOR_CLAIM_STALENESS_DAYS == 7
    assert settings.AUDITOR_UNCLAIMED_PURGE_DAYS == 30
    assert settings.AUDITOR_PER_PAGE_TIMEOUT_S == 10
    assert settings.AUDITOR_CRAWL_RPS == 1
    assert settings.AUDITOR_MODEL_PROFILE == "gpt-4.1-mini"
    assert settings.AUDITOR_MODEL_CLASSIFY == "gpt-4o-mini"
    assert settings.AUDITOR_MODEL_GAPS == "gpt-4.1-mini"


def test_auditor_dependencies_importable():
    import bs4  # noqa: F401
    import trafilatura  # noqa: F401
    from playwright.async_api import async_playwright  # noqa: F401
