"""All HTTP is mocked. Env files contain invented test credentials only."""

import base64
import hashlib
import hmac
import logging
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from okx_bot import live_connection as live

MOCK_ENV = """OKX_LIVE_API_KEY=mock-live-key
OKX_LIVE_API_SECRET=mock-live-secret
OKX_LIVE_API_PASSPHRASE='mock-live-passphrase'
OKX_BASE_URL=https://eea.okx.com
OKX_DEMO=0
LIVE_TRADING_ENABLED=false
"""
PRIVATE_MARKERS = ("mock-live-key", "mock-live-secret", "mock-live-passphrase", "private-uid")


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    # If any checker test forgets MockTransport, fail before network or DNS access.
    async def no_real_http(*args, **kwargs):
        pytest.fail("Live checker tests must not use a real HTTP transport")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_real_http)
    monkeypatch.setattr(live, "REQUEST_INTERVAL", 0)


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / "mock-live.env"
    path.write_text(MOCK_ENV, encoding="utf-8")
    return path


@pytest.fixture
def config(env_file):
    return live.load_live_config(env_file)


def envelope(rows):
    return httpx.Response(200, json={"code": "0", "msg": "", "data": rows})


def wire(*, empty=False, mode="2", position_mode="net_mode", custom=None):
    seen = []

    def handle(request):
        seen.append(request)
        assert request.method == "GET"
        assert not request.content
        assert "x-simulated-trading" not in request.headers
        assert str(request.url).startswith("https://eea.okx.com/api/v5/")
        prehash = request.headers["OK-ACCESS-TIMESTAMP"] + "GET" + request.url.raw_path.decode()
        expected = base64.b64encode(
            hmac.new(b"mock-live-secret", prehash.encode(), hashlib.sha256).digest()
        ).decode()
        assert request.headers["OK-ACCESS-SIGN"] == expected
        assert request.headers["OK-ACCESS-KEY"] == "mock-live-key"
        assert request.headers["OK-ACCESS-PASSPHRASE"] == "mock-live-passphrase"
        if custom:
            response = custom(request)
            if response is not None:
                return response
        match request.url.path:
            case live.CONFIG:
                return envelope([{"acctLv": mode, "posMode": position_mode, "uid": "private-uid"}])
            case live.BALANCE:
                return envelope(
                    [
                        {
                            "totalEq": "0" if empty else "1200.50",
                            "details": [] if empty else [{"ccy": "USDT", "availBal": "900.25"}],
                        }
                    ]
                )
            case live.POSITIONS:
                return envelope([] if empty else [{"pos": "0"}, {"pos": "2"}, {"pos": "-3"}])
            case live.ORDERS:
                return envelope([] if empty else [{"ordId": "300"}])
            case live.ALGOS:
                kind = request.url.params["ordType"]
                order_id = "400" if kind == "conditional" else "401"
                return envelope([] if empty else [{"algoId": order_id, "ordType": kind}])
        pytest.fail("Unexpected endpoint")

    return httpx.MockTransport(handle), seen


async def test_success_signed_live_reads_and_safe_summary(config):
    transport, seen = wire()
    result = await live.check_live_connection(config, transport=transport)
    assert result.connected
    assert result.equity == Decimal("1200.50")
    assert result.available == Decimal("900.25")
    assert (result.positions, result.orders, result.tpsl) == (2, 1, 2)
    assert len(seen) == 6
    assert {r.url.path for r in seen} == live.READ_PATHS
    assert all("instType" not in r.url.params for r in seen)
    output = result.render()
    assert "LIVE API: CONNECTED" in output
    assert "Gerçek emir gönderme: DISABLED" in output
    assert "Açık pozisyon sayısı: 2\n" in output
    for marker in PRIVATE_MARKERS:
        assert marker not in output


async def test_empty_unfunded_live_account_is_success(config):
    transport, _ = wire(empty=True)
    result = await live.check_live_connection(config, transport=transport)
    assert result.connected
    assert (result.equity, result.available, result.positions, result.orders, result.tpsl) == (
        0,
        0,
        0,
        0,
        0,
    )


