"""Download the latest official provider releases for the image build."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from urllib.request import Request, urlopen

PROVIDER_RELEASES = (
    (
        "sakuramedia_local_provider",
        "https://api.github.com/repos/tinypinglite/sakuramedia_local_provider/releases/latest",
    ),
    (
        "sakuramedia_115_provider",
        "https://api.github.com/repos/tinypinglite/sakuramedia_115_provider/releases/latest",
    ),
)


def _request_bytes(url: str) -> bytes:
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "sakuramedia-image-build",
        },
    )
    with urlopen(request) as response:
        return response.read()


def _release_asset(plugin_id: str, release_api_url: str) -> tuple[str, bytes, str]:
    try:
        release = json.loads(_request_bytes(release_api_url))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid latest release response: {plugin_id}") from exc
    if not isinstance(release, dict):
        raise TypeError(f"invalid latest release response: {plugin_id}")

    tag_name = release.get("tag_name")
    assets = release.get("assets")
    if not isinstance(tag_name, str) or not tag_name or not isinstance(assets, list):
        raise ValueError(f"invalid latest release metadata: {plugin_id}")
    version = tag_name.removeprefix("v")
    asset_name = f"{plugin_id}-{version}.zip"
    asset = next(
        (
            item
            for item in assets
            if isinstance(item, dict) and item.get("name") == asset_name
        ),
        None,
    )
    if asset is None:
        raise ValueError(f"latest release ZIP missing: {plugin_id} tag={tag_name}")

    download_url = asset.get("browser_download_url")
    digest = asset.get("digest")
    if (
        not isinstance(download_url, str)
        or not isinstance(digest, str)
        or not digest.startswith("sha256:")
    ):
        raise ValueError(f"latest release ZIP checksum missing: {plugin_id}")
    content = _request_bytes(download_url)
    sha256 = hashlib.sha256(content).hexdigest()
    if sha256 != digest.removeprefix("sha256:").lower():
        raise ValueError(f"latest release ZIP checksum mismatch: {plugin_id}")
    return tag_name, content, sha256


def package_latest_releases(output: Path) -> list[dict[str, str]]:
    downloaded = [
        (plugin_id, *_release_asset(plugin_id, release_api_url))
        for plugin_id, release_api_url in PROVIDER_RELEASES
    ]
    output.mkdir(parents=True, exist_ok=True)
    plugins: list[dict[str, str]] = []
    for plugin_id, tag_name, content, sha256 in downloaded:
        filename = f"{plugin_id}.zip"
        (output / filename).write_bytes(content)
        plugins.append({"plugin_id": plugin_id, "filename": filename, "sha256": sha256})
        print(f"bundled {plugin_id} release={tag_name}")
    (output / "official-providers.json").write_text(
        json.dumps({"version": 1, "plugins": plugins}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return plugins


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    package_latest_releases(args.output)


if __name__ == "__main__":
    main()
