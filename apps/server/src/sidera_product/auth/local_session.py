import hashlib
import hmac
import json
import os
import secrets
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictStr, ValidationError
from sidera_control_plane.application.request_authority import ControlPlaneIdentity
from starlette.requests import Request

from sidera_product.config import ProductSettings
from sidera_product.postgres.authority_store import (
    BootstrapRecord,
    BrowserSessionRecord,
    SessionStore,
)

SESSION_COOKIE = "sidera_local_session"
CSRF_COOKIE = "sidera_csrf"
CSRF_HEADER = "X-Sidera-CSRF"


class BootstrapTokenBody(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    token: StrictStr = Field(min_length=1, max_length=1024, repr=False)


class LocalSessionError(Exception):
    def __init__(self, code: str, *, status_code: int = 401) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class BootstrapIssue:
    bootstrap_id: UUID
    token: SecretStr = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class SessionIssue:
    session_id: UUID
    token: SecretStr = field(repr=False)
    csrf: SecretStr = field(repr=False)
    expires_at: datetime


def token_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


class LocalSessions:
    def __init__(
        self,
        settings: ProductSettings,
        store: SessionStore,
        *,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.settings = settings
        self.store = store
        self.clock = clock
        self._hosts = frozenset(
            urlsplit(value).netloc
            for value in (settings.local_url, settings.web_origin)
        )

    def create_bootstrap(self) -> BootstrapIssue:
        now = self.clock()
        issue = BootstrapIssue(
            uuid4(),
            SecretStr(secrets.token_urlsafe(32)),
            now + timedelta(seconds=self.settings.bootstrap_ttl_seconds),
        )
        self.store.save_bootstrap(
            BootstrapRecord(
                issue.bootstrap_id,
                self.settings.workspace_id,
                self.settings.local_principal_id,
                token_digest(issue.token.get_secret_value()),
                now,
                issue.expires_at,
            )
        )
        return issue

    def exchange(self, token: SecretStr) -> SessionIssue:
        raw = token.get_secret_value()
        if not raw or len(raw) > 1024:
            raise LocalSessionError("bootstrap_invalid")
        now = self.clock()
        issue = SessionIssue(
            uuid4(),
            SecretStr(secrets.token_urlsafe(32)),
            SecretStr(secrets.token_urlsafe(32)),
            now + timedelta(seconds=self.settings.local_session_ttl_seconds),
        )
        result = self.store.exchange_bootstrap(
            token_digest(raw),
            BrowserSessionRecord(
                issue.session_id,
                self.settings.workspace_id,
                self.settings.local_principal_id,
                token_digest(issue.token.get_secret_value()),
                token_digest(issue.csrf.get_secret_value()),
                now,
                issue.expires_at,
            ),
            now=now,
        )
        if result != "accepted":
            code = {
                "missing": "bootstrap_invalid",
                "consumed": "bootstrap_consumed",
                "expired": "bootstrap_expired",
            }[result]
            raise LocalSessionError(code)
        return issue

    def require_browser_boundary(
        self, request: Request, *, require_origin: bool = True
    ) -> None:
        hosts = request.headers.getlist("host")
        if len(hosts) != 1 or hosts[0] not in self._hosts:
            raise LocalSessionError("host_denied", status_code=403)
        origins = request.headers.getlist("origin")
        if (require_origin or origins) and origins != [self.settings.web_origin]:
            raise LocalSessionError("origin_denied", status_code=403)

    def _current(self, row: BrowserSessionRecord | None) -> bool:
        return (
            row is not None
            and row.workspace_id == self.settings.workspace_id
            and row.principal_id == self.settings.local_principal_id
            and row.csrf_digest is not None
            and row.revoked_at is None
            and row.expires_at > self.clock()
        )

    def authenticate_request(self, request: Request) -> ControlPlaneIdentity | None:
        token = request.cookies.get(SESSION_COOKIE)
        if not token or len(token) > 1024:
            return None
        if "authorization" in request.headers:
            raise LocalSessionError("ambiguous_authentication", status_code=403)
        is_write = request.method not in {"GET", "HEAD", "OPTIONS"}
        self.require_browser_boundary(request, require_origin=is_write)
        row = self.store.find_session(token_digest=token_digest(token))
        if row is None or not self._current(row):
            return None
        if is_write:
            csrf = request.cookies.get(CSRF_COOKIE)
            headers = request.headers.getlist(CSRF_HEADER)
            if (
                not csrf
                or len(csrf) > 1024
                or len(headers) != 1
                or not hmac.compare_digest(
                    csrf.encode("utf-8"), headers[0].encode("utf-8")
                )
                or row.csrf_digest is None
                or not hmac.compare_digest(token_digest(csrf), row.csrf_digest)
            ):
                raise LocalSessionError("csrf_required", status_code=403)
        return ControlPlaneIdentity(row.principal_id, row.session_id, "user")

    def identity_for_grant(self, grant_id: UUID) -> ControlPlaneIdentity | None:
        row = self.store.find_session(session_id=grant_id)
        if row is None or not self._current(row):
            return None
        return ControlPlaneIdentity(row.principal_id, row.session_id, "user")

    def revoke(self, session_id: UUID) -> None:
        self.store.revoke_session(session_id, now=self.clock())


def _private_directory(directory: Path) -> os.stat_result:
    if not directory.is_absolute() or ".." in directory.parts:
        raise LocalSessionError("bootstrap_output_unsafe", status_code=400)
    info = directory.lstat()
    physical = directory.resolve(strict=True)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
        or any(
            (parent / ".git").exists()
            for parent in (
                directory,
                *directory.parents,
                physical,
                *physical.parents,
            )
        )
    ):
        raise LocalSessionError("bootstrap_output_unsafe", status_code=400)
    return info


def write_bootstrap_file(issue: BootstrapIssue, *, output: Path | None = None) -> Path:
    if output is None:
        output = (
            Path(tempfile.mkdtemp(prefix="sidera-bootstrap-")).resolve()
            / "bootstrap.json"
        )
    directory_fd: int | None = None
    try:
        if not output.is_absolute() or ".." in output.parts:
            raise LocalSessionError("bootstrap_output_unsafe", status_code=400)
        expected = _private_directory(output.parent)
        directory_fd = os.open(
            output.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        actual = os.fstat(directory_fd)
        if (
            (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)
            or actual.st_uid != os.getuid()
            or stat.S_IMODE(actual.st_mode) != 0o700
        ):
            raise LocalSessionError("bootstrap_output_unsafe", status_code=400)
        fd = os.open(
            output.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as destination:
            json.dump({"token": issue.token.get_secret_value()}, destination)
            destination.flush()
            os.fsync(destination.fileno())
        return output
    except (OSError, RuntimeError, ValueError):
        raise LocalSessionError("bootstrap_output_unsafe", status_code=400) from None
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def read_bootstrap_file(location: Path) -> SecretStr:
    directory_fd: int | None = None
    try:
        if not location.is_absolute() or ".." in location.parts:
            raise LocalSessionError("bootstrap_output_unsafe", status_code=400)
        expected = _private_directory(location.parent)
        directory_fd = os.open(
            location.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        directory = os.fstat(directory_fd)
        if (
            (directory.st_dev, directory.st_ino) != (expected.st_dev, expected.st_ino)
            or directory.st_uid != os.getuid()
            or stat.S_IMODE(directory.st_mode) != 0o700
        ):
            raise LocalSessionError("bootstrap_output_unsafe", status_code=400)
        fd = os.open(location.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        with os.fdopen(fd, "rb") as source:
            metadata = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size > 4096
            ):
                raise LocalSessionError("bootstrap_output_unsafe", status_code=400)
            body = source.read(4097)
        if len(body) > 4096:
            raise LocalSessionError("bootstrap_invalid", status_code=400)
        return SecretStr(BootstrapTokenBody.model_validate_json(body).token)
    except FileNotFoundError:
        raise LocalSessionError("local_session_required") from None
    except ValidationError:
        raise LocalSessionError("bootstrap_invalid", status_code=400) from None
    except (OSError, RuntimeError, ValueError):
        raise LocalSessionError("bootstrap_output_unsafe", status_code=400) from None
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
