import gzip
import hashlib
import json
import re
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from src.common.han_variants import han_script_variants
from src.metadata.provider import MetadataRequestClient


@dataclass(frozen=True)
class GfriendsActorCandidate:
    candidate_id: str
    name: str
    alias_names: tuple[str, ...]
    relative_path: str
    avatar_url: str


@dataclass(frozen=True)
class _GfriendsDirectoryEntry:
    name: str
    alias_names: tuple[str, ...]
    name_forms: tuple[str, ...]
    alias_forms: tuple[str, ...]


class GfriendsActorImageResolver(MetadataRequestClient):
    """GFriends Filetree 的本地头像与演员候选索引。"""

    IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
    FILETREE_REQUEST_TIMEOUT = 60.0
    _DISK_RECHECK_INTERVAL_SECONDS = 60.0

    def __init__(
        self,
        filetree_url: str,
        cdn_base_url: str,
        cache_path: str,
        cache_ttl_hours: int,
        bundled_filetree_path: str | Path | None = None,
    ):
        MetadataRequestClient.__init__(self, timeout=self.FILETREE_REQUEST_TIMEOUT)
        self.filetree_url = filetree_url
        self.cdn_base_url = cdn_base_url.rstrip("/")
        self.cache_path = Path(cache_path).expanduser()
        if not self.cache_path.is_absolute():
            self.cache_path = (Path.cwd() / self.cache_path).resolve()
        self.bundled_filetree_path = (
            Path(bundled_filetree_path).expanduser()
            if bundled_filetree_path is not None
            else None
        )
        if (
            self.bundled_filetree_path is not None
            and not self.bundled_filetree_path.is_absolute()
        ):
            self.bundled_filetree_path = (
                Path.cwd() / self.bundled_filetree_path
            ).resolve()
        self.cache_ttl_seconds = max(cache_ttl_hours, 1) * 3600
        self._index: dict[str, str] | None = None
        self._variant_index: dict[str, str] = {}
        self._entries: dict[str, _GfriendsDirectoryEntry] = {}
        self._candidate_paths: dict[str, str] = {}
        self._disk_hydrated: bool = False
        self._hydrated_mtime: float | None = None
        self._next_disk_check: float = 0.0
        self._lock = threading.Lock()

    def resolve(self, candidate_names: list[str]) -> str | None:
        """按候选名/别名返回头像 URL；只读内存或本地快照。"""
        index = self._read_index_lazy()
        if not index:
            return None
        for candidate_name in candidate_names:
            normalized_name = self._normalize_name(candidate_name)
            if not normalized_name:
                continue
            relative_path = index.get(normalized_name)
            if relative_path:
                return self._avatar_url(relative_path)
        for candidate_name in candidate_names:
            for normalized_variant in self._normalize_name_variants(candidate_name):
                relative_path = self._variant_index.get(normalized_variant)
                if relative_path:
                    return self._avatar_url(relative_path)
        return None

    def search(self, query: str, *, limit: int = 20) -> list[GfriendsActorCandidate]:
        """按姓名/别名搜索候选，按头像路径去重并保留 exact→prefix→contains 顺序。"""
        self._read_index_lazy()
        query_variants = self._normalize_name_variants(query)
        if not query_variants or not self._entries:
            return []

        ranked: list[tuple[int, int, int, str]] = []
        for relative_path, entry in self._entries.items():
            best_match: tuple[int, int] | None = None
            for variant_index, normalized_query in enumerate(query_variants):
                for form in entry.name_forms:
                    if form == normalized_query:
                        score = 0
                    elif form.startswith(normalized_query):
                        score = 2
                    elif normalized_query in form:
                        score = 4
                    else:
                        continue
                    best_match = self._better_match(
                        best_match, (score, variant_index)
                    )
                for form in entry.alias_forms:
                    if form == normalized_query:
                        score = 1
                    elif form.startswith(normalized_query):
                        score = 3
                    elif normalized_query in form:
                        score = 5
                    else:
                        continue
                    best_match = self._better_match(
                        best_match, (score, variant_index)
                    )
            if best_match is not None:
                ranked.append(
                    (
                        best_match[0],
                        best_match[1],
                        len(entry.name),
                        relative_path,
                    )
                )

        ranked.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
        candidates: list[GfriendsActorCandidate] = []
        for _score, _variant_index, _name_len, relative_path in ranked[:limit]:
            candidates.append(self._candidate_from_entry(relative_path))
        return candidates

    def candidate(self, candidate_id: str) -> GfriendsActorCandidate | None:
        self._read_index_lazy()
        relative_path = self._candidate_paths.get(candidate_id)
        if relative_path is None:
            return None
        return self._candidate_from_entry(relative_path)

    def refresh(self, *, force: bool = False) -> dict[str, Any]:
        """拉取 Filetree、写 disk cache 并更新内存索引。"""
        with self._lock:
            if not force and self._is_cache_fresh():
                self._hydrate_from_disk_locked()
                return {
                    "entries": len(self._entries),
                    "source": "cache_fresh",
                    "bytes_written": 0,
                    "force": force,
                }

            logger.info("GFriends filetree refresh started force={}", force)
            try:
                payload = self.request_json("GET", self.filetree_url)
            except Exception as exc:
                self._hydrate_from_disk_locked()
                if self._entries:
                    logger.warning(
                        "GFriends filetree refresh failed, staying on local entries={} detail={}",
                        len(self._entries),
                        exc,
                    )
                    return {
                        "entries": len(self._entries),
                        "source": "stale_cache",
                        "bytes_written": 0,
                        "force": force,
                    }
                raise

            bytes_written = self._write_cache_payload(payload)
            self._build_directory(payload)
            self._disk_hydrated = True
            self._hydrated_mtime = self._cache_mtime()
            return {
                "entries": len(self._entries),
                "source": "network",
                "bytes_written": bytes_written,
                "force": force,
            }

    def _read_index_lazy(self) -> dict[str, str]:
        now_ts = time.time()
        if now_ts >= self._next_disk_check:
            with self._lock:
                if now_ts >= self._next_disk_check:
                    self._next_disk_check = now_ts + self._DISK_RECHECK_INTERVAL_SECONDS
                    if self._cache_mtime() != self._hydrated_mtime:
                        self._index = None
                        self._variant_index = {}
                        self._entries = {}
                        self._candidate_paths = {}
                        self._disk_hydrated = False
        if self._index is not None:
            return self._index
        if self._disk_hydrated:
            return {}
        with self._lock:
            if self._index is not None:
                return self._index
            if self._disk_hydrated:
                return {}
            self._hydrate_from_disk_locked()
            return self._index or {}

    def _hydrate_from_disk_locked(self) -> None:
        self._hydrated_mtime = self._cache_mtime()
        if self._index:
            self._disk_hydrated = True
            return
        payload = self._read_payload()
        self._disk_hydrated = True
        if payload is None:
            return
        try:
            self._build_directory(payload)
        except Exception as exc:
            logger.warning(
                "Failed to build GFriends index cache={} bundled={} detail={}",
                str(self.cache_path),
                str(self.bundled_filetree_path),
                exc,
            )

    def _is_cache_fresh(self) -> bool:
        if not self.cache_path.exists():
            return False
        age_seconds = time.time() - self.cache_path.stat().st_mtime
        return age_seconds <= self.cache_ttl_seconds

    def _cache_mtime(self) -> float | None:
        try:
            return self.cache_path.stat().st_mtime
        except OSError:
            return None

    def _read_payload(self) -> Any | None:
        payload = self._read_cache_payload()
        if payload is not None:
            return payload
        return self._read_bundled_payload()

    def _read_cache_payload(self) -> Any | None:
        if not self.cache_path.exists():
            return None
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                "Failed to read GFriends cache path={} detail={}",
                str(self.cache_path),
                exc,
            )
            return None

    def _read_bundled_payload(self) -> Any | None:
        path = self.bundled_filetree_path
        if path is None or not path.is_file():
            return None
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception as exc:
            logger.warning(
                "Failed to read bundled GFriends filetree path={} detail={}",
                str(path),
                exc,
            )
            return None

    def _write_cache_payload(self, payload: Any) -> int:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(self.cache_path.parent),
            delete=False,
        ) as temp_file:
            temp_file.write(content)
            temp_path = Path(temp_file.name)
        temp_path.replace(self.cache_path)
        return len(content.encode("utf-8"))

    def _build_directory(self, payload: Any) -> None:
        index: dict[str, str] = {}
        variant_index: dict[str, str] = {}
        variant_cache: dict[str, tuple[str, ...]] = {}

        def variants_for(value: str) -> tuple[str, ...]:
            variants = variant_cache.get(value)
            if variants is None:
                variants = self._normalize_name_variants(value)
                variant_cache[value] = variants
            return variants

        grouped: dict[str, list[str]] = {}
        for display_name, relative_path in self._extract_file_entries(payload):
            grouped.setdefault(relative_path, []).append(Path(display_name).stem)

        entries: dict[str, _GfriendsDirectoryEntry] = {}
        candidate_paths: dict[str, str] = {}
        for relative_path, names in grouped.items():
            canonical_name = self._display_name_from_path(relative_path)
            aliases: list[str] = []
            seen_aliases: set[str] = set()
            normalized_canonical = self._normalize_name(canonical_name)
            for name in [Path(relative_path).stem, *names]:
                cleaned = self._display_name_from_path(name)
                key = self._normalize_name(cleaned)
                if not key or key in seen_aliases or key == normalized_canonical:
                    continue
                seen_aliases.add(key)
                aliases.append(cleaned)
            for name in (canonical_name, *aliases):
                normalized_name = self._normalize_name(name)
                if normalized_name and normalized_name not in index:
                    index[normalized_name] = relative_path
                for normalized_variant in variants_for(name):
                    if normalized_variant and normalized_variant not in variant_index:
                        variant_index[normalized_variant] = relative_path
            entries[relative_path] = _GfriendsDirectoryEntry(
                name=canonical_name,
                alias_names=tuple(aliases),
                name_forms=variants_for(canonical_name),
                alias_forms=tuple(
                    dict.fromkeys(
                        variant
                        for alias in aliases
                        for variant in variants_for(alias)
                    )
                ),
            )
            candidate_paths[self._candidate_id(relative_path)] = relative_path

        self._index = index
        self._variant_index = variant_index
        self._entries = entries
        self._candidate_paths = candidate_paths

    def _candidate_from_entry(self, relative_path: str) -> GfriendsActorCandidate:
        entry = self._entries[relative_path]
        return self._candidate(relative_path, entry.name, entry.alias_names)

    def _candidate(
        self,
        relative_path: str,
        name: str,
        alias_names: tuple[str, ...],
    ) -> GfriendsActorCandidate:
        return GfriendsActorCandidate(
            candidate_id=self._candidate_id(relative_path),
            name=name,
            alias_names=alias_names,
            relative_path=relative_path,
            avatar_url=self._avatar_url(relative_path),
        )

    @staticmethod
    def _better_match(
        current: tuple[int, int] | None,
        candidate: tuple[int, int],
    ) -> tuple[int, int]:
        return candidate if current is None or candidate < current else current

    def _candidate_id(self, relative_path: str) -> str:
        return hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:16]

    def _avatar_url(self, relative_path: str) -> str:
        return f"{self.cdn_base_url}/{relative_path}"

    @staticmethod
    def _display_name_from_path(value: str) -> str:
        stem = Path(value).stem
        return re.sub(r"^AI-Fix-", "", stem)

    def _extract_file_entries(self, payload: Any) -> list[tuple[str, str]]:
        if isinstance(payload, dict) and isinstance(payload.get("Content"), dict):
            return self._extract_content_mapping_entries(payload["Content"], ["Content"])
        return self._extract_tree_entries(payload)

    def _extract_content_mapping_entries(
        self, node: Any, path_parts: list[str]
    ) -> list[tuple[str, str]]:
        entries: list[tuple[str, str]] = []
        if not isinstance(node, dict):
            return entries
        for key, value in node.items():
            if isinstance(value, str):
                relative_path = "/".join(path_parts + [value.lstrip("/")])
                extension = Path(key).suffix.lower()
                if extension in self.IMAGE_EXTENSIONS:
                    entries.append((key, relative_path))
                continue
            entries.extend(
                self._extract_content_mapping_entries(value, path_parts + [key])
            )
        return entries

    def _extract_tree_entries(self, node: Any) -> list[tuple[str, str]]:
        entries: list[tuple[str, str]] = []
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

    @staticmethod
    def _normalize_name(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value or "")
        normalized = normalized.strip().lower()
        return re.sub(r"\s+", "", normalized)

    @classmethod
    def _normalize_name_variants(cls, value: str) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                normalized
                for variant in han_script_variants(value)
                if (normalized := cls._normalize_name(variant))
            )
        )


_DIRECTORY: GfriendsActorImageResolver | None = None
_DIRECTORY_KEY: tuple[Any, ...] | None = None
_DIRECTORY_LOCK = threading.Lock()


def get_gfriends_directory() -> GfriendsActorImageResolver:
    from src.config.config import settings

    global _DIRECTORY, _DIRECTORY_KEY
    key = (
        settings.metadata.gfriends_filetree_url,
        settings.metadata.gfriends_cdn_base_url,
        settings.metadata.gfriends_filetree_cache_path,
        settings.metadata.gfriends_bundled_filetree_path,
        settings.metadata.gfriends_filetree_cache_ttl_hours,
    )
    with _DIRECTORY_LOCK:
        if _DIRECTORY is None or _DIRECTORY_KEY != key:
            _DIRECTORY = GfriendsActorImageResolver(
                filetree_url=key[0],
                cdn_base_url=key[1],
                cache_path=key[2],
                bundled_filetree_path=key[3],
                cache_ttl_hours=key[4],
            )
            _DIRECTORY_KEY = key
        return _DIRECTORY


def refresh_gfriends_filetree(*, force: bool = False) -> dict[str, Any]:
    return get_gfriends_directory().refresh(force=force)
