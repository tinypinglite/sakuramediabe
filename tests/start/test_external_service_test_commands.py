import json

import pytest
from click.testing import CliRunner

from src.metadata._providers.models import JavdbMovieDetailResource
from src.metadata.provider import MetadataNotFoundError, MetadataRequestError
from src.start.commands import main


@pytest.fixture(autouse=True)
def _patch_command_environment(monkeypatch):
    # 这组命令只测试外部服务探测能力，任何数据库准备调用都应该视为回归。
    def _unexpected_database_prepare():
        raise AssertionError("_ensure_database_ready should not be called")

    monkeypatch.setattr("src.start.commands._ensure_database_ready", _unexpected_database_prepare)
    monkeypatch.setattr("src.start.commands.configure_logging", lambda: None)


def test_test_javdb_command_prints_movie_summary(monkeypatch):
    captured = {}

    class FakeJavdbProvider:
        def get_movie_by_number(self, movie_number: str):
            captured["movie_number"] = movie_number
            return JavdbMovieDetailResource(
                javdb_id="javdb-1",
                movie_number="ABP-123",
                title="Test Movie",
                cover_image="https://example.com/cover.jpg",
                release_date="2024-01-02",
                duration_minutes=120,
                score=4.2,
                watched_count=5,
                want_watch_count=6,
                comment_count=7,
                score_number=8,
                summary="这是 JavDB 简介",
                actors=[],
                tags=[],
                plot_images=[],
            )

    monkeypatch.setattr("src.start.commands.build_javdb_provider", lambda: FakeJavdbProvider())

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["test-javdb", "--movie-number", "ABP-123"],
    )

    assert result.exit_code == 0
    assert "javdb test succeeded" in result.output
    assert "movie_number=ABP-123" in result.output
    assert "title=Test Movie" in result.output
    assert "summary=这是 JavDB 简介" in result.output
    assert captured == {"movie_number": "ABP-123"}


def test_test_javdb_command_outputs_json_success(monkeypatch):
    class FakeJavdbProvider:
        def get_movie_by_number(self, movie_number: str):
            return JavdbMovieDetailResource(
                javdb_id="javdb-2",
                movie_number="IPZZ-001",
                title="JSON Movie",
                cover_image=None,
                release_date="2024-02-03",
                duration_minutes=150,
                summary="结构化 JavDB 简介",
                actors=[],
                tags=[],
                plot_images=[],
            )

    monkeypatch.setattr("src.start.commands.build_javdb_provider", lambda: FakeJavdbProvider())

    runner = CliRunner()
    result = runner.invoke(main, ["test-javdb", "--movie-number", "IPZZ-001", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload == {
        "actors_count": 0,
        "javdb_id": "javdb-2",
        "movie_number": "IPZZ-001",
        "ok": True,
        "release_date": "2024-02-03",
        "service": "javdb",
        "summary": "结构化 JavDB 简介",
        "tags_count": 0,
        "title": "JSON Movie",
    }


def test_test_javdb_command_outputs_json_error_when_provider_fails(monkeypatch):
    class FakeJavdbProvider:
        def get_movie_by_number(self, movie_number: str):
            raise MetadataRequestError("GET", "https://javdb.example/api", "boom")

    monkeypatch.setattr("src.start.commands.build_javdb_provider", lambda: FakeJavdbProvider())

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["test-javdb", "--movie-number", "ABP-123", "--json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["type"] == "metadata_request_error"
    assert payload["error"]["method"] == "GET"
    assert payload["error"]["url"] == "https://javdb.example/api"
