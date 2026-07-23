---
name: build-trading-lab
description: Build or review Automation Foundry's market-data, backtesting, strategy research, simulation, and paper-trading domain. Use for crypto or market adapters, historical data ingestion, strategy interfaces, backtests, walk-forward evaluation, paper brokers, risk metrics, reconciliation, and kill switches. Do not use this skill to promise profit, provide personal financial advice, or enable real-money execution without a separate explicit authorization and safety review.
---

# Build the Trading Research Lab

Build a research and paper-trading system, not a profit promise.

## Non-negotiable boundary

- Keep live execution disabled by default.
- Never place a real order or request a real trading credential unless the user explicitly authorizes that separate phase.
- Never let an LLM directly decide or submit an order. Use deterministic, versioned strategy code.
- Keep trading credentials, workers, tables, queues, and kill switches isolated from other automations.
- If live execution is ever approved, require API keys without withdrawal permission and use minimal capital.

## Research workflow

1. Define the hypothesis, universe, timeframe, benchmark, and expected failure modes.
2. Ingest timestamped market data with provenance and freshness metadata.
3. Normalize symbols, intervals, time zones, corporate or token events, and missing data.
4. Implement a deterministic strategy interface.
5. Backtest with fees, spread, slippage, latency assumptions, and realistic order constraints.
6. Prevent look-ahead bias, survivorship bias, leakage, and optimization on the test set.
7. Run out-of-sample and walk-forward evaluation.
8. Compare against a simple benchmark.
9. Run an internal paper broker before using any exchange sandbox.
10. Add reconciliation, stale-data detection, daily loss limits, position limits, heartbeat, and a manual kill switch.

## Required metrics

Record at least:

- net return after costs;
- maximum drawdown;
- benchmark-relative return;
- profit factor and win rate;
- exposure and turnover;
- realized slippage;
- performance by market regime;
- out-of-sample degradation;
- data gaps and rejected orders.

Do not select a strategy from headline return alone. Reject results that depend on one period, one asset, unrealistic fills, or repeated parameter searching.

## Engineering rules

- Place exchange-specific behavior behind adapters and contract tests.
- Store raw market data immutably and derive normalized datasets reproducibly.
- Version strategies, parameters, datasets, and cost assumptions with every experiment.
- Make order submission idempotent and reconcile uncertain responses before retrying.
- Display paper and live modes with unmistakably different labels and storage namespaces.
- Treat sandbox responses as integration tests, not evidence of profitability.

## Definition of done

A research change is done only when it is reproducible, benchmarked, cost-adjusted, evaluated out of sample, visible in the dashboard, and incapable of reaching real funds by default.

