import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


def _load_packager():
    script = (
        Path(__file__).resolve().parents[2]
        / "docker"
        / "backend"
        / "package_bundled_providers.py"
    )
    spec = importlib.util.spec_from_file_location("package_bundled_providers", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_package_bundled_providers_downloads_and_verifies_latest_releases(
    monkeypatch, tmp_path
):
    packager = _load_packager()
    local_zip = b"local provider zip"
    cloud115_zip = b"115 provider zip"
    local_api_url = packager.PROVIDER_RELEASES[0][1]
    cloud115_api_url = packager.PROVIDER_RELEASES[1][1]
    local_download_url = "https://downloads.example/local.zip"
    cloud115_download_url = "https://downloads.example/cloud115.zip"
    responses = {
        local_api_url: json.dumps(
            {
                "tag_name": "v1.2.3",
                "assets": [
                    {
                        "name": "sakuramedia_local_provider-1.2.3.zip",
                        "browser_download_url": local_download_url,
                        "digest": f"sha256:{hashlib.sha256(local_zip).hexdigest()}",
                    }
                ],
            }
        ).encode(),
        cloud115_api_url: json.dumps(
            {
                "tag_name": "v4.5.6",
                "assets": [
                    {
                        "name": "sakuramedia_115_provider-4.5.6.zip",
                        "browser_download_url": cloud115_download_url,
                        "digest": f"sha256:{hashlib.sha256(cloud115_zip).hexdigest()}",
                    }
                ],
            }
        ).encode(),
        local_download_url: local_zip,
        cloud115_download_url: cloud115_zip,
    }
    monkeypatch.setattr(packager, "_request_bytes", responses.__getitem__)

    output = tmp_path / "output"
    plugins = packager.package_latest_releases(output)

    index = json.loads((output / "official-providers.json").read_text(encoding="utf-8"))
    assert index == {"version": 1, "plugins": plugins}
    assert (output / "sakuramedia_local_provider.zip").read_bytes() == local_zip
    assert (output / "sakuramedia_115_provider.zip").read_bytes() == cloud115_zip


def test_package_bundled_providers_rejects_a_release_asset_with_wrong_digest(
    monkeypatch,
):
    packager = _load_packager()
    release_api_url = "https://api.example/releases/latest"
    download_url = "https://downloads.example/local.zip"
    monkeypatch.setattr(
        packager,
        "_request_bytes",
        {
            release_api_url: json.dumps(
                {
                    "tag_name": "v1.2.3",
                    "assets": [
                        {
                            "name": "sakuramedia_local_provider-1.2.3.zip",
                            "browser_download_url": download_url,
                            "digest": f"sha256:{'0' * 64}",
                        }
                    ],
                }
            ).encode(),
            download_url: b"unexpected content",
        }.__getitem__,
    )

    with pytest.raises(ValueError, match="checksum mismatch"):
        packager._release_asset("sakuramedia_local_provider", release_api_url)
