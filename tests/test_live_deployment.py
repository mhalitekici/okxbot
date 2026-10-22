import asyncio
import json
import os
import shutil
import subprocess
import sys
import zipfile
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select

from okx_bot.live_connection import (
    ALGOS,
    BALANCE,
    CONFIG,
    ORDERS,
    POSITIONS,
    LiveReadOnlyClient,
    LiveReadOnlyConfig,
)
from okx_bot.live_deployment import (
    AccountIdentity,
    DeploymentError,
    LiveDeployment,
    TelegramSecrets,
    account_fingerprint,
    enroll_identity,
)
from okx_bot.live_migration import validate_live_database_url
from okx_bot.live_observer import LiveObserver
from okx_bot.live_service import LiveAdmin, create_live_app, report_text
from okx_bot.models import (
    CapitalCycle,
    CapitalSlot,
    DailyReport,
    PersistentSettings,
    TelegramSignal,
    TradeOrder,
)
from scripts.export_live_source import export_source

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = {"uid": "12345678", "mainUid": "87654321", "acctLv": "2", "posMode": "net_mode"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    async def blocked(*args, **kwargs):
        pytest.fail("Live deployment tests must use mock HTTP")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked)
    monkeypatch.setattr("okx_bot.live_connection.REQUEST_INTERVAL", 0)


@pytest.fixture
def deployment():
    return LiveDeployment.from_file(ROOT / "config" / "live.toml")


@pytest.fixture
def live_config():
    return LiveReadOnlyConfig(
        base_url="https://eea.okx.com",
        api_key=SecretStr("test-only-key"),
        api_secret=SecretStr("test-only-secret"),
        api_passphrase=SecretStr("test-only-pass"),
    )


@pytest.fixture
def identity(live_config):
    return AccountIdentity(
        manually_verified=True,
        base_url=live_config.base_url,
        fingerprint=account_fingerprint(ACCOUNT),
        verified_at=datetime.now(UTC) - timedelta(seconds=1),
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )


def mock_wire(*, funded=False, positions=0, failed=False):
    seen = []

    def handle(request):
        seen.append(request)
        assert request.method == "GET" and not request.content
        assert "x-simulated-trading" not in request.headers
        if failed:
            raise httpx.ReadTimeout("do not print test-only-secret", request=request)
        rows = {
            CONFIG: [ACCOUNT],
            BALANCE: [
                {
                    "totalEq": "4000" if funded else "0",
                    "details": [
                        {"ccy": "USDT", "eq": "1500", "availBal": "1300"},
                    ]
                    if funded
                    else [],
                }
            ],
            POSITIONS: [{"pos": str(positions)}] if positions else [],
            ORDERS: [],
            ALGOS: [],
        }
        return httpx.Response(200, json={"code": "0", "data": rows[request.url.path]})

    return httpx.MockTransport(handle), seen


async def make_observer(database, deployment, live_config, identity, **kwargs):
    transport, seen = mock_wire(**kwargs)
    client = LiveReadOnlyClient(live_config, transport)
    observer = LiveObserver(deployment, database, client, identity)
    await observer.initialize()
    return observer, seen


async def test_zero_account_paused_without_cycle_or_orders(
    database, deployment, live_config, identity
):
    observer, seen = await make_observer(database, deployment, live_config, identity)
    try:
        await observer.refresh()
        status = await observer.status()
        assert status["paused"] and status["order_execution"] == "DISABLED"
        assert status["allocation"]["cycle_id"] is None
        assert status["snapshot"]["equity_usdt"] == "0"
        assert len(seen) == 6
        assert "test-only-secret" not in report_text(status)
    finally:
        await observer.client.close()


