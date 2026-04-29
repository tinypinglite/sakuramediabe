from types import SimpleNamespace

from click.testing import CliRunner

from src.api.exception.errors import ApiError
from src.start.commands import main


def test_add_media_library_command_reports_service_validation_error(monkeypatch):
    def fake_create_library(payload):
        raise ApiError(422, "invalid_media_library_root_path", "invalid root path")

    runner = CliRunner()
    monkeypatch.setattr("src.start.commands._ensure_database_ready", lambda: None)
    monkeypatch.setattr("src.start.commands.MediaLibraryService.create_library", fake_create_library)
    result = runner.invoke(
        main,
        ["add-media-library", "--name", "Main", "--root-path", "relative/path"],
    )

    assert result.exit_code != 0
    assert "invalid_media_library_root_path" in result.output


def test_add_media_library_command_prints_created_library(monkeypatch):
    def fake_create_library(payload):
        return SimpleNamespace(id=3, name="Main", root_path="/library/main")

    runner = CliRunner()
    monkeypatch.setattr("src.start.commands._ensure_database_ready", lambda: None)
    monkeypatch.setattr("src.start.commands.MediaLibraryService.create_library", fake_create_library)
    result = runner.invoke(
        main,
        ["add-media-library", "--name", "Main", "--root-path", "/library/main"],
    )

    assert result.exit_code == 0
    assert "library_id=3" in result.output
    assert "name=Main" in result.output
    assert "root_path=/library/main" in result.output
