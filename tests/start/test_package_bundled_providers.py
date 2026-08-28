import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

PLUGIN_IDS = ("sakuramedia_local_provider", "sakuramedia_115_provider")


def _write_source(root: Path, plugin_id: str) -> None:
    root.mkdir(parents=True)
    (root / "manifest.json").write_text(
        json.dumps({"plugin_id": plugin_id}), encoding="utf-8"
    )
    (root / "__init__.py").write_text("value = 1\n", encoding="utf-8")
    (root / "plugin.py").write_text("value = 2\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_plugin.py").write_text("ignored\n", encoding="utf-8")
    (root / "data").mkdir()
    (root / "data" / "secret").write_text("ignored\n", encoding="utf-8")


def test_package_bundled_providers_is_deterministic_and_excludes_runtime_data(tmp_path):
    sources = tmp_path / "sources"
    local = sources / "local"
    cloud115 = sources / "cloud115"
    _write_source(local, PLUGIN_IDS[0])
    _write_source(cloud115, PLUGIN_IDS[1])
    output = tmp_path / "output"
    script = (
        Path(__file__).resolve().parents[2]
        / "docker"
        / "backend"
        / "package_bundled_providers.py"
    )
    command = [
        sys.executable,
        str(script),
        "--local",
        str(local),
        "--cloud115",
        str(cloud115),
        "--output",
        str(output),
    ]

    subprocess.run(command, check=True)
    first_bytes = {path.name: path.read_bytes() for path in output.iterdir()}
    subprocess.run(command, check=True)
    assert {path.name: path.read_bytes() for path in output.iterdir()} == first_bytes

    index = json.loads((output / "official-providers.json").read_text(encoding="utf-8"))
    assert {item["plugin_id"] for item in index["plugins"]} == set(PLUGIN_IDS)
    for item in index["plugins"]:
        archive_path = output / item["filename"]
        assert hashlib.sha256(archive_path.read_bytes()).hexdigest() == item["sha256"]
        with zipfile.ZipFile(archive_path) as archive:
            names = set(archive.namelist())
        assert {"manifest.json", "__init__.py", "plugin.py"} <= names
        assert not any(name.startswith(("tests/", "data/")) for name in names)