async def test_live_cycle_uses_live_usdt_equity_not_total_usd(
    database, deployment, live_config, identity
):
    observer, _ = await make_observer(database, deployment, live_config, identity, funded=True)
    try:
        await observer.refresh()
        await observer.refresh()
        async with database.sessions() as session:
            cycles = list((await session.scalars(select(CapitalCycle))).all())
            assert len(cycles) == 1
            cycle = cycles[0]
            assert cycle.environment == cycle.active_key == "live"
            assert cycle.allocation_base_equity == 1500 and cycle.slot_target_margin == 500
            assert await session.scalar(select(func.count()).select_from(CapitalSlot)) == 3
            assert await session.scalar(select(func.count()).select_from(TradeOrder)) == 0
            saved = await session.get(PersistentSettings, 1)
            assert saved.environment == "live"
        assert (await observer.state()).paused
    finally:
        await observer.client.close()


async def test_foreign_positions_are_not_touched_or_allocated(
    database, deployment, live_config, identity
):
    observer, seen = await make_observer(
        database, deployment, live_config, identity, funded=True, positions=1
    )
    try:
        await observer.refresh()
        state = await observer.state()
        assert state.emergency and state.paused and state.reason == "EXTERNAL_EXPOSURE"
        assert (await observer.status())["allocation"]["cycle_id"] is None
        assert all(request.method == "GET" for request in seen)
    finally:
        await observer.client.close()


async def test_outage_and_restart_preserve_emergency(database, deployment, live_config, identity):
    observer, _ = await make_observer(database, deployment, live_config, identity, failed=True)
    try:
        with pytest.raises(DeploymentError, match="CONNECTION_LOST"):
            await observer.refresh()
        await observer.initialize()
        assert (await observer.state()).paused and (await observer.state()).emergency
        observer.client._http._transport = mock_wire()[0]
        await observer.refresh()
        assert observer.healthy and (await observer.state()).emergency
        for command in (observer.request_resume, observer.resume):
            with pytest.raises(DeploymentError, match="EXECUTION_DISABLED"):
                await command(confirmation="pretend-two-step-confirmation")
        assert "test-only" not in str((await observer.status())["reason"])
    finally:
        await observer.client.close()


async def test_demo_database_binding_refused(database, deployment, live_config, identity):
    async with database.sessions.begin() as session:
        session.add(PersistentSettings(id=1, environment="demo", values={}))
    client = LiveReadOnlyClient(live_config, mock_wire()[0])
    try:
        with pytest.raises(DeploymentError, match="DATABASE_GUARD"):
            await LiveObserver(deployment, database, client, identity).initialize()
    finally:
        await client.close()


async def test_signal_dedup_and_daily_report_create_no_intents(
    database, deployment, live_config, identity
):
    from tests.test_mz_parser import message

    observer, _ = await make_observer(database, deployment, live_config, identity)
    try:
        item = replace(
            message(),
            channel_id=deployment.telegram_channel_id,
            topic_id=deployment.telegram_topic_id,
        )
        assert await observer.ingest(item) == "rejected"
        assert await observer.ingest(item) == "duplicate"
        await observer.queue_daily()
        await observer.queue_daily()
        async with database.sessions() as session:
            assert await session.scalar(select(func.count()).select_from(TradeOrder)) == 0
            assert await session.scalar(select(func.count()).select_from(DailyReport)) == 1
            saved = await session.scalar(select(TelegramSignal))
            from okx_bot.parser import ChannelSignalParser

            expected = ChannelSignalParser(deployment.mztradingbot_channel_id, 23576).parse(item)
            assert saved.parsed == expected.model_dump(mode="json")
    finally:
        await observer.client.close()


@pytest.mark.parametrize(
    "change",
    [
        {"live_trading_enabled": True},
        {"paused": False},
        {"allocation_slots": 4},
        {"max_portfolio_risk_percent": "40"},
        {"minimum_free_margin": "10"},
        {"margin_mode": "cross"},
        {"leverage": 20},
    ],
)
def test_live_policy_cannot_relax_safety(change):
    with pytest.raises(ValidationError):
        LiveDeployment(**change)


