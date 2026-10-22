"""Export source only; no discovery of credentials, sessions, databases or backups."""

import argparse
import zipfile
from pathlib import Path

ROOT_FILES = {
    "pyproject.toml",
    "uv.lock",
    "alembic.ini",
    "Dockerfile",
    "Dockerfile.live",
    "docker-compose.yml",
    "docker-compose.live.yml",
    ".gitignore",
    ".dockerignore",
    "README.md",
}
SOURCE_DIRS = ("src", "scripts", "migrations", "tests", "docs", "config")
SOURCE_EXTENSIONS = {".py", ".ps1", ".md", ".toml", ".yml", ".yaml"}


def export_source(root: Path, output: Path):
    candidates = [root / name for name in ROOT_FILES]
    for directory in SOURCE_DIRS:
        candidates.extend((root / directory).rglob("*"))
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(candidates):
            relative = path.relative_to(root)
            if path.is_symlink() or any(
                part in {"__pycache__", "secrets", ".artifacts"} for part in relative.parts
            ):
                continue
            if not path.is_file():
                continue
            if (
                str(relative).replace("\\", "/") == "config/live.compose.env"
                or (len(relative.parts) == 1 and path.name in ROOT_FILES)
                or (path.suffix in SOURCE_EXTENSIONS and ".env" not in path.name)
            ):
                archive.write(path, relative.as_posix())


def main():
    parser = argparse.ArgumentParser(description="Source-only Live deployment package")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    export_source(Path(__file__).resolve().parents[1], args.output)
    print("Source-only package created; credentials and runtime data excluded.")


if __name__ == "__main__":
    main()
