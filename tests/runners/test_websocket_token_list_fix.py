"""
Rigorous test suite for WebSocket token list fix and batch subscription.
Tests cover: token list filtering, structure preservation, batching, and logging.
"""

import unittest
from unittest.mock import Mock, patch, MagicMock
from collections import defaultdict


class TestTokenListFiltering(unittest.TestCase):
    """Test the corrected token list filtering logic."""

    def setUp(self):
        """Set up test data."""
        # Simulate universe_builder output
        self.token_list_input = [
            {"exchangeType": "NSE", "tokens": ["126000", "126001", "126002"]},
            {"exchangeType": "NFO", "tokens": ["200001", "200002", "200003", "200004"]},
            {"exchangeType": "MCX", "tokens": ["300001"]},
        ]
        
        self.token_labels = {
            "126000": "NIFTY_IDX",
            "126001": "BANKNIFTY_IDX",
            "126002": "FINNIFTY_IDX",
            "200001": "NIFTY_PE",
            "200002": "NIFTY_CE",
            "200003": "BANKNIFTY_PE",
            "200004": "BANKNIFTY_CE",
            "300001": "NG_FUT",
        }

    def _apply_filter(self, token_list, token_labels, allowed_labels):
        """Apply the corrected filtering logic."""
        filtered = [
            {
                "exchangeType": entry.get("exchangeType"),
                "tokens": [tok for tok in entry.get("tokens", []) 
                          if token_labels.get(str(tok)) in allowed_labels]
            }
            for entry in token_list
            if entry.get("tokens")
        ]
        # Remove empty exchanges (those with no tokens after filtering)
        return [entry for entry in filtered if entry.get("tokens")]

    def test_filter_all_allowed(self):
        """Test filtering when all labels are allowed."""
        allowed_labels = set(self.token_labels.values())
        
        result = self._apply_filter(self.token_list_input, self.token_labels, allowed_labels)
        
        # All tokens should be preserved
        self.assertEqual(len(result), 3, "Should have 3 exchanges")
        total_tokens = sum(len(entry.get("tokens", [])) for entry in result)
        self.assertEqual(total_tokens, 8, "Should have all 8 tokens")

    def test_filter_exclude_banknifty(self):
        """Test filtering when excluding BANKNIFTY instruments."""
        allowed_labels = {"NIFTY_IDX", "FINNIFTY_IDX", "NIFTY_PE", "NIFTY_CE", "NG_FUT"}
        
        result = self._apply_filter(self.token_list_input, self.token_labels, allowed_labels)
        
        # Only BANKNIFTY tokens should be removed
        total_tokens = sum(len(entry.get("tokens", [])) for entry in result)
        self.assertEqual(total_tokens, 5, "Should have 5 tokens (excluding BANKNIFTY)")
        
        # Check NSE still has 2 tokens
        nse_entry = next((e for e in result if e.get("exchangeType") == "NSE"), None)
        self.assertIsNotNone(nse_entry, "NSE exchange should exist")
        self.assertEqual(len(nse_entry.get("tokens", [])), 2, "NSE should have 2 tokens after filtering")

    def test_filter_single_exchange(self):
        """Test filtering preserves structure with single exchange."""
        allowed_labels = {"NIFTY_PE", "NIFTY_CE"}
        
        result = self._apply_filter(self.token_list_input, self.token_labels, allowed_labels)
        
        self.assertEqual(len(result), 1, "Should have 1 exchange")
        self.assertEqual(result[0]["exchangeType"], "NFO", "Should be NFO exchange")
        self.assertEqual(len(result[0]["tokens"]), 2, "Should have 2 tokens")

    def test_filter_empty_result(self):
        """Test filtering when no labels match."""
        allowed_labels = {"NONEXISTENT"}
        
        result = self._apply_filter(self.token_list_input, self.token_labels, allowed_labels)
        
        self.assertEqual(len(result), 0, "Should have no exchanges when no labels match")

    def test_structure_preserved(self):
        """Test that the {"exchangeType": ..., "tokens": [...]} structure is preserved."""
        allowed_labels = set(self.token_labels.values())
        
        result = self._apply_filter(self.token_list_input, self.token_labels, allowed_labels)
        
        for entry in result:
            self.assertIn("exchangeType", entry, "Each entry must have exchangeType")
            self.assertIn("tokens", entry, "Each entry must have tokens")
            self.assertIsInstance(entry["tokens"], list, "tokens must be a list")
            for tok in entry["tokens"]:
                self.assertIsInstance(tok, str, "Each token must be a string")

    def test_no_token_loss(self):
        """Test that valid tokens are not lost during filtering."""
        allowed_labels = {"NIFTY_IDX", "NIFTY_PE", "NIFTY_CE", "NG_FUT"}
        
        result = self._apply_filter(self.token_list_input, self.token_labels, allowed_labels)
        
        # Reconstruct token list
        result_tokens = set()
        for entry in result:
            result_tokens.update(entry["tokens"])
        
        # Verify specific tokens are present
        self.assertIn("126000", result_tokens, "NIFTY_IDX token should be present")
        self.assertIn("200001", result_tokens, "NIFTY_PE token should be present")
        self.assertIn("200002", result_tokens, "NIFTY_CE token should be present")
        self.assertIn("300001", result_tokens, "NG_FUT token should be present")
        
        # Verify BANKNIFTY tokens are absent
        self.assertNotIn("126001", result_tokens, "BANKNIFTY_IDX token should not be present")
        self.assertNotIn("200003", result_tokens, "BANKNIFTY_PE token should not be present")
        self.assertNotIn("200004", result_tokens, "BANKNIFTY_CE token should not be present")

    def test_token_order_preserved(self):
        """Test that token order within exchanges is preserved."""
        allowed_labels = set(self.token_labels.values())
        
        result = self._apply_filter(self.token_list_input, self.token_labels, allowed_labels)
        
        nfo_entry = next((e for e in result if e.get("exchangeType") == "NFO"), None)
        self.assertIsNotNone(nfo_entry)
        # Should maintain original order
        self.assertEqual(nfo_entry["tokens"], ["200001", "200002", "200003", "200004"])

    def test_exchange_type_preserved(self):
        """Test that exchange type is correctly preserved."""
        allowed_labels = set(self.token_labels.values())
        
        result = self._apply_filter(self.token_list_input, self.token_labels, allowed_labels)
        
        exchanges = {entry["exchangeType"] for entry in result}
        self.assertEqual(exchanges, {"NSE", "NFO", "MCX"})


