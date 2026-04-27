# Phoenix v9 -- Complete Application Flow

> **Backend note:** The bundled Docker/Desktop LIVE path uses Postgres for all durable backends
> (leader lease, sweep state, control plane, PnL). Firestore references in this diagram apply
> to the Cloud Run / GCP deployment path only. Both paths share the same application code;
> the backend is selected by `LEADER_LEASE_BACKEND`, `CONTROL_PLANE_BACKEND`, etc.

## 1. System Boot & Initialization

```mermaid
flowchart TD
    START([python -m app.main]) --> LOAD_ENV[_load_cloudrun_env_if_local<br/>Load .env / localrun.env / cloudrun.env]
    LOAD_ENV --> CONFIG_LOG[_configure_logging<br/>Setup console + rotating file handlers]
    CONFIG_LOG --> PORT_CHECK{_preflight_port_bind<br/>Port available?}
    PORT_CHECK -- No --> EXIT_FAIL([sys.exit 1])
    PORT_CHECK -- Yes --> UVICORN[uvicorn.run app.server:app<br/>host:port]

    UVICORN --> LIFESPAN[FastAPI lifespan context manager]
    LIFESPAN --> GET_RUNTIME[get_app_runtime singleton<br/>Creates AppRuntime]
    GET_RUNTIME --> RUNTIME_START[await runtime.start]

    subgraph AppRuntime.start
        RUNTIME_START --> BOOT_CFG[initialize_boot_config<br/>Load strategy_env.yaml,<br/>universe.yaml, settings]
        BOOT_CFG --> FEATURE_FLAGS[load_stability_feature_flags<br/>CAPITAL_MARGIN_CHECK_MODE, etc.]
        FEATURE_FLAGS --> SCHEMA_CHECK[check_startup_schema<br/>Verify Postgres tables/indexes]
        SCHEMA_CHECK --> VALIDATE{LIVE mode?}
        VALIDATE -- Yes --> STRICT_VALIDATE[validate_runtime_startup_settings<br/>validate_startup_config<br/>Strict schema check]
        VALIDATE -- No --> LEADER_CHECK
        STRICT_VALIDATE --> LEADER_CHECK

        LEADER_CHECK{Leader lease<br/>enabled?}
        LEADER_CHECK -- Yes --> ACQUIRE_LEASE[LeaderLease.start<br/>Postgres or Firestore transactional lease]
        LEADER_CHECK -- No --> IS_LEADER[is_leader = True]
        ACQUIRE_LEASE --> LEASE_RESULT{Lease acquired?}
        LEASE_RESULT -- Yes --> IS_LEADER
        LEASE_RESULT -- No --> NOT_LEADER[Standby mode:<br/>no workers started]

        IS_LEADER --> WORKER_CHECK{disable_stream_worker?}
        WORKER_CHECK -- Yes --> BQ_START
        WORKER_CHECK -- No --> START_WORKER[StreamWorker.start<br/>Background thread]
        START_WORKER --> START_WATCHDOG[WorkerWatchdog.start<br/>Monitor thread every 15s]
        START_WATCHDOG --> BQ_START

        BQ_START[start_global_writer<br/>BigQuery async writer]
        BQ_START --> HUB_CHECK{enable_multi_hub?}
        HUB_CHECK -- No --> RUNTIME_READY([Runtime Ready])
        HUB_CHECK -- Yes --> HUB_INIT
    end

    subgraph HubRuntime Init
        HUB_INIT[get_hub_runtime singleton] --> CREATE_ENGINES[Create CapitalEngine,<br/>PnLEngine, RiskEngine,<br/>ProfitEngine]
        CREATE_ENGINES --> CREATE_OWNERSHIP[build_position_ownership_store<br/>build_order_submission_outbox]
        CREATE_OWNERSHIP --> CREATE_HUB[Hub + StateStore]
        CREATE_HUB --> CREATE_LIFECYCLE[OrderLifecycleService]
        CREATE_LIFECYCLE --> CREATE_CB[TradingCircuitBreaker<br/>Wire fault tracker]
        CREATE_CB --> CREATE_ROUTER[OrderRouter<br/>Wire all engines]
        CREATE_ROUTER --> CREATE_SWEEP[SweepStateManager<br/>EODStateManager<br/>Postgres or Firestore]
        CREATE_SWEEP --> CREATE_EXIT_ENGINES[ProfitSweepEngine<br/>EODExitEngine]
        CREATE_EXIT_ENGINES --> HUB_INITIALIZE[hub.initialize<br/>Load tenants, broker accounts]
        HUB_INITIALIZE --> HUB_START_ALL[hub.start_all<br/>Start AccountRunners]
        HUB_START_ALL --> LIFECYCLE_START[order_lifecycle.start<br/>Begin polling broker statuses]
        LIFECYCLE_START --> OUTBOX_RECOVER[recover_submission_outbox<br/>Replay PENDING orders]
        OUTBOX_RECOVER --> RUNTIME_READY
    end
```

