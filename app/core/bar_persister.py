"""Bar/indicator persistence backends used by stream runners."""

import csv
import hashlib
import logging
import os
import re
import sqlite3
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("bar_persister")

try:
    from google.cloud import bigquery  # type: ignore
except ImportError:  # optional BigQuery support
    bigquery = None

try:
    from app.data.postgres import connect_with_retry
except Exception:  # optional Postgres support in some local setups
    connect_with_retry = None


class BarPersister:
    """Persist each closed bar + indicators to configured backends."""

    _CSV_COLUMNS = [
        "ts_start",
        "ts_end",
        "label",
        "timeframe_seconds",
        "o",
        "h",
        "l",
        "c",
        "atr",
        "rsi",
        "macd",
        "macd_signal",
        "macd_hist",
        "ema_20",
        "ema_30",
        "ema_50",
        "adx",
        "plus_di",
        "minus_di",
        "di_spread",
    ]
    _POSTGRES_NAME_PART = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    _POSTGRES_IDENTIFIER_MAX_LEN = 63

    def __init__(
        self,
        csv_path: Optional[str] = None,
        sqlite_path: Optional[str] = None,
        bq_project: Optional[str] = None,
        bq_dataset: Optional[str] = None,
        bq_table: Optional[str] = None,
        postgres_dsn: Optional[str] = None,
        postgres_table: str = "indicator_bars",
        postgres_retention_months: Optional[int] = None,
        postgres_retention_prune_interval_seconds: Optional[float] = None,
    ) -> None:
        self.csv_path = csv_path
        self.sqlite_path = sqlite_path
        self.bq_project = bq_project
        self.bq_dataset = bq_dataset
        self.bq_table = bq_table
        self.postgres_dsn = postgres_dsn
        self.postgres_table = postgres_table
        self.postgres_retention_months = self._coerce_nonnegative_int(
            postgres_retention_months,
            env_name="INDICATOR_POSTGRES_RETENTION_MONTHS",
            default=0,
        )
        self.postgres_retention_prune_interval_seconds = (
            self._coerce_nonnegative_float(
                postgres_retention_prune_interval_seconds,
                env_name="INDICATOR_POSTGRES_RETENTION_PRUNE_INTERVAL_SECONDS",
                default=900.0,
            )
        )

        self._csv_header_written = False
        self._csv_seen_keys: set[str] = set()
        self._csv_seen_loaded = False
        self._csv_seen_lock = threading.Lock()
        self._sqlite_conn: Optional[sqlite3.Connection] = None
        self._bq_client = None
        self._bq_table_ref = None
        self._pg_conn = None
        self._pg_table_sql: Optional[str] = None
        self._pg_last_retention_prune_mono = 0.0

        if self.sqlite_path:
            self._init_sqlite()
        if (
            self.bq_project
            and self.bq_dataset
            and self.bq_table
            and bigquery is not None
        ):
            self._init_bigquery()
        if self.postgres_dsn:
            self._init_postgres()

    # ---------- SQLite ----------

    def _init_sqlite(self) -> None:
        self._sqlite_conn = sqlite3.connect(self.sqlite_path, check_same_thread=False)
        cur = self._sqlite_conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS indicator_bars (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_start TEXT,
                ts_end TEXT,
                label TEXT,
                timeframe_seconds INTEGER,
                o REAL,
                h REAL,
                l REAL,
                c REAL,
                atr REAL,
                rsi REAL,
                macd REAL,
                macd_signal REAL,
                macd_hist REAL,
                ema_20 REAL,
                ema_30 REAL,
                ema_50 REAL,
                exclusive_nifty_ce_buy_ema20_30s REAL,
                adx REAL,
                plus_di REAL,
                minus_di REAL,
                di_spread REAL
            )
            """
        )
        try:
            cols = {
                row[1]
                for row in cur.execute("PRAGMA table_info(indicator_bars)").fetchall()
            }
            if "ema_20" not in cols:
                cur.execute("ALTER TABLE indicator_bars ADD COLUMN ema_20 REAL")
            if "ema_30" not in cols:
                cur.execute("ALTER TABLE indicator_bars ADD COLUMN ema_30 REAL")
            if "ema_50" not in cols:
                cur.execute("ALTER TABLE indicator_bars ADD COLUMN ema_50 REAL")
            if "exclusive_nifty_ce_buy_ema20_30s" not in cols:
                cur.execute(
                    "ALTER TABLE indicator_bars ADD COLUMN exclusive_nifty_ce_buy_ema20_30s REAL"
                )
            if "adx" not in cols:
                cur.execute("ALTER TABLE indicator_bars ADD COLUMN adx REAL")
            if "plus_di" not in cols:
                cur.execute("ALTER TABLE indicator_bars ADD COLUMN plus_di REAL")
            if "minus_di" not in cols:
                cur.execute("ALTER TABLE indicator_bars ADD COLUMN minus_di REAL")
            if "di_spread" not in cols:
                cur.execute("ALTER TABLE indicator_bars ADD COLUMN di_spread REAL")
        except Exception as exc:
            logger.warning("SQLite indicator_bars migration failed: %s", exc)
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_indicator_bars_unique
            ON indicator_bars (label, timeframe_seconds, ts_start)
            """
        )
        self._sqlite_conn.commit()

    # ---------- Postgres ----------

    @staticmethod
    def _coerce_nonnegative_int(
        value: Optional[int],
        *,
        env_name: str,
        default: int,
    ) -> int:
        raw = value
        if raw is None:
            raw = os.getenv(env_name, str(default))
        try:
            parsed = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            logger.warning("Invalid %s=%r; using %d", env_name, raw, default)
            return default
        return max(0, parsed)

    @staticmethod
    def _coerce_nonnegative_float(
        value: Optional[float],
        *,
        env_name: str,
        default: float,
    ) -> float:
        raw = value
        if raw is None:
            raw = os.getenv(env_name, str(default))
        try:
            parsed = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            logger.warning("Invalid %s=%r; using %.1f", env_name, raw, default)
            return default
        return max(0.0, parsed)

    def _quoted_postgres_table(self) -> str:
        raw = (self.postgres_table or "indicator_bars").strip()
        parts = [p for p in raw.split(".") if p]
        if not parts or len(parts) > 2:
            raise ValueError(f"Invalid postgres table name: {raw!r}")
        for part in parts:
            if not self._POSTGRES_NAME_PART.match(part):
                raise ValueError(f"Invalid postgres table identifier: {part!r}")
        return ".".join(f'"{part}"' for part in parts)

    @staticmethod
    def _quote_postgres_identifier(identifier: str) -> str:
        if not BarPersister._POSTGRES_NAME_PART.match(identifier):
            raise ValueError(f"Invalid postgres identifier: {identifier!r}")
        return f'"{identifier}"'

    def _postgres_index_identifier(self, suffix: str) -> str:
        base = re.sub(r"[^A-Za-z0-9_]+", "_", (self.postgres_table or "indicator_bars").strip())
        base = base.strip("_") or "indicator_bars"
        if not re.match(r"^[A-Za-z_]", base):
            base = f"t_{base}"
        candidate = f"{base}_{suffix}"
        if len(candidate) <= self._POSTGRES_IDENTIFIER_MAX_LEN:
            return candidate
        digest = hashlib.sha1(candidate.encode("ascii", "ignore")).hexdigest()[:8]
        keep = self._POSTGRES_IDENTIFIER_MAX_LEN - len(digest) - 1
        return f"{candidate[:keep]}_{digest}"

    def _postgres_retention_enabled(self) -> bool:
        return bool(self.postgres_retention_months > 0)

    def _maybe_prune_postgres_retention(self, *, force: bool = False) -> None:
        if (
            not self._pg_conn
            or not self._pg_table_sql
            or not self._postgres_retention_enabled()
        ):
            return
        now_mono = time.monotonic()
        interval_seconds = self.postgres_retention_prune_interval_seconds
        if (
            not force
            and interval_seconds > 0
            and self._pg_last_retention_prune_mono > 0
            and (now_mono - self._pg_last_retention_prune_mono) < interval_seconds
        ):
            return
        try:
            deleted_rows = -1
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    f"""
                    DELETE FROM {self._pg_table_sql}
                    WHERE ts_start < (CURRENT_TIMESTAMP - make_interval(months => %s))
                    """,
                    (self.postgres_retention_months,),
                )
                deleted_rows = getattr(cur, "rowcount", -1)
            self._pg_last_retention_prune_mono = now_mono
            if deleted_rows > 0:
                logger.info(
                    "Pruned %d indicator bars older than %d month(s) from Postgres table %s",
                    deleted_rows,
                    self.postgres_retention_months,
                    self.postgres_table,
                )
        except Exception as exc:
            self._pg_last_retention_prune_mono = now_mono
            logger.warning(
                "Failed to prune Postgres indicator retention for %s: %s",
                self.postgres_table,
                exc,
            )

    def _init_postgres(self) -> None:
        if connect_with_retry is None:
            logger.error(
                "Postgres persistence requested but connect_with_retry is unavailable"
            )
            return
        try:
            self._pg_table_sql = self._quoted_postgres_table()
            self._pg_conn = connect_with_retry(
                self.postgres_dsn,
                autocommit=True,
                max_attempts=3,
            )
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self._pg_table_sql} (
                        id BIGSERIAL PRIMARY KEY,
                        ts_start TIMESTAMPTZ,
                        ts_end TIMESTAMPTZ,
                        label TEXT,
                        timeframe_seconds INTEGER,
                        o DOUBLE PRECISION,
                        h DOUBLE PRECISION,
                        l DOUBLE PRECISION,
                        c DOUBLE PRECISION,
                        atr DOUBLE PRECISION,
                        rsi DOUBLE PRECISION,
                        macd DOUBLE PRECISION,
                        macd_signal DOUBLE PRECISION,
                        macd_hist DOUBLE PRECISION,
                        ema_20 DOUBLE PRECISION,
                        ema_30 DOUBLE PRECISION,
                        ema_50 DOUBLE PRECISION,
                        exclusive_nifty_ce_buy_ema20_30s DOUBLE PRECISION,
                        adx DOUBLE PRECISION,
                        plus_di DOUBLE PRECISION,
                        minus_di DOUBLE PRECISION,
                        di_spread DOUBLE PRECISION
                    )
                    """
                )
                cur.execute(
                    f"ALTER TABLE {self._pg_table_sql} ADD COLUMN IF NOT EXISTS ema_20 DOUBLE PRECISION"
                )
                cur.execute(
                    f"ALTER TABLE {self._pg_table_sql} ADD COLUMN IF NOT EXISTS ema_30 DOUBLE PRECISION"
                )
                cur.execute(
                    f"ALTER TABLE {self._pg_table_sql} ADD COLUMN IF NOT EXISTS ema_50 DOUBLE PRECISION"
                )
                cur.execute(
                    f"ALTER TABLE {self._pg_table_sql} ADD COLUMN IF NOT EXISTS exclusive_nifty_ce_buy_ema20_30s DOUBLE PRECISION"
                )
                cur.execute(
                    f"ALTER TABLE {self._pg_table_sql} ADD COLUMN IF NOT EXISTS adx DOUBLE PRECISION"
                )
                cur.execute(
                    f"ALTER TABLE {self._pg_table_sql} ADD COLUMN IF NOT EXISTS plus_di DOUBLE PRECISION"
                )
                cur.execute(
                    f"ALTER TABLE {self._pg_table_sql} ADD COLUMN IF NOT EXISTS minus_di DOUBLE PRECISION"
                )
                cur.execute(
                    f"ALTER TABLE {self._pg_table_sql} ADD COLUMN IF NOT EXISTS di_spread DOUBLE PRECISION"
                )
                try:
                    cur.execute(
                        f"""
                        CREATE UNIQUE INDEX IF NOT EXISTS indicator_bars_unique_idx
                        ON {self._pg_table_sql} (label, timeframe_seconds, ts_start)
                        """
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to ensure Postgres unique index for indicator bars: %s",
                        exc,
                    )
                if self._postgres_retention_enabled():
                    cur.execute(
                        f"""
                        CREATE INDEX IF NOT EXISTS {self._quote_postgres_identifier(self._postgres_index_identifier("ts_start_idx"))}
                        ON {self._pg_table_sql} (ts_start)
                        """
                    )
                    logger.info(
                        "Postgres indicator retention enabled | table=%s retention_months=%d prune_interval_seconds=%.1f",
                        self.postgres_table,
                        self.postgres_retention_months,
                        self.postgres_retention_prune_interval_seconds,
                    )
            self._maybe_prune_postgres_retention(force=True)
        except Exception as exc:
            logger.error("Failed to init Postgres indicator table: %s", exc)
            self._pg_conn = None
            self._pg_table_sql = None

    # ---------- BigQuery ----------

    def _init_bigquery(self) -> None:
        try:
            self._bq_client = bigquery.Client(project=self.bq_project)
            dataset_ref = bigquery.DatasetReference(self.bq_project, self.bq_dataset)
            self._bq_table_ref = dataset_ref.table(self.bq_table)
        except Exception as exc:
            logger.error("Failed to init BigQuery client: %s", exc)
            self._bq_client = None
            self._bq_table_ref = None

    # ---------- CSV ----------

    @staticmethod
    def _bar_key(
        *,
        label: object,
        timeframe_seconds: object,
        ts_start: object,
    ) -> str:
        return "|".join(
            [
                str(label or "").strip(),
                str(timeframe_seconds or "").strip(),
                str(ts_start or "").strip(),
            ]
        )

    def _ensure_csv_seen_keys(self) -> None:
        if self._csv_seen_loaded:
            return
        with self._csv_seen_lock:
            if self._csv_seen_loaded:
                return
            self._csv_seen_loaded = True
            if not self.csv_path or not os.path.exists(self.csv_path):
                return
            try:
                with open(self.csv_path, mode="r", newline="") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        key = self._bar_key(
                            label=row.get("label"),
                            timeframe_seconds=row.get("timeframe_seconds"),
                            ts_start=row.get("ts_start"),
                        )
                        if key.strip("|"):
                            self._csv_seen_keys.add(key)
            except Exception as exc:
                logger.warning(
                    "Failed to bootstrap CSV bar dedupe cache from %s: %s",
                    self.csv_path,
                    exc,
                )

    def _upsert_existing_csv_row(self, row: Dict[str, Any]) -> bool:
        if not self.csv_path or not os.path.exists(self.csv_path):
            return False
        row_key = self._bar_key(
            label=row.get("label"),
            timeframe_seconds=row.get("timeframe_seconds"),
            ts_start=row.get("ts_start"),
        )
        replaced = False
        rows: list[dict[str, Any]] = []
        try:
            with open(self.csv_path, mode="r", newline="") as src:
                reader = csv.DictReader(src)
                for existing in reader:
                    existing_key = self._bar_key(
                        label=existing.get("label"),
                        timeframe_seconds=existing.get("timeframe_seconds"),
                        ts_start=existing.get("ts_start"),
                    )
                    if existing_key == row_key:
                        merged = dict(existing)
                        for col in self._CSV_COLUMNS:
                            merged[col] = row.get(col)
                        rows.append(merged)
                        replaced = True
                    else:
                        rows.append(existing)
            if not replaced:
                return False
            with open(self.csv_path, mode="w", newline="") as dst:
                writer = csv.DictWriter(dst, fieldnames=self._CSV_COLUMNS)
                writer.writeheader()
                for existing in rows:
                    writer.writerow({col: existing.get(col) for col in self._CSV_COLUMNS})
            return True
        except Exception as exc:
            logger.warning("CSV upsert failed for %s: %s", self.csv_path, exc)
            return False

    def _ensure_csv_header(self) -> None:
        if not self.csv_path or self._csv_header_written:
            return
        os.makedirs(os.path.dirname(self.csv_path) or ".", exist_ok=True)
        need_header = (
            not os.path.exists(self.csv_path) or os.path.getsize(self.csv_path) == 0
        )
        if need_header:
            with open(self.csv_path, mode="a", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(self._CSV_COLUMNS)
        else:
            try:
                with open(self.csv_path, mode="r", newline="") as f:
                    reader = csv.reader(f)
                    header = next(reader, [])
                if header:
                    missing = [col for col in self._CSV_COLUMNS if col not in header]
                    if missing:
                        target_header = list(header) + missing
                        with open(self.csv_path, mode="r", newline="") as src:
                            rows = list(csv.reader(src))
                        if rows:
                            rows[0] = target_header
                            for idx in range(1, len(rows)):
                                pad = len(target_header) - len(rows[idx])
                                if pad > 0:
                                    rows[idx] = list(rows[idx]) + ([""] * pad)
                            with open(self.csv_path, mode="w", newline="") as dst:
                                writer = csv.writer(dst)
                                writer.writerows(rows)
            except Exception as exc:
                logger.warning("CSV indicator migration failed for %s: %s", self.csv_path, exc)
        self._csv_header_written = True

    # ---------- Public API ----------

    def persist_bar(
        self,
        label: str,
        timeframe_seconds: int,
        candle,
        indicators: Dict[str, Any],
    ) -> None:
        row = {
            "ts_start": candle.start_ts.isoformat(),
            "ts_end": candle.end_ts.isoformat() if candle.end_ts else None,
            "label": label,
            "timeframe_seconds": timeframe_seconds,
            "o": candle.o,
            "h": candle.h,
            "l": candle.low,
            "c": candle.c,
            "atr": indicators.get("atr"),
            "rsi": indicators.get("rsi"),
            "macd": indicators.get("macd"),
            "macd_signal": indicators.get("macd_signal"),
            "macd_hist": indicators.get("macd_hist"),
            "ema_20": indicators.get("ema_20"),
            "ema_30": indicators.get("ema_30"),
            "ema_50": indicators.get("ema_50"),
            "adx": indicators.get("adx"),
            "plus_di": indicators.get("plus_di"),
            "minus_di": indicators.get("minus_di"),
            "di_spread": indicators.get("di_spread"),
        }
        self._to_csv(row)
        self._to_sqlite(row)
        self._to_postgres(row)
        self._to_bigquery(row)

    def _to_csv(self, row: Dict[str, Any]) -> None:
        if not self.csv_path:
            return
        try:
            self._ensure_csv_header()
            self._ensure_csv_seen_keys()
            row_key = self._bar_key(
                label=row.get("label"),
                timeframe_seconds=row.get("timeframe_seconds"),
                ts_start=row.get("ts_start"),
            )
            if row_key in self._csv_seen_keys:
                self._upsert_existing_csv_row(row)
                return
            with open(self.csv_path, mode="a", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([row.get(col) for col in self._CSV_COLUMNS])
            self._csv_seen_keys.add(row_key)
        except Exception as exc:
            logger.error("Failed to write CSV: %s", exc)

    def _to_sqlite(self, row: Dict[str, Any]) -> None:
        if not self._sqlite_conn:
            return
        try:
            cur = self._sqlite_conn.cursor()
            cur.execute(
                """
                INSERT INTO indicator_bars (
                    ts_start, ts_end, label, timeframe_seconds,
                    o, h, l, c, atr, rsi, macd, macd_signal, macd_hist, ema_20, ema_30, ema_50, adx, plus_di, minus_di, di_spread
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(label, timeframe_seconds, ts_start) DO UPDATE SET
                    ts_end=excluded.ts_end,
                    o=excluded.o,
                    h=excluded.h,
                    l=excluded.l,
                    c=excluded.c,
                    atr=excluded.atr,
                    rsi=excluded.rsi,
                    macd=excluded.macd,
                    macd_signal=excluded.macd_signal,
                    macd_hist=excluded.macd_hist,
                    ema_20=excluded.ema_20,
                    ema_30=excluded.ema_30,
                    ema_50=excluded.ema_50,
                    adx=excluded.adx,
                    plus_di=excluded.plus_di,
                    minus_di=excluded.minus_di,
                    di_spread=excluded.di_spread
                """,
                (
                    row["ts_start"],
                    row["ts_end"],
                    row["label"],
                    row["timeframe_seconds"],
                    row["o"],
                    row["h"],
                    row["l"],
                    row["c"],
                    row["atr"],
                    row["rsi"],
                    row["macd"],
                    row["macd_signal"],
                    row["macd_hist"],
                    row["ema_20"],
                    row["ema_30"],
                    row["ema_50"],
                    row["adx"],
                    row["plus_di"],
                    row["minus_di"],
                    row["di_spread"],
                ),
            )
            self._sqlite_conn.commit()
        except Exception as exc:
            logger.error("Failed to write SQLite: %s", exc)

    def _to_postgres(self, row: Dict[str, Any]) -> None:
        if not self._pg_conn or not self._pg_table_sql:
            return
        try:
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {self._pg_table_sql} (
                        ts_start, ts_end, label, timeframe_seconds,
                        o, h, l, c, atr, rsi, macd, macd_signal, macd_hist,
                        ema_20, adx, plus_di, minus_di, di_spread, ema_30, ema_50
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (label, timeframe_seconds, ts_start) DO UPDATE SET
                        ts_end = EXCLUDED.ts_end,
                        o = EXCLUDED.o,
                        h = EXCLUDED.h,
                        l = EXCLUDED.l,
                        c = EXCLUDED.c,
                        atr = EXCLUDED.atr,
                        rsi = EXCLUDED.rsi,
                        macd = EXCLUDED.macd,
                        macd_signal = EXCLUDED.macd_signal,
                        macd_hist = EXCLUDED.macd_hist,
                        ema_20 = EXCLUDED.ema_20,
                        ema_30 = EXCLUDED.ema_30,
                        ema_50 = EXCLUDED.ema_50,
                        adx = EXCLUDED.adx,
                        plus_di = EXCLUDED.plus_di,
                        minus_di = EXCLUDED.minus_di,
                        di_spread = EXCLUDED.di_spread
                    """,
                    (
                        row["ts_start"],
                        row["ts_end"],
                        row["label"],
                        row["timeframe_seconds"],
                        row["o"],
                        row["h"],
                        row["l"],
                        row["c"],
                        row["atr"],
                        row["rsi"],
                        row["macd"],
                        row["macd_signal"],
                        row["macd_hist"],
                        row["ema_20"],
                        row["adx"],
                        row["plus_di"],
                        row["minus_di"],
                        row["di_spread"],
                        row["ema_30"],
                        row["ema_50"],
                    ),
                )
            self._maybe_prune_postgres_retention()
        except Exception as exc:
            logger.error("Failed to write Postgres: %s", exc)

    def _to_bigquery(self, row: Dict[str, Any]) -> None:
        if not self._bq_client or not self._bq_table_ref:
            return
        try:
            row_id = self._bar_key(
                label=row.get("label"),
                timeframe_seconds=row.get("timeframe_seconds"),
                ts_start=row.get("ts_start"),
            )
            try:
                errors = self._bq_client.insert_rows_json(
                    self._bq_table_ref,
                    [row],
                    row_ids=[row_id],
                    ignore_unknown_values=True,
                )
            except TypeError:
                errors = self._bq_client.insert_rows_json(
                    self._bq_table_ref, [row], ignore_unknown_values=True
                )
            if errors:
                logger.error("BigQuery insert errors: %s", errors)
        except Exception as exc:
            logger.error("Failed to write BigQuery: %s", exc)

    def close(self) -> None:
        if self._sqlite_conn:
            self._sqlite_conn.close()
            self._sqlite_conn = None
        if self._pg_conn:
            self._pg_conn.close()
            self._pg_conn = None
