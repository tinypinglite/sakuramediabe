"""Build deterministic provider ZIPs and their checksum index for the image."""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

PLUGIN_IDS = (
    "sakuramedia_local_provider",
    "sakuramedia_115_provider",
)
EXCLUDED_PARTS = {
    ".git",
    ".github",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "data",
    "tests",
}


def _package(source: Path, output: Path, plugin_id: str) -> dict[str, str]:
    manifest_path = source / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("plugin_id") != plugin_id or not (source / "__init__.py").is_file():
        raise ValueError(f"invalid provider source: {source}")
    zip_path = output / f"{plugin_id}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            if path.is_dir() or EXCLUDED_PARTS.intersection(relative.parts):
                continue
            info = zipfile.ZipInfo(relative.as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    return {
        "plugin_id": plugin_id,
        "filename": zip_path.name,
        "sha256": hashlib.sha256(zip_path.read_bytes()).hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", required=True, type=Path)
    parser.add_argument("--cloud115", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    for plugin_id in PLUGIN_IDS:
        (args.output / f"{plugin_id}.zip").unlink(missing_ok=True)
    (args.output / "official-providers.json").unlink(missing_ok=True)
    sources = dict(zip(PLUGIN_IDS, (args.local, args.cloud115)))
    plugins = [
        _package(sources[plugin_id], args.output, plugin_id) for plugin_id in PLUGIN_IDS
    ]
    (args.output / "official-providers.json").write_text(
        json.dumps({"version": 1, "plugins": plugins}, indent=2, sort_keys=True),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