## 2. Stream Worker Initialization (Current Market-Data & Strategy Path)

```mermaid
flowchart TD
    SW_START([StreamWorker._run]) --> SMI[stream_multi_instruments]

    subgraph stream_multi_instruments
        SMI --> LOGIN[angel_login_and_get_tokens<br/>TOTP -> Angel Broker Login<br/>Get JWT + Feed Token]
        LOGIN --> BUILD_UNIVERSE[build_instrument_universe<br/>Download scrip master<br/>Resolve ATM strikes<br/>Build token-to-label map]
        BUILD_UNIVERSE --> FILTER_UNIVERSE[Filter by InstrumentController<br/>enabled/disabled underlyings]
        FILTER_UNIVERSE --> DAILY_LEVELS[DailyLevelsCache.refresh<br/>Fetch PDH/PDL/VWAP]

        DAILY_LEVELS --> CREATE_RISK[Create RiskManager<br/>Load risk_positions.json<br/>Restore kill switch state]
        CREATE_RISK --> CREATE_ENGINE[Create MultiInstrumentIndicatorEngine<br/>EMA, ATR, RSI, ADX, MACD<br/>per timeframe per instrument]
        CREATE_ENGINE --> SEED_INDICATORS{Seed history?}
        SEED_INDICATORS -- CSV/Postgres --> SEED[seed_from_closed_bars<br/>Warm up indicators]
        SEED_INDICATORS -- No --> BOOTSTRAP
        SEED --> BOOTSTRAP

        BOOTSTRAP[Bootstrap executed_tokens_tracker<br/>from restored positions]
        BOOTSTRAP --> INSTANTIATE_STRATEGIES[Instantiate strategies<br/>from strategy_env.yaml]
        INSTANTIATE_STRATEGIES --> VALIDATE_ROUTES{Hub router<br/>enabled?}
        VALIDATE_ROUTES -- Yes --> FILTER_ROUTES[Filter strategies<br/>with missing hub routes]
        VALIDATE_ROUTES -- No --> CREATE_WS
        FILTER_ROUTES --> CREATE_WS

        CREATE_WS[Create SmartWebSocketRunner<br/>Angel WebSocket V2 client]
        CREATE_WS --> SETUP_CALLBACKS[Wire on_tick callback<br/>Wire on_bar_close callback]
        SETUP_CALLBACKS --> RESTORE_POSITIONS[Restore positions from<br/>risk_manager.restored_positions<br/>via restart_state.py]
        RESTORE_POSITIONS --> POSITION_SYNC_INITIAL[sync_positions_with_broker<br/>Initial broker reconciliation]
    end

    POSITION_SYNC_INITIAL --> LAUNCH_THREADS
    subgraph Launch Background Threads
        LAUNCH_THREADS[run_stream_lifecycle] --> T1[Thread: atm-refresh<br/>Periodic ATM strike refresh]
        LAUNCH_THREADS --> T2[Thread: position-sync<br/>Periodic broker reconciliation]
        LAUNCH_THREADS --> T3[Thread: ws-runner<br/>WebSocket connect + subscribe]
        LAUNCH_THREADS --> T4[Main loop: sleep 0.5s<br/>Check stop_event]
    end
```

## 3. Market Data Flow (Tick Processing)

```mermaid
flowchart TD
    WS([Angel WebSocket V2]) -->|Tick JSON| ON_DATA[SmartWebSocketRunner._on_data]
    ON_DATA --> PARSE[Parse token, LTP, OHLCV<br/>from binary/JSON frame]
    PARSE --> TOKEN_LOOKUP{token in<br/>token_labels?}
    TOKEN_LOOKUP -- No --> SELF_HEAL{Self-heal<br/>enabled?}
    SELF_HEAL -- Yes --> RESOLVE_LABEL[_resolve_label_from_runtime_state]
    SELF_HEAL -- No --> DROP_TICK[Drop unmapped tick]
    TOKEN_LOOKUP -- Yes --> LABEL_FOUND[Resolve label]
    RESOLVE_LABEL --> LABEL_FOUND

    LABEL_FOUND --> DISPATCH[LatestPerKeyDispatcher<br/>Coalesce rapid ticks per label]
    DISPATCH --> PROCESS_TICK[Process tick for label]

    subgraph Tick Processing Pipeline
        PROCESS_TICK --> UPDATE_BUS[DashboardBus.update_tick<br/>Store latest LTP]
        UPDATE_BUS --> FEED_ENGINE[IndicatorEngine.on_tick<br/>Build OHLCV candles]
        FEED_ENGINE --> BAR_CHECK{Bar closed?}
        BAR_CHECK -- No --> CHECK_STRATEGIES_TICK[Dispatch on_tick to<br/>attached strategies]
        BAR_CHECK -- Yes --> BAR_CLOSE[on_bar_close callback]
    end

    subgraph Bar Close Processing
        BAR_CLOSE --> PERSIST_BAR[BarPersister.persist<br/>CSV + SQLite + Postgres + BQ]
        PERSIST_BAR --> COMPUTE_INDICATORS[Compute EMA, ATR, RSI,<br/>ADX, MACD for closed bar]
        COMPUTE_INDICATORS --> UPDATE_CONTEXT[MarketContextBuilder.update<br/>Compute vol_regime, trend_strength]
        UPDATE_CONTEXT --> UPDATE_REGIME[RegimeClassifier.update<br/>Classify: TRENDING / MEAN_REVERT /<br/>VOLATILE / NO_TRADE]
        UPDATE_REGIME --> SELECT_STRATEGY[StrategySelector.select<br/>Choose active strategy per underlying]
        SELECT_STRATEGY --> DISPATCH_STRATEGIES[Dispatch on_bar to<br/>selected strategies]
    end
```

