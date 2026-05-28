from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import pytest
import requests


@dataclass
class LiveUserConfig:
    email: str
    password: str


@dataclass
class LiveE2EConfig:
    base_url: str
    pro: LiveUserConfig
    lite: LiveUserConfig
    quota: LiveUserConfig
    customer_email: str | None
    customer_password: str | None
    platform_email: str | None
    platform_password: str | None
    timeout_seconds: int


class LiveApiClient:
    def __init__(self, base_url: str, timeout_seconds: int = 30):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()

    def login(self, email: str, password: str) -> dict[str, Any]:
        response = self.session.post(
            f"{self.base_url}/auth/login",
            json={"email": email, "password": password},
            timeout=self.timeout_seconds,
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        self.session.headers.update({"Authorization": f"Bearer {payload['access_token']}"})
        return payload

    def platform_login(self, email: str, password: str) -> dict[str, Any]:
        response = self.session.post(
            f"{self.base_url}/platform/auth/login",
            json={"email": email, "password": password},
            timeout=self.timeout_seconds,
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        self.session.headers.update({"Authorization": f"Bearer {payload['access_token']}"})
        return payload

    def customer_login(self, email: str, password: str) -> dict[str, Any]:
        response = self.session.post(
            f"{self.base_url}/customer/auth/login",
            json={"email": email, "password": password},
            timeout=self.timeout_seconds,
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        self.session.headers.update({"Authorization": f"Bearer {payload['access_token']}"})
        return payload

    def get(self, path: str, **kwargs) -> requests.Response:
        return self.session.get(f"{self.base_url}{path}", timeout=self.timeout_seconds, **kwargs)

    def post(self, path: str, **kwargs) -> requests.Response:
        timeout = kwargs.pop("timeout", self.timeout_seconds)
        return self.session.post(f"{self.base_url}{path}", timeout=timeout, **kwargs)

    def put(self, path: str, **kwargs) -> requests.Response:
        return self.session.put(f"{self.base_url}{path}", timeout=self.timeout_seconds, **kwargs)

    def patch(self, path: str, **kwargs) -> requests.Response:
        return self.session.patch(f"{self.base_url}{path}", timeout=self.timeout_seconds, **kwargs)

    def delete(self, path: str, **kwargs) -> requests.Response:
        return self.session.delete(f"{self.base_url}{path}", timeout=self.timeout_seconds, **kwargs)


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    if value is None:
        return None
    return value.strip()


@pytest.fixture(scope="session")
def e2e_config() -> LiveE2EConfig:
    base_url = _env("LURAN_E2E_BASE_URL")
    if not base_url:
        pytest.skip(
            "LURAN_E2E_BASE_URL is not set; skipping live E2E tests.", allow_module_level=True
        )

    return LiveE2EConfig(
        base_url=base_url.rstrip("/"),
        pro=LiveUserConfig(
            email=_env("LURAN_E2E_PRO_EMAIL", "staging-e2e-pro@luranai.com") or "",
            password=_env("LURAN_E2E_PRO_PASSWORD", "StagingE2E123$") or "",
        ),
        lite=LiveUserConfig(
            email=_env("LURAN_E2E_LITE_EMAIL", "staging-e2e-lite@luranai.com") or "",
            password=_env("LURAN_E2E_LITE_PASSWORD", "StagingE2E123$") or "",
        ),
        quota=LiveUserConfig(
            email=_env("LURAN_E2E_QUOTA_EMAIL", "staging-e2e-quota@luranai.com") or "",
            password=_env("LURAN_E2E_QUOTA_PASSWORD", "StagingE2E123$") or "",
        ),
        customer_email=_env("LURAN_E2E_CUSTOMER_EMAIL"),
        customer_password=_env("LURAN_E2E_CUSTOMER_PASSWORD"),
        platform_email=_env("LURAN_E2E_PLATFORM_EMAIL"),
        platform_password=_env("LURAN_E2E_PLATFORM_PASSWORD"),
        timeout_seconds=int(_env("LURAN_E2E_TIMEOUT_SECONDS", "45") or "45"),
    )


@pytest.fixture
def pro_client(e2e_config: LiveE2EConfig) -> LiveApiClient:
    client = LiveApiClient(e2e_config.base_url, e2e_config.timeout_seconds)
    client.login(e2e_config.pro.email, e2e_config.pro.password)
    return client


@pytest.fixture
def lite_client(e2e_config: LiveE2EConfig) -> LiveApiClient:
    client = LiveApiClient(e2e_config.base_url, e2e_config.timeout_seconds)
    client.login(e2e_config.lite.email, e2e_config.lite.password)
    return client


@pytest.fixture
def quota_client(e2e_config: LiveE2EConfig) -> LiveApiClient:
    client = LiveApiClient(e2e_config.base_url, e2e_config.timeout_seconds)
    client.login(e2e_config.quota.email, e2e_config.quota.password)
    return client


@pytest.fixture
def platform_client(e2e_config: LiveE2EConfig) -> LiveApiClient:
    if not e2e_config.platform_email or not e2e_config.platform_password:
        pytest.skip("Platform admin credentials were not provided for live E2E.")
    client = LiveApiClient(e2e_config.base_url, e2e_config.timeout_seconds)
    client.platform_login(e2e_config.platform_email, e2e_config.platform_password)
    return client


@pytest.fixture
def customer_client(e2e_config: LiveE2EConfig) -> LiveApiClient:
    if not e2e_config.customer_email or not e2e_config.customer_password:
        pytest.skip("Customer credentials were not provided for live E2E.")
    client = LiveApiClient(e2e_config.base_url, e2e_config.timeout_seconds)
    client.customer_login(e2e_config.customer_email, e2e_config.customer_password)
    return client


@pytest.fixture
def anonymous_client(e2e_config: LiveE2EConfig) -> LiveApiClient:
    """Unauthenticated client — for public endpoints like visitor browse."""
    return LiveApiClient(e2e_config.base_url, e2e_config.timeout_seconds)
