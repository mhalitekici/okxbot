import os
import subprocess

from okx_bot.config import Settings

Settings().validate_runtime()
subprocess.run(["alembic", "upgrade", "head"], check=True)
os.execvp(
    "uvicorn",
    [
        "uvicorn",
        "okx_bot.app:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
        "--workers",
        "1",
        "--no-access-log",
    ],
)