## 4. Strategy Signal Generation

```mermaid
flowchart TD
    ON_BAR([Strategy.on_bar called]) --> TRADING_WINDOW{Within trading<br/>window?}
    TRADING_WINDOW -- No --> NO_SIGNAL([No action])
    TRADING_WINDOW -- Yes --> SWITCH_CHECK{StrategySwitchboard<br/>enabled?}
    SWITCH_CHECK -- No --> NO_SIGNAL
    SWITCH_CHECK -- Yes --> EXISTING_POS{Has open<br/>position?}

    EXISTING_POS -- Yes --> EXIT_LOGIC
    EXISTING_POS -- No --> ENTRY_LOGIC

    subgraph Entry Signal Logic
        ENTRY_LOGIC[Check entry conditions<br/>EMA cross / ORB breakout /<br/>PDH range / Delta strangle /<br/>Put momentum / etc.] --> ENTRY_COND{Conditions<br/>met?}
        ENTRY_COND -- No --> NO_SIGNAL
        ENTRY_COND -- Yes --> COOLDOWN{Cooldown<br/>active?}
        COOLDOWN -- Yes --> NO_SIGNAL
        COOLDOWN -- No --> GEN_ENTRY[Generate entry signal<br/>Symbol, side, qty, SL, TP]
    end

    subgraph Exit Signal Logic
        EXIT_LOGIC[Check exit conditions] --> SL_CHECK{SL hit?}
        SL_CHECK -- Yes --> GEN_EXIT_SL[Exit: STOPLOSS reason]
        SL_CHECK -- No --> TP_CHECK{TP hit?}
        TP_CHECK -- Yes --> GEN_EXIT_TP[Exit: TARGET reason]
        TP_CHECK -- No --> TRAIL_CHECK{Trailing SL<br/>triggered?}
        TRAIL_CHECK -- Yes --> GEN_EXIT_TRAIL[Exit: TRAIL_SL reason]
        TRAIL_CHECK -- No --> TIME_CHECK{Square-off<br/>time?}
        TIME_CHECK -- Yes --> GEN_EXIT_TIME[Exit: TIME_EXIT reason]
        TIME_CHECK -- No --> STRATEGY_EXIT{Strategy-specific<br/>exit signal?}
        STRATEGY_EXIT -- Yes --> GEN_EXIT_STRAT[Exit: SIGNAL reason]
        STRATEGY_EXIT -- No --> NO_EXIT([Hold position])
    end

    GEN_ENTRY --> PLACE_ORDER
    GEN_EXIT_SL --> PLACE_ORDER
    GEN_EXIT_TP --> PLACE_ORDER
    GEN_EXIT_TRAIL --> PLACE_ORDER
    GEN_EXIT_TIME --> PLACE_ORDER
    GEN_EXIT_STRAT --> PLACE_ORDER

    PLACE_ORDER[Call risk_manager.place_order<br/>or place_order_via_bridge]
```

## 5. Order Lifecycle (Legacy Path via RiskManager)

