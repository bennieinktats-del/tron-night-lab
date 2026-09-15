from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOGGER = logging.getLogger("market_mapper")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = REPOSITORY_ROOT / "config" / "config.json"


@dataclass
class MapperConfig:
    config: Dict[str, Any]
    repository_root: Path = REPOSITORY_ROOT

    @property
    def sun_api_base_url(self) -> str:
        return os.getenv("SUN_API_BASE_URL", self.config["sun_api"]["base_url"]).rstrip("/")

    @property
    def tron_api_base_url(self) -> str:
        return os.getenv("TRON_API_BASE_URL", self.config["tron_api"]["base_url"]).rstrip("/")

    @property
    def timeout(self) -> int:
        return int(self.config["sun_api"]["timeout_seconds"])

    @property
    def max_retries(self) -> int:
        return int(self.config["sun_api"]["max_retries"])

    @property
    def output_json(self) -> Path:
        return self._resolve_path(self.config["output"]["json_path"])

    @property
    def output_jsonl(self) -> Path:
        return self._resolve_path(self.config["output"]["jsonl_path"])

    def _resolve_path(self, configured_path: str) -> Path:
        path = Path(configured_path)
        return path if path.is_absolute() else self.repository_root / path


class ApiError(RuntimeError):
    """Raised when a documented API request fails."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> MapperConfig:
    path = path.resolve()
    with path.open("r", encoding="utf-8") as handle:
        return MapperConfig(json.load(handle), repository_root=path.parents[1])


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def build_session(max_retries: int) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=max_retries,
        connect=max_retries,
        read=max_retries,
        status=max_retries,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class RateLimiter:
    def __init__(self, requests_per_second: float) -> None:
        self.minimum_interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self.last_request = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_request
        if elapsed < self.minimum_interval:
            time.sleep(self.minimum_interval - elapsed)
        self.last_request = time.monotonic()


class SunApiClient:
    def __init__(self, config: MapperConfig) -> None:
        self.config = config
        self.session = build_session(config.max_retries)
        rps = float(config.config["sun_api"].get("requests_per_second_limit", 8))
        self.rate_limiter = RateLimiter(rps)

        api_key = os.getenv(config.config["sun_api"].get("api_key_env", "SUN_API_KEY"))
        self.headers: Dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": "tron-night-lab-market-mapper/0.1",
        }
        if api_key:
            self.headers["X-API-KEY"] = api_key

    def get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.config.sun_api_base_url}{endpoint}"
        self.rate_limiter.wait()
        try:
            response = self.session.get(url, params=params, headers=self.headers, timeout=self.config.timeout)
        except requests.RequestException as exc:
            raise ApiError(f"SUN.io request failed: {exc}") from exc

        if not response.ok:
            raise ApiError(f"SUN.io returned HTTP {response.status_code}: {response.text[:300]}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise ApiError("SUN.io returned non-JSON data") from exc

        if payload.get("code") not in (None, 0):
            raise ApiError(f"SUN.io API error: {payload.get('msg') or payload}")
        return payload


class TronApiClient:
    """Minimal read-only TRON API client. Never signs or broadcasts."""

    def __init__(self, config: MapperConfig) -> None:
        self.config = config
        self.session = build_session(int(config.config["tron_api"]["max_retries"]))

    def get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.config.tron_api_base_url}{endpoint}"
        try:
            response = self.session.get(
                url,
                params=params,
                headers={"Accept": "application/json", "User-Agent": "tron-night-lab-market-mapper/0.1"},
                timeout=int(self.config.config["tron_api"]["timeout_seconds"]),
            )
        except requests.RequestException as exc:
            raise ApiError(f"TRON request failed: {exc}") from exc

        if not response.ok:
            raise ApiError(f"TRON returned HTTP {response.status_code}: {response.text[:300]}")

        try:
            return response.json()
        except ValueError as exc:
            raise ApiError("TRON API returned non-JSON data") from exc


def request_record(provider: str, endpoint: str, status: str, error: Optional[str] = None) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "timestamp": utc_now(),
        "record_type": "request_status",
        "source": {"provider": provider, "endpoint": endpoint},
        "status": status,
    }
    if error:
        record["error"] = error
    return record


def normalize_pool(raw: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": raw.get("id"),
        "protocol": raw.get("protocol"),
        "pool_address": raw.get("poolAddress"),
        "pool_type": raw.get("poolType"),
        "contract_index": raw.get("contractIndex"),
        "created_at_ms": raw.get("createBlockTimestamp"),
        "creation_tx_hash": raw.get("createTxHash"),
        "fee_rate": raw.get("feeRate"),
        "protocol_fee_rate": raw.get("protocolFeeRate"),
        "token_addresses": raw.get("tokenAddressList") or [],
        "token_amounts": raw.get("tokenAmountList") or [],
        "token_amount_volume_1d": raw.get("tokenAmountVol1dList") or [],
        "token_names": raw.get("tokenNameList") or [],
        "token_symbols": raw.get("tokenSymbolList") or [],
        "token_decimals": raw.get("tokenDecimalList") or [],
        "token_prices_usd": raw.get("tokenPriceUsdList") or [],
        "swap_rates": raw.get("swapRateList") or [],
        "reserve_usd": raw.get("reserveUsd"),
        "reserve_usd_1d_rate": raw.get("reserveUsd1dRate"),
        "volume_usd_1d": raw.get("volumeUsd1d"),
        "volume_usd_1d_rate": raw.get("volumeUsd1dRate"),
        "volume_usd_7d": raw.get("volumeUsd7d"),
        "volume_usd_7d_rate": raw.get("volumeUsd7dRate"),
        "volume_usd_14d": raw.get("volumeUsd14d"),
        "transaction_count_1d": raw.get("transaction1d"),
        "transaction_count_1d_rate": raw.get("transaction1dRate"),
        "transaction_recent_total": raw.get("transactionRecentTotal"),
        "fee_usd_1d": raw.get("feeUsd1d"),
        "lp_price_usd": raw.get("lpPriceUsd"),
        "farm_apr": raw.get("farmApr"),
        "fee_apr": raw.get("feeApr"),
        "total_apr": raw.get("totalApr"),
        "extra_info": raw.get("extraInfo") or {},
    }


def pool_records(pools: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "timestamp": utc_now(),
            "record_type": "pool",
            "source": {"provider": "sun_io", "endpoint": "/apiv2/pools"},
            "pool": normalize_pool(pool),
        }
        for pool in pools
    ]


def collect_pools(client: SunApiClient, config: MapperConfig) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    mapping = config.config["market_mapping"]
    protocol = mapping.get("protocol", "ALL")
    page_size = int(mapping.get("page_size", 50))
    max_pages = int(mapping.get("max_pages", 5))
    filter_blacklist = not bool(mapping.get("include_blacklisted", False))

    pools: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []

    for page in range(1, max_pages + 1):
        params = {
            "protocol": protocol,
            "pageNo": page,
            "pageSize": page_size,
            "sort": "reserveUsd",
            "desc": "true",
            "filterBlackList": str(filter_blacklist).lower(),
        }
        try:
            payload = client.get("/apiv2/pools", params)
        except ApiError as exc:
            LOGGER.error("Pool page %s failed: %s", page, exc)
            failures.append(request_record("sun_io", "/apiv2/pools", "error", str(exc)))
            continue

        data = payload.get("data") or {}
        page_items = data.get("list") or []
        pools.extend(page_items)
        LOGGER.info("Collected %d pools from page %d", len(page_items), page)

        if not data.get("meta", {}).get("hasMore", False):
            break

    return pool_records(pools), failures


def collect_pool_scan(client: SunApiClient, config: MapperConfig) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    mapping = config.config["market_mapping"]
    protocols = mapping.get("scan_protocols", ["V2", "V3"])
    page_size = min(int(mapping.get("scan_page_size", 25)), 100)

    records: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []

    for protocol in protocols:
        try:
            payload = client.get(
                "/apiv2/pools/scan",
                {"protocol": protocol, "pageSize": page_size, "contractIndex": 0},
            )
            for raw in (payload.get("data") or {}).get("list") or []:
                records.append({
                    "timestamp": utc_now(),
                    "record_type": "pool_scan",
                    "source": {"provider": "sun_io", "endpoint": "/apiv2/pools/scan"},
                    "pool": normalize_pool(raw),
                })
        except ApiError as exc:
            LOGGER.warning("Pool scan for %s failed: %s", protocol, exc)
            failures.append(request_record("sun_io", "/apiv2/pools/scan", "error", str(exc)))

    return records, failures


def unique_token_addresses(pools: Iterable[Dict[str, Any]]) -> List[str]:
    addresses = set()
    for raw in pools:
        for address in raw.get("tokenAddressList") or []:
            if isinstance(address, str) and address:
                addresses.add(address)
    return sorted(addresses)


def chunked(values: List[str], size: int) -> Iterable[List[str]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]


def collect_token_metadata(
    client: SunApiClient,
    token_addresses: List[str],
    config: MapperConfig,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    batch_size = int(config.config["market_mapping"].get("token_batch_size", 50))
    records: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []

    for batch in chunked(token_addresses, batch_size):
        try:
            payload = client.get(
                "/apiv2/tokens",
                {
                    "tokenAddress": ",".join(batch),
                    "protocol": "ALL",
                    "pageNo": 1,
                    "pageSize": len(batch),
                    "sort": "reserveUsd",
                    "filterBlackList": "false",
                },
            )
            for token in (payload.get("data") or {}).get("list") or []:
                records.append({
                    "timestamp": utc_now(),
                    "record_type": "token",
                    "source": {"provider": "sun_io", "endpoint": "/apiv2/tokens"},
                    "token": token,
                })
        except ApiError as exc:
            LOGGER.warning("Token metadata batch failed: %s", exc)
            failures.append(request_record("sun_io", "/apiv2/tokens", "error", str(exc)))

    return records, failures


def collect_pair_records(
    client: SunApiClient,
    config: MapperConfig,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    mapping = config.config["market_mapping"]
    records: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []

    try:
        payload = client.get(
            "/apiv2/pairs",
            {
                "protocol": "V1,V1_5,V2,CURVE",
                "pageNo": 1,
                "pageSize": min(int(mapping.get("pair_page_size", 100)), 200),
                "sort": "updateTime",
                "desc": "true",
            },
        )
        for pair in (payload.get("data") or {}).get("list") or []:
            records.append({
                "timestamp": utc_now(),
                "record_type": "pair",
                "source": {"provider": "sun_io", "endpoint": "/apiv2/pairs"},
                "pair": pair,
            })
    except ApiError as exc:
        LOGGER.warning("Pair collection failed: %s", exc)
        failures.append(request_record("sun_io", "/apiv2/pairs", "error", str(exc)))

    return records, failures


def decimal_to_raw(value: str, decimals: int) -> str:
    amount = Decimal(value)
    scale = Decimal(10) ** decimals
    raw = amount * scale
    if raw != raw.to_integral_value():
        raise ValueError(
            f"Amount {value} cannot be represented using {decimals} decimals"
        )
    if raw <= 0:
        raise ValueError("Amount must be greater than zero")
    return str(int(raw))


def collect_quotes(
    client: SunApiClient,
    config: MapperConfig,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    mapping = config.config["market_mapping"]
    if not mapping.get("quote_enabled", True):
        return [], []

    configured_pairs = mapping.get("quote_pairs") or []
    max_pairs = int(mapping.get("max_quote_pairs", 10))
    human_amount = str(mapping.get("quote_amount_human", "1"))

    records: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []

    for pair in configured_pairs[:max_pairs]:
        try:
            from_decimals = int(pair["from_decimals"])
            to_decimals = int(pair["to_decimals"])
            raw_amount = decimal_to_raw(human_amount, from_decimals)

            params = {
                "fromTokenAddr": pair["from_token"],
                "toTokenAddr": pair["to_token"],
                "inAmount": raw_amount,
                "fromToken": pair.get("from_symbol"),
                "toToken": pair.get("to_symbol"),
                "fromDecimal": from_decimals,
                "toDecimal": to_decimals,
            }
            payload = client.get("/apiv2/quote/swap/routingInV2", params)

            records.append({
                "timestamp": utc_now(),
                "record_type": "quote",
                "source": {
                    "provider": "sun_io",
                    "endpoint": "/apiv2/quote/swap/routingInV2",
                },
                "request": {
                    "from_token": pair["from_token"],
                    "from_symbol": pair.get("from_symbol"),
                    "from_decimals": from_decimals,
                    "to_token": pair["to_token"],
                    "to_symbol": pair.get("to_symbol"),
                    "to_decimals": to_decimals,
                    "input_amount_human": human_amount,
                    "input_amount_raw": raw_amount,
                },
                "routes": payload.get("data") or [],
            })
        except (ApiError, KeyError, ValueError) as exc:
            LOGGER.warning(
                "Quote failed for %s -> %s: %s",
                pair.get("from_symbol", pair.get("from_token")),
                pair.get("to_symbol", pair.get("to_token")),
                exc,
            )
            failures.append(
                request_record(
                    "sun_io",
                    "/apiv2/quote/swap/routingInV2",
                    "error",
                    str(exc),
                )
            )

    return records, failures


def collect_tron_status(client: TronApiClient) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    records: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []

    try:
        payload = client.get("/wallet/getnowblock")
        raw_data = payload.get("block", {}).get("header", {}).get("raw_data", {})
        records.append({
            "timestamp": utc_now(),
            "record_type": "chain_status",
            "source": {"provider": "tron", "endpoint": "/wallet/getnowblock"},
            "chain": {
                "block_number": raw_data.get("number"),
                "block_id": payload.get("blockID"),
                "block_timestamp": raw_data.get("timestamp"),
            },
        })
    except ApiError as exc:
        LOGGER.warning("TRON chain-status request failed: %s", exc)
        failures.append(request_record("tron", "/wallet/getnowblock", "error", str(exc)))

    return records, failures


def write_outputs(
    records: List[Dict[str, Any]],
    failures: List[Dict[str, Any]],
    config: MapperConfig,
) -> None:
    output_json = config.output_json
    output_jsonl = config.output_jsonl
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_jsonl.parent.mkdir(parents=True, exist_ok=True)

    all_records = records + failures
    document = {
        "timestamp": utc_now(),
        "mapper_version": "0.1.0",
        "network": config.config.get("network", "mainnet"),
        "read_only": True,
        "record_count": len(records),
        "failure_count": len(failures),
        "records": records,
        "failures": failures,
    }

    temporary_json = output_json.with_suffix(output_json.suffix + ".tmp")
    with temporary_json.open("w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, ensure_ascii=False)
    temporary_json.replace(output_json)

    temporary_jsonl = output_jsonl.with_suffix(output_jsonl.suffix + ".tmp")
    with temporary_jsonl.open("w", encoding="utf-8") as handle:
        for record in all_records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    temporary_jsonl.replace(output_jsonl)


def run_mapper(config_path: Path = DEFAULT_CONFIG_PATH) -> int:
    configure_logging()

    try:
        config = load_config(config_path)
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        LOGGER.error("Could not load configuration: %s", exc)
        return 1

    LOGGER.info("Starting read-only TRON market mapper")

    sun = SunApiClient(config)
    tron = TronApiClient(config)
    records: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []

    pool_records_result, pool_failures = collect_pools(sun, config)
    records.extend(pool_records_result)
    failures.extend(pool_failures)

    scan_records, scan_failures = collect_pool_scan(sun, config)
    records.extend(scan_records)
    failures.extend(scan_failures)

    raw_pool_like_records = [
        record["pool"] for record in pool_records_result if "pool" in record
    ]
    addresses = unique_token_addresses(raw_pool_like_records)

    LOGGER.info("Discovered %d unique token addresses", len(addresses))

    token_records, token_failures = collect_token_metadata(sun, addresses, config)
    records.extend(token_records)
    failures.extend(token_failures)

    pair_records, pair_failures = collect_pair_records(sun, config)
    records.extend(pair_records)
    failures.extend(pair_failures)

    quote_records, quote_failures = collect_quotes(sun, config)
    records.extend(quote_records)
    failures.extend(quote_failures)

    chain_records, chain_failures = collect_tron_status(tron)
    records.extend(chain_records)
    failures.extend(chain_failures)

    write_outputs(records, failures, config)

    LOGGER.info(
        "Mapper finished: %d records, %d failures",
        len(records),
        len(failures),
    )
    return 0


def main() -> None:
    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CONFIG_PATH
    raise SystemExit(run_mapper(config_path))


if __name__ == "__main__":
    main()