async def test_explicit_zero_usdt_balance_is_success(config):
    def custom(request):
        if request.url.path == live.BALANCE:
            return envelope([{"totalEq": "0", "details": [{"ccy": "USDT", "availBal": "0"}]}])

    transport, _ = wire(empty=True, custom=custom)
    assert (await live.check_live_connection(config, transport=transport)).connected


@pytest.mark.parametrize("position_mode", ["net_mode", "long_short_mode"])
async def test_supported_position_modes(config, position_mode):
    transport, _ = wire(position_mode=position_mode)
    assert (await live.check_live_connection(config, transport=transport)).connected


@pytest.mark.parametrize(
    "status,code,expected",
    [
        (200, "50111", "50111"),
        (401, "50111", "50111"),
        (401, None, "AUTH"),
        (403, None, "PERMISSION"),
        (401, "50110", "50110"),
        (401, "50101", "50101"),
        (200, "50101", "50101"),
        (401, "50119", "50119"),
        (401, "50105", "50105"),
        (401, "50113", "50113"),
        (401, "50102", "50102"),
        (200, "50120", "50120"),
        (429, None, "RATE_LIMIT"),
        (503, None, "HTTP"),
        (200, "private-uid", "PROVIDER"),
    ],
)
async def test_safe_auth_ip_environment_and_http_errors(config, status, code, expected):
    response = httpx.Response(
        status,
        json={
            "code": code,
            "msg": " ".join(PRIVATE_MARKERS),
            "data": [],
            "uid": "private-uid",
            "headers": {"OK-ACCESS-SIGN": "private-signature"},
        },
    )
    seen = []

    def handle(request):
        seen.append(request)
        return response

    result = await live.check_live_connection(config, transport=httpx.MockTransport(handle))
    assert not result.connected
    assert result.error.code == expected
    assert result.error.http_status == status
    assert len(seen) == 1  # No fallback to Demo, other regions, retries or mutations.
    output = result.render()
    assert f"HTTP: {status}" in output
    for marker in (*PRIVATE_MARKERS, "private-signature", "OK-ACCESS-SIGN"):
        assert marker not in output


@pytest.mark.parametrize("mode", ["1", "3", "4", "private-uid"])
async def test_account_mode_mismatch_stops_without_changing_account(config, mode):
    transport, seen = wire(mode=mode)
    result = await live.check_live_connection(config, transport=transport)
    assert result.error.code == "ACCOUNT_MODE"
    assert not result.connected
    assert len(seen) == 1
    assert "private-uid" not in result.render()


async def test_expected_position_mode_mismatch(config):
    config = config.model_copy(update={"expected_position_mode": "long_short_mode"})
    transport, seen = wire(position_mode="net_mode")
    result = await live.check_live_connection(config, transport=transport)
    assert result.error.code == "POSITION_MODE"
    assert len(seen) == 1


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
@pytest.mark.parametrize("path", [live.CONFIG, "/api/v5/trade/order", "/api/v5/asset/transfer"])
async def test_every_non_get_method_is_blocked_before_transport(config, method, path):
    client = live.LiveReadOnlyClient(config, httpx.MockTransport(lambda _: pytest.fail("Blocked")))
    try:
        with pytest.raises(live.LiveCheckError, match="GET-only"):
            await client.request(method, path)
    finally:
        await client.close()


