"""Restart only a generated offline test worker, never a Compose service."""

import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from sqlalchemy.engine import make_url


def run(args):
    return subprocess.run(args, check=True, capture_output=True, text=True)


def check(image, postgres_container, database_url):
    assert postgres_container.startswith("okx-mvp-check-")
    suffix = uuid.uuid4().hex[:12]
    name, database = f"okx-restart-check-{suffix}", f"restart_{suffix}"
    root = Path(__file__).resolve().parents[1]
    created = False
    with tempfile.TemporaryDirectory(prefix="okx-restart-") as directory:
        run(
            [
                "docker",
                "exec",
                postgres_container,
                "psql",
                "-U",
                "postgres",
                "-d",
                "postgres",
                "-c",
                f"CREATE DATABASE {database}",
            ]
        )
        try:
            url = make_url(database_url).set(database=database)
            env = {**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False)}
            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=root, env=env, check=True
            )
            internal = url.set(host="127.0.0.1", port=5432).render_as_string(hide_password=False)
            run(
                [
                    "docker",
                    "run",
                    "--detach",
                    "--name",
                    name,
                    "--network",
                    f"container:{postgres_container}",
                    "--mount",
                    f"type=bind,source={root / 'tests'},target=/app/tests,readonly",
                    "--mount",
                    f"type=bind,source={directory},target=/state",
                    "--env",
                    f"TEST_RESTART_DATABASE_URL={internal}",
                    "--entrypoint",
                    "python",
                    image,
                    "/app/tests/docker_restart_harness.py",
                ]
            )
            created = True

            def wait_phase(phase):
                path = Path(directory) / f"phase{phase}.json"
                deadline = time.monotonic() + 40
                while time.monotonic() < deadline:
                    if path.exists():
                        return json.loads(path.read_text())
                    running = run(["docker", "inspect", "--format", "{{.State.Running}}", name])
                    if running.stdout.strip() != "true":
                        raise RuntimeError(
                            "Offline restart harness stopped: "
                            + run(["docker", "logs", "--tail", "25", name]).stderr
                        )
                    time.sleep(0.25)
                raise RuntimeError(f"Offline restart phase {phase} timed out")

            assert wait_phase(1)["open_and_protected"]
            before = run(["docker", "inspect", "--format", "{{.State.StartedAt}}", name]).stdout
            run(["docker", "restart", "--time", "5", name])
            result = wait_phase(2)
            after = run(["docker", "inspect", "--format", "{{.State.StartedAt}}", name]).stdout
            assert before != after and result["reconciled"] and result["duplicate_blocked"]
            print("DISPOSABLE_DOCKER_RESTART " + json.dumps(result))
        finally:
            if created:
                run(["docker", "rm", "--force", "--volumes", name])
            run(
                [
                    "docker",
                    "exec",
                    postgres_container,
                    "psql",
                    "-U",
                    "postgres",
                    "-d",
                    "postgres",
                    "-c",
                    f"DROP DATABASE {database}",
                ]
            )
