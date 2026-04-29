from typing import List, Optional

from src.api.exception.errors import ApiError
from src.config.config import IndexerKind
from src.schema.transfers.downloads import DownloadCandidateResource
from src.service.transfers.common import validate_non_empty
from src.service.transfers.jackett_client import JackettClient, JackettClientError


class DownloadSearchService:
    def __init__(self, jackett_client: JackettClient | None = None):
        self.jackett_client = jackett_client or JackettClient()

    def search_candidates(
        self,
        *,
        movie_number: str,
        indexer_kind: Optional[str] = None,
    ) -> List[DownloadCandidateResource]:
        normalized_movie_number = validate_non_empty(
            movie_number,
            "invalid_download_candidate_movie_number",
            "movie_number cannot be empty",
        ).upper()
        normalized_kind = self._validate_indexer_kind(indexer_kind)
        try:
            return self.jackett_client.search(normalized_movie_number, normalized_kind)
        except JackettClientError as exc:
            raise ApiError(
                502,
                "download_candidate_search_failed",
                "Jackett search failed",
                {"detail": str(exc)},
            ) from exc

    @staticmethod
    def _validate_indexer_kind(indexer_kind: Optional[str]) -> Optional[str]:
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