@pytest.mark.parametrize(
    "path,kwargs",
    [
        ("/api/v5/trade/order", {}),
        ("/api/v5/trade/cancel-order", {}),
        ("/api/v5/trade/cancel-algos", {}),
        ("/api/v5/account/set-leverage", {}),
        ("/api/v5/account/set-position-mode", {}),
        ("/api/v5/asset/transfer", {}),
        ("https://untrusted.example/api/v5/account/config", {}),
        (live.CONFIG + "?anything=1", {}),
        (live.CONFIG, {"payload": {}}),
        (live.CONFIG, {"params": {"action": "trade"}}),
        (live.ALGOS, {"params": {"ordType": "trigger"}}),
        (live.ORDERS, {"params": {"after": "private-uid"}}),
    ],
)
async def test_mutation_paths_payloads_and_parameter_injection_are_blocked(config, path, kwargs):
    client = live.LiveReadOnlyClient(config, httpx.MockTransport(lambda _: pytest.fail("Blocked")))
    try:
        with pytest.raises(live.LiveCheckError, match="GET-only"):
            await client.request("GET", path, **kwargs)
    finally:
        await client.close()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"method": "POST", "url": live.CONFIG},
        {"method": "GET", "url": "https://www.okx.com/api/v5/account/config"},
        {"method": "GET", "url": live.CONFIG, "headers": {"x-simulated-trading": "1"}},
        {"method": "GET", "url": live.ORDERS + "?limit=100&limit=100"},
        {"method": "GET", "url": live.CONFIG, "content": b"{}"},
    ],
)
async def test_last_transport_boundary_also_blocks_unsafe_direct_use(config, kwargs):
    client = live.LiveReadOnlyClient(config, httpx.MockTransport(lambda _: pytest.fail("Blocked")))
    try:
        with pytest.raises(live.LiveCheckError):
            await client._http.request(**kwargs)
    finally:
        await client.close()


@pytest.mark.parametrize(
    "update,error",
    [
        ({"demo": True}, "ENVIRONMENT"),
        ({"live_trading_enabled": True}, "LIVE_GATE"),
        ({"base_url": "https://eea.okx.com.evil.example"}, "DOMAIN"),
    ],
)
def test_client_revalidates_unvalidated_config_copies(config, update, error):
    with pytest.raises(live.LiveCheckError) as caught:
        live.LiveReadOnlyClient(config.model_copy(update=update))
    assert caught.value.code == error


@pytest.mark.parametrize("domain", sorted(live.LIVE_HOSTS))
async def test_allowlisted_regions_are_used_exactly(config, domain):
    seen = []

    def handle(request):
        seen.append(str(request.url))
        assert "x-simulated-trading" not in request.headers
        return envelope([])

    client = live.LiveReadOnlyClient(
        config.model_copy(update={"base_url": domain}), httpx.MockTransport(handle)
    )
    try:
        await client.request("GET", live.CONFIG)
        assert seen == [domain + live.CONFIG]
    finally:
        await client.close()


async def test_redirect_never_forwards_credentials(config):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://untrusted.example/"})

    result = await live.check_live_connection(config, transport=httpx.MockTransport(handle))
    assert result.error.code == "REDIRECT" and result.error.http_status == 302
    assert len(seen) == 1


async def test_timeout_is_safe_and_not_retried(config):
    seen = []

    def handle(request):
        seen.append(request)
        raise httpx.ReadTimeout(" ".join(PRIVATE_MARKERS), request=request)

    result = await live.check_live_connection(config, transport=httpx.MockTransport(handle))
    assert result.error.code == "TRANSPORT" and result.error.http_status is None
    assert len(seen) == 1
    assert all(marker not in result.render() for marker in PRIVATE_MARKERS)


@pytest.mark.parametrize(
    "path,bad_rows",
    [
        (live.CONFIG, []),
        (live.CONFIG, [{"acctLv": "2", "posMode": "private-uid"}]),
        (live.BALANCE, []),
        (live.BALANCE, [{"totalEq": "NaN", "details": []}]),
        (live.BALANCE, [{"totalEq": "0", "details": [{"ccy": "USDT", "availBal": "Infinity"}]}]),
        (live.BALANCE, [{"totalEq": "0", "details": ["private-uid"]}]),
        (live.POSITIONS, [{"pos": "private-uid"}]),
        (live.ORDERS, [{"ordId": "private-uid"}]),
    ],
)
async def test_incomplete_untrusted_data_never_becomes_success(config, path, bad_rows):
    transport, _ = wire(custom=lambda req: envelope(bad_rows) if req.url.path == path else None)
    result = await live.check_live_connection(config, transport=transport)
    assert not result.connected
    assert "private-uid" not in result.render()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="private-uid"),
        httpx.Response(200, json={"code": "0", "data": {"uid": "private-uid"}}),
        httpx.Response(200, json={"code": "0", "data": ["private-uid"]}),
        httpx.Response(200, json={"code": "0", "data": [{"sCode": "50111"}]}),
    ],
)
async def test_malformed_envelopes_fail_closed(config, response):
    result = await live.check_live_connection(
        config, transport=httpx.MockTransport(lambda _: response)
    )
    assert not result.connected
    assert "private-uid" not in result.render()


