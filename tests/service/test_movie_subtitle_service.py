from pathlib import Path

import pytest

from src.config.config import settings
from src.model import Image, Media, MediaLibrary, Movie, MovieSeries, Subtitle
from src.service.catalog.movie_subtitle_service import MovieSubtitleService


@pytest.fixture()
def movie_subtitle_tables(test_db):
    models = [Image, MovieSeries, Movie, Subtitle, MediaLibrary, Media]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)
    yield test_db
    test_db.drop_tables(list(reversed(models)))


def _create_movie(movie_number: str, javdb_id: str, **kwargs):
    payload = {
        "movie_number": movie_number,
        "javdb_id": javdb_id,
        "title": movie_number,
    }
    payload.update(kwargs)
    return Movie.create(**payload)


def test_get_movie_subtitles_is_read_only(movie_subtitle_tables, tmp_path, monkeypatch):
    subtitle_root = tmp_path / "subtitles"
    monkeypatch.setattr(settings.media, "subtitle_root_path", str(subtitle_root), raising=False)

    movie = _create_movie("ABC-401", "Movie401")
    valid_path = subtitle_root / "ABC-401" / "valid.srt"
    valid_path.parent.mkdir(parents=True)
    valid_path.write_text("subtitle", encoding="utf-8")
    missing_path = subtitle_root / "ABC-401" / "missing.srt"
    Subtitle.create(movie=movie, file_path=str(valid_path))
    Subtitle.create(movie=movie, file_path=str(missing_path))

    result = MovieSubtitleService.get_movie_subtitles("ABC-401")

    assert result.movie_number == "ABC-401"
    assert len(result.items) == 1
    assert result.items[0].file_name == "valid.srt"
    assert Subtitle.select().where(Subtitle.movie == movie).count() == 2


def test_sync_movie_subtitles_rejects_symlink_outside_media_directory(movie_subtitle_tables, tmp_path):
    movie = _create_movie("ABC-402", "Movie402")
    library = MediaLibrary.create(name="Main", root_path=str(tmp_path / "library"))
    outside_subtitle_path = tmp_path / "outside.srt"
    outside_subtitle_path.write_text("secret", encoding="utf-8")
    media_dir = tmp_path / "movie"
    media_dir.mkdir()
    media_path = media_dir / "video.mp4"
    media_path.write_bytes(b"video")
    (media_dir / "linked.srt").symlink_to(outside_subtitle_path)
    Media.create(movie=movie, library=library, path=str(media_path), valid=True)

    result = MovieSubtitleService.sync_movie_subtitles(movie)

    assert result == {
        "created_subtitles": 0,
        "deleted_subtitles": 0,
        "total_subtitles": 0,
    }
    assert Subtitle.select().where(Subtitle.movie == movie).count() == 0


def test_sync_movie_subtitles_discovers_new_sidecar_subtitle(movie_subtitle_tables, tmp_path):
    movie = _create_movie("ABC-403", "Movie403")
    library = MediaLibrary.create(name="Main", root_path=str(tmp_path / "library"))
    media_dir = tmp_path / "movie-403"
    media_dir.mkdir()
    media_path = media_dir / "video.mp4"
    subtitle_path = media_dir / "video.srt"
    media_path.write_bytes(b"video")
    subtitle_path.write_text("subtitle", encoding="utf-8")
    Media.create(movie=movie, library=library, path=str(media_path), valid=True)

    result = MovieSubtitleService.sync_movie_subtitles(movie)
    stored_items = list(Subtitle.select().where(Subtitle.movie == movie))

    assert result == {
        "created_subtitles": 1,
        "deleted_subtitles": 0,
        "total_subtitles": 1,
    }
    assert len(stored_items) == 1
    assert Path(stored_items[0].file_path) == subtitle_path.resolve()


def test_sync_movie_subtitles_deletes_missing_rows(movie_subtitle_tables, tmp_path, monkeypatch):
    subtitle_root = tmp_path / "subtitles"
    monkeypatch.setattr(settings.media, "subtitle_root_path", str(subtitle_root), raising=False)
    movie = _create_movie("ABC-404", "Movie404")
    missing_path = subtitle_root / "ABC-404" / "missing.srt"
    Subtitle.create(movie=movie, file_path=str(missing_path))

    result = MovieSubtitleService.sync_movie_subtitles(movie)

    assert result == {
        "created_subtitles": 0,
        "deleted_subtitles": 1,
        "total_subtitles": 0,
    }
    assert Subtitle.select().where(Subtitle.movie == movie).count() == 0
