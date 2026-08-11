
from src.api.exception.errors import ApiError
from src.config.config import IndexerKind
from src.schema.transfers.downloads import DownloadCandidateResource
from src.service.transfers.downloads.common import validate_non_empty
from src.service.transfers.downloads.clients.torznab import TorznabClient, TorznabClientError


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
            return self.torznab_client.search(normalized_movie_number, normalized_kind)
        except TorznabClientError as exc:
            raise ApiError(
                502,
                "download_candidate_search_failed",
                "Torznab search failed",
                {"detail": str(exc)},
            ) from exc

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