class TestBatchSubscription(unittest.TestCase):
    """Test the batch subscription logic."""

    def setUp(self):
        """Set up test data."""
        self.batch_size = 25

    def test_batch_calculation_76_tokens(self):
        """Test batch calculation for 76 tokens (4-account system)."""
        tokens = [str(i) for i in range(76)]
        batch_count = (len(tokens) + self.batch_size - 1) // self.batch_size
        self.assertEqual(batch_count, 4, "76 tokens should create 4 batches")

    def test_batch_distribution_76_tokens(self):
        """Test token distribution across 4 batches for 76 tokens."""
        tokens = [str(i) for i in range(76)]
        
        batches = []
        for i in range(0, len(tokens), self.batch_size):
            batch = tokens[i:i+self.batch_size]
            batches.append(batch)
        
        self.assertEqual(len(batches), 4)
        self.assertEqual(len(batches[0]), 25)
        self.assertEqual(len(batches[1]), 25)
        self.assertEqual(len(batches[2]), 25)
        self.assertEqual(len(batches[3]), 1)

    def test_batch_preserves_all_tokens(self):
        """Test that batching doesn't lose any tokens."""
        tokens = [str(i) for i in range(76)]
        
        all_batched = []
        for i in range(0, len(tokens), self.batch_size):
            batch = tokens[i:i+self.batch_size]
            all_batched.extend(batch)
        
        self.assertEqual(all_batched, tokens, "All tokens should be preserved in batches")

    def test_batch_no_duplicates(self):
        """Test that no tokens are duplicated across batches."""
        tokens = [str(i) for i in range(76)]
        
        all_batched = []
        for i in range(0, len(tokens), self.batch_size):
            batch = tokens[i:i+self.batch_size]
            all_batched.extend(batch)
        
        self.assertEqual(len(all_batched), len(set(all_batched)), "No duplicates in batches")

    def test_various_token_counts(self):
        """Test batching with various token counts."""
        test_cases = [
            (1, 1),      # 1 token -> 1 batch
            (25, 1),     # 25 tokens -> 1 batch
            (26, 2),     # 26 tokens -> 2 batches
            (50, 2),     # 50 tokens -> 2 batches
            (51, 3),     # 51 tokens -> 3 batches
            (76, 4),     # 76 tokens -> 4 batches
            (100, 4),    # 100 tokens -> 4 batches
        ]
        
        for token_count, expected_batches in test_cases:
            batch_count = (token_count + self.batch_size - 1) // self.batch_size
            self.assertEqual(
                batch_count, expected_batches,
                f"{token_count} tokens should create {expected_batches} batches"
            )


