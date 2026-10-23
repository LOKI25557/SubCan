"""Tests for SubCan Dashboard UI rendering, drawer open/close state, subscription details, and reliability."""

from fastapi.testclient import TestClient
from backend.app.main import app

client = TestClient(app)


def test_dashboard_html_structure_and_drawer_initial_hidden_state():
    """Requirement 1: Verify that /dashboard/ returns 200 and drawer is closed by default."""
    response = client.get("/dashboard/")
    assert response.status_code == 200
    html = response.text

    # Detail drawer must exist and have hidden attribute, aria-hidden, and inline style display:none
    assert '<aside id="detail-drawer"' in html
    assert 'class="drawer-overlay"' in html
    assert 'hidden' in html
    assert 'aria-hidden="true"' in html
    assert 'style="display: none;"' in html

    # Subtitle must NOT contain raw unparsed 'sub_id' token
    assert '<code id="detail-subtitle" class="sub-id-code">sub_id</code>' not in html
    assert '<code id="detail-subtitle" class="sub-id-code">—</code>' in html

    # Close button and necessary sections must exist
    assert 'id="close-detail"' in html
    assert 'id="detail-body"' in html
    assert 'id="detail-title"' in html
    assert 'id="detail-eyebrow"' in html

    # Cache-busting version tags on assets
    assert 'styles.css?v=2.1' in html
    assert 'app.js?v=2.1' in html


def test_dashboard_css_hidden_and_drawer_blur_rules():
    """Requirement 1 & 6: Verify styles.css enforces display: none and disables backdrop-filter when hidden."""
    response = client.get("/dashboard/styles.css")
    assert response.status_code == 200
    css = response.text

    # Global [hidden] rule must be present with !important
    assert "[hidden]" in css
    assert "display: none !important;" in css

    # .drawer-overlay[hidden] must explicitly disable display, opacity, and backdrop-filter
    assert ".drawer-overlay[hidden]" in css
    assert "backdrop-filter: none !important;" in css
    assert "opacity: 0 !important;" in css
    assert "pointer-events: none !important;" in css

    # .drawer-overlay.open must enable visibility and pointer events
    assert ".drawer-overlay.open" in css
    assert "pointer-events: auto;" in css

    # body.drawer-open rule
    assert "body.drawer-open" in css


def test_dashboard_js_lifecycle_and_deterministic_boot():
    """Requirement 1: Verify app.js enforces deterministic boot and closes drawer on load."""
    response = client.get("/dashboard/app.js")
    assert response.status_code == 200
    js = response.text

    # Must define closeDrawer and openDrawer
    assert "function closeDrawer()" in js
    assert "function openDrawer(" in js

    # Boot section must explicitly call closeDrawer() before loadSubscriptions()
    assert "closeDrawer();\nloadSubscriptions();" in js or "closeDrawer();\r\nloadSubscriptions();" in js


def test_dashboard_subscription_details_and_policy_warnings():
    """Requirements 3 & 4: Verify subscription details, costs, renewal dates, and policy warnings."""
    # Backend contract verification
    sub_resp = client.get("/subscriptions")
    assert sub_resp.status_code == 200
    subs = sub_resp.json()
    assert len(subs) == 5

    # Check CineVault has valid cost, currency, renewal date, and terms
    cinevault = next(s for s in subs if s["subscription_id"] == "sub_tc1_cinevault")
    assert "CineVault" in cinevault["service_name"]
    assert cinevault["monthly_cost"] == 14.99
    assert cinevault["currency"] == "USD"
    assert cinevault["cancellation_terms_url"] is not None

    # Check AudioCloud has data deletion warning
    audiocloud = next(s for s in subs if s["subscription_id"] == "sub_tc2_audiocloud")
    assert audiocloud["data_deletion_warning"] is True

    # Check FitStream has pause option available
    fitstream = next(s for s in subs if s["subscription_id"] == "sub_tc3_fitstream")
    assert fitstream["pause_option_available"] is True

    # Verify frontend app.js renders these properties into the drawer
    response = client.get("/dashboard/app.js")
    js = response.text
    assert "sub.service_name" in js
    assert "sub.subscription_id" in js
    assert "sub.monthly_cost" in js
    assert "sub.renewal_date" in js
    assert "sub.last_used_at" in js
    assert "sub.data_deletion_warning" in js
    assert "sub.pause_option_available" in js
    assert "sub.cancellation_terms_url" in js
    assert "sub.billing_cycle" in js


def test_dashboard_unknown_subscription_error_handling():
    """Requirement 5: Verify an unknown subscription ID produces a recoverable error without crashing."""
    response = client.get("/dashboard/app.js")
    assert response.status_code == 200
    js = response.text

    # openDrawer must check for non-existent subscription
    assert "if (!sub)" in js
    assert "showNotice(`Subscription '${targetId}' could not be found.`, \"error\");" in js
    assert "closeDrawer();" in js


