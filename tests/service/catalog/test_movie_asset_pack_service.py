from src.common.media_paths import movie_asset_relative_dir, normalize_asset_dir_name
from src.config.config import settings
from src.model import Image
from src.service.catalog.movie_asset_pack_service import MovieAssetPackService


def test_live_origins_returns_direct_children_for_underscore_number(
    test_db, monkeypatch, tmp_path
):
    image_root = tmp_path / "assets"
    monkeypatch.setattr(settings.media, "import_image_root_path", str(image_root))
    movie_dir = movie_asset_relative_dir(normalize_asset_dir_name("052411_100"))
    scope_dir = image_root / movie_dir
    scope_dir.mkdir(parents=True)
    cover = (scope_dir / "cover.jpg").relative_to(image_root).as_posix()
    plot = (scope_dir / "plot-0.jpg").relative_to(image_root).as_posix()
    nested = (
        scope_dir / "media" / "1" / "thumbnails" / "0.webp"
    ).relative_to(image_root).as_posix()
    other_dir = movie_asset_relative_dir(normalize_asset_dir_name("052411X100"))
    other_scope_dir = image_root / other_dir
    other_scope_dir.mkdir(parents=True)
    other_cover = (other_scope_dir / "cover.jpg").relative_to(image_root).as_posix()
    for origin in (cover, plot, nested, other_cover):
        Image.create(origin=origin)

    assert MovieAssetPackService.live_origins(movie_dir) == sorted([cover, plot])