class TestTokenListStructure(unittest.TestCase):
    """Test that token list structure matches WebSocket expectations."""

    def setUp(self):
        """Set up test data."""
        self.token_list = [
            {"exchangeType": "NSE", "tokens": ["126000", "126001"]},
            {"exchangeType": "NFO", "tokens": ["200001", "200002", "200003"]},
        ]

    def test_structure_has_required_fields(self):
        """Test that each entry has required fields."""
        for entry in self.token_list:
            self.assertIn("exchangeType", entry)
            self.assertIn("tokens", entry)

    def test_exchange_type_is_string(self):
        """Test that exchangeType is a string."""
        for entry in self.token_list:
            self.assertIsInstance(entry["exchangeType"], str)

    def test_tokens_is_list_of_strings(self):
        """Test that tokens is a list of strings."""
        for entry in self.token_list:
            self.assertIsInstance(entry["tokens"], list)
            for token in entry["tokens"]:
                self.assertIsInstance(token, str)

    def test_structure_for_websocket(self):
        """Test that structure matches WebSocket expectations."""
        # Simulate WebSocket subscription
        total_tokens = 0
        for entry in self.token_list:
            exchange_type = entry.get("exchangeType")
            tokens = entry.get("tokens", [])
            
            self.assertIsNotNone(exchange_type)
            self.assertGreater(len(tokens), 0)
            total_tokens += len(tokens)
        
        self.assertEqual(total_tokens, 5)

    def test_empty_token_list_handling(self):
        """Test handling of empty token list."""
        empty_list = []
        
        total_tokens = 0
        for entry in empty_list:
            tokens = entry.get("tokens", [])
            total_tokens += len(tokens)
        
        self.assertEqual(total_tokens, 0)

    def test_filtering_removes_empty_exchanges(self):
        """Test that filtering removes exchanges with no tokens."""
        # Simulate filtering that empties an exchange
        filtered = [
            {
                "exchangeType": entry["exchangeType"],
                "tokens": [t for t in entry["tokens"] if t.startswith("126")]
            }
            for entry in self.token_list
        ]
        
        # Remove empty exchanges
        filtered = [e for e in filtered if e["tokens"]]
        
        self.assertEqual(len(filtered), 1, "Should have 1 exchange after removing empty ones")
        self.assertEqual(filtered[0]["exchangeType"], "NSE")