def test_live_reserve_scales_with_equity(deployment):
    assert deployment.limits(D(100)).minimum_free_margin == 50
    assert deployment.limits(D(1500)).minimum_free_margin == 150
    assert deployment.limits(D(1500)).max_portfolio_risk_percent == 3


def test_live_risk_limit_rejects_without_rewriting_channel_levels(settings, deployment):
    from okx_bot.domain import RiskRejected
    from okx_bot.parser import MZTradingBotParser
    from okx_bot.risk import Balance, Instrument, RiskManager
    from tests.test_mz_parser import CHANNEL, message

    item = message()
    signal = MZTradingBotParser(CHANNEL, 23576).parse(
        message(item.text.replace("%17.33", "%5") + "\nLONG")
    )
    before = signal.model_dump()
    with pytest.raises(RiskRejected) as caught:
        RiskManager(settings).plan(
            signal=signal,
            instrument=Instrument(
                signal.inst_id, "CT-USDT", "4", D(1), D("0.0001"), D(1), D(1), D(100000)
            ),
            balance=Balance(D(1000), D(1000), D(0)),
            market=signal.entry,
            mark=signal.entry,
            fee=D("0.0005"),
            tiers=[{"minSz": "0", "maxSz": "100000", "maxLever": "10", "mmr": "0.005"}],
            open_count=0,
            starting_equity=D(1000),
            daily_loss=D(0),
            target_margin=D(1000) / 3,
            limits=deployment.limits(D(1000)),
        )
    assert caught.value.code == "PORTFOLIO_RISK_LIMIT"
    assert signal.model_dump() == before


@pytest.mark.parametrize(
    "change",
    [
        {"uid": "87654321"},
        {"uid": ""},
        {"mainUid": ""},
    ],
)
def test_subaccount_required(change):
    with pytest.raises(DeploymentError, match="SUBACCOUNT_REQUIRED"):
        account_fingerprint({**ACCOUNT, **change})


@pytest.mark.parametrize(
    "change",
    [
        {"fingerprint": "a" * 64},
        {"base_url": "https://us.okx.com"},
        {"expires_at": datetime.now(UTC) - timedelta(days=1)},
    ],
)
def test_identity_mismatch_stops(identity, live_config, change):
    with pytest.raises(DeploymentError, match="IDENTITY_MISMATCH"):
        identity.model_copy(update=change).verify(live_config, ACCOUNT)


async def test_manual_identity_enrollment_is_get_only_and_contains_no_uid(live_config, tmp_path):
    output = tmp_path / "account.json"
    transport, seen = mock_wire()
    with pytest.raises(DeploymentError, match="MANUAL_SUBACCOUNT"):
        await enroll_identity(live_config, output, "", transport=transport)
    assert seen == [] and not output.exists()
    result = await enroll_identity(live_config, output, "TelegramBot1", transport=transport)
    assert len(seen) == 1 and result.manually_verified
    text = output.read_text(encoding="utf-8")
    assert ACCOUNT["uid"] not in text and "test-only-key" not in text
    with pytest.raises(FileExistsError):
        await enroll_identity(live_config, output, "TelegramBot1", transport=transport)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://okx_live:password@postgres-live:5432/okx_live",
    ],
)
def test_live_db_target_is_explicit(url):
    validate_live_database_url(url)
    for bad in (
        url.replace("/okx_live", "/okx"),
        url.replace("postgres-live", "postgres"),
        url.replace("okx_live:", "okx:"),
    ):
        with pytest.raises(DeploymentError, match="DATABASE_GUARD"):
            validate_live_database_url(bad)