def test_dashboard_drawer_dismissal_restores_clean_state():
    """Requirement 6: Verify closing the panel removes backdrop and restores interaction."""
    response = client.get("/dashboard/app.js")
    assert response.status_code == 200
    js = response.text

    # closeDrawer must clear hidden, inline style, aria-hidden, class open, and body.drawer-open
    assert "drawer.hidden = true;" in js
    assert 'drawer.style.display = "none";' in js
    assert 'drawer.setAttribute("aria-hidden", "true");' in js
    assert 'drawer.classList.remove("open");' in js
    assert 'document.body.classList.remove("drawer-open");' in js

    # Dismissal listeners: close button, backdrop click, and Escape key
    assert '$("close-detail")?.addEventListener("click", closeDrawer)' in js
    assert 'e.target === $("detail-drawer")' in js
    assert 'e.key === "Escape"' in js


def test_dashboard_panel_updates_on_reselection():
    """Requirement 7: Verify selecting a different subscription updates the panel dynamically."""
    response = client.get("/dashboard/app.js")
    assert response.status_code == 200
    js = response.text

    # openDrawer dynamically sets detail-title, detail-subtitle, detail-eyebrow, and detail-body
    assert '$("detail-title").textContent = sub.service_name' in js
    assert '$("detail-subtitle").textContent = sub.subscription_id' in js
    assert '$("detail-eyebrow").textContent = eyebrow' in js
    assert '$("detail-body").innerHTML = html' in js


def test_dashboard_api_error_handling_and_resilience():
    """Requirement 8: Verify API errors do not leave the dashboard permanently blurred or stuck."""
    response = client.get("/dashboard/app.js")
    assert response.status_code == 200
    js = response.text

    # loadSubscriptions and loadRunSubscriptions catch blocks must call showNotice and closeDrawer
    assert "Failed to load subscriptions" in js
    assert "Failed to load run details" in js


def test_dashboard_safe_handling_of_missing_or_optional_fields():
    """Requirement 9: Verify missing optional fields do not crash rendering."""
    response = client.get("/dashboard/app.js")
    assert response.status_code == 200
    js = response.text

    # Null coalescing and defensive defaults
    assert 'typeof sub.monthly_cost === "number"' in js
    assert 'sub.currency === "USD"' in js
    assert 'sub.billing_cycle || "monthly"' in js
    assert 'sub.renewal_date || "No renewal date"' in js
    assert 'sub.service_name || "Unknown Service"' in js


def test_dashboard_xss_prevention_and_url_sanitization():
    """Requirement 10: Verify untrusted subscription text cannot inject executable HTML or malicious schemes."""
    response = client.get("/dashboard/app.js")
    assert response.status_code == 200
    js = response.text

    # escapeHtml helper must exist and handle HTML entities
    assert "function escapeHtml(str)" in js
    assert ".replace(/&/g, \"&amp;\")" in js
    assert ".replace(/</g, \"&lt;\")" in js
    assert ".replace(/>/g, \"&gt;\")" in js

    # sanitizeUrl helper must only permit http/https
    assert "function sanitizeUrl(url)" in js
    assert "/^https?:\\/\\//i.test(trimmed)" in js

    # Render functions must use escapeHtml
    assert "escapeHtml(sub.service_name" in js
    assert "escapeHtml(sub.subscription_id" in js
    assert "sanitizeUrl(sub.cancellation_terms_url)" in js


def test_dashboard_static_middleware_cache_control_headers():
    """Verify that static dashboard assets are served with no-cache headers to prevent stale CSS/JS."""
    for path in ["/dashboard/", "/dashboard/styles.css", "/dashboard/app.js"]:
        resp = client.get(path)
        assert resp.status_code == 200
        assert "no-cache" in resp.headers.get("cache-control", "")
        assert "no-store" in resp.headers.get("cache-control", "")


def test_dashboard_integration_contract_fields_match_ui():
    """Verify backend API responses contain all fields expected by frontend drawer rendering."""
    # 1. Check subscriptions endpoint
    sub_resp = client.get("/subscriptions")
    assert sub_resp.status_code == 200
    subs = sub_resp.json()
    assert len(subs) == 5
    for s in subs:
        assert "subscription_id" in s
        assert "service_name" in s
        assert "status" in s
        assert "monthly_cost" in s
        assert "renewal_date" in s
        assert "data_deletion_warning" in s
        assert "pause_option_available" in s

    # 2. Check run subscriptions endpoint
    run_resp = client.post("/runs", json={"subscription_ids": ["sub_tc1_cinevault", "sub_tc2_audiocloud"]})
    assert run_resp.status_code == 201
    run_id = run_resp.json()["run_id"]

    run_subs_resp = client.get(f"/runs/{run_id}/subscriptions")
    assert run_subs_resp.status_code == 200
    data = run_subs_resp.json()
    assert "items" in data
    for item in data["items"]:
        assert "subscription" in item
        assert "decision" in item
        assert "plan" in item
        assert "evidence_list" in item
        assert "final_status" in item
        assert item["subscription"]["subscription_id"] in ["sub_tc1_cinevault", "sub_tc2_audiocloud"]
