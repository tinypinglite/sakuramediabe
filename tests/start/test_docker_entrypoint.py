import os
import subprocess
from pathlib import Path


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _build_fake_bin(bin_dir: Path) -> None:
    # 这些桩命令只用于隔离入口脚本的系统副作用，不改动真实用户和 supervisor。
    _write_executable(bin_dir / "id", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(bin_dir / "getent", "#!/usr/bin/env bash\nexit 1\n")
    _write_executable(bin_dir / "usermod", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(bin_dir / "groupmod", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(bin_dir / "useradd", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(
        bin_dir / "su",
        "#!/usr/bin/env bash\n"
        "printf 'su:%s\\n' \"$*\" >> \"$LOG_PATH\"\n"
        "if [ \"${FAIL_MIGRATE:-0}\" = \"1\" ]; then\n"
        "  exit 1\n"
        "fi\n"
        "exit 0\n",
    )
    _write_executable(
        bin_dir / "supervisord",
        "#!/usr/bin/env bash\n"
        "printf 'supervisord:%s\\n' \"$*\" >> \"$LOG_PATH\"\n"
        "exit 0\n",
    )


def _run_entrypoint(tmp_path: Path, *, fail_migrate: bool = False, args: list[str] | None = None):
    repo_root = Path(__file__).resolve().parents[2]
    script_path = repo_root / "docker-entrypoint.sh"
    data_root = tmp_path / "data"
    bin_dir = tmp_path / "bin"
    log_path = tmp_path / "entrypoint.log"
    app_root = tmp_path / "app"

    (data_root / "config").mkdir(parents=True, exist_ok=True)
    (data_root / "config" / "config.toml").write_text("", encoding="utf-8")
    bin_dir.mkdir(parents=True, exist_ok=True)
    app_root.mkdir(parents=True, exist_ok=True)
    _build_fake_bin(bin_dir)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env.get('PATH', '')}",
            "LOG_PATH": str(log_path),
            "FAIL_MIGRATE": "1" if fail_migrate else "0",
            "SAKURAMEDIA_DATA_ROOT": str(data_root),
            "SAKURAMEDIA_APP_ROOT": str(app_root),
            "SAKURAMEDIA_SUPERVISORD_BIN": str(bin_dir / "supervisord"),
            "SAKURAMEDIA_SUPERVISORD_CONFIG": str(tmp_path / "supervisord.conf"),
        }
    )
    command = ["bash", str(script_path), *(args or ["start"])]
    result = subprocess.run(command, capture_output=True, text=True, env=env)
    lines = log_path.read_text(encoding="utf-8").splitlines() if log_path.exists() else []
    return result, lines


def test_docker_entrypoint_runs_migrations_before_starting_supervisor(tmp_path):
    result, lines = _run_entrypoint(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "Running database migrations..." in result.stdout
    assert "Bootstrapping default account and system playlists..." in result.stdout
    assert "Starting supervisor..." in result.stdout
    assert len(lines) == 3
    assert "-m src.start.commands migrate" in lines[0]
    assert "-m src.start.commands initdb" in lines[1]
    assert lines[0].startswith("su:")
    assert lines[1].startswith("su:")
    assert lines[2].startswith("supervisord:")


def test_docker_entrypoint_stops_when_migration_fails(tmp_path):
    result, lines = _run_entrypoint(tmp_path, fail_migrate=True)

    assert result.returncode != 0
    assert "Running database migrations..." in result.stdout
    assert "Bootstrapping default account and system playlists..." not in result.stdout
    assert "Starting supervisor..." not in result.stdout
    assert len(lines) == 1
    assert lines[0].startswith("su:")


def test_docker_entrypoint_passthrough_for_non_start_commands(tmp_path):
    result, lines = _run_entrypoint(tmp_path, args=["echo", "hello"])

    assert result.returncode == 0
    assert result.stdout.strip() == "hello"
    assert lines == []
