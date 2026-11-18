from pathlib import Path

import pytest

from src.service.catalog.movie_image_service import (
    ImagePersistTask,
    MovieImageService,
    should_skip_thin_cover_crop,
)


@pytest.mark.parametrize(
    "movie_number",
    [
        "FC2-4811064",
        "fc2-123",
        "FC2-PPV-3717154",
        "HEYZO-0733",
        "heyzo-0024",
        "tushyraw.19.06.23",
        "Tushyraw.2026.07.19",
        "blackedraw.19.04.12",
        "Blacked.2026.05.13",
        "051325_100",
        "050725-001",
        "130104_597_01",
        "  FC2-4811064  ",
    ],
)
def test_should_skip_thin_cover_crop_hits(movie_number):
    assert should_skip_thin_cover_crop(movie_number) is True


@pytest.mark.parametrize(
    "movie_number",
    [
        "SSIS-001",
        "IPZZ-740",
        "PRED-817",
        "MGNL-046",
        "GANA-2514",
        "UZU-004",
        "TZ-123",
        "0602meyd424",
        "",
    ],
)
def test_should_skip_thin_cover_crop_misses(movie_number):
    assert should_skip_thin_cover_crop(movie_number) is False


def _cover_task(tmp_path) -> ImagePersistTask:
    cover_path = tmp_path / "cover.jpg"
    cover_path.write_bytes(b"cover")
    return ImagePersistTask(
        image_type="cover",
        image_url="",
        relative_path="movies/65/FC2-4811064/cover.jpg",
        absolute_path=cover_path,
    )


def test_downloaded_resolution_skips_split_for_hit_number(tmp_path, monkeypatch):
    service = MovieImageService()
    split_calls = []
    monkeypatch.setattr(
        service,
        "_generate_thin_cover_task_from_cover",
        lambda *args, **kwargs: split_calls.append(args),
    )

    resolution = service.resolve_thin_cover_from_downloaded_images(
        "FC2-4811064", _cover_task(tmp_path), []
    )

    assert split_calls == []
    assert resolution.generated_task is None
    assert resolution.selected_plot_index is None


def test_downloaded_resolution_still_runs_split_for_other_numbers(tmp_path, monkeypatch):
    service = MovieImageService()
    split_calls = []
    monkeypatch.setattr(
        service,
        "_generate_thin_cover_task_from_cover",
        lambda *args, **kwargs: split_calls.append(args),
    )

    service.resolve_thin_cover_from_downloaded_images(
        "SSIS-001", _cover_task(tmp_path), []
    )

    assert len(split_calls) == 1


def test_hit_number_keeps_portrait_plot_fallback(tmp_path, monkeypatch):
    service = MovieImageService()
    monkeypatch.setattr(
        service,
        "_generate_thin_cover_task_from_cover",
        lambda *args, **kwargs: pytest.fail("命中规则的番号不应尝试裁切"),
    )
    monkeypatch.setattr(service, "_select_portrait_plot_index", lambda items: 0)

    resolution = service.resolve_thin_cover_from_downloaded_images(
        "FC2-4811064", _cover_task(tmp_path), []
    )

    assert resolution.generated_task is None
    assert resolution.selected_plot_index == 0


def test_prepared_resolution_skips_split_for_hit_number(monkeypatch):
    service = MovieImageService()
    split_calls = []
    monkeypatch.setattr(
        service,
        "_generate_prepared_thin_cover_from_cover",
        lambda *args, **kwargs: split_calls.append(args),
    )
    cover_task = ImagePersistTask(
        image_type="cover",
        image_url="",
        relative_path="movies/65/FC2-4811064/cover.jpg",
        absolute_path=Path("unused"),
    )

    resolution = service.resolve_thin_cover_from_prepared_images(
        "FC2-4811064", cover_task, [], []
    )

    assert split_calls == []
    assert resolution.generated_task is None
    assert resolution.selected_plot_index is None