async def test_http_and_telegram_cannot_resume_or_close(
    database, deployment, live_config, identity
):
    observer, _ = await make_observer(database, deployment, live_config, identity)
    secrets = TelegramSecrets(
        telegram_api_id=1,
        telegram_api_hash=SecretStr("mock"),
        telegram_bot_token=SecretStr("mock-token"),
        telegram_admin_id=42,
        admin_api_token=SecretStr("x" * 32),
    )
    app = create_live_app(deployment, secrets, live_config, identity, database)
    app.state.observer = observer
    app.state.listener = SimpleNamespace(ready=False)
    bot = LiveAdmin(secrets, observer)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as http:
            assert (await http.get("/admin/status")).status_code == 401
            for path in ("resume", "confirm", "positions/BTC-USDT-SWAP/close"):
                reply = await http.post(
                    "/admin/" + path, headers={"Authorization": "Bearer " + "x" * 32}
                )
                assert reply.status_code == 409
            assert (await http.get("/health/ready")).status_code == 503
        for command in (
            "/resume",
            "/resume CONFIRM anything",
            "/confirm code",
            "/close BTC-USDT-SWAP",
        ):
            update = {
                "message": {
                    "from": {"id": 42},
                    "chat": {"id": 42, "type": "private"},
                    "text": command,
                }
            }
            assert "EXECUTION_DISABLED" in await bot.command(update)
        assert (await observer.state()).paused
    finally:
        await bot.http.aclose()
        await observer.client.close()


def test_source_export_excludes_all_runtime_credentials(tmp_path):
    source = tmp_path / "source"
    (source / "config").mkdir(parents=True)
    (source / "src").mkdir()
    (source / ".env").write_text("must-not-copy")
    (source / "config" / "live.env").write_text("must-not-copy")
    (source / "src" / "private.env").write_text("must-not-copy")
    (source / "src" / "telegram.session-string").write_text("must-not-copy")
    (source / "config" / "live.compose.env").write_text("COMPOSE_PROJECT_NAME=telegram-okx-live")
    (source / "src" / "service.py").write_text("# safe source")
    output = tmp_path / "package.zip"
    export_source(source, output)
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {"config/live.compose.env", "src/service.py"}


