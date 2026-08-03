import json
import re
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

from loguru import logger

from src.metadata.provider import MetadataRequestClient


class GfriendsActorImageResolver(MetadataRequestClient):
    IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
    # Filetree.json 单文件较大（全量演员映射），保留 60s 超时给 refresh 用；
    # resolve 只读内存不发网络，业务主流程不再受此超时影响。
    FILETREE_REQUEST_TIMEOUT = 60.0

    def __init__(
        self,
        filetree_url: str,
        cdn_base_url: str,
        cache_path: str,
        cache_ttl_hours: int,
        proxy: str | None = None,
    ):
        MetadataRequestClient.__init__(self, proxy=proxy, timeout=self.FILETREE_REQUEST_TIMEOUT)
        self.filetree_url = filetree_url
        self.cdn_base_url = cdn_base_url.rstrip("/")
        self.cache_path = Path(cache_path).expanduser()
        if not self.cache_path.is_absolute():
            self.cache_path = (Path.cwd() / self.cache_path).resolve()
        self.cache_ttl_seconds = max(cache_ttl_hours, 1) * 3600
        self._index: dict[str, str] | None = None
        # 记录是否已尝试过懒加载 disk cache，避免 resolve() 反复读盘。
        self._disk_hydrated: bool = False
        # 跨进程缓存失效：refresh job 跑在 APS 进程，本进程（如 API）靠比对
        # disk cache 的 mtime 发现被重写，作废内存 index 后重新加载；
        # 检查按 _DISK_RECHECK_INTERVAL_SECONDS 节流，平时 resolve() 零 stat 开销。
        self._hydrated_mtime: float | None = None
        self._next_disk_check: float = 0.0
        self._lock = threading.Lock()

    _DISK_RECHECK_INTERVAL_SECONDS = 60.0

    def _cache_mtime(self) -> float | None:
        try:
            return self.cache_path.stat().st_mtime
        except OSError:
            return None

    def resolve(self, candidate_names: list[str]) -> str | None:
        """只读内存 index 查找头像 URL；永远不发网络请求、不阻塞。

        首次调用时若内存为空，会非阻塞地尝试从 disk cache 加载一次；
        没有 disk cache 或加载失败一律返回 None，交给上层回退到 JavDB 原头像。
        """
        index = self._read_index_lazy()
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

    def refresh(self, *, force: bool = False) -> dict[str, Any]:
        """APS 预热/定期任务的唯一网络入口：拉 Filetree + 落 disk cache + 更新内存 index。

        - force=False 且 disk cache 新鲜：跳过网络请求，仅按需 hydrate 内存 index
        - force=True：无条件重新拉取
        - 网络失败但存在 stale disk cache：读取 stale cache 填充内存 index，避免全空
        - 网络失败且无任何 cache：抛异常给上层任务框架记录，业务侧 resolve() 继续返回 None

        返回统计 dict：``{"entries", "source", "bytes_written", "force"}``。
        source ∈ {"network", "cache_fresh", "stale_cache"}。
        """
        with self._lock:
            if not force and self._is_cache_fresh():
                self._hydrate_from_disk_locked()
                return {
                    "entries": len(self._index or {}),
                    "source": "cache_fresh",
                    "bytes_written": 0,
                    "force": force,
                }

            try:
                payload = self.request_json("GET", self.filetree_url)
            except Exception as exc:
                # 网络失败：先尽力用 stale cache 填充内存 index，再把异常抛给调度器记录。
                self._hydrate_from_disk_locked()
                if self._index:
                    logger.warning(
                        "GFriends filetree refresh failed, staying on stale cache entries={} detail={}",
                        len(self._index), exc,
                    )
                    return {
                        "entries": len(self._index),
                        "source": "stale_cache",
                        "bytes_written": 0,
                        "force": force,
                    }
                raise

            bytes_written = self._write_cache_payload(payload)
            self._index = self._build_index(payload)
            self._disk_hydrated = True
            # 同步记录刚写入的 mtime，避免下一次节流检查把自己刚建好的 index 误判为过期。
            self._hydrated_mtime = self._cache_mtime()
            return {
                "entries": len(self._index),
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
                        # disk cache 被其他进程重写（或首次出现）：作废内存 index 重新加载。
                        self._index = None
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
        """在锁内调用：若内存 index 空且 disk cache 存在，尝试构建内存 index。"""
        self._hydrated_mtime = self._cache_mtime()
        if self._index:
            self._disk_hydrated = True
            return
        payload = self._read_cache_payload()
        self._disk_hydrated = True
        if payload is None:
            return
        try:
            self._index = self._build_index(payload)
        except Exception as exc:
            logger.warning(
                "Failed to build gfriends index from cached filetree path={} detail={}",
                str(self.cache_path), exc,
            )

    def _is_cache_fresh(self) -> bool:
        if not self.cache_path.exists():
            return False
        age_seconds = time.time() - self.cache_path.stat().st_mtime
        return age_seconds <= self.cache_ttl_seconds

    def _read_cache_payload(self) -> Any | None:
        if not self.cache_path.exists():
            return None
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to read gfriends cache path={} detail={}", str(self.cache_path), exc)
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

    def _build_index(self, payload: Any) -> dict[str, str]:
        index: dict[str, str] = {}
        for display_name, relative_path in self._extract_file_entries(payload):
            normalized_name = self._normalize_name(Path(display_name).stem)
            if not normalized_name:
                continue
            if normalized_name in index:
                continue
            index[normalized_name] = relative_path
        return index

    def _extract_file_entries(self, payload: Any) -> list[tuple[str, str]]:
        if isinstance(payload, dict) and isinstance(payload.get("Content"), dict):
            return self._extract_content_mapping_entries(payload["Content"], ["Content"])
        return self._extract_tree_entries(payload)

    def _extract_content_mapping_entries(self, node: Any, path_parts: list[str]) -> list[tuple[str, str]]:
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
            entries.extend(self._extract_content_mapping_entries(value, path_parts + [key]))
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

    def _normalize_name(self, value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value or "")
        normalized = normalized.strip().lower()
        normalized = re.sub(r"\s+", "", normalized)
        return normalized

    def build_request_headers(self) -> dict[str, str]:
        return {
        }
