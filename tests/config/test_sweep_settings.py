"""Tests for profit sweep settings configuration."""

import pytest

from app.config.settings import Settings


class TestProfitSweepSettings:
    """Test profit sweep configuration and validation."""

    def test_simple_strategy_default_values(self, monkeypatch):
        """Test that SIMPLE strategy loads with correct defaults."""
        monkeypatch.setenv("PROFIT_SWEEP_STRATEGY", "SIMPLE")
        monkeypatch.setenv("PROFIT_SIMPLE_ENABLED", "true")
        monkeypatch.setenv("PROFIT_DAILY_TARGET", "1000")
        monkeypatch.setenv("PROFIT_MULTIPLE_ENABLED", "false")
        
        settings = Settings()
        assert settings.profit_sweep_strategy == "SIMPLE"
        assert settings.profit_simple_enabled is True
        assert settings.profit_daily_target == 1000
        assert settings.profit_sweep_enabled is True

    def test_multiple_strategy_default_values(self, monkeypatch):
        """Test that MULTIPLE strategy loads with correct defaults."""
        monkeypatch.setenv("PROFIT_SWEEP_STRATEGY", "MULTIPLE")
        monkeypatch.setenv("PROFIT_SIMPLE_ENABLED", "false")
        monkeypatch.setenv("PROFIT_MULTIPLE_ENABLED", "true")
        monkeypatch.setenv("PROFIT_MULTIPLE_TARGET", "500")
        monkeypatch.setenv("PROFIT_SWEEP_COOLDOWN_MINUTES", "30")
        monkeypatch.setenv("PROFIT_MAX_SWEEPS_PER_DAY", "5")
        
        settings = Settings()
        assert settings.profit_sweep_strategy == "MULTIPLE"
        assert settings.profit_multiple_enabled is True
        assert settings.profit_multiple_target == 500
        assert settings.profit_sweep_cooldown_minutes == 30
        assert settings.profit_max_sweeps_per_day == 5

    def test_custom_values_override_defaults(self, monkeypatch):
        """Test that custom values properly override defaults."""
        monkeypatch.setenv("PROFIT_SWEEP_STRATEGY", "MULTIPLE")
        monkeypatch.setenv("PROFIT_MULTIPLE_TARGET", "750")
        monkeypatch.setenv("PROFIT_SWEEP_COOLDOWN_MINUTES", "15")
        monkeypatch.setenv("PROFIT_MAX_SWEEPS_PER_DAY", "10")
        
        settings = Settings()
        assert settings.profit_multiple_target == 750
        assert settings.profit_sweep_cooldown_minutes == 15
        assert settings.profit_max_sweeps_per_day == 10

    def test_invalid_strategy_raises_error(self, monkeypatch):
        """Test that invalid strategy value raises ValueError."""
        monkeypatch.setenv("PROFIT_SWEEP_STRATEGY", "INVALID_STRATEGY")
        monkeypatch.setenv("PROFIT_DAILY_TARGET", "1000")
        
        with pytest.raises(ValueError, match="profit_sweep_strategy must be"):
            Settings()

    def test_simple_strategy_requires_daily_target(self, monkeypatch):
        """Test that SIMPLE strategy requires profit_daily_target > 0."""
        # Missing profit_daily_target (should fail in validation)
        monkeypatch.setenv("PROFIT_SWEEP_STRATEGY", "SIMPLE")
        monkeypatch.delenv("PROFIT_DAILY_TARGET", raising=False)
        
        with pytest.raises(ValueError, match="profit_daily_target > 0"):
            Settings()
        
        # Zero profit_daily_target
        monkeypatch.setenv("PROFIT_DAILY_TARGET", "0")
        with pytest.raises(ValueError, match="profit_daily_target > 0"):
            Settings()
        
        # Negative profit_daily_target
        monkeypatch.setenv("PROFIT_DAILY_TARGET", "-100")
        with pytest.raises(ValueError, match="profit_daily_target > 0"):
            Settings()

    def test_multiple_strategy_requires_multiple_target(self, monkeypatch):
        """Test that MULTIPLE strategy requires profit_multiple_target > 0."""
        # Missing profit_multiple_target
        monkeypatch.setenv("PROFIT_SWEEP_STRATEGY", "MULTIPLE")
        
        with pytest.raises(ValueError, match="profit_multiple_target > 0"):
            Settings()
        
        # Zero profit_multiple_target
        monkeypatch.setenv("PROFIT_MULTIPLE_TARGET", "0")
        with pytest.raises(ValueError, match="profit_multiple_target > 0"):
            Settings()

    def test_multiple_strategy_requires_valid_cooldown(self, monkeypatch):
        """Test that MULTIPLE strategy requires cooldown >= 5 minutes."""
        monkeypatch.setenv("PROFIT_SWEEP_STRATEGY", "MULTIPLE")
        monkeypatch.setenv("PROFIT_MULTIPLE_TARGET", "500")
        monkeypatch.setenv("PROFIT_SWEEP_COOLDOWN_MINUTES", "2")
        
        with pytest.raises(ValueError, match="cooldown_minutes must be >= 5"):
            Settings()

    def test_multiple_strategy_requires_positive_max_sweeps(self, monkeypatch):
        """Test that MULTIPLE strategy requires max_sweeps_per_day > 0."""
        monkeypatch.setenv("PROFIT_SWEEP_STRATEGY", "MULTIPLE")
        monkeypatch.setenv("PROFIT_MULTIPLE_TARGET", "500")
        monkeypatch.setenv("PROFIT_MAX_SWEEPS_PER_DAY", "0")
        
        with pytest.raises(ValueError, match="profit_max_sweeps_per_day must be > 0"):
            Settings()

    def test_profit_sweep_can_be_disabled(self, monkeypatch):
        """Test that profit sweep feature can be completely disabled."""
        monkeypatch.setenv("PROFIT_SWEEP_ENABLED", "false")
        monkeypatch.setenv("PROFIT_SWEEP_STRATEGY", "SIMPLE")
        monkeypatch.setenv("PROFIT_DAILY_TARGET", "1000")
        
        settings = Settings()
        assert settings.profit_sweep_enabled is False
        # Validation should still pass even if disabled

    def test_both_modes_can_be_disabled(self, monkeypatch):
        """Test that both sweep modes can be disabled simultaneously."""
        monkeypatch.setenv("PROFIT_SWEEP_ENABLED", "true")
        monkeypatch.setenv("PROFIT_SIMPLE_ENABLED", "false")
        monkeypatch.setenv("PROFIT_MULTIPLE_ENABLED", "false")
        monkeypatch.setenv("PROFIT_SWEEP_STRATEGY", "SIMPLE")
        monkeypatch.setenv("PROFIT_DAILY_TARGET", "1000")
        
        settings = Settings()
        assert settings.profit_simple_enabled is False
        assert settings.profit_multiple_enabled is False
        # Validation should still pass - disabled modes don't validate

    def test_realistic_simple_configuration(self, monkeypatch):
        """Test a realistic SIMPLE mode configuration."""
        monkeypatch.setenv("PROFIT_SWEEP_ENABLED", "true")
        monkeypatch.setenv("PROFIT_SWEEP_STRATEGY", "SIMPLE")
        monkeypatch.setenv("PROFIT_SIMPLE_ENABLED", "true")
        monkeypatch.setenv("PROFIT_MULTIPLE_ENABLED", "false")
        monkeypatch.setenv("PROFIT_DAILY_TARGET", "1500")  # Custom daily target
        
        settings = Settings()
        assert settings.profit_sweep_enabled is True
        assert settings.profit_sweep_strategy == "SIMPLE"
        assert settings.profit_daily_target == 1500
        assert settings.profit_simple_enabled is True

    def test_realistic_multiple_configuration(self, monkeypatch):
        """Test a realistic MULTIPLE mode configuration."""
        monkeypatch.setenv("PROFIT_SWEEP_ENABLED", "true")
        monkeypatch.setenv("PROFIT_SWEEP_STRATEGY", "MULTIPLE")
        monkeypatch.setenv("PROFIT_SIMPLE_ENABLED", "false")
        monkeypatch.setenv("PROFIT_MULTIPLE_ENABLED", "true")
        monkeypatch.setenv("PROFIT_MULTIPLE_TARGET", "600")  # Custom per-sweep target
        monkeypatch.setenv("PROFIT_SWEEP_COOLDOWN_MINUTES", "20")  # Custom cooldown
        monkeypatch.setenv("PROFIT_MAX_SWEEPS_PER_DAY", "8")  # Custom max sweeps
        
        settings = Settings()
        assert settings.profit_sweep_enabled is True
        assert settings.profit_sweep_strategy == "MULTIPLE"
        assert settings.profit_multiple_target == 600
        assert settings.profit_sweep_cooldown_minutes == 20
        assert settings.profit_max_sweeps_per_day == 8


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
