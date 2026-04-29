import json
import re
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

from src.metadata.provider import MetadataRequestClient


class GfriendsActorImageResolver(MetadataRequestClient):
    IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
    FILETREE_REQUEST_TIMEOUT = 60.0

    def __init__(
        self,
        filetree_url: str,
        cdn_base_url: str,
        cache_path: str,
        cache_ttl_hours: int,
        proxy: Optional[str] = None,
    ):
        MetadataRequestClient.__init__(self, proxy=proxy, timeout=self.FILETREE_REQUEST_TIMEOUT)
        self.filetree_url = filetree_url
        self.cdn_base_url = cdn_base_url.rstrip("/")
        self.cache_path = Path(cache_path).expanduser()
        if not self.cache_path.is_absolute():
            self.cache_path = (Path.cwd() / self.cache_path).resolve()
        self.cache_ttl_seconds = max(cache_ttl_hours, 1) * 3600
        self._index: Optional[Dict[str, str]] = None
        self._index_loaded_at: float = 0
        self._lock = threading.Lock()

    def resolve(self, candidate_names: List[str]) -> Optional[str]:
        index = self._load_index()
        if not index:
            return None

        for candidate_name in candidate_names:
            normalized_name = self._normalize_name(candidate_name)
            if not normalized_name:
                continue
            relative_path = index.get(normalized_name)
            if relative_path:
                return f"{self.cdn_base_url}/{relative_path}"
        return None

    def _load_index(self) -> Dict[str, str]:
        if self._index is not None and self._is_index_fresh():
            return self._index

        with self._lock:
            if self._index is not None and self._is_index_fresh():
                return self._index

            payload = self._load_filetree_payload()
            if payload is None:
                self._index = {}
                self._index_loaded_at = time.time()
                return self._index

            self._index = self._build_index(payload)
            self._index_loaded_at = time.time()
            return self._index

    def _load_filetree_payload(self) -> Optional[Any]:
        if self._is_cache_fresh():
            payload = self._read_cache_payload()
            if payload is not None:
                return payload

        cached_payload = self._read_cache_payload()
        try:
            payload = self.request_json("GET", self.filetree_url)
            self._write_cache_payload(payload)
            return payload
        except Exception as exc:
            if cached_payload is not None:
                logger.warning("Gfriends filetree refresh failed, using stale cache detail={}", exc)
                return cached_payload
            logger.warning("Gfriends filetree unavailable detail={}", exc)
            return None

    def _is_cache_fresh(self) -> bool:
        if not self.cache_path.exists():
            return False
        age_seconds = time.time() - self.cache_path.stat().st_mtime
        return age_seconds <= self.cache_ttl_seconds

    def _is_index_fresh(self) -> bool:
        if self._index is None:
            return False
        if self._index_loaded_at <= 0:
            return False
        age_seconds = time.time() - self._index_loaded_at
        return age_seconds <= self.cache_ttl_seconds

    def _read_cache_payload(self) -> Optional[Any]:
        if not self.cache_path.exists():
            return None
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to read gfriends cache path={} detail={}", str(self.cache_path), exc)
            return None

    def _write_cache_payload(self, payload: Any) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(self.cache_path.parent),
            delete=False,
        ) as temp_file:
            temp_file.write(json.dumps(payload, ensure_ascii=False, indent=2))
            temp_path = Path(temp_file.name)
        temp_path.replace(self.cache_path)

    def _build_index(self, payload: Any) -> Dict[str, str]:
        index: Dict[str, str] = {}
        for display_name, relative_path in self._extract_file_entries(payload):
            normalized_name = self._normalize_name(Path(display_name).stem)
            if not normalized_name:
                continue
            if normalized_name in index:
                continue
            index[normalized_name] = relative_path
        return index

    def _extract_file_entries(self, payload: Any) -> List[tuple[str, str]]:
        if isinstance(payload, dict) and isinstance(payload.get("Content"), dict):
            return self._extract_content_mapping_entries(payload["Content"], ["Content"])
        return self._extract_tree_entries(payload)

    def _extract_content_mapping_entries(self, node: Any, path_parts: List[str]) -> List[tuple[str, str]]:
        entries: List[tuple[str, str]] = []
        if not isinstance(node, dict):
            return entries

        for key, value in node.items():
            if isinstance(value, str):
                relative_path = "/".join(path_parts + [value.lstrip("/")])
                extension = Path(key).suffix.lower()
                if extension in self.IMAGE_EXTENSIONS:
                    entries.append((key, relative_path))
                continue
            entries.extend(self._extract_content_mapping_entries(value, path_parts + [key]))
        return entries

    def _extract_tree_entries(self, node: Any) -> List[tuple[str, str]]:
        entries: List[tuple[str, str]] = []
        if isinstance(node, list):
            for item in node:
                entries.extend(self._extract_tree_entries(item))
            return entries

        if not isinstance(node, dict):
            return entries

        node_type = node.get("type")
        if node_type == "file":
            full_path = node.get("fullPath") or node.get("path") or ""
            extension = Path(full_path).suffix.lower()
            if full_path and extension in self.IMAGE_EXTENSIONS:
                entries.append((Path(full_path).name, str(full_path).lstrip("/")))
            return entries

        children = node.get("children")
        if isinstance(children, list):
            for child in children:
                entries.extend(self._extract_tree_entries(child))
        return entries

    def _normalize_name(self, value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value or "")
        normalized = normalized.strip().lower()
        normalized = re.sub(r"\s+", "", normalized)
        return normalized

    def build_request_headers(self) -> Dict[str, str]:
        return {
        }
