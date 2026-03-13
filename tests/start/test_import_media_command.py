from types import SimpleNamespace

from click.testing import CliRunner

from src.start.commands import main


def test_import_media_command_validates_source_path_exists():
    runner = CliRunner()
    result = runner.invoke(main, ["import-media", "--source-path", "/not/exist", "--library-id", "1"])

    assert result.exit_code != 0
    assert "does not exist" in result.output


def test_import_media_command_reports_service_validation_error(monkeypatch, tmp_path):
    class FakeMediaImportService:
        def import_from_source(self, source_path: str, library_id: int, progress_callback=None):
            raise ValueError("media_library_not_found")

    runner = CliRunner()
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)

    monkeypatch.setattr("src.start.commands.MediaImportService", FakeMediaImportService)
    result = runner.invoke(
        main,
        ["import-media", "--source-path", str(source_dir), "--library-id", "999"],
    )

    assert result.exit_code != 0
    assert "media_library_not_found" in result.output


def test_import_media_command_prints_job_summary(monkeypatch, tmp_path):
    class FakeMediaImportService:
        def import_from_source(self, source_path: str, library_id: int, progress_callback=None):
            return SimpleNamespace(
                id=7,
                state="completed",
                imported_count=9,
                skipped_count=2,
                failed_count=1,
            )

    runner = CliRunner()
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)

    monkeypatch.setattr("src.start.commands.MediaImportService", FakeMediaImportService)
    result = runner.invoke(
        main,
        ["import-media", "--source-path", str(source_dir), "--library-id", "1"],
    )

    assert result.exit_code == 0
    assert "job_id=7" in result.output
    assert "state=completed" in result.output
    assert "imported=9" in result.output
    assert "skipped=2" in result.output
    assert "failed=1" in result.output


def test_import_media_command_configures_logging_before_running(monkeypatch, tmp_path):
    events = []

    class FakeMediaImportService:
        def import_from_source(self, source_path: str, library_id: int, progress_callback=None):
            events.append("service")
            return SimpleNamespace(
                id=7,
                state="completed",
                imported_count=1,
                skipped_count=0,
                failed_count=0,
            )

    runner = CliRunner()
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)

    monkeypatch.setattr("src.start.commands.configure_logging", lambda: events.append("configure"))
    monkeypatch.setattr("src.start.commands._ensure_database_ready", lambda: events.append("db"))
    monkeypatch.setattr("src.start.commands.MediaImportService", FakeMediaImportService)
    result = runner.invoke(
        main,
        ["import-media", "--source-path", str(source_dir), "--library-id", "1"],
    )

    assert result.exit_code == 0
    assert events == ["configure", "db", "service"]


def test_import_media_command_shows_tqdm_progress(monkeypatch, tmp_path):
    recorded = {"kwargs": None, "updates": [], "descriptions": [], "postfixes": [], "closed": False}

    class FakeTqdm:
        def __init__(self, **kwargs):
            recorded["kwargs"] = kwargs

        def set_description(self, value):
            recorded["descriptions"].append(value)

        def set_postfix(self, value):
            recorded["postfixes"].append(value)

        def update(self, value):
            recorded["updates"].append(value)

        def close(self):
            recorded["closed"] = True

    class FakeMediaImportService:
        def import_from_source(self, source_path: str, library_id: int, progress_callback=None):
            assert progress_callback is not None
            progress_callback({"event": "scan_complete", "total_movies": 2})
            progress_callback(
                {
                    "event": "movie_started",
                    "stage": "metadata",
                    "movie_number": "ABP-123",
                    "imported_count": 0,
                    "skipped_count": 0,
                    "failed_count": 0,
                }
            )
            progress_callback(
                {
                    "event": "movie_finished",
                    "stage": "import-media",
                    "movie_number": "ABP-123",
                    "imported_count": 1,
                    "skipped_count": 0,
                    "failed_count": 0,
                }
            )
            progress_callback(
                {
                    "event": "movie_finished",
                    "stage": "import-media",
                    "movie_number": "ABP-124",
                    "imported_count": 2,
                    "skipped_count": 0,
                    "failed_count": 1,
                }
            )
            return SimpleNamespace(
                id=8,
                state="failed",
                imported_count=2,
                skipped_count=0,
                failed_count=1,
            )

    runner = CliRunner()
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)

    monkeypatch.setattr("src.start.commands.MediaImportService", FakeMediaImportService)
    monkeypatch.setattr("src.start.commands.tqdm", FakeTqdm)
    result = runner.invoke(
        main,
        ["import-media", "--source-path", str(source_dir), "--library-id", "1"],
    )

    assert result.exit_code == 0
    assert "scanning source..." in result.output
    assert recorded["kwargs"]["total"] == 2
    assert recorded["kwargs"]["unit"] == "movie"
    assert recorded["descriptions"] == ["metadata", "import-media", "import-media"]
    assert recorded["updates"] == [1, 1]
    assert recorded["postfixes"][-1]["failed"] == 1
    assert recorded["postfixes"][-1]["movie_number"] == "ABP-124"
    assert recorded["closed"] is True