def test_compose_configuration_is_isolated_and_valid_without_running_containers(tmp_path):
    if shutil.which("docker") is None:
        pytest.fail("Docker Compose CLI is required for the requested configuration validation")
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(ROOT / "config" / "live.compose.env"),
            "-f",
            str(ROOT / "docker-compose.live.yml"),
            "-p",
            "telegram-okx-live",
            "--profile",
            "maintenance",
            "--profile",
            "tools",
            "config",
            "--format",
            "json",
        ],
        env={**os.environ, "LOCALAPPDATA": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    compose = json.loads(result.stdout)
    assert compose["name"] == "telegram-okx-live"
    assert "trading-bot" not in compose["services"] and "postgres" not in compose["services"]
    observer = compose["services"]["live-observer"]
    assert observer["command"] == ["serve"] and observer["read_only"]
    assert observer["environment"]["LIVE_TRADING_ENABLED"] == "false"
    assert "env_file" not in observer
    assert all("telegram-okx-demo" not in str(v) for v in compose["volumes"].values())
    assert compose["networks"]["live-backend"]["internal"]
    assert compose["services"]["live-migrate"]["command"] == ["migrate"]


def test_live_entry_point_does_not_import_trading_engine():
    code = """
import importlib.abc, runpy, sys
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        blocked = {'okx_bot.engine','okx_bot.app',
                   'okx_bot.exchange.gateway','okx_bot.exchange.client'}
        if fullname in blocked:
            raise AssertionError('Trading runtime import forbidden')
sys.meta_path.insert(0, Guard())
sys.argv=['live_service.py','--help']
runpy.run_path('scripts/live_service.py',run_name='__main__')
"""
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr
    assert "no OKX order executor" in result.stdout


async def test_live_lifespan_is_paused_with_mock_telegram_and_mock_okx(
    database, deployment, live_config, identity, monkeypatch
):
    import okx_bot.live_service as service

    @asynccontextmanager
    async def sqlite_test_lease():
        yield SimpleNamespace(invalidated=False)

    async def schema_checked(db):
        pass  # Real Alembic/schema and lease behavior is covered by PostgreSQL tests.

    class Listener:
        def __init__(self, *args):
            self.ready = True

        async def run(self):
            await asyncio.Event().wait()

    class Admin:
        def __init__(self, *args):
            pass

        async def poll(self):
            await asyncio.Event().wait()

        async def notifications(self):
            await asyncio.Event().wait()

    monkeypatch.setattr(database, "singleton", sqlite_test_lease)
    monkeypatch.setattr(service, "require_schema", schema_checked)
    monkeypatch.setattr(service, "TelegramListener", Listener)
    monkeypatch.setattr(service, "LiveAdmin", Admin)
    credentials = TelegramSecrets(
        telegram_api_id=1,
        telegram_api_hash=SecretStr("mock"),
        telegram_bot_token=SecretStr("mock"),
        telegram_admin_id=42,
        admin_api_token=SecretStr("x" * 32),
    )
    transport, seen = mock_wire()
    app = service.create_live_app(
        deployment, credentials, live_config, identity, database, transport=transport
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as http:
            response = await http.get("/health/ready")
            assert response.status_code == 200
            assert response.json()["paused"] is True
            assert (await app.state.observer.state()).paused
    assert seen and all(request.method == "GET" for request in seen)


def test_powershell_maintenance_scripts_parse_without_execution():
    if os.name != "nt":
        pytest.skip("Windows PowerShell syntax validation")
    for path in (ROOT / "scripts").glob("*live*.ps1"):
        expression = (
            "$parseErrors=$null; $tokens=$null; "
            "[System.Management.Automation.Language.Parser]::ParseFile('"
            + str(path).replace("'", "''")
            + "',[ref]$tokens,[ref]$parseErrors) | Out-Null; "
            "if ($parseErrors.Count -gt 0) { exit 1 }"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", expression],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, path.name


@pytest.mark.parametrize(
    "args,expected",
    [
        (["up", "-d", "live-observer"], "up -d live-observer"),
        (
            [
                "exec",
                "-T",
                "postgres-live",
                "pg_dump",
                "-U",
                "okx_live",
                "-d",
                "okx_live",
                "-Fc",
                "-f",
                "/tmp/test.dump",
            ],
            "pg_dump -U okx_live -d okx_live -Fc -f /tmp/test.dump",
        ),
        (
            ["run", "--rm", "--no-deps", "live-migrate", "migrate", "--confirm-live-database"],
            "run --rm --no-deps live-migrate migrate --confirm-live-database",
        ),
    ],
)
def test_wrapper_forwards_native_flags_without_running_docker(tmp_path, args, expected):
    if os.name != "nt":
        pytest.skip("Windows PowerShell argument forwarding")
    (tmp_path / "docker.cmd").write_text("@echo off\necho %*\nexit /b 0\n", encoding="ascii")
    env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]}
    result = subprocess.run(
        ["powershell", "-NoProfile", "-File", str(ROOT / "scripts/live_compose.ps1"), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert expected in result.stdout
    assert "--project-name telegram-okx-live" in result.stdout


@pytest.mark.parametrize(
    "args",
    [
        ["run", "--project-name", "telegram-okx-demo", "live-migrate"],
        ["up", "-d", "-f", "docker-compose.yml"],
        ["down", "-v"],
    ],
)
def test_wrapper_rejects_project_switches_and_volume_deletion(tmp_path, args):
    if os.name != "nt":
        pytest.skip("Windows PowerShell argument guard")
    (tmp_path / "docker.cmd").write_text("@echo off\necho UNEXPECTED_DOCKER_CALL\nexit /b 0\n")
    env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]}
    result = subprocess.run(
        ["powershell", "-NoProfile", "-File", str(ROOT / "scripts/live_compose.ps1"), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert "UNEXPECTED_DOCKER_CALL" not in result.stdout
