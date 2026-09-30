"""Basic UI checks after `make bootstrap` (§12 Phase 5)."""

from conftest import UI


def test_only_leo_in_model_picker(ui):
    ui.click("#model-selector-model-button")
    ui.wait_for_timeout(800)
    items = ui.locator("[role=menu] button, [role=listbox] [role=option], [data-melt-dropdown-menu] button")
    names = {t.strip().split("\n")[0] for t in items.all_inner_texts() if t.strip()}
    ui.keyboard.press("Escape")
    assert "Leo" in names and not any(n.startswith("leo (raw)") or n == "leo" for n in names), names


def test_think_toggle_in_integrations_menu(ui):
    ui.click("#integration-menu-button")
    ui.wait_for_timeout(800)
    menu = ui.inner_text("body")
    ui.keyboard.press("Escape")
    assert "Think" in menu and "Web Search" in menu and "Code Interpreter" in menu


def test_memory_tools_attached(ui):
    assert ui.locator("button[aria-label='Available Tools']").inner_text().strip() == "1"


def test_signup_disabled(api):
    import httpx

    r = httpx.post(f"{UI}/api/v1/auths/signup", json={"email": "x@example.com", "password": "abcdefgh1", "name": "x"})
    assert r.status_code in (401, 403)