async def test_all_pending_order_and_tpsl_pages_are_counted(config):
    seen_cursors = []

    def custom(request):
        if request.url.path not in {live.ORDERS, live.ALGOS}:
            return None
        assert request.url.params["limit"] == "100"
        cursor = request.url.params.get("after")
        seen_cursors.append(cursor)
        key = "ordId" if request.url.path == live.ORDERS else "algoId"
        offset = 1000 if request.url.params.get("ordType") == "oco" else 0
        ids = range(1000 + offset, 900 + offset, -1) if cursor is None else [900 + offset]
        rows = [{key: str(i)} for i in ids]
        if key == "algoId":
            for row in rows:
                row["ordType"] = request.url.params["ordType"]
        return envelope(rows)

    transport, _ = wire(custom=custom)
    result = await live.check_live_connection(config, transport=transport)
    assert result.connected and result.orders == 101 and result.tpsl == 202
    assert seen_cursors == [None, "901", None, "901", None, "1901"]


async def test_repeated_pagination_fails_instead_of_reporting_a_partial_count(config):
    rows = [{"ordId": str(i)} for i in range(1000, 900, -1)]
    transport, _ = wire(custom=lambda req: envelope(rows) if req.url.path == live.ORDERS else None)
    result = await live.check_live_connection(config, transport=transport)
    assert result.error.code == "PAGINATION"
    assert result.orders is None


@pytest.mark.parametrize(
    "change,error",
    [
        ("OKX_DEMO=1", "ENVIRONMENT"),
        ("LIVE_TRADING_ENABLED=true", "LIVE_GATE"),
        ("OKX_BASE_URL=http://eea.okx.com", "DOMAIN"),
        ("OKX_BASE_URL=https://eea.okx.com/", "DOMAIN"),
        ("OKX_BASE_URL=https://eea.okx.com:443", "DOMAIN"),
        ("OKX_BASE_URL=https://eea.okx.com.evil.example", "DOMAIN"),
        ("OKX_ENVIRONMENT=demo", "ENVIRONMENT"),
        ("OKX_DEMO_API_KEY=mock-demo", "ENVIRONMENT"),
        ("OKX_EXPECTED_ACCOUNT_MODE=1", "ACCOUNT_MODE"),
        ("OKX_EXPECTED_POSITION_MODE=private-uid", "POSITION_MODE"),
        ("OKX_LIVE_API_SECRET=", "CREDENTIALS"),
        ("OKX_LIVE_API_PASSPHRASE='${SECRET_FROM_ENV}'", "CREDENTIALS"),
        ("UNSUPPORTED=private-uid", "ENV_FORMAT"),
    ],
)
def test_unsafe_config_rejected_before_http(tmp_path, change, error):
    key = change.split("=", 1)[0]
    content = "\n".join(line for line in MOCK_ENV.splitlines() if not line.startswith(key + "="))
    path = tmp_path / "mock-invalid.env"
    path.write_text(content + "\n" + change + "\n", encoding="utf-8")
    # Missing values are rejected as credentials; unknown raw values are never echoed.
    with pytest.raises(live.LiveCheckError) as caught:
        live.load_live_config(path)
    assert caught.value.code == error
    assert "private-uid" not in str(caught.value)