```mermaid
flowchart TD
    PO([risk_manager.place_order]) --> KILL_CHECK{Kill switch<br/>activated?}
    KILL_CHECK -- Yes --> BLOCK_KILL([Order blocked:<br/>kill switch active])
    KILL_CHECK -- No --> TRADING_SESSION{In trading<br/>session?}
    TRADING_SESSION -- No --> BLOCK_SESSION([Order blocked:<br/>outside session])
    TRADING_SESSION -- Yes --> SPREAD_CHECK{Max spreads<br/>per underlying?}
    SPREAD_CHECK -- Exceeded --> BLOCK_SPREAD([Order blocked:<br/>spread limit])
    SPREAD_CHECK -- OK --> LOSS_CHECK{Daily loss<br/>limit exceeded?}
    LOSS_CHECK -- Yes --> ACTIVATE_KILL[Activate kill switch<br/>Square off positions]
    LOSS_CHECK -- No --> LOT_VALIDATE[Validate lot size<br/>lot_size_for_symbol]

    LOT_VALIDATE --> IS_CLOSING{Closing<br/>existing?}
    IS_CLOSING -- Yes --> SUBMIT_EXIT
    IS_CLOSING -- No --> REGISTER_ENTRY[_register_entry<br/>Track position internally]
    REGISTER_ENTRY --> SUBMIT_ENTRY

    subgraph Broker Submission
        SUBMIT_ENTRY[order_client.place_order<br/>Angel REST API] --> BROKER_RESP{Broker response}
        SUBMIT_EXIT[order_client.place_order] --> BROKER_RESP
        BROKER_RESP -- Success --> RECORD_TRADE[Record trade<br/>TradePersister.record_trade<br/>CSV + BQ]
        BROKER_RESP -- Rejection --> LOG_REJECT[Log rejection<br/>Reverse internal state]
        BROKER_RESP -- Exception --> LOG_ERROR[Log error<br/>BackoffState]
    end

    RECORD_TRADE --> UPDATE_PNL[Update realized PnL<br/>Update dashboard_bus]
    UPDATE_PNL --> UPDATE_POSITIONS[Update open_positions<br/>Update kill switch state]
    UPDATE_POSITIONS --> PERSIST_STATE[Persist risk_positions.json]
```

## 6. Order Lifecycle (Hub Path via OrderRouter)

```mermaid
flowchart TD
    BRIDGE([place_order_via_bridge]) --> BRIDGE_LOOP[BridgeLoop._submit<br/>Cross-thread async bridge]
    BRIDGE_LOOP --> ROUTE[OrderRouter.submit_order]

    subgraph OrderRouter Interceptor Pipeline
        ROUTE --> IDEM_CHECK{IdempotencyStore<br/>claim key}
        IDEM_CHECK -- Duplicate --> RETURN_DUP([Return cached response])
        IDEM_CHECK -- New claim --> CAPITAL_CHECK[CapitalEngine.check_order<br/>Notional limits, gross exposure,<br/>margin check]
        CAPITAL_CHECK -- Blocked --> RELEASE_IDEM[Release claim + return blocked]
        CAPITAL_CHECK -- OK --> RISK_CHECK[RiskEngine.check_order_allowed<br/>Daily loss guard, PnL check]
        RISK_CHECK -- Blocked --> RELEASE_IDEM
        RISK_CHECK -- OK --> PROFIT_CHECK[ProfitEngine.check_order<br/>Profit lock, sweep status]
        PROFIT_CHECK -- Blocked --> RELEASE_IDEM
        PROFIT_CHECK -- OK --> OWNERSHIP_CHECK[PositionOwnershipStore<br/>Acquire pending lock]
        OWNERSHIP_CHECK -- Locked --> RELEASE_IDEM
        OWNERSHIP_CHECK -- OK --> CB_CHECK[CircuitBreaker.is_tripped?]
        CB_CHECK -- Tripped --> RELEASE_OWN[Release ownership + idem]
        CB_CHECK -- OK --> RESOLVE_RUNNER[Resolve AccountRunner<br/>via RoutingTable]
    end

    RESOLVE_RUNNER --> BROKER_SUBMIT[runner.place_order<br/>BrokerClient.place_order]

    subgraph Broker Response Processing
        BROKER_SUBMIT --> RESP{Response status}
        RESP -- FILLED/COMPLETE --> RECORD[_record_trade_and_pnl<br/>PnLEngine.on_trade<br/>OwnershipStore.apply_fill]
        RESP -- REJECTED/FAILED --> RELEASE_ALL[Release ownership +<br/>idempotency + log]
        RESP -- OPEN/PENDING --> TRACK_LIFECYCLE[OrderLifecycleService<br/>Track and poll]
    end

    subgraph OrderLifecycleService Polling
        TRACK_LIFECYCLE --> POLL_LOOP[Periodic poll broker<br/>get_order_status]
        POLL_LOOP --> STATUS{Order status?}
        STATUS -- FILLED --> EMIT_TRADE[_emit_trade<br/>Dedup via ProcessedTradeStore<br/>Apply fill to ownership]
        STATUS -- REJECTED/CANCELLED --> RELEASE_LIFECYCLE[Release pending lock<br/>Record terminal state]
        STATUS -- Still open --> POLL_LOOP
    end

    RECORD --> OUTBOX_COMPLETE[Outbox mark COMPLETED]
    EMIT_TRADE --> OUTBOX_COMPLETE
    OUTBOX_COMPLETE --> UPDATE_BQ[BQ async writer<br/>Persist to BigQuery]
```

