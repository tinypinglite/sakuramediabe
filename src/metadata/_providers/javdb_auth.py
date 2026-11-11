from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import portalocker
from loguru import logger

from .exceptions import JavdbAuthError

TOKEN_STATE_VERSION = 1
DEFAULT_JAVDB_SESSION_PATH = "/data/cache/javdb/session.json"
LOGIN_FAILURE_COOLDOWN_SECONDS = 30.0
JWT_EXPIRY_SKEW_SECONDS = 30.0
TOKEN_REFRESH_LOCK_TIMEOUT_SECONDS = 150.0


@dataclass(frozen=True)
class JavdbAuthState:
    version: int
    token: str | None
    account_fingerprint: str
    obtained_at: float | None
    expires_at: float | None
    last_failure_at: float | None
    last_failure_detail: str | None


def javdb_account_fingerprint(
    host: str,
    username: str | None,
    password: str | None,
) -> str:
    """生成账号指纹，避免在共享状态中保存明文密码。"""
    raw = "\0".join((host.strip(), (username or "").strip(), password or ""))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _jwt_expires_at(token: str) -> float | None:
    """读取 JWT 的 exp；只用于提前刷新，不承担签名校验。"""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        expires_at = claims.get("exp")
        if isinstance(expires_at, (int, float)):
            return float(expires_at)
        if isinstance(expires_at, str) and expires_at.strip():
            return float(expires_at)
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    return None


def _process_role() -> str:
    command = " ".join(sys.argv).lower()
    if "uvicorn" in command:
        return "api"
    if " aps" in f" {command}" or command.endswith("aps"):
        return "aps"
    if "src.start.commands" in command or "src/start/commands.py" in command:
        return "cli"
    return "runtime"


def _state_from_payload(payload: Any) -> JavdbAuthState | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("version") != TOKEN_STATE_VERSION:
        return None
    token = payload.get("token")
    account_fingerprint = payload.get("account_fingerprint")
    if token is not None and not isinstance(token, str):
        return None
    if not isinstance(account_fingerprint, str) or not account_fingerprint:
        return None
    return JavdbAuthState(
        version=TOKEN_STATE_VERSION,
        token=token or None,
        account_fingerprint=account_fingerprint,
        obtained_at=_optional_float(payload.get("obtained_at")),
        expires_at=_optional_float(payload.get("expires_at")),
        last_failure_at=_optional_float(payload.get("last_failure_at")),
        last_failure_detail=_optional_str(payload.get("last_failure_detail")),
    )


