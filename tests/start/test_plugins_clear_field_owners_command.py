from click.testing import CliRunner

from src.start.commands import main


def _invoke(monkeypatch, *args, affected: int = 3):
    runner = CliRunner()
    monkeypatch.setattr("src.start.commands._ensure_database_ready", lambda: None)
    monkeypatch.setattr(
        "src.service.catalog.movie_ownership_gateway.MovieOwnershipGateway.release_plugin_owners",
        lambda plugin_id, fields: affected,
    )
    return runner.invoke(main, ["plugins", "clear-field-owners", *args])


def test_plugins_clear_field_owners_all_fields(monkeypatch):
    result = _invoke(monkeypatch, "--plugin-id", "sakuramedia_x")

    assert result.exit_code == 0
    assert "全部字段接管" in result.output
    assert "sakuramedia_x" in result.output


def test_plugins_clear_field_owners_specific_fields(monkeypatch):
    result = _invoke(
        monkeypatch,
        "--plugin-id",
        "sakuramedia_x",
        "--field",
        "title",
        "--field",
        "summary",
    )

    assert result.exit_code == 0
    assert "title, summary" in result.output


def test_plugins_clear_field_owners_reports_invalid_field(monkeypatch):
    runner = CliRunner()
    monkeypatch.setattr("src.start.commands._ensure_database_ready", lambda: None)
    monkeypatch.setattr(
        "src.service.catalog.movie_ownership_gateway.MovieOwnershipGateway.release_plugin_owners",
        lambda plugin_id, fields: (_ for _ in ()).throw(
            ValueError("非受保护字段: ['heat']")
        ),
    )
    result = runner.invoke(
        main,
        ["plugins", "clear-field-owners", "--plugin-id", "sakuramedia_x", "--field", "heat"],
    )

    assert result.exit_code != 0
    assert "非受保护字段" in result.output
