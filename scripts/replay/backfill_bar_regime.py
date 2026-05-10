#!/usr/bin/env python3
"""Backfill bar_regime rows from historical indicator_bars."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import psycopg

from app.strategies.adaptive.market_context import MarketContextBuilder
from app.strategies.adaptive.regime import CLASSIFIER_VERSION, RegimeClassifier
from scripts.replay.schema import normalize_table_name

IST = timezone(timedelta(hours=5, minutes=30))


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill dynamic_policy bar regimes")
    parser.add_argument("--dsn", default=os.environ.get("REPLAY_PG_DSN", ""))
    parser.add_argument("--table", default=os.environ.get("REPLAY_TABLE", "indicator_bars"))
    parser.add_argument("--label", help="Optional indicator_bars.label filter")
    parser.add_argument("--timeframe-seconds", type=int)
    parser.add_argument("--start-date", type=parse_date)
    parser.add_argument("--end-date", type=parse_date)
    parser.add_argument("--classifier-version", default=CLASSIFIER_VERSION)
    parser.add_argument("--batch-size", type=int, default=5000)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.dsn:
        print("ERROR: --dsn or REPLAY_PG_DSN is required", file=sys.stderr)
        raise SystemExit(1)

    table, _ = normalize_table_name(args.table)
    where = []
    params: list[Any] = []
    if args.label:
        where.append("label = %s")
        params.append(args.label)
    if args.timeframe_seconds:
        where.append("timeframe_seconds = %s")
        params.append(int(args.timeframe_seconds))
    if args.start_date:
        where.append("ts_start >= %s")
        params.append(datetime.combine(args.start_date, time.min, tzinfo=IST))
    if args.end_date:
        where.append("ts_start < %s")
        params.append(datetime.combine(args.end_date + timedelta(days=1), time.min, tzinfo=IST))

    query = f"""
        SELECT id, ts_start, ts_end, label, timeframe_seconds,
               o, h, l, c, atr, ema_20, adx, plus_di, minus_di, di_spread
        FROM {table}
        {("WHERE " + " AND ".join(where)) if where else ""}
        ORDER BY label, timeframe_seconds, ts_start
    """

    builders: Dict[tuple[str, int], MarketContextBuilder] = {}
    classifiers: Dict[tuple[str, int], RegimeClassifier] = {}
    pending: list[tuple[str, str, str, int]] = []
    total = 0
    batch_size = max(1, int(args.batch_size))

    with psycopg.connect(args.dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            while True:
                rows = cur.fetchmany(batch_size)
                if not rows:
                    break
                for row in rows:
                    bar_id = int(row[0])
                    key = (str(row[3]), int(row[4]))
                    candle = SimpleNamespace(
                        start_ts=_ensure_tz(row[1]),
                        end_ts=_ensure_tz(row[2]) if row[2] is not None else None,
                        o=float(row[5] or 0.0),
                        h=float(row[6] or 0.0),
                        low=float(row[7] or 0.0),
                        l=float(row[7] or 0.0),
                        c=float(row[8] or 0.0),
                    )
                    indicators = {
                        "atr": row[9],
                        "ema_20": row[10],
                        "adx": row[11],
                        "plus_di": row[12],
                        "minus_di": row[13],
                        "di_spread": row[14],
                    }
                    context = builders.setdefault(key, MarketContextBuilder()).build(
                        candle=candle,
                        indicators=indicators,
                    )
                    regime = classifiers.setdefault(key, RegimeClassifier()).update(context)
                    signals = {
                        "atr14": context.atr14,
                        "atr_norm": context.atr_norm,
                        "adx14": context.adx14,
                        "di_spread": context.di_spread,
                        "plus_di": context.plus_di,
                        "minus_di": context.minus_di,
                        "ema_slope": context.ema_slope,
                        "ema_value": context.ema_value,
                        "last_price": context.last_price,
                        "gap_ratio": context.gap_ratio,
                        "chop_index": context.chop_index,
                    }
                    pending.append(
                        (
                            regime.value,
                            str(args.classifier_version),
                            json.dumps(signals, sort_keys=True, default=str),
                            bar_id,
                        )
                    )
                with conn.cursor() as writer:
                    writer.executemany(
                        """
                        INSERT INTO bar_regime (
                            regime, classifier_version, signals, bar_id
                        )
                        VALUES (%s, %s, %s::jsonb, %s)
                        ON CONFLICT (bar_id, classifier_version) DO UPDATE SET
                            regime = EXCLUDED.regime,
                            signals = EXCLUDED.signals,
                            classified_at = CURRENT_TIMESTAMP
                        """,
                        pending,
                    )
                total += len(pending)
                pending.clear()

    print(f"Backfilled {total} bar_regime rows for classifier_version={args.classifier_version}")


def _ensure_tz(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=IST)
        return value
    return datetime.fromisoformat(str(value)).replace(tzinfo=IST)


if __name__ == "__main__":
    main()
