import time

from lastbridge.features.playback.domain.models import (
    MEDIA_FILE_STATES,
    RENDITIONS,
    MediaDescriptor,
    MediaFile,
)


class MediaFileRepository:
    """Persist the physical representation of a logical audio rendition."""

    def __init__(self, database):
        self.database = database

    def get(self, source: str, policy: str) -> MediaFile | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM media_files WHERE source=? AND policy=?",
                (source, policy),
            ).fetchone()
        return self._record(row) if row is not None else None

    def list(self) -> list[MediaFile]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM media_files ORDER BY source,policy"
            ).fetchall()
        return [self._record(row) for row in rows]

    def ensure(self, descriptor: MediaDescriptor) -> MediaFile:
        container = RENDITIONS[descriptor.policy][0]
        now = time.time()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO media_files "
                "(source,policy,container,state,created,updated) "
                "VALUES(?,?,?,'pending',?,?)",
                (
                    descriptor.source,
                    descriptor.policy,
                    container,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM media_files WHERE source=? AND policy=?",
                (descriptor.source, descriptor.policy),
            ).fetchone()
        return self._record(row)

    def save(self, media_file: MediaFile) -> MediaFile:
        self._validate(media_file)
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO media_files "
                "(source,policy,relative_path,container,size,checksum,"
                "provider_identity,state,created,updated) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(source,policy) DO UPDATE SET "
                "relative_path=excluded.relative_path,"
                "container=excluded.container,size=excluded.size,"
                "checksum=excluded.checksum,"
                "provider_identity=excluded.provider_identity,"
                "state=excluded.state,created=excluded.created,"
                "updated=excluded.updated",
                (
                    media_file.source,
                    media_file.policy,
                    media_file.relative_path,
                    media_file.container,
                    media_file.size,
                    media_file.checksum,
                    media_file.provider_identity,
                    media_file.state,
                    media_file.created,
                    media_file.updated,
                ),
            )
        return media_file

    def transition(
        self,
        source: str,
        policy: str,
        state: str,
        *,
        relative_path: str | None = None,
        size: int | None = None,
        checksum: str | None = None,
        provider_identity: str | None = None,
    ) -> MediaFile:
        if state not in MEDIA_FILE_STATES:
            raise ValueError("Unknown media file state")
        now = time.time()
        with self.database.transaction() as connection:
            result = connection.execute(
                "UPDATE media_files SET relative_path=?,size=?,checksum=?,"
                "provider_identity=?,state=?,updated=? "
                "WHERE source=? AND policy=?",
                (
                    relative_path,
                    size,
                    checksum,
                    provider_identity,
                    state,
                    now,
                    source,
                    policy,
                ),
            )
            if result.rowcount != 1:
                raise KeyError((source, policy))
            row = connection.execute(
                "SELECT * FROM media_files WHERE source=? AND policy=?",
                (source, policy),
            ).fetchone()
        return self._record(row)

    def delete(self, source: str, policy: str) -> bool:
        with self.database.transaction() as connection:
            result = connection.execute(
                "DELETE FROM media_files WHERE source=? AND policy=?",
                (source, policy),
            )
        return result.rowcount == 1

    @staticmethod
    def _record(row) -> MediaFile:
        return MediaFile(
            source=row["source"],
            policy=row["policy"],
            relative_path=row["relative_path"],
            container=row["container"],
            size=row["size"],
            checksum=row["checksum"],
            provider_identity=row["provider_identity"],
            state=row["state"],
            created=row["created"],
            updated=row["updated"],
        )

    @staticmethod
    def _validate(media_file: MediaFile):
        if media_file.policy not in RENDITIONS:
            raise ValueError("Unknown audio policy")
        if media_file.state not in MEDIA_FILE_STATES:
            raise ValueError("Unknown media file state")
        if media_file.size is not None and media_file.size < 0:
            raise ValueError("Media file size cannot be negative")
