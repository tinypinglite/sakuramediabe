"""批量写请求统一 1000 条上限的契约测试。"""

import pytest
from pydantic import ValidationError

from src.schema.catalog.actors import ActorMergeRequest
from src.schema.catalog.movies import (
    MovieBlacklistBatchRequest,
    MovieCollectionMarkRequest,
    MovieSubscriptionBatchRequest,
)
from src.schema.catalog.subscriptions import MovieSubscriptionSearchResetRequest
from src.schema.collections.clips import ClipCollectionSetClipsRequest
from src.schema.collections.moments import MomentCollectionSetPointsRequest
from src.schema.collections.playlists import PlaylistMovieBatchAddRequest
from src.schema.common.batch import MAX_BATCH_ITEMS
from src.schema.playback.media import (
    MediaThumbnailResetRequest,
    MultiVersionProcessedRequest,
)
from src.schema.system.activity import NotificationUpdateRequest
from src.schema.system.batch_operations import BatchOperationRequest
from src.schema.transfers.downloads import DownloadTaskBatchImportRequest
from src.schema.transfers.media_transfer import MediaStorageTransferSelectionRequest
from src.schema.videos.collections import VideoCollectionSetVideosRequest


def _int_batch_models():
    ids = list(range(1, MAX_BATCH_ITEMS + 1))
    return [
        BatchOperationRequest(operation="media_delete", ids=ids),
        MediaThumbnailResetRequest(media_ids=ids),
        MediaStorageTransferSelectionRequest(media_ids=ids),
        MultiVersionProcessedRequest(media_ids=ids),
        NotificationUpdateRequest(ids=ids, is_read=True),
        DownloadTaskBatchImportRequest(task_ids=ids),
        ClipCollectionSetClipsRequest(clip_ids=ids),
        MomentCollectionSetPointsRequest(point_ids=ids),
        VideoCollectionSetVideosRequest(
            video_item_ids=ids,
            candidate_id="candidate",
            name="name",
        ),
        ActorMergeRequest(source_actor_ids=ids),
        MovieSubscriptionSearchResetRequest(movie_ids=ids),
    ]


def _string_batch_models():
    numbers = [f"BATCH-{index:04d}" for index in range(1, MAX_BATCH_ITEMS + 1)]
    return [
        MovieCollectionMarkRequest(
            movie_numbers=numbers,
            collection_type="single",
        ),
        MovieSubscriptionBatchRequest(movie_numbers=numbers),
        MovieBlacklistBatchRequest(movie_numbers=numbers),
        PlaylistMovieBatchAddRequest(movie_numbers=numbers),
    ]


def test_batch_models_accept_exactly_1000_items():
    assert len(_int_batch_models()) == 11
    assert len(_string_batch_models()) == 4


@pytest.mark.parametrize(
    "factory",
    [
        lambda size: BatchOperationRequest(
            operation="media_delete",
            ids=list(range(1, size + 1)),
        ),
        lambda size: DownloadTaskBatchImportRequest(
            task_ids=list(range(1, size + 1)),
        ),
        lambda size: MovieSubscriptionBatchRequest(
            movie_numbers=[f"BATCH-{index:04d}" for index in range(1, size + 1)],
        ),
        lambda size: ClipCollectionSetClipsRequest(
            clip_ids=list(range(1, size + 1)),
        ),
    ],
)
def test_batch_models_reject_1001_items(factory):
    with pytest.raises(ValidationError):
        factory(MAX_BATCH_ITEMS + 1)