def test_no_dotenv_discovery_no_environment_inheritance_no_writes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(MOCK_ENV, encoding="utf-8")
    monkeypatch.setenv("OKX_LIVE_API_SECRET", "must-not-be-read")
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    path = tmp_path / "mock-explicit.env"
    path.write_text(MOCK_ENV, encoding="utf-8")
    before = dict(os.environ)
    config = live.load_live_config(path)
    assert config.api_secret.get_secret_value() == "mock-live-secret"
    assert dict(os.environ) == before
    assert path.read_text(encoding="utf-8") == MOCK_ENV
    missing = MOCK_ENV.replace("OKX_LIVE_API_SECRET=mock-live-secret\n", "")
    path.write_text(missing, encoding="utf-8")
    with pytest.raises(live.LiveCheckError, match="incomplete"):
        live.load_live_config(path)


@pytest.mark.parametrize("generic", [False, True])
def test_explicit_live_file_is_the_only_source_with_conflicting_demo_config(
    tmp_path, monkeypatch, capsys, generic
):
    project = tmp_path / "project"
    project.mkdir()
    demo_env = (
        "OKX_DEMO=1\nLIVE_TRADING_ENABLED=false\nOKX_ENVIRONMENT=demo\n"
        "OKX_API_KEY=mock-demo-key\nOKX_API_SECRET=mock-demo-secret\n"
        "OKX_API_PASSPHRASE=mock-demo-passphrase\n"
        "OKX_DEMO_API_KEY=mock-demo-key\nOKX_BASE_URL=https://www.okx.com\n"
    )
    (project / ".env").write_text(demo_env, encoding="utf-8")
    selected = tmp_path / "secrets" / "mock-live.env"
    selected.parent.mkdir()
    content = MOCK_ENV.replace("OKX_LIVE_API_", "OKX_API_") if generic else MOCK_ENV
    selected.write_text(content, encoding="utf-8")
    monkeypatch.chdir(project)
    for key, value in (line.split("=", 1) for line in demo_env.splitlines()):
        monkeypatch.setenv(key, value)
    # A process-level trading flag cannot affect the file-only read checker either.
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    before = dict(os.environ)
    opened = []
    original_open = Path.open

    def only_selected_file(path, *args, **kwargs):
        assert path.resolve() == selected.resolve(), "An implicit configuration file was opened"
        opened.append(path.resolve())
        return original_open(path, *args, **kwargs)

    transport, seen = wire(empty=True)
    with monkeypatch.context() as guard:
        guard.setattr(Path, "open", only_selected_file)
        assert live.main(["--env-file", str(selected)], transport=transport) == 0
    assert opened == [selected.resolve()]
    assert dict(os.environ) == before
    assert (project / ".env").read_text(encoding="utf-8") == demo_env
    assert selected.read_text(encoding="utf-8") == content
    assert len(seen) == 6 and all(request.method == "GET" for request in seen)
    output = capsys.readouterr()
    assert "LIVE API: CONNECTED" in output.out
    assert "Gerçek emir gönderme: DISABLED" in output.out
    assert output.err == ""
    assert all(marker not in output.out for marker in (*PRIVATE_MARKERS, "mock-demo-key"))


def test_cli_script_uses_this_checkout_instead_of_another_installation(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    foreign = tmp_path / "foreign" / "okx_bot"
    foreign.mkdir(parents=True)
    (foreign / "__init__.py").write_text("", encoding="utf-8")
    (foreign / "live_connection.py").write_text(
        "def main():\n    print('FOREIGN_INSTALLATION_ENVIRONMENT_ERROR')\n    return 17\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", str(foreign.parent))
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "check_live_connection.py"), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0
    assert "GET-only" in result.stdout
    assert "FOREIGN_INSTALLATION" not in result.stdout + result.stderr


@pytest.mark.parametrize("value", ['" 0 "', "' false '"])
def test_non_secret_mode_flags_are_trimmed_without_changing_credentials(env_file, value):
    env_file.write_text(
        MOCK_ENV.replace("OKX_DEMO=0", "OKX_DEMO=" + value).replace(
            "LIVE_TRADING_ENABLED=false", 'LIVE_TRADING_ENABLED=" false "'
        )
        + 'OKX_ENVIRONMENT=" production "\n',
        encoding="utf-8",
    )
    config = live.load_live_config(env_file)
    assert config.demo is False and config.live_trading_enabled is False
    assert config.api_passphrase == SecretStr("mock-live-passphrase")


