"""Content-addressed files, immutable metadata, and non-destructive recovery."""
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile

from app.contracts.common import ArtifactRef, StrictModel


class ArtifactIntegrityError(ValueError):
    """An artifact cannot safely be handed to a downstream consumer."""


class RecoveryReport(StrictModel):
    missing: tuple[ArtifactRef, ...] = ()
    corrupt: tuple[ArtifactRef, ...] = ()
    orphans: tuple[str, ...] = ()


def _reject_symlinks(path: Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("artifact paths cannot contain symlinks")


class ArtifactStore:
    def __init__(self, root: Path, conn: sqlite3.Connection):
        if not isinstance(root, Path):
            raise TypeError("artifact root must be a local Path")
        self.root = root.absolute()
        _reject_symlinks(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.conn = conn

    def _path(self, ref: ArtifactRef) -> Path:
        # Revalidate even model_construct/model_copy inputs before deriving paths.
        ref = ArtifactRef.model_validate(ref.model_dump())
        path = self.root / ref.sha256[:2] / ref.sha256
        _reject_symlinks(path)
        return path

    def _registered(self, digest: str) -> ArtifactRef | None:
        row = self.conn.execute(
            "SELECT sha256, media_type, size_bytes, schema_version FROM artifacts WHERE sha256 = ?",
            (digest,),
        ).fetchone()
        return ArtifactRef(**dict(zip(("sha256", "media_type", "size_bytes", "schema_version"), row))) if row else None

    @staticmethod
    def _verify(path: Path, ref: ArtifactRef) -> bytes:
        try:
            data = path.read_bytes()
        except OSError as error:
            raise ArtifactIntegrityError("artifact content unavailable") from error
        if len(data) != ref.size_bytes or hashlib.sha256(data).hexdigest() != ref.sha256:
            raise ArtifactIntegrityError("artifact content does not match its reference")
        return data

    def put(self, data: bytes, media_type: str, schema_version: str) -> ArtifactRef:
        if not isinstance(data, bytes):
            raise TypeError("artifact content must be bytes")
        ref = ArtifactRef(sha256=hashlib.sha256(data).hexdigest(), media_type=media_type,
                          size_bytes=len(data), schema_version=schema_version)
        path = self._path(ref)
        registered = self._registered(ref.sha256)
        if registered is not None:
            if registered != ref:
                raise ArtifactIntegrityError("content already registered with different metadata")
            self.read(ref)
            return ref
        if self.conn.in_transaction:
            raise ValueError("put requires an idle connection")
        path.parent.mkdir(parents=True, exist_ok=True)
        _reject_symlinks(path)
        if path.exists():
            self._verify(path, ref)
            with path.open("rb") as existing_file:
                os.fsync(existing_file.fileno())
        else:
            fd, temp_name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
            temp_path = Path(temp_name)
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                _reject_symlinks(path)
                os.replace(temp_path, path)
            finally:
                temp_path.unlink(missing_ok=True)
        # Sync through every ancestor, including on orphan adoption after restart.
        # Existing directories can be remnants of an interrupted unsynced mkdir;
        # presence alone cannot establish their durability.
        for directory in (path.parent, *path.parent.parents):
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            existing = self._registered(ref.sha256)
            if existing is None:
                self.conn.execute("INSERT INTO artifacts VALUES (?, ?, ?, ?)",
                                  (ref.sha256, ref.media_type, ref.size_bytes, ref.schema_version))
            elif existing != ref:
                raise ArtifactIntegrityError("content already registered with different metadata")
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        return ref

    def read(self, ref: ArtifactRef) -> bytes:
        path = self._path(ref)
        if self._registered(ref.sha256) != ref:
            raise ArtifactIntegrityError("artifact reference is not registered")
        return self._verify(path, ref)

    def reconcile(self) -> RecoveryReport:
        """Report inconsistencies without repairing, deleting, or following symlinks."""
        _reject_symlinks(self.root)
        missing, corrupt, referenced = [], [], set()
        for row in self.conn.execute("SELECT sha256, media_type, size_bytes, schema_version FROM artifacts ORDER BY sha256"):
            ref = ArtifactRef(**dict(zip(("sha256", "media_type", "size_bytes", "schema_version"), row)))
            referenced.add(f"{ref.sha256[:2]}/{ref.sha256}")
            try:
                path = self._path(ref)
                if not path.exists():
                    missing.append(ref)
                else:
                    self._verify(path, ref)
            except ValueError:
                corrupt.append(ref)
        orphans = []
        for directory, dirs, files in os.walk(self.root, followlinks=False):
            # Include unexpected symlink entries as orphans, without dereferencing.
            links = [name for name in dirs if (Path(directory) / name).is_symlink()]
            dirs[:] = [name for name in dirs if name not in links]
            for name in files + links:
                relative = (Path(directory) / name).relative_to(self.root).as_posix()
                if relative not in referenced:
                    orphans.append(relative)
        return RecoveryReport(missing=tuple(missing), corrupt=tuple(corrupt), orphans=tuple(sorted(orphans)))
