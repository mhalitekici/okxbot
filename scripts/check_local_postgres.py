"""Run isolated PostgreSQL tests using installed binaries, without existing services or volumes."""

import argparse
import os
import socket
import subprocess
import sys
import uuid
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bin", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    directory = root / ".artifacts" / ("postgres-test-" + uuid.uuid4().hex[:12])
    directory.mkdir(parents=True)
    data = directory / "data"
    database = "okx_test_" + uuid.uuid4().hex[:12]
    hidden = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

    def run(command, **kwargs):
        if "pg_ctl" in command[0]:
            # Windows server children can inherit PIPE handles and keep communicate()
            # waiting after pg_ctl exits. Use a real file for this background launcher.
            kwargs.pop("capture_output", None)
            with (directory / "pg_ctl.log").open("ab") as output:
                return subprocess.run(
                    command,
                    check=True,
                    cwd=root,
                    **hidden,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    **kwargs,
                )
        if "capture_output" not in kwargs:
            result = subprocess.run(command, cwd=root, **hidden, capture_output=True, **kwargs)
            output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
            with (directory / "validation.log").open("a", encoding="utf-8") as log:
                log.write(output)
            print(output, flush=True)
            result.check_returncode()
            return result
        return subprocess.run(command, check=True, cwd=root, **hidden, **kwargs)

    def binary(name):
        return str(args.bin / (name + (".exe" if os.name == "nt" else "")))

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    started = False
    try:
        run(
            [
                binary("initdb"),
                "-D",
                str(data),
                "--no-locale",
                "--encoding=UTF8",
                "-A",
                "trust",
                "-U",
                "postgres",
            ],
            capture_output=True,
        )
        run(
            [
                binary("pg_ctl"),
                "-D",
                str(data),
                "-l",
                str(directory / "postgres.log"),
                "-o",
                f"-h 127.0.0.1 -p {port}",
                "-w",
                "start",
            ],
            capture_output=True,
        )
        started = True
        run(
            [binary("createdb"), "-h", "127.0.0.1", "-p", str(port), "-U", "postgres", database],
            capture_output=True,
        )
        url = f"postgresql+asyncpg://postgres@127.0.0.1:{port}/{database}"
        env = {
            **os.environ,
            "DATABASE_URL": url,
            "TEST_POSTGRES_URL": url,
            "TEST_POSTGRES_DISPOSABLE": database,
            "TEST_POSTGRES_BIN": str(args.bin),
        }
        for command in (
            ["alembic", "upgrade", "head"],
            ["alembic", "check"],
            [
                "pytest",
                "-q",
                "-m",
                "postgres",
                "tests/test_postgres.py",
                "tests/test_live_deployment_postgres.py",
                "--tb=short",
            ],
            ["alembic", "downgrade", "base"],
            ["alembic", "upgrade", "head"],
        ):
            run([sys.executable, "-m", *command], env=env)
        print(
            "Isolated PostgreSQL migrations, drift, ownership lock and reservation checks passed."
        )
    finally:
        if started:
            run(
                [binary("pg_ctl"), "-D", str(data), "-m", "fast", "-w", "stop"], capture_output=True
            )
        # Retain only this test cluster for inspection. Never delete or stop other clusters.


if __name__ == "__main__":
    main()
