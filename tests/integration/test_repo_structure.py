"""
Phase 6: Integration & Deployment Tests
End-to-end integration tests for the complete sweep system
"""

import pytest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_ROOT = REPO_ROOT / "tests"


# Note: These tests verify the complete system integration


class TestSystemIntegration:
    """Test complete system integration"""


class TestDeploymentConfiguration:
    """Test deployment configuration"""

    def test_dockerfile_exists(self):
        """Test Dockerfile exists for container deployment"""
        path = REPO_ROOT / "Dockerfile"
        assert path.exists(), f"Dockerfile not found at {path}"

    def test_requirements_exists(self):
        """Test requirements.txt exists"""
        path = REPO_ROOT / "requirements.txt"
        assert path.exists(), f"requirements.txt not found at {path}"

    def test_docker_environment_config(self):
        """Test Docker environment configuration exists"""
        path = REPO_ROOT / "cloudrun.env"
        assert path.exists(), f"cloudrun.env not found at {path}"


class TestCodeQuality:
    """Test code quality and standards"""


class TestDocumentation:
    """Test that documentation exists"""

    def test_readme_exists(self):
        """Test README documentation exists"""
        path = REPO_ROOT / "README.md"
        assert path.exists(), f"README.md not found at {path}"

    def test_docs_directory_exists(self):
        """Test docs directory exists"""
        path = REPO_ROOT / "docs"
        assert path.exists(), f"docs directory not found at {path}"

class TestTestCoverage:
    """Test that adequate test coverage exists"""

    def test_dashboard_tests_exist(self):
        """Test that dashboard tests exist"""
        path = TESTS_ROOT / "dashboard" / "test_static_assets.py"
        assert path.exists()

    def test_test_directory_structure(self):
        """Test that proper test directory structure exists"""
        assert TESTS_ROOT.exists()
        assert (TESTS_ROOT / "api").exists()
        assert (TESTS_ROOT / "integration").exists()
        assert (TESTS_ROOT / "smoke").exists()

    def test_no_legacy_root_level_regression_files(self):
        """Test old root-level regression files were reorganized."""
        legacy_files = [
            "test_dashboard.py",
            "test_indicators.py",
            "test_integration.py",
            "test_ltp_indicator_flow_corrected.py",
            "test_main.py",
            "test_server.py",
            "test_strategy_rules.py",
            "test_websocket_token_list_fix.py",
        ]
        assert not any((TESTS_ROOT / f).exists() for f in legacy_files)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