## 7. Risk Management & Kill Switch Flow

```mermaid
flowchart TD
    subgraph Per-Trade Risk Gate
        ORDER_IN([Order arrives]) --> KS{Kill switch?}
        KS -- Active --> BLOCK([Block all entries])
        KS -- Off --> DLG{Daily loss guard<br/>enabled?}
        DLG -- No --> CAP_CHECK
        DLG -- Yes --> PNL_CHECK{PnL engine:<br/>realized + unrealized<br/>below threshold?}
        PNL_CHECK -- Exceeded --> TRIP_KS[Activate kill switch]
        PNL_CHECK -- OK --> CAP_CHECK

        CAP_CHECK[CapitalEngine checks:<br/>max_notional_per_order<br/>max_gross_exposure<br/>margin requirement]
        CAP_CHECK -- Blocked --> BLOCK
        CAP_CHECK -- OK --> ALLOW([Allow order])
    end

    subgraph Kill Switch Activation
        TRIP_KS --> PERSIST_KS[Persist kill switch state<br/>Durable Postgres store in LIVE hub mode<br/>risk_positions.json in legacy/dev mode]
        PERSIST_KS --> SQUARE_OFF{Square off<br/>enabled?}
        SQUARE_OFF -- Yes --> EXIT_ALL[Exit all open positions<br/>Market orders]
        SQUARE_OFF -- No --> HALT([Halt new entries only])
    end

    subgraph Circuit Breaker
        CB_TRIGGERS[Triggers:<br/>- Loss streak >= N consecutive<br/>- Reject rate >= threshold<br/>- India VIX >= threshold] --> TRIP_CB[Trip circuit breaker]
        TRIP_CB --> COOLDOWN_CB[Cooldown period<br/>15-30 min auto-expire]
        COOLDOWN_CB --> RESUME([Resume trading])
    end

    subgraph EOD Square-Off
        EOD_TIME([15:15 - 15:20 IST]) --> EOD_ENGINE[EODExitEngine<br/>Check all open positions]
        EOD_ENGINE --> EOD_EXIT[Submit market exit orders<br/>for every open position]
        EOD_EXIT --> EOD_RECORD[EODStateManager<br/>Record exit completion]
    end

    subgraph Profit Sweep
        SWEEP_TRIGGER([PnL >= target]) --> SWEEP_CHECK[ProfitSweepEngine<br/>Check sweep state + cooldown]
        SWEEP_CHECK --> SWEEP_EXIT[Exit all positions<br/>for account]
        SWEEP_EXIT --> SWEEP_RECORD[SweepStateManager<br/>Record sweep in Firestore/Postgres]
    end
```

## 8. ATM Refresh & Position Sync Cycles

```mermaid
flowchart TD
    subgraph ATM Refresh Thread
        ATM_LOOP([atm-refresh thread<br/>Every N minutes]) --> CHECK_MARKET{Market<br/>open?}
        CHECK_MARKET -- No --> ATM_SLEEP([Sleep])
        CHECK_MARKET -- Yes --> FETCH_ATM[Re-fetch ATM strikes<br/>from Angel API]
        FETCH_ATM --> STRIKES_CHANGED{ATM strikes<br/>changed?}
        STRIKES_CHANGED -- No --> ATM_SLEEP
        STRIKES_CHANGED -- Yes --> REBUILD_META[Rebuild instrument_meta<br/>New labels, new tokens]
        REBUILD_META --> REFRESH_STRATEGIES[_refresh_strategy_instances_after_atm_update]
        REFRESH_STRATEGIES --> RECONCILE{Strategy has<br/>reconcile_open_legs?}
        RECONCILE -- Yes --> REBOUND[Reconcile: map old labels<br/>to new labels]
        RECONCILE -- No --> DEGRADED_ATM[Mark strategy/account scope DEGRADED<br/>Block entries; allow safe exits or manual review]
        REBOUND --> RESUBSCRIBE[Resubscribe WebSocket<br/>to new token list]
        DEGRADED_ATM --> RESUBSCRIBE
    end

    subgraph Position Sync Thread
        PSYNC_LOOP([position-sync thread<br/>Every 60-120s]) --> FETCH_BROKER[order_client.get_positions<br/>Fetch broker-side positions]
        FETCH_BROKER --> COMPARE[Compare broker positions<br/>vs risk_manager.open_positions]
        COMPARE --> GHOST{Ghost positions?<br/>Broker has, we don't}
        GHOST -- Yes --> REGISTER_GHOST[Register in risk_manager<br/>as BROKER_SYNC]
        GHOST -- No --> STALE{Stale positions?<br/>We have, broker doesn't}
        STALE -- Yes --> REMOVE_STALE[Remove from<br/>risk_manager]
        STALE -- No --> SYNC_OK([Positions in sync])
    end
```

