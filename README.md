# TRON Night Lab — Market Mapper

Phase 1 of the TRON quantitative trading research system.

`market_mapper` is strictly read-only. It collects market information from SUN.io and a read-only TRON chain-status endpoint, then writes machine-readable JSON/JSONL data.

It does **not** execute trades, build/sign/broadcast transactions, require a wallet/private key, perform arbitrage, copy trading, or flash loans.

## Repository structure

```text
tron-night-lab/
├── README.md
├── requirements.txt
├── .env.example
├── .gitignore
├── pytest.ini
├── config/
│   └── config.json
├── market_mapper/
│   ├── __init__.py
│   └── mapper.py
├── tests/
│   └── test_mapper.py
└── data/
    └── .gitkeep
```

## Install

From the repository root:

```bash
python -m pip install -r requirements.txt
```

## Test

```bash
pytest -q
```

## Run

```bash
python -m market_mapper.mapper
```

or:

```bash
python market_mapper/mapper.py
```

You can also pass a config file:

```bash
python -m market_mapper.mapper config/config.json
```

## Output

A successful run writes:

- `data/market_map.json`
- `data/market_map.jsonl`

The mapper creates the `data/` directory when needed and writes outputs atomically.

## Environment

`.env.example` documents optional environment variables. The mapper does not require a private key or wallet.

## Scope boundary

This repository is Phase 1 only. Do not add Bot 2 or transaction signing/execution to this repository until the Market Mapper is tested and producing reliable data.