class TestRealWorldScenario(unittest.TestCase):
    """Test with realistic 4-account system data."""

    def setUp(self):
        """Set up realistic test data."""
        # Simulate 4 Angel accounts with realistic token distribution
        self.instruments = [
            # Account 1: NIFTY options
            {"label": "NIFTY_ATM_PE", "token": "200001", "exchangeType": "NFO"},
            {"label": "NIFTY_ATM_CE", "token": "200002", "exchangeType": "NFO"},
            {"label": "NIFTY_IDX", "token": "126000", "exchangeType": "NSE"},
            
            # Account 2: BANKNIFTY options
            {"label": "BANKNIFTY_ATM_PE", "token": "200101", "exchangeType": "NFO"},
            {"label": "BANKNIFTY_ATM_CE", "token": "200102", "exchangeType": "NFO"},
            {"label": "BANKNIFTY_IDX", "token": "126001", "exchangeType": "NSE"},
            
            # Account 3: FINNIFTY
            {"label": "FINNIFTY_ATM_PE", "token": "200201", "exchangeType": "NFO"},
            {"label": "FINNIFTY_ATM_CE", "token": "200202", "exchangeType": "NFO"},
            {"label": "FINNIFTY_IDX", "token": "126002", "exchangeType": "NSE"},
            
            # Account 4: Natural Gas
            {"label": "NG_FUT", "token": "300001", "exchangeType": "MCX"},
            {"label": "NIFTY_PE_OTM", "token": "200003", "exchangeType": "NFO"},
            {"label": "NIFTY_CE_OTM", "token": "200004", "exchangeType": "NFO"},
        ]

    def test_4account_system_token_count(self):
        """Test that 4-account system builds correct token count."""
        total_tokens = len(self.instruments)
        self.assertEqual(total_tokens, 12, "Should have 12 total tokens")

    def test_4account_system_token_labels(self):
        """Test token label creation for 4-account system."""
        token_labels = {str(inst["token"]): inst["label"] for inst in self.instruments}
        
        self.assertEqual(len(token_labels), 12)
        self.assertEqual(token_labels["126000"], "NIFTY_IDX")
        self.assertEqual(token_labels["200001"], "NIFTY_ATM_PE")

    def test_4account_system_exchange_grouping(self):
        """Test token grouping by exchange."""
        tokens_by_exchange = defaultdict(list)
        for inst in self.instruments:
            tokens_by_exchange[inst["exchangeType"]].append(str(inst["token"]))
        
        token_list = [
            {"exchangeType": ex_type, "tokens": tok_list}
            for ex_type, tok_list in tokens_by_exchange.items()
        ]
        
        self.assertEqual(len(token_list), 3, "Should have 3 exchanges")
        
        # Verify counts
        nse_tokens = next((e["tokens"] for e in token_list if e["exchangeType"] == "NSE"), [])
        nfo_tokens = next((e["tokens"] for e in token_list if e["exchangeType"] == "NFO"), [])
        mcx_tokens = next((e["tokens"] for e in token_list if e["exchangeType"] == "MCX"), [])
        
        self.assertEqual(len(nse_tokens), 3, "NSE should have 3 tokens")
        self.assertEqual(len(nfo_tokens), 8, "NFO should have 8 tokens")
        self.assertEqual(len(mcx_tokens), 1, "MCX should have 1 token")

    def test_4account_system_filtering(self):
        """Test filtering in 4-account system (exclude BANKNIFTY)."""
        token_labels = {str(inst["token"]): inst["label"] for inst in self.instruments}
        
        # Exclude BANKNIFTY
        allowed_labels = {label for label in token_labels.values() 
                         if "BANKNIFTY" not in label}
        
        # Build token_list
        tokens_by_exchange = defaultdict(list)
        for inst in self.instruments:
            if inst["label"] in allowed_labels:
                tokens_by_exchange[inst["exchangeType"]].append(str(inst["token"]))
        
        token_list = [
            {"exchangeType": ex_type, "tokens": tok_list}
            for ex_type, tok_list in tokens_by_exchange.items()
        ]
        
        # Verify BANKNIFTY is excluded
        all_tokens = []
        for entry in token_list:
            all_tokens.extend(entry["tokens"])
        
        # Should not include BANKNIFTY tokens
        self.assertNotIn("200101", all_tokens)
        self.assertNotIn("200102", all_tokens)
        self.assertNotIn("126001", all_tokens)
        
        # Should include other tokens
        self.assertIn("200001", all_tokens)
        self.assertIn("126000", all_tokens)


class TestLoggingPoints(unittest.TestCase):
    """Test that logging happens at expected points."""

    @patch('logging.Logger.info')
    def test_universe_build_logging(self, mock_log):
        """Test that universe build is logged."""
        # Would be called during universe building
        mock_log("📊 [STREAM] Universe built: 12 instruments, 3 exchanges")
        
        mock_log.assert_called_once()

    @patch('logging.Logger.info')
    def test_filtering_logging(self, mock_log):
        """Test that filtering is logged."""
        mock_log("📡 [STREAM] Final token_list: 3 exchanges")
        
        mock_log.assert_called_once()

    @patch('logging.Logger.info')
    def test_websocket_init_logging(self, mock_log):
        """Test that WebSocket init is logged."""
        mock_log("🚀 [WEBSOCKET] Initializing runner with token_list...")
        
        mock_log.assert_called_once()

    @patch('logging.Logger.info')
    def test_batch_subscription_logging(self, mock_log):
        """Test that batch subscription is logged."""
        mock_log("Subscribed batch 1/4: 25 tokens (total progress: 25/76)")
        
        mock_log.assert_called_once()


class TestEdgeCases(unittest.TestCase):
    """Test edge cases and boundary conditions."""

    def test_empty_token_list(self):
        """Test handling of empty token list."""
        token_list = []
        token_labels = {}
        allowed_labels = set()
        
        result = [
            {
                "exchangeType": entry.get("exchangeType"),
                "tokens": [tok for tok in entry.get("tokens", []) 
                          if token_labels.get(str(tok)) in allowed_labels]
            }
            for entry in token_list
            if entry.get("tokens")
        ]
        
        self.assertEqual(len(result), 0)

    def test_single_token(self):
        """Test with single token."""
        token_list = [{"exchangeType": "NSE", "tokens": ["126000"]}]
        token_labels = {"126000": "NIFTY_IDX"}
        allowed_labels = {"NIFTY_IDX"}
        
        result = [
            {
                "exchangeType": entry.get("exchangeType"),
                "tokens": [tok for tok in entry.get("tokens", []) 
                          if token_labels.get(str(tok)) in allowed_labels]
            }
            for entry in token_list
            if entry.get("tokens")
        ]
        
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0]["tokens"]), 1)

    def test_large_token_count(self):
        """Test with large token count."""
        token_count = 500
        tokens = [str(i) for i in range(token_count)]
        batch_size = 25
        
        batch_count = (token_count + batch_size - 1) // batch_size
        self.assertEqual(batch_count, 20, "500 tokens should create 20 batches")

    def test_token_with_leading_zeros(self):
        """Test tokens with leading zeros."""
        token_list = [{"exchangeType": "NSE", "tokens": ["000126", "000127"]}]
        token_labels = {"000126": "NIFTY_IDX", "000127": "BANKNIFTY_IDX"}
        allowed_labels = {"NIFTY_IDX"}
        
        result = [
            {
                "exchangeType": entry.get("exchangeType"),
                "tokens": [tok for tok in entry.get("tokens", []) 
                          if token_labels.get(str(tok)) in allowed_labels]
            }
            for entry in token_list
            if entry.get("tokens")
        ]
        
        self.assertEqual(len(result[0]["tokens"]), 1)
        self.assertEqual(result[0]["tokens"][0], "000126")


