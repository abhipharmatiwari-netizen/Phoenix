"""
Data plane for the multi-tenant hub.

Contains:
- StateStore: in-memory per-account state (balances, positions, order results).
- BigQuery persister utilities for orders/trades/PnL snapshots.
"""
