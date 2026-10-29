"""下载资源黑名单只读查询。"""

from __future__ import annotations

from src.model import DownloadResourceBlacklist, DownloadSubmissionRecord
from src.schema.common.pagination import PageResponse
from src.schema.transfers.downloads import (
    DownloadResourceBlacklistItemResource,
    DownloadResourceBlacklistQuery,
    DownloadResourceBlacklistSubmissionResource,
)


class DownloadResourceBlacklistService:
    @staticmethod
    def list_items(
        query: DownloadResourceBlacklistQuery,
    ) -> PageResponse[DownloadResourceBlacklistItemResource]:
        total = DownloadResourceBlacklist.select().count()
        rows = list(
            DownloadResourceBlacklist.select()
            .order_by(
                DownloadResourceBlacklist.created_at.desc(),
                DownloadResourceBlacklist.id.desc(),
            )
            .paginate(query.page, query.page_size)
        )
        hashes = [row.info_hash for row in rows]
        latest_submissions: dict[str, DownloadSubmissionRecord] = {}
        if hashes:
            for record in (
                DownloadSubmissionRecord.select()
                .where(DownloadSubmissionRecord.info_hash.in_(hashes))
                .order_by(
                    DownloadSubmissionRecord.id.desc(),
                )
            ):
                latest_submissions.setdefault(record.info_hash, record)

        items: list[DownloadResourceBlacklistItemResource] = []
        for row in rows:
            record = latest_submissions.get(row.info_hash)
            latest_submission = None
            if record is not None:
                latest_submission = DownloadResourceBlacklistSubmissionResource(
                    movie_number=record.movie_number,
                    indexer_name=record.indexer_name,
                    title=record.title,
                    client_id=record.client_id,
                    task_id=record.task_id,
                    submitted_at=record.created_at,
                )
            items.append(
                DownloadResourceBlacklistItemResource(
                    info_hash=row.info_hash,
                    blacklisted_at=row.created_at,
                    latest_submission=latest_submission,
                )
            )
        return PageResponse[DownloadResourceBlacklistItemResource](
            items=items,
            page=query.page,
            page_size=query.page_size,
            total=total,
        )
