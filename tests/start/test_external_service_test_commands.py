import json

import pytest
from click.testing import CliRunner

from src.metadata.provider import MetadataNotFoundError, MetadataRequestError
from sakuramedia_metadata_providers.models import JavdbMovieDetailResource
from src.start.commands import main
from src.service.catalog.movie_desc_translation_client import MovieDescTranslationClientError


@pytest.fixture(autouse=True)
def _patch_command_environment(monkeypatch):
    # 这组命令只测试外部服务探测能力，任何数据库准备调用都应该视为回归。
    def _unexpected_database_prepare():
        raise AssertionError("_ensure_database_ready should not be called")

    monkeypatch.setattr("src.start.commands._ensure_database_ready", _unexpected_database_prepare)
    monkeypatch.setattr("src.start.commands.configure_logging", lambda: None)


def test_test_trans_command_prints_translation_result(monkeypatch):
    captured = {}

    class FakeTranslationClient:
        def __init__(self, *, base_url=None, api_key=None, model=None, **kwargs):
            captured["init"] = {
                "base_url": base_url,
                "api_key": api_key,
                "model": model,
            }
            self.base_url = base_url or "http://default-llm:8000"
            self.model = model or "translator-v1"

        def translate(self, *, system_prompt: str, source_text: str) -> str:
            captured["translate"] = {
                "system_prompt": system_prompt,
                "source_text": source_text,
            }
            return "你好，世界"

    monkeypatch.setattr("src.start.commands.MovieDescTranslationClient", FakeTranslationClient)

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "test-trans",
            "--text",
            "hello world",
            "--base-url",
            "http://llm.internal:9000",
            "--api-key",
            "secret-token",
            "--model",
            "translator-v2",
        ],
    )

    assert result.exit_code == 0
    assert "translation test succeeded" in result.output
    assert "translated_text=你好，世界" in result.output
    assert captured["init"] == {
        "base_url": "http://llm.internal:9000",
        "api_key": "secret-token",
        "model": "translator-v2",
    }
    assert captured["translate"]["source_text"] == "hello world"


def test_test_trans_command_reads_text_and_prompt_from_files(monkeypatch, tmp_path):
    captured = {}

    class FakeTranslationClient:
        def __init__(self, *, base_url=None, api_key=None, model=None, **kwargs):
            self.base_url = base_url or "http://default-llm:8000"
            self.model = model or "translator-v1"

        def translate(self, *, system_prompt: str, source_text: str) -> str:
            captured["system_prompt"] = system_prompt
            captured["source_text"] = source_text
            return "文件译文"

    source_file = tmp_path / "source.txt"
    prompt_file = tmp_path / "prompt.txt"
    source_file.write_text("raw text", encoding="utf-8")
    prompt_file.write_text("custom prompt", encoding="utf-8")
    monkeypatch.setattr("src.start.commands.MovieDescTranslationClient", FakeTranslationClient)

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "test-trans",
            "--text-file",
            str(source_file),
            "--prompt-file",
            str(prompt_file),
        ],
    )

    assert result.exit_code == 0
    assert "translated_text=文件译文" in result.output
    assert captured == {
        "system_prompt": "custom prompt",
        "source_text": "raw text",
    }


def test_test_trans_command_rejects_multiple_text_inputs():
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "test-trans",
            "--text",
            "inline text",
            "--text-file",
            __file__,
        ],
    )

    assert result.exit_code != 0
    assert "must provide exactly one of --text or --text-file" in result.output


def test_test_trans_command_outputs_json_error_for_translation_failure(monkeypatch):
    class FakeTranslationClient:
        def __init__(self, *, base_url=None, api_key=None, model=None, **kwargs):
            self.base_url = base_url or "http://default-llm:8000"
            self.model = model or "translator-v1"

        def translate(self, *, system_prompt: str, source_text: str) -> str:
            raise MovieDescTranslationClientError(429, "rate_limit", "too many requests")

    monkeypatch.setattr("src.start.commands.MovieDescTranslationClient", FakeTranslationClient)

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "test-trans",
            "--text",
            "hello",
            "--json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["message"] == "too many requests"
    assert payload["error"]["status_code"] == 429
    assert payload["error"]["error_code"] == "rate_limit"