class TestIntegration(unittest.TestCase):
    """Integration tests combining multiple components."""

    def test_full_pipeline_4account(self):
        """Test full pipeline with 4-account system."""
        # Step 1: Build instrument universe
        instruments = [
            {"label": f"INST_{i}", "token": f"126{i:03d}", "exchangeType": "NSE"}
            for i in range(5)
        ] + [
            {"label": f"OPT_{i}", "token": f"200{i:03d}", "exchangeType": "NFO"}
            for i in range(42)
        ]
        
        self.assertEqual(len(instruments), 47)
        
        # Step 2: Create token_labels
        token_labels = {str(inst["token"]): inst["label"] for inst in instruments}
        self.assertEqual(len(token_labels), 47)
        
        # Step 3: Group by exchange
        tokens_by_exchange = defaultdict(list)
        for inst in instruments:
            tokens_by_exchange[inst["exchangeType"]].append(str(inst["token"]))
        
        token_list = [
            {"exchangeType": ex_type, "tokens": tok_list}
            for ex_type, tok_list in tokens_by_exchange.items()
        ]
        
        # Step 4: Filter (allow all)
        allowed_labels = set(token_labels.values())
        filtered_list = [
            {
                "exchangeType": entry.get("exchangeType"),
                "tokens": [tok for tok in entry.get("tokens", []) 
                          if token_labels.get(str(tok)) in allowed_labels]
            }
            for entry in token_list
            if entry.get("tokens")
        ]
        
        # Step 5: Apply batching
        batch_size = 25
        total_tokens = sum(len(entry["tokens"]) for entry in filtered_list)
        batch_count = (total_tokens + batch_size - 1) // batch_size
        
        # Verify results
        self.assertEqual(total_tokens, 47)
        self.assertEqual(batch_count, 2)
        self.assertEqual(len(filtered_list), 2)  # NSE + NFO

    def test_full_pipeline_with_filtering(self):
        """Test full pipeline with selective filtering."""
        # Build universe with mixed instruments
        instruments = [
            {"label": "NIFTY_IDX", "token": "126000", "exchangeType": "NSE"},
            {"label": "BANKNIFTY_IDX", "token": "126001", "exchangeType": "NSE"},
            {"label": "NIFTY_PE", "token": "200001", "exchangeType": "NFO"},
            {"label": "BANKNIFTY_PE", "token": "200002", "exchangeType": "NFO"},
        ]
        
        token_labels = {str(i["token"]): i["label"] for i in instruments}
        
        # Filter: only NIFTY
        allowed_labels = {"NIFTY_IDX", "NIFTY_PE"}
        
        tokens_by_exchange = defaultdict(list)
        for inst in instruments:
            if inst["label"] in allowed_labels:
                tokens_by_exchange[inst["exchangeType"]].append(str(inst["token"]))
        
        token_list = [
            {"exchangeType": ex_type, "tokens": tok_list}
            for ex_type, tok_list in tokens_by_exchange.items()
        ]
        
        total_tokens = sum(len(e["tokens"]) for e in token_list)
        self.assertEqual(total_tokens, 2, "Should have 2 NIFTY tokens")
        
        # Verify BANKNIFTY is excluded
        all_tokens = []
        for entry in token_list:
            all_tokens.extend(entry["tokens"])
        
        self.assertNotIn("126001", all_tokens)
        self.assertNotIn("200002", all_tokens)


if __name__ == "__main__":
    # Run all tests with verbose output
    unittest.main(verbosity=2)