@pytest.mark.parametrize(
    "content,reason,field",
    [
        (MOCK_ENV.replace("OKX_DEMO=0", "OKX_DEMO=1"), "demo_flag", "OKX_DEMO"),
        (MOCK_ENV + "OKX_ENVIRONMENT=demo\n", "environment_label", "OKX_ENVIRONMENT"),
        (MOCK_ENV + "OKX_DEMO_API_KEY=mock-demo-key\n", "demo_credentials", "OKX_DEMO_API_*"),
        (
            MOCK_ENV.replace("OKX_LIVE_API_", "OKX_API_").replace("OKX_DEMO=0\n", ""),
            "live_declaration",
            "OKX_DEMO=0",
        ),
    ],
)
def test_environment_error_identifies_the_selected_file_condition_without_values(
    env_file, monkeypatch, capsys, content, reason, field
):
    env_file.write_text(content, encoding="utf-8")
    monkeypatch.setattr(live, "LiveReadOnlyClient", lambda *a, **kw: pytest.fail("No HTTP client"))
    with pytest.raises(live.LiveCheckError) as caught:
        live.load_live_config(env_file)
    assert caught.value.reason == reason
    assert live.main(["--env-file", str(env_file)]) == 1
    output = capsys.readouterr()
    assert "ENVIRONMENT" in output.out and field in output.out
    assert "selected file" in output.out
    assert all(marker not in output.out for marker in (*PRIVATE_MARKERS, "mock-demo-key"))
    assert "Gerçek emir gönderme: DISABLED" in output.out and output.err == ""


@pytest.mark.parametrize(
    "extra,reason",
    [
        ("OKX_API_KEY=mock-live-key\n", "credentials_missing"),
        (
            "OKX_API_SECRET=mock-live-secret\nOKX_API_PASSPHRASE=mock-live-passphrase\n",
            "credentials_missing",
        ),
        (
            "OKX_API_KEY=other-mock-key\nOKX_API_SECRET=mock-live-secret\n"
            "OKX_API_PASSPHRASE=mock-live-passphrase\n",
            "credentials_conflict",
        ),
    ],
)
def test_incomplete_and_conflicting_alias_groups_are_rejected_without_merging(
    env_file, capsys, extra, reason
):
    env_file.write_text(MOCK_ENV + extra, encoding="utf-8")
    with pytest.raises(live.LiveCheckError) as caught:
        live.load_live_config(env_file)
    assert caught.value.code == "CREDENTIALS" and caught.value.reason == reason
    assert all(marker not in str(caught.value) for marker in PRIVATE_MARKERS)
    assert capsys.readouterr().out == ""


def test_matching_complete_alias_groups_are_accepted_from_same_file(env_file):
    env_file.write_text(
        MOCK_ENV + "OKX_API_KEY=mock-live-key\nOKX_API_SECRET=mock-live-secret\n"
        "OKX_API_PASSPHRASE=mock-live-passphrase\n",
        encoding="utf-8",
    )
    config = live.load_live_config(env_file)
    assert config.api_key == SecretStr("mock-live-key")


def test_no_valid_process_credentials_can_complete_a_missing_file_group(env_file, monkeypatch):
    monkeypatch.setenv("OKX_LIVE_API_SECRET", "mock-live-secret")
    monkeypatch.setenv("OKX_API_SECRET", "mock-live-secret")
    env_file.write_text(
        MOCK_ENV.replace("OKX_LIVE_API_SECRET=mock-live-secret\n", ""), encoding="utf-8"
    )
    with pytest.raises(live.LiveCheckError) as caught:
        live.load_live_config(env_file)
    assert caught.value.code == "CREDENTIALS" and caught.value.reason == "credentials_missing"


def test_generic_credential_aliases_require_explicit_live_mode(env_file):
    generic = MOCK_ENV.replace("OKX_LIVE_API_", "OKX_API_")
    env_file.write_text(generic, encoding="utf-8")
    assert live.load_live_config(env_file).api_key == SecretStr("mock-live-key")
    env_file.write_text(generic.replace("OKX_DEMO=0\n", ""), encoding="utf-8")
    with pytest.raises(live.LiveCheckError) as caught:
        live.load_live_config(env_file)
    assert caught.value.code == "ENVIRONMENT"