def test_test_trans_command_outputs_json_success(monkeypatch):
    class FakeTranslationClient:
        def __init__(self, *, base_url=None, api_key=None, model=None, **kwargs):
            self.base_url = base_url or "http://default-llm:8000"
            self.model = model or "translator-v1"

        def translate(self, *, system_prompt: str, source_text: str) -> str:
            return "结构化译文"

    monkeypatch.setattr("src.start.commands.MovieDescTranslationClient", FakeTranslationClient)

    runner = CliRunner()
    result = runner.invoke(main, ["test-trans", "--text", "hello", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload == {
        "base_url": "http://default-llm:8000",
        "model": "translator-v1",
        "ok": True,
        "service": "translation",
        "source_text": "hello",
        "system_prompt": "请将用户提供的文本翻译成简体中文，只返回译文，不要添加任何解释。",
        "translated_text": "结构化译文",
    }


def test_test_javdb_command_prints_movie_summary_and_uses_proxy_flag(monkeypatch):
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

    def fake_build_javdb_provider(*, use_metadata_proxy: bool = False):
        captured["use_metadata_proxy"] = use_metadata_proxy
        return FakeJavdbProvider()

    monkeypatch.setattr("src.start.commands.build_javdb_provider", fake_build_javdb_provider)

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["test-javdb", "--movie-number", "ABP-123", "--use-metadata-proxy"],
    )

    assert result.exit_code == 0
    assert "javdb test succeeded" in result.output
    assert "movie_number=ABP-123" in result.output
    assert "title=Test Movie" in result.output
    assert "summary=这是 JavDB 简介" in result.output
    assert captured == {
        "use_metadata_proxy": True,
        "movie_number": "ABP-123",
    }


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

    monkeypatch.setattr("src.start.commands.build_javdb_provider", lambda **kwargs: FakeJavdbProvider())

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
        "use_metadata_proxy": False,
    }


def test_test_javdb_command_outputs_json_error_when_provider_fails(monkeypatch):
    class FakeJavdbProvider:
        def get_movie_by_number(self, movie_number: str):
            raise MetadataRequestError("GET", "https://javdb.example/api", "boom")

    monkeypatch.setattr("src.start.commands.build_javdb_provider", lambda **kwargs: FakeJavdbProvider())

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


def test_test_dmm_command_prints_description(monkeypatch):
    class FakeDmmProvider:
        def get_movie_desc(self, movie_number: str) -> str:
            assert movie_number == "ABP-123"
            return "这是 DMM 简介"

    monkeypatch.setattr("src.start.commands.build_dmm_provider", lambda: FakeDmmProvider())

    runner = CliRunner()
    result = runner.invoke(main, ["test-dmm", "--movie-number", "ABP-123"])

    assert result.exit_code == 0
    assert "dmm test succeeded" in result.output
    assert "description=这是 DMM 简介" in result.output


def test_test_dmm_command_outputs_json_success(monkeypatch):
    class FakeDmmProvider:
        def get_movie_desc(self, movie_number: str) -> str:
            return "结构化简介"

    monkeypatch.setattr("src.start.commands.build_dmm_provider", lambda: FakeDmmProvider())

    runner = CliRunner()
    result = runner.invoke(main, ["test-dmm", "--movie-number", "IPZZ-001", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload == {
        "description": "结构化简介",
        "movie_number": "IPZZ-001",
        "ok": True,
        "service": "dmm",
    }


def test_test_dmm_command_exits_non_zero_when_movie_not_found(monkeypatch):
    class FakeDmmProvider:
        def get_movie_desc(self, movie_number: str) -> str:
            raise MetadataNotFoundError("movie_desc", movie_number)

    monkeypatch.setattr("src.start.commands.build_dmm_provider", lambda: FakeDmmProvider())

    runner = CliRunner()
    result = runner.invoke(main, ["test-dmm", "--movie-number", "MISS-404"])

    assert result.exit_code != 0
    assert "movie_desc not found: MISS-404" in result.output
