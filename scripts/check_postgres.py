"""Run migrations and integration tests against a disposable local Docker database.

No Telegram or OKX network requests are made. Existing containers/volumes are untouched.
"""

import argparse
import os
import secrets
import subprocess
import sys
import time
import uuid
from pathlib import Path


def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--docker-restart-image", help="Optional local image; uses only an offline fake"
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    name = f"okx-mvp-check-{uuid.uuid4().hex[:12]}"
    password = secrets.token_hex(24)
    created = False
    try:
        run(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                name,
                "--publish",
                "127.0.0.1::5432",
                "--env",
                f"POSTGRES_PASSWORD={password}",
                "--env",
                "POSTGRES_DB=okx_test",
                "postgres:17-alpine",
            ],
            capture_output=True,
        )
        created = True
        for _ in range(40):
            result = subprocess.run(
                ["docker", "exec", name, "pg_isready", "-U", "postgres"], capture_output=True
            )
            if result.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("Disposable PostgreSQL failed to start")
        port = (
            run(["docker", "port", name, "5432/tcp"], capture_output=True, text=True)
            .stdout.strip()
            .rsplit(":", 1)[1]
        )
        url = f"postgresql+asyncpg://postgres:{password}@127.0.0.1:{port}/okx_test"
        env = {
            **os.environ,
            "DATABASE_URL": url,
            "TEST_POSTGRES_URL": url,
            "TEST_POSTGRES_DISPOSABLE": "okx_test",
        }
        commands = [
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            [sys.executable, "-m", "alembic", "check"],
            [sys.executable, "-m", "pytest", "-q", "-m", "postgres", "tests/test_postgres.py"],
            [sys.executable, "-m", "alembic", "downgrade", "base"],
            [sys.executable, "-m", "alembic", "upgrade", "head"],
        ]
        for command in commands:
            run(command, cwd=root, env=env)
        if args.docker_restart_image:
            from check_docker_restart import check

            check(args.docker_restart_image, name, url)
        print(
            "PostgreSQL migration, schema drift, singleton lock, restart, "
            "and rollback checks passed."
        )
    finally:
        if created:
            run(["docker", "rm", "--force", "--volumes", name], capture_output=True)


if __name__ == "__main__":
    main()