> **Architecture note — Position Sync Thread:** The ghost-registration and single-poll removal paths shown above are the **legacy stream-side** code (`risk_manager` / `restart_state.py`). In the current hub-authoritative LIVE mode, ARCHITECTURE.md §19.3 and P0 rule 5 forbid single-poll ghost creation or removal without reconciliation evidence. The authoritative sync path is the hub `AccountRunner` → `OrderLifecycleService` / `PositionOwnershipStore` flow governed by reconciliation rules in §11. The stream-side paths shown here exist only for the legacy authority path and for restart-helper seeding.

## 9. Hub Multi-Tenant Architecture

```mermaid
flowchart TD
    subgraph Hub Controller
        HUB([Hub]) --> RECONCILE_LOOP[_reconcile_runners_once<br/>Load tenants from control plane<br/>(Postgres or Firestore per CONTROL_PLANE_BACKEND)]
        RECONCILE_LOOP --> FOR_EACH[For each active<br/>broker account]
        FOR_EACH --> HAS_RUNNER{Runner<br/>exists?}
        HAS_RUNNER -- No --> CREATE_RUNNER[Create AccountRunner<br/>Create BrokerClient via factory]
        HAS_RUNNER -- Yes --> UPDATE_RUNNER[Update subscription<br/>mode if changed]
        CREATE_RUNNER --> START_RUNNER[Start AccountRunner<br/>asyncio task]
        UPDATE_RUNNER --> CHECK_STALE
        START_RUNNER --> CHECK_STALE

        CHECK_STALE[Check for removed accounts] --> STOP_STALE[Stop runners for<br/>deactivated accounts]
    end

    subgraph Subscription Watchdog
        WATCHDOG_LOOP([_subscription_watchdog_loop<br/>Every 60s]) --> RE_RECONCILE[Call _reconcile_runners_once<br/>Detect new/removed accounts]
    end

    subgraph Profit Watchdog
        PROFIT_LOOP([_profit_watchdog_loop<br/>Every 30s]) --> SWEEP_ALL[ProfitSweepEngine<br/>maybe_sweep_for_runners]
        SWEEP_ALL --> EOD_ALL[EODExitEngine<br/>maybe_exit_for_runners]
    end

    subgraph AccountRunner
        RUNNER([AccountRunner]) --> POLL_BALANCE[Poll broker balance<br/>Update StateStore]
        POLL_BALANCE --> POLL_POSITIONS[Poll broker positions<br/>Update StateStore]
        POLL_POSITIONS --> POLL_ORDERS[Poll broker orders<br/>Feed OrderLifecycleService]
    end
```

## 10. Dashboard & API Layer

```mermaid
flowchart TD
    subgraph Core Endpoints
        HEALTH[GET /health<br/>Liveness probe]
        READY[GET /ready<br/>Readiness probe]
        HEALTH_SUMMARY[GET /health/summary<br/>Startup and dependency summary]
        METRICS[GET /metrics<br/>Prometheus metrics]
        POSITIONS[GET /positions<br/>Open positions - legacy compat]
    end

    subgraph Admin Routes -- prefix /admin
        STRATEGIES[GET /admin/strategies<br/>Strategy list]
        TOGGLE[POST /admin/strategies/toggle<br/>Enable/disable strategy]
        INSTRUMENTS[GET /admin/instruments<br/>Instrument policy]
        REFRESH[POST /admin/daily-levels/refresh<br/>Reload PDH/PDL]
        VOLATILITY[POST /admin/volatility/update<br/>Update India VIX level]
        TENANTS[GET /admin/tenants<br/>Tenant list]
        ACCOUNTS[GET /admin/broker-accounts<br/>Broker account list]
        RUNNERS[GET /admin/runners<br/>Active runner list]
        SWEEP[POST /admin/manual-sweep<br/>Manual profit sweep]
        EOD_EXIT[POST /admin/manual-eod-exit<br/>Manual EOD exit]
        BREAK_GLASS[POST /admin/break-glass/flatten<br/>Break-glass flatten]
        AUDIT[GET /admin/audit<br/>Audit event log]
    end

    subgraph Control Tower -- disabled when DISABLE_CONTROL_TOWER_ROUTES=true
        CT_MATRIX[GET /api/control_tower/matrix<br/>Control tower matrix view]
        CT_TOGGLE[POST /api/control_tower/toggle<br/>Toggle control]
    end

    subgraph ML Health
        ML_HEALTH[GET /ml/health<br/>ML config and model status]
    end

    subgraph WebSocket Dashboard
        WS_DASH[WS /ws/dashboard<br/>Real-time updates] --> SNAPSHOT[Periodic full snapshot or delta<br/>Ticks, PnL, positions,<br/>strategies, indicators]
    end

    subgraph DashboardBus Singleton
        BUS([DashboardBus]) --> TICKS[Latest ticks per label]
        BUS --> PNL_LIVE[Realized + Unrealized PnL]
        BUS --> POS_LIVE[Open positions]
        BUS --> SIGNALS[Recent signals/trades]
        BUS --> INDICATOR_BARS[Indicator bar snapshots]
    end
```

