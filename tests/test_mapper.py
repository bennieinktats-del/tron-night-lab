import json
from pathlib import Path

import pytest

from market_mapper.mapper import (
    ApiError,
    MapperConfig,
    SunApiClient,
    decimal_to_raw,
    load_config,
    normalize_pool,
    write_outputs,
)


def test_decimal_to_raw_six_decimals() -> None:
    assert decimal_to_raw("1", 6) == "1000000"
    assert decimal_to_raw("1.5", 6) == "1500000"
    assert decimal_to_raw("0.000001", 6) == "1"


def test_decimal_to_raw_rejects_excess_precision_and_non_positive() -> None:
    with pytest.raises(ValueError):
        decimal_to_raw("0.0000001", 6)
    with pytest.raises(ValueError):
        decimal_to_raw("0", 6)
    with pytest.raises(ValueError):
        decimal_to_raw("-1", 6)


def test_normalize_pool() -> None:
    raw = {
        "id": 10,
        "protocol": "V2",
        "poolAddress": "TPOOL",
        "poolType": "V2",
        "contractIndex": 123,
        "createBlockTimestamp": 123456789,
        "createTxHash": "abc",
        "feeRate": 0.003,
        "protocolFeeRate": 0,
        "tokenAddressList": ["TOKEN_A", "TOKEN_B"],
        "tokenAmountList": ["1000000", "2000000"],
        "tokenAmountVol1dList": ["100", "200"],
        "tokenNameList": ["Token A", "Token B"],
        "tokenSymbolList": ["A", "B"],
        "tokenDecimalList": [6, 6],
        "tokenPriceUsdList": [1.0, 2.0],
        "swapRateList": ["0.5", "1"],
        "reserveUsd": 3000,
        "volumeUsd1d": 1000,
        "transaction1d": 20,
        "extraInfo": {"liquidity": "123"},
    }

    result = normalize_pool(raw)
    assert result["protocol"] == "V2"
    assert result["pool_address"] == "TPOOL"
    assert result["token_addresses"] == ["TOKEN_A", "TOKEN_B"]
    assert result["token_decimals"] == [6, 6]
    assert result["reserve_usd"] == 3000
    assert result["extra_info"]["liquidity"] == "123"


def test_load_config_from_repository() -> None:
    config = load_config()
    assert config.config["network"] == "mainnet"
    assert config.sun_api_base_url == "https://open.sun.io"
    assert config.output_json == Path(__file__).resolve().parents[1] / "data" / "market_map.json"


def test_mapper_config_properties() -> None:
    config = MapperConfig(
        {
            "sun_api": {"base_url": "https://example.com", "max_retries": 1, "timeout_seconds": 5},
            "tron_api": {"base_url": "https://tron.example.com", "max_retries": 1, "timeout_seconds": 5},
            "output": {"json_path": "data/test.json", "jsonl_path": "data/test.jsonl"},
        },
        repository_root=Path("/tmp/repo"),
    )
    assert config.sun_api_base_url == "https://example.com"
    assert config.tron_api_base_url == "https://tron.example.com"
    assert config.output_json == Path("/tmp/repo/data/test.json")
    assert config.output_jsonl == Path("/tmp/repo/data/test.jsonl")


def test_write_outputs(tmp_path: Path) -> None:
    config = MapperConfig(
        {
            "sun_api": {"base_url": "https://example.com", "max_retries": 0, "timeout_seconds": 5},
            "tron_api": {"base_url": "https://tron.example.com", "max_retries": 0, "timeout_seconds": 5},
            "output": {"json_path": "data/market_map.json", "jsonl_path": "data/market_map.jsonl"},
        },
        repository_root=tmp_path,
    )
    write_outputs([{"record_type": "test", "value": 1}], [{"record_type": "request_status", "status": "error"}], config)
    document = json.loads((tmp_path / "data/market_map.json").read_text(encoding="utf-8"))
    assert document["read_only"] is True
    assert document["record_count"] == 1
    assert document["failure_count"] == 1
    assert len((tmp_path / "data/market_map.jsonl").read_text(encoding="utf-8").splitlines()) == 2


def test_sun_api_error_handling(monkeypatch: pytest.MonkeyPatch) -> None:
    config = MapperConfig(
        {
            "sun_api": {"base_url": "https://example.com", "max_retries": 0, "timeout_seconds": 5, "requests_per_second_limit": 1000},
            "tron_api": {"base_url": "https://tron.example.com", "max_retries": 0, "timeout_seconds": 5},
            "output": {"json_path": "data/a.json", "jsonl_path": "data/a.jsonl"},
        },
        repository_root=Path("/tmp/repo"),
    )
    client = SunApiClient(config)

    class FakeResponse:
        ok = False
        status_code = 503
        text = "service unavailable"

        def json(self):
            return {"code": 500, "msg": "server error"}

    monkeypatch.setattr(client.session, "get", lambda *args, **kwargs: FakeResponse())
    with pytest.raises(ApiError, match="HTTP 503"):
        client.get("/apiv2/pools")


def test_sun_api_json_error_handling(monkeypatch: pytest.MonkeyPatch) -> None:
    config = MapperConfig(
        {
            "sun_api": {"base_url": "https://example.com", "max_retries": 0, "timeout_seconds": 5, "requests_per_second_limit": 1000},
            "tron_api": {"base_url": "https://tron.example.com", "max_retries": 0, "timeout_seconds": 5},
            "output": {"json_path": "data/a.json", "jsonl_path": "data/a.jsonl"},
        },
        repository_root=Path("/tmp/repo"),
    )
    client = SunApiClient(config)

    class FakeResponse:
        ok = True
        status_code = 200
        text = "not json"

        def json(self):
            raise ValueError("bad json")

    monkeypatch.setattr(client.session, "get", lambda *args, **kwargs: FakeResponse())
    with pytest.raises(ApiError, match="non-JSON"):
        client.get("/apiv2/pools")


def test_sun_api_application_error_handling(monkeypatch: pytest.MonkeyPatch) -> None:
    config = MapperConfig(
        {
            "sun_api": {"base_url": "https://example.com", "max_retries": 0, "timeout_seconds": 5, "requests_per_second_limit": 1000},
            "tron_api": {"base_url": "https://tron.example.com", "max_retries": 0, "timeout_seconds": 5},
            "output": {"json_path": "data/a.json", "jsonl_path": "data/a.jsonl"},
        },
        repository_root=Path("/tmp/repo"),
    )
    client = SunApiClient(config)

    class FakeResponse:
        ok = True
        status_code = 200
        text = ""

        def json(self):
            return {"code": 123, "msg": "bad request"}

    monkeypatch.setattr(client.session, "get", lambda *args, **kwargs: FakeResponse())
    with pytest.raises(ApiError, match="bad request"):
        client.get("/apiv2/pools")