@pytest.mark.parametrize(
    "extra",
    [
        "OKX_LIVE_API_KEY=conflicting-key\n",
        "okx_live_api_key=mock-live-key\n",
        "this is not dotenv\n",
        "OKX_LIVE_BASE_URL=https://us.okx.com\n",
        "OKX_API_KEY=conflicting-key\n",
    ],
)
def test_ambiguous_or_malformed_config_is_rejected(env_file, extra, capsys):
    env_file.write_text(MOCK_ENV + extra, encoding="utf-8")
    with pytest.raises(live.LiveCheckError):
        live.load_live_config(env_file)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_cli_success_prints_only_safe_summary_and_never_changes_file(env_file, capsys):
    transport, _ = wire(empty=True)
    before = env_file.read_bytes()
    assert live.main(["--env-file", str(env_file)], transport=transport) == 0
    captured = capsys.readouterr()
    assert len(captured.out.splitlines()) == 9
    assert "LIVE API: CONNECTED" in captured.out
    assert "Gerçek emir gönderme: DISABLED" in captured.out
    assert captured.err == ""
    assert env_file.read_bytes() == before
    assert all(marker not in captured.out for marker in PRIVATE_MARKERS)


def test_cli_invalid_config_cannot_create_an_http_client(env_file, monkeypatch, capsys):
    env_file.write_text(
        MOCK_ENV.replace("LIVE_TRADING_ENABLED=false", "LIVE_TRADING_ENABLED=true"),
        encoding="utf-8",
    )
    monkeypatch.setattr(live, "LiveReadOnlyClient", lambda *a, **kw: pytest.fail("No HTTP client"))
    assert live.main(["--env-file", str(env_file)]) == 1
    output = capsys.readouterr().out
    assert "LIVE API: FAILED" in output and "LIVE_GATE" in output
    assert "Gerçek emir gönderme: DISABLED" in output


@pytest.mark.parametrize("args", [[], ["--trade"], ["--env-file", "missing-private-uid.env"]])
def test_cli_usage_and_file_errors_never_echo_input(args, capsys):
    assert live.main(args) == 1
    output = capsys.readouterr()
    assert "LIVE API: FAILED" in output.out
    assert "private-uid" not in output.out
    assert output.err == ""


def test_standalone_import_cannot_import_or_start_application_lifecycles():
    root = Path(__file__).resolve().parents[1]
    code = """
import importlib.abc, runpy, sys
blocked = ('okx_bot.app', 'okx_bot.engine', 'okx_bot.db', 'okx_bot.models',
           'okx_bot.telegram', 'okx_bot.exchange', 'okx_bot.config', 'alembic', 'telethon')
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name+'.') for name in blocked):
            raise AssertionError('Application lifecycle import is forbidden')
sys.meta_path.insert(0, Guard())
sys.argv = ['check_live_connection.py', '--help']
runpy.run_path('scripts/check_live_connection.py', run_name='__main__')
"""
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr
    assert "GET-only" in result.stdout


def test_cli_suppresses_debug_child_loggers_and_restores_logging(env_file, capsys, caplog):
    def log_raw_data(request):
        logging.getLogger("httpcore.http11").critical(" ".join(PRIVATE_MARKERS))
        return httpx.Response(401, json={"code": "50110", "msg": " ".join(PRIVATE_MARKERS)})

    before = logging.root.manager.disable
    assert (
        live.main(["--env-file", str(env_file)], transport=httpx.MockTransport(log_raw_data)) == 1
    )
    assert logging.root.manager.disable == before
    output = capsys.readouterr()
    assert "50110" in output.out and output.err == ""
    assert all(marker not in output.out + output.err + caplog.text for marker in PRIVATE_MARKERS)