## 11. Shutdown Sequence

```mermaid
flowchart TD
    SHUTDOWN([Shutdown signal<br/>SIGTERM / Ctrl+C / Lease loss]) --> LIFESPAN_FINALLY[FastAPI lifespan finally block]
    LIFESPAN_FINALLY --> RUNTIME_STOP[await runtime.stop]

    subgraph AppRuntime.stop
        RUNTIME_STOP --> STOP_WATCHDOG[WorkerWatchdog.stop<br/>Set stop_event, join thread]
        STOP_WATCHDOG --> STOP_WORKER[StreamWorker.stop<br/>Set stop_event, join thread]

        subgraph StreamWorker Shutdown
            STOP_WORKER --> SET_REFRESH_STOP[refresh_stop.set<br/>Stop ATM refresh thread]
            SET_REFRESH_STOP --> CLOSE_WS[WebSocketRunner.close<br/>Disconnect WebSocket]
            CLOSE_WS --> JOIN_THREADS[Join ws-runner,<br/>atm-refresh, position-sync<br/>threads with timeout]
            JOIN_THREADS --> CLOSE_PERSISTER[BarPersister.close<br/>Flush + close CSV/SQLite]
        end

        STOP_WORKER --> STOP_BQ[stop_global_writer<br/>Flush BigQuery buffer]

        STOP_BQ --> HUB_STOP{Hub started?}
        HUB_STOP -- Yes --> LIFECYCLE_STOP[order_lifecycle.stop<br/>Stop polling]
        LIFECYCLE_STOP --> HUB_STOP_ALL[hub.stop_all<br/>Stop all AccountRunners]
        HUB_STOP -- No --> LEASE_STOP

        HUB_STOP_ALL --> LEASE_STOP[leader_lease.stop<br/>Release Postgres/Firestore lease]
    end

    LEASE_STOP --> UVICORN_EXIT([Process exits])

    subgraph Lease Loss Emergency Path
        LEASE_LOST([Leader lease lost<br/>during renewal]) --> OS_EXIT[os._exit 2<br/>HARD EXIT<br/>No cleanup!]
    end
```

## 12. End-to-End Trade Lifecycle (Complete Path)

```mermaid
flowchart TD
    TICK_IN([Market tick arrives<br/>via WebSocket]) --> INDICATOR[Indicator engine<br/>builds candle, computes<br/>EMA/ATR/RSI/ADX]
    INDICATOR --> BAR_CLOSE[Bar closes]
    BAR_CLOSE --> REGIME[Regime classifier<br/>determines market state]
    REGIME --> SELECTOR[Strategy selector<br/>picks active strategy]
    SELECTOR --> STRATEGY[Strategy.on_bar<br/>evaluates entry/exit]

    STRATEGY --> SIGNAL{Signal?}
    SIGNAL -- No --> WAIT([Wait for next bar])
    SIGNAL -- Entry --> ENTRY_FLOW
    SIGNAL -- Exit --> EXIT_FLOW

    subgraph Entry Flow
        ENTRY_FLOW[Generate OrderRequest<br/>BUY CE/PE, qty, SL, TP]
        ENTRY_FLOW --> BRIDGE_OR_DIRECT{Hub routing<br/>enabled?}
        BRIDGE_OR_DIRECT -- Yes/Hub --> HUB_PATH[place_order_via_bridge<br/>-> OrderRouter pipeline<br/>Capital + Risk + Profit +<br/>Ownership + CircuitBreaker]
        BRIDGE_OR_DIRECT -- No/Legacy --> LEGACY_PATH[risk_manager.place_order<br/>Kill switch + spread limit +<br/>daily loss check]
        HUB_PATH --> BROKER_ENTRY[Angel REST API<br/>placeOrder]
        LEGACY_PATH --> BROKER_ENTRY
    end

    subgraph Exit Flow
        EXIT_FLOW[Generate exit OrderRequest<br/>SELL, qty, reason code]
        EXIT_FLOW --> BRIDGE_OR_DIRECT_EXIT{Hub routing?}
        BRIDGE_OR_DIRECT_EXIT -- Yes --> HUB_EXIT[OrderRouter pipeline<br/>skip entry-only checks]
        BRIDGE_OR_DIRECT_EXIT -- No --> LEGACY_EXIT[risk_manager.place_order]
        HUB_EXIT --> BROKER_EXIT[Angel REST API<br/>placeOrder]
        LEGACY_EXIT --> BROKER_EXIT
    end

    BROKER_ENTRY --> FILL{Fill status}
    BROKER_EXIT --> FILL

    FILL -- Immediate fill --> RECORD_TRADE[Record trade<br/>CSV + BQ + PnL update]
    FILL -- Pending --> LIFECYCLE_POLL[OrderLifecycleService<br/>poll until terminal]
    FILL -- Rejected --> LOG_REJECT[Log + release locks]

    LIFECYCLE_POLL --> TERMINAL{Terminal state}
    TERMINAL -- Filled --> EMIT[_emit_trade<br/>Dedup check<br/>PnL + Ownership update]
    TERMINAL -- Cancelled/Expired --> RELEASE[Release all locks]

    RECORD_TRADE --> UPDATE_STATE[Update authoritative state + DashboardBus<br/>Hub path: PnLEngine + StateStore + Postgres<br/>Legacy path: risk_manager + risk_positions.json]
    EMIT --> UPDATE_STATE
    UPDATE_STATE --> CHECK_KS{Daily loss<br/>exceeded?}
    CHECK_KS -- Yes --> KILL_SWITCH[Activate kill switch<br/>Square off all]
    CHECK_KS -- No --> DONE([Trade complete])
    KILL_SWITCH --> DONE
```

