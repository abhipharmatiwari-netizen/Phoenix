"""
Phase 4: Dashboard & UI Tests
Tests for sweep manager integration with dashboard UI
"""

import pytest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_STATIC_DIR = REPO_ROOT / "app" / "static"


# Note: This test file focuses on static file and configuration testing.


class TestDashboardEndpoints:
    """Test dashboard page serving"""

    def test_dashboard_html_exists(self):
        """Test that dashboard HTML file exists"""
        html_path = APP_STATIC_DIR / "dashboard.html"
        assert html_path.exists()


class TestDashboardStaticFiles:
    """Test static file serving"""

    def test_sweep_manager_js_exists(self):
        """Test that sweep manager JS file exists"""
        js_path = APP_STATIC_DIR / "sweep_manager.js"
        assert js_path.exists()

    def test_sweep_manager_js_content(self):
        """Test that sweep manager JS has required functions"""
        js_path = APP_STATIC_DIR / "sweep_manager.js"
        content = js_path.read_text()
        assert "SweepManager" in content
        assert "executeSweep" in content
        assert "refresh" in content
        assert "loadStatus" in content
        assert "loadHistory" in content


class TestDashboardUI:
    """Test UI rendering and interactivity"""

    def test_dashboard_html_has_content(self):
        """Test dashboard HTML has proper structure"""
        html_path = APP_STATIC_DIR / "dashboard.html"
        html = html_path.read_text()

        # Check for essential dashboard elements
        assert "<html" in html
        assert "<body" in html
        assert "style" in html or "<link" in html  # CSS present


class TestDashboardSecurity:
    """Test dashboard security features"""

    def test_sweep_manager_has_error_handling(self):
        """Test that sweep manager JS has error handling"""
        js_path = APP_STATIC_DIR / "sweep_manager.js"
        content = js_path.read_text()
        assert "showError" in content
        assert "catch" in content


class TestDashboardResponsiveness:
    """Test dashboard responsiveness"""

    def test_dashboard_is_mobile_responsive(self):
        """Test that dashboard is mobile-responsive"""
        html_path = APP_STATIC_DIR / "dashboard.html"
        html = html_path.read_text()
        assert 'viewport' in html or 'content="width=device-width' in html


class TestDashboardPerformance:
    """Test dashboard performance considerations"""

    def test_sweep_manager_is_efficient(self):
        """Test that sweep manager is designed efficiently"""
        js_path = APP_STATIC_DIR / "sweep_manager.js"
        content = js_path.read_text()
        
        # Check for good practices
        assert "async" in content  # Uses async/await
        assert "fetch" in content  # Uses fetch API
        # Reasonable file size (not bloated)
        assert len(content) < 50000, "Sweep manager JS is too large"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
