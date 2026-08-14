
from loguru import logger

from src.api.exception.errors import ApiError
from src.common.movie_numbers import parse_movie_number_from_text
from src.config.config import IndexerKind
from src.schema.transfers.downloads import DownloadCandidateResource
from src.service.transfers.downloads.clients.torznab import (
    TorznabClient,
    TorznabClientError,
)
from src.service.transfers.downloads.common import validate_non_empty


class DownloadSearchService:
    def __init__(self, torznab_client: TorznabClient | None = None):
        self.torznab_client = torznab_client or TorznabClient()

    def search_candidates(
        self,
        *,
        movie_number: str,
        indexer_kind: str | None = None,
    ) -> list[DownloadCandidateResource]:
        normalized_movie_number = validate_non_empty(
            movie_number,
            "invalid_download_candidate_movie_number",
            "movie_number cannot be empty",
        ).upper()
        normalized_kind = self._validate_indexer_kind(indexer_kind)
        try:
            candidates = self.torznab_client.search(normalized_movie_number, normalized_kind)
        except TorznabClientError as exc:
            raise ApiError(
                502,
                "download_candidate_search_failed",
                "Torznab search failed",
                {"detail": str(exc)},
            ) from exc
        return self._filter_title_mismatched_candidates(candidates, normalized_movie_number)

    @staticmethod
    def _filter_title_mismatched_candidates(
        candidates: list[DownloadCandidateResource],
        movie_number: str,
    ) -> list[DownloadCandidateResource]:
        """按候选标题解析出的番号过滤明显错配资源，解析不出番号的候选保留。

        标题解析是启发式：能解析出且与请求不一致的候选，提交后导入侧也会归到别的影片，
        直接在列表阶段剔除；解析不出的候选保留，由提交阶段的内容闸门做最终确认。
        比对口径与内容闸门一致（strip + 大写原串），不做分隔符折叠。
        """
        requested = (movie_number or "").strip().upper()
        filtered: list[DownloadCandidateResource] = []
        dropped_count = 0
        for candidate in candidates:
            parsed = parse_movie_number_from_text(candidate.title or "")
            if parsed and parsed.upper() != requested:
                dropped_count += 1
                logger.info(
                    "Torznab candidate filtered by title mismatch movie_number={} title={} parsed={}",
                    movie_number,
                    candidate.title,
                    parsed,
                )
                continue
            filtered.append(candidate)
        if dropped_count:
            logger.info(
                "Torznab title filter finished movie_number={} total={} dropped={}",
                movie_number,
                len(candidates),
                dropped_count,
            )
        return filtered

    @staticmethod
    def _validate_indexer_kind(indexer_kind: str | None) -> str | None:
        if indexer_kind is None:
            return None
        normalized = indexer_kind.strip().lower()
        if not normalized:
            return None
        try:
            return IndexerKind(normalized).value
        except ValueError as exc:
            raise ApiError(
                422,
                "invalid_download_candidate_indexer_kind",
                "Unsupported indexer kind",
                {"indexer_kind": indexer_kind},
            ) from exc