## 13. Data Persistence Architecture

```mermaid
flowchart TD
    subgraph Write Paths
        TRADE([Trade event]) --> CSV_TRADE[TradePersister<br/>trades.csv]
        TRADE --> BQ_TRADE[BQ async writer<br/>BigQuery trades table]
        TRADE --> PG_TRADE[Postgres trade_processed_markers]

        BAR([Indicator bar]) --> CSV_BAR[BarPersister<br/>indicator_bars.csv]
        BAR --> SQLITE_BAR[SQLite indicator DB]
        BAR --> PG_BAR[Postgres indicator_bars]
        BAR --> BQ_BAR[BigQuery indicator table]

        RISK_STATE([Risk state]) --> JSON_FILE[risk_positions.json<br/>Kill switch, positions,<br/>realized PnL]

        PNL([PnL snapshots]) --> FIRESTORE[Firestore<br/>pnl_snapshots collection]
        PNL --> PG_PNL[Postgres pnl_snapshots]

        SWEEP([Sweep state]) --> FS_SWEEP[Firestore sweep_states]
        SWEEP --> PG_SWEEP[Postgres sweep_states]

        ORDER([Order outbox]) --> PG_OUTBOX[Postgres order_submission_outbox]
        ORDER --> MEM_OUTBOX[In-memory fallback<br/>dev/non-LIVE only]

        OWNERSHIP([Position ownership]) --> PG_OWN[Postgres position_ownership]
        OWNERSHIP --> MEM_OWN[In-memory fallback<br/>dev/non-LIVE only]

        AUDIT([Audit events]) --> JSONL[Local JSONL files]
    end

    subgraph Read Paths on Restart
        JSON_FILE --> RESTORE_RISK[Restore kill switch,<br/>open positions, PnL]
        CSV_BAR --> SEED_ENGINE[Seed indicator engine<br/>with historical bars]
        PG_BAR --> SEED_ENGINE
        PG_OUTBOX --> REPLAY_PENDING[Replay PENDING orders]
        FIRESTORE --> RESTORE_PNL[Restore PnL snapshots]
    end
```

## 14. Thread / Process Architecture

```
Process: python -m app.main
├── Main Thread (uvicorn event loop)
│   ├── FastAPI HTTP handlers
│   ├── WebSocket /ws/dashboard
│   ├── Hub asyncio tasks (if multi-hub enabled)
│   │   ├── AccountRunner tasks (one per broker account)
│   │   ├── Subscription watchdog task
│   │   ├── Profit watchdog task
│   │   └── OrderLifecycleService polling task
│   └── Leader lease renewal task
│
├── Thread: stream-worker
│   └── stream_multi_instruments()
│       ├── Thread: ws-runner (WebSocket client)
│       ├── Thread: atm-refresh (ATM strike refresh)
│       ├── Thread: position-sync (Broker reconciliation)
│       └── Thread: stream-event-queue (LatestPerKeyDispatcher)
│           └── Strategy on_bar/on_tick callbacks
│
├── Thread: stream-watchdog
│   └── Monitor stream-worker, restart with backoff
│
├── Thread: bq-async-writer
│   └── Batch flush BigQuery inserts
│
└── Thread: leader-lease-renew (if enabled)
    └── Postgres or Firestore transactional lease renewal
        └── os._exit(2) on lease loss
```
