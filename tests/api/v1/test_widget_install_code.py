"""The install snippet the dashboard hands to tenants must start the widget.

The production loader (cdn.luranai.com/widget/v1/widget.js) starts only when
Luran.init({...}) is called; it never read `window.luranSettings`, so the old
snippet left tenants with no chat button at all.
"""

from types import SimpleNamespace

from app.api.deps import get_current_tenant_admin
from app.main import app
from tests.api.v1 import test_chat_image_send as img


def _install_code(client, db):
    _, tenant_id = img._session(db)
    app.dependency_overrides[get_current_tenant_admin] = lambda: SimpleNamespace(
        tenant_id=tenant_id
    )
    try:
        response = client.get("/api/v1/widget/install-code")
    finally:
        app.dependency_overrides.pop(get_current_tenant_admin, None)
    return response, tenant_id


def test_install_snippet_calls_luran_init_after_loading_the_loader(client, db):
    response, tenant_id = _install_code(client, db)
    assert response.status_code == 200, response.text
    snippet = response.json()["html_snippet"]

    loader = '<script src="https://cdn.luranai.com/widget/v1/widget.js"></script>'
    assert loader in snippet
    assert "Luran.init({" in snippet
    assert snippet.index(loader) < snippet.index("Luran.init({")
    assert f"tenantId: '{tenant_id}'" in snippet
    assert "apiKey: 'YOUR_PUBLIC_API_KEY_HERE'" in snippet


def test_install_snippet_does_not_load_the_loader_async(client, db):
    # Luran.init runs straight after the loader tag, so the loader must not be
    # async/defer or `Luran` would not exist yet.
    response, _ = _install_code(client, db)
    snippet = response.json()["html_snippet"]
    assert "async" not in snippet
    assert "defer" not in snippet
