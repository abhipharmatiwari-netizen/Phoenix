"""Tests for PnL per-instrument-class stale thresholds (H4)."""

import os
from unittest.mock import patch

from app.pnl.pnl_engine import PnLEngine


class TestPerInstrumentClassThresholds:
    def test_options_default_threshold(self):
        assert PnLEngine.get_freshness_threshold_for_class("OPTIONS") == 120.0

    def test_futures_default_threshold(self):
        assert PnLEngine.get_freshness_threshold_for_class("FUTURES") == 60.0

    def test_equity_default_threshold(self):
        assert PnLEngine.get_freshness_threshold_for_class("EQUITY") == 300.0

    def test_unknown_class_falls_back(self):
        assert PnLEngine.get_freshness_threshold_for_class("CRYPTO") == 300.0

    def test_case_insensitive(self):
        assert PnLEngine.get_freshness_threshold_for_class("options") == 120.0
        assert PnLEngine.get_freshness_threshold_for_class("FUT") == 60.0

    def test_ce_pe_maps_to_options(self):
        assert PnLEngine.get_freshness_threshold_for_class("CE") == 120.0
        assert PnLEngine.get_freshness_threshold_for_class("PE") == 120.0

    def test_custom_threshold_via_env(self):
        with patch.dict(os.environ, {"QUOTE_FRESHNESS_THRESHOLD_OPTIONS_SECONDS": "30"}):
            assert PnLEngine.get_freshness_threshold_for_class("OPTIONS") == 30.0

    def test_custom_futures_threshold_via_env(self):
        with patch.dict(os.environ, {"QUOTE_FRESHNESS_THRESHOLD_FUTURES_SECONDS": "15"}):
            assert PnLEngine.get_freshness_threshold_for_class("FUTURES") == 15.0