def test_unexpected_local_error_does_not_print_traceback_or_exception(
    env_file, monkeypatch, capsys
):
    def unexpected(*args, **kwargs):
        raise RuntimeError(" ".join(PRIVATE_MARKERS))

    monkeypatch.setattr(live, "load_live_config", unexpected)
    assert live.main(["--env-file", str(env_file)]) == 1
    output = capsys.readouterr()
    assert "INTERNAL" in output.out and "Traceback" not in output.out
    assert all(marker not in output.out for marker in PRIVATE_MARKERS)
    assert output.err == ""


def test_live_prefix_is_explicit_and_trading_defaults_disabled(env_file):
    content = MOCK_ENV.replace("OKX_DEMO=0\n", "").replace("LIVE_TRADING_ENABLED=false\n", "")
    env_file.write_text(content, encoding="utf-8-sig")
    config = live.load_live_config(env_file)
    assert config.demo is False and config.live_trading_enabled is False


def test_domain_is_mandatory_even_if_present_in_process_env(env_file, monkeypatch):
    monkeypatch.setenv("OKX_BASE_URL", "https://eea.okx.com")
    env_file.write_text(
        MOCK_ENV.replace("OKX_BASE_URL=https://eea.okx.com\n", ""), encoding="utf-8"
    )
    with pytest.raises(live.LiveCheckError) as caught:
        live.load_live_config(env_file)
    assert caught.value.code == "DOMAIN"


@pytest.mark.parametrize(
    "replacement",
    [
        'OKX_LIVE_API_KEY="mock\\nkey"',
        'OKX_LIVE_API_PASSPHRASE="mock\\rpass"',
        'OKX_LIVE_API_SECRET=" mock-live-secret"',
    ],
)
def test_credentials_cannot_inject_headers(env_file, replacement):
    key = replacement.split("=", 1)[0]
    content = "\n".join(line for line in MOCK_ENV.splitlines() if not line.startswith(key + "="))
    env_file.write_text(content + "\n" + replacement, encoding="utf-8")
    with pytest.raises(live.LiveCheckError) as caught:
        live.load_live_config(env_file)
    assert caught.value.code == "CREDENTIALS"


@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "1e99999999", "1e-99999999"])
async def test_untrusted_decimal_bounds_fail_safely(config, value):
    def custom(request):
        if request.url.path == live.BALANCE:
            return envelope([{"totalEq": value, "details": []}])

    transport, _ = wire(custom=custom)
    result = await live.check_live_connection(config, transport=transport)
    assert result.error.code == "RESPONSE" and not result.connected


async def test_partial_tpsl_read_failure_does_not_report_a_partial_count(config):
    def custom(request):
        if request.url.path == live.ALGOS and request.url.params["ordType"] == "oco":
            return httpx.Response(503)

    transport, _ = wire(custom=custom)
    result = await live.check_live_connection(config, transport=transport)
    assert not result.connected and result.tpsl is None


async def test_duplicate_ids_across_algo_types_fail_closed(config):
    def custom(request):
        if request.url.path == live.ALGOS:
            return envelope([{"algoId": "100", "ordType": request.url.params["ordType"]}])

    transport, _ = wire(custom=custom)
    result = await live.check_live_connection(config, transport=transport)
    assert result.error.code == "PAGINATION" and result.tpsl is None


async def test_pagination_cap_cannot_silently_truncate_counts(config):
    pages = 0

    def custom(request):
        nonlocal pages
        if request.url.path == live.ORDERS:
            pages += 1
            return envelope([{"ordId": str(100_000 - pages * 100 - i)} for i in range(100)])

    transport, _ = wire(custom=custom)
    result = await live.check_live_connection(config, transport=transport)
    assert result.error.code == "PAGINATION" and result.orders is None
    assert pages == 100


async def test_environment_proxy_and_tls_overrides_are_not_inherited(config, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://untrusted.example")
    monkeypatch.setenv("SSL_CERT_FILE", "not-a-real-certificate-file")
    client = live.LiveReadOnlyClient(config)
    try:
        assert client._http.trust_env is False
        assert client._http.follow_redirects is False
        assert client._http._transport._pool._ssl_context.verify_mode == 2  # ssl.CERT_REQUIRED
    finally:
        await client.close()