def _optional_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _optional_str(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value[:300]
    return None


class JavdbAuthManager:
    """基于共享文件协调多个进程使用同一个 JavDB 登录态。"""

    def __init__(
        self,
        path: str | Path,
        *,
        failure_cooldown_seconds: float = LOGIN_FAILURE_COOLDOWN_SECONDS,
        lock_timeout_seconds: float = TOKEN_REFRESH_LOCK_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.time,
        process_role: str | None = None,
    ):
        self.path = self._absolute_path(path)
        self.lock_path = self.path.with_suffix(".lock")
        self.failure_cooldown_seconds = failure_cooldown_seconds
        self.lock_timeout_seconds = lock_timeout_seconds
        self._clock = clock
        self.process_role = process_role or _process_role()
        self._thread_lock = threading.RLock()

    @staticmethod
    def _absolute_path(path: str | Path) -> Path:
        resolved = Path(path).expanduser()
        if not resolved.is_absolute():
            resolved = (Path.cwd() / resolved).resolve()
        return resolved

    def get_token(
        self,
        login: Callable[[], str],
        *,
        account_fingerprint: str,
    ) -> str:
        now = self._clock()
        state = self._read_state()
        token = self._valid_token(state, account_fingerprint, now)
        if token:
            return token

        with self._thread_lock:
            state = self._read_state()
            token = self._valid_token(state, account_fingerprint, now=self._clock())
            if token:
                return token

            with self._exclusive_file_lock():
                now = self._clock()
                state = self._read_state()
                token = self._valid_token(state, account_fingerprint, now)
                if token:
                    return token

                if self._failure_is_active(state, account_fingerprint, now):
                    detail = state.last_failure_detail or "unknown"
                    raise JavdbAuthError(
                        f"login temporarily skipped after a recent failure: {detail}"
                    )

                reason = self._refresh_reason(state, account_fingerprint, now)
                try:
                    token = login()
                except JavdbAuthError as exc:
                    failed_at = self._clock()
                    self._write_state(
                        JavdbAuthState(
                            version=TOKEN_STATE_VERSION,
                            token=None,
                            account_fingerprint=account_fingerprint,
                            obtained_at=None,
                            expires_at=None,
                            last_failure_at=failed_at,
                            last_failure_detail=exc.detail,
                        )
                    )
                    raise

                obtained_at = self._clock()
                self._write_state(
                    JavdbAuthState(
                        version=TOKEN_STATE_VERSION,
                        token=token,
                        account_fingerprint=account_fingerprint,
                        obtained_at=obtained_at,
                        expires_at=_jwt_expires_at(token),
                        last_failure_at=None,
                        last_failure_detail=None,
                    )
                )
                logger.info(
                    "Javdb shared token refreshed reason={} process={} pid={} token_len={}",
                    reason,
                    self.process_role,
                    os.getpid(),
                    len(token),
                )
                return token

    def invalidate(self, token: str | None, *, account_fingerprint: str) -> None:
        """仅清除仍然等于失败 token 的共享状态，避免覆盖其他进程的新 token。"""
        if not token:
            return
        with self._thread_lock, self._exclusive_file_lock():
            state = self._read_state()
            if (
                state is None
                or state.account_fingerprint != account_fingerprint
                or state.token != token
            ):
                return
            self._write_state(
                JavdbAuthState(
                    version=TOKEN_STATE_VERSION,
                    token=None,
                    account_fingerprint=account_fingerprint,
                    obtained_at=None,
                    expires_at=None,
                    last_failure_at=None,
                    last_failure_detail=None,
                )
            )

    def _valid_token(
        self,
        state: JavdbAuthState | None,
        account_fingerprint: str,
        now: float,
    ) -> str | None:
        if state is None or state.account_fingerprint != account_fingerprint:
            return None
        if not state.token:
            return None
        if (
            state.expires_at is not None
            and state.expires_at <= now + JWT_EXPIRY_SKEW_SECONDS
        ):
            return None
        return state.token

    def _failure_is_active(
        self,
        state: JavdbAuthState | None,
        account_fingerprint: str,
        now: float,
    ) -> bool:
        if (
            state is None
            or state.account_fingerprint != account_fingerprint
            or state.last_failure_at is None
        ):
            return False
        return now - state.last_failure_at < self.failure_cooldown_seconds

    @staticmethod
    def _refresh_reason(
        state: JavdbAuthState | None,
        account_fingerprint: str,
        now: float,
    ) -> str:
        if state is None:
            return "missing"
        if state.account_fingerprint != account_fingerprint:
            return "account_changed"
        if (
            state.token
            and state.expires_at is not None
            and state.expires_at <= now + JWT_EXPIRY_SKEW_SECONDS
        ):
            return "expired"
        return "invalidated"

    @contextmanager
    def _exclusive_file_lock(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock = portalocker.Lock(
                self.lock_path,
                mode="a",
                timeout=self.lock_timeout_seconds,
            )
        except OSError as exc:
            raise JavdbAuthError(
                f"JavDB shared token state is unavailable: {exc}"
            ) from exc
        try:
            with lock:
                yield
        except portalocker.exceptions.LockException as exc:
            raise JavdbAuthError(
                "timed out waiting for JavDB shared token refresh lock"
            ) from exc

    def _read_state(self) -> JavdbAuthState | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            logger.warning(
                "Javdb shared token state unreadable path={} detail={}",
                self.path,
                exc,
            )
            return None
        state = _state_from_payload(payload)
        if state is None:
            logger.warning(
                "Javdb shared token state has unsupported schema path={}",
                self.path,
            )
        return state

    def _write_state(self, state: JavdbAuthState) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            content = json.dumps(asdict(state), ensure_ascii=False, indent=2)
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.path.parent,
                    prefix=f".{self.path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as file:
                    temporary_path = Path(file.name)
                    file.write(content)
                    file.flush()
                    os.fsync(file.fileno())
                os.chmod(temporary_path, 0o600)
                os.replace(temporary_path, self.path)
            finally:
                if temporary_path is not None and temporary_path.exists():
                    temporary_path.unlink(missing_ok=True)
        except OSError as exc:
            raise JavdbAuthError(
                f"failed to persist JavDB shared token state: {exc}"
            ) from exc


_MANAGERS_LOCK = threading.Lock()
_MANAGERS: dict[Path, JavdbAuthManager] = {}


def get_javdb_auth_manager(path: str | Path = DEFAULT_JAVDB_SESSION_PATH) -> JavdbAuthManager:
    resolved = JavdbAuthManager._absolute_path(path)
    with _MANAGERS_LOCK:
        manager = _MANAGERS.get(resolved)
        if manager is None:
            manager = JavdbAuthManager(resolved)
            _MANAGERS[resolved] = manager
        return manager
