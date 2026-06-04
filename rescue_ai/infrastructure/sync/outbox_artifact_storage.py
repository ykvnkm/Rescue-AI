"""ArtifactStorage-обёртка, поднимающая outbox-запись на каждый upload.

Используется в offline-профиле (ADR-0007 §3, диплом §3.5.1). При записи
артефакта (кадр миссии, отчёт, график траектории, COCO-labels) wrapper:

  1. Делегирует во внутреннее хранилище (локальный MinIO).
  2. Извлекает S3-key возвращённого URI.
  3. Enqueue в ``replication_outbox`` строку с координатами источника
     (локальный MinIO) и назначения (удалённое S3).

Sync-worker потом дренирует эти записи в фоне, копируя объекты из
локального в удалённое S3 через S3-to-S3 GET/PUT при наличии связи.

В cloud-профиле обёртка НЕ применяется — там api пишет сразу в Yandex
Object Storage, и второй уровень репликации не нужен.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from rescue_ai.domain.entities import TrajectoryPoint
from rescue_ai.domain.ports import ArtifactStorage, OutboxRecord, SyncOutbox
from rescue_ai.domain.value_objects import ArtifactBlob


class OutboxArtifactStorage:
    """Wrapper над ``ArtifactStorage``, прозрачно ставящий outbox-записи.

    Аналог ``offline_first_repositories.OutboxMissionRepository`` —
    оборачивает write-методы inner-хранилища, read-методы пропускает
    как есть.

    Параметры:
        inner            : фактический ``ArtifactStorage`` (S3-бэкенд,
                           пишет в локальный MinIO).
        outbox           : ``SyncOutbox`` для постановки записей.
        local_bucket     : имя локального S3-бакета (источник копии).
        remote_bucket    : имя удалённого S3-бакета (цель копии).
        local_prefix     : префикс ключа в локальном S3, может быть
                           пустым; нужен только если cloud-side хранит
                           объекты под другим prefix.
        remote_prefix    : префикс ключа в удалённом S3; обычно
                           совпадает с ``local_prefix``.

    По умолчанию ``remote_prefix`` и ``local_prefix`` одинаковы — это
    даёт совпадение ключей и упрощает дедупликацию (один и тот же
    объект «миссия/кадр» имеет один и тот же относительный путь и в
    станционном MinIO, и в облачном S3).
    """

    def __init__(
        self,
        *,
        inner: ArtifactStorage,
        outbox: SyncOutbox,
        local_bucket: str,
        remote_bucket: str,
        local_prefix: str = "",
        remote_prefix: str = "",
    ) -> None:
        self._inner = inner
        self._outbox = outbox
        self._local_bucket = local_bucket
        self._remote_bucket = remote_bucket
        self._local_prefix = local_prefix.strip("/")
        self._remote_prefix = remote_prefix.strip("/")

    # ── Write-методы: делегируем + enqueue ──────────────────────

    def store_frame(
        self,
        mission_id: str,
        frame_id: int,
        source_uri: str,
        ds: str,
        *,
        frame_bgr: object | None = None,
    ) -> str:
        uri = self._inner.store_frame(
            mission_id, frame_id, source_uri, ds, frame_bgr=frame_bgr
        )
        self._enqueue_if_s3(
            uri,
            entity_type="frame",
            entity_id=f"{mission_id}:{frame_id}",
            operation="upload",
        )
        return uri

    def save_mission_report(
        self, mission_id: str, ds: str, report: Mapping[str, object]
    ) -> str:
        uri = self._inner.save_mission_report(mission_id, ds, report)
        self._enqueue_if_s3(
            uri,
            entity_type="mission_report",
            entity_id=mission_id,
            operation="upload",
        )
        return uri

    def save_mission_annotations(
        self, mission_id: str, ds: str, payload: Mapping[str, object]
    ) -> str:
        uri = self._inner.save_mission_annotations(mission_id, ds, payload)
        self._enqueue_if_s3(
            uri,
            entity_type="mission_labels",
            entity_id=mission_id,
            operation="upload",
        )
        return uri

    def save_trajectory_csv(
        self,
        mission_id: str,
        ds: str,
        points: Sequence[TrajectoryPoint],
        *,
        origin: tuple[float, float] | None = None,
    ) -> str:
        uri = self._inner.save_trajectory_csv(mission_id, ds, points, origin=origin)
        self._enqueue_if_s3(
            uri,
            entity_type="trajectory_csv",
            entity_id=mission_id,
            operation="upload",
        )
        return uri

    def save_trajectory_plot(self, mission_id: str, ds: str, png_bytes: bytes) -> str:
        uri = self._inner.save_trajectory_plot(mission_id, ds, png_bytes)
        self._enqueue_if_s3(
            uri,
            entity_type="trajectory_plot",
            entity_id=mission_id,
            operation="upload",
        )
        return uri

    # ── Read-методы: проброс без побочных эффектов ──────────────

    def load_frame(self, image_uri: str) -> ArtifactBlob | None:
        return self._inner.load_frame(image_uri)

    def load_mission_report(
        self, mission_id: str, ds: str
    ) -> Mapping[str, object] | None:
        return self._inner.load_mission_report(mission_id, ds)

    def load_trajectory_plot(self, mission_id: str, ds: str) -> ArtifactBlob | None:
        return self._inner.load_trajectory_plot(mission_id, ds)

    def load_trajectory_csv(self, mission_id: str, ds: str) -> ArtifactBlob | None:
        return self._inner.load_trajectory_csv(mission_id, ds)

    # ── Внутренняя кухня ───────────────────────────────────────

    def _enqueue_if_s3(
        self,
        uri: str,
        *,
        entity_type: str,
        entity_id: str,
        operation: str,
    ) -> None:
        """Поставить outbox-запись если возвращённый URI указывает на S3.

        Inner-storage в некоторых сценариях возвращает не-S3 URI
        (например ``store_frame`` возвращает source_uri если файл не
        найден локально — это no-op). Такие URI пропускаем, outbox не
        растёт мусором.
        """
        parsed = _parse_s3_uri(uri)
        if parsed is None:
            return
        _, key = parsed
        remote_key = self._remap_prefix(key)
        self._outbox.enqueue(
            OutboxRecord(
                entity_type=entity_type,
                entity_id=entity_id,
                operation=operation,
                payload_json={"local_uri": uri, "remote_key": remote_key},
                idempotency_key=f"s3:{self._remote_bucket}:{remote_key}",
                source_s3_bucket=self._local_bucket,
                source_s3_key=key,
                s3_bucket=self._remote_bucket,
                s3_key=remote_key,
            )
        )

    def _remap_prefix(self, key: str) -> str:
        """Заменить local_prefix на remote_prefix в начале ключа.

        Если префиксы совпадают (типичный случай) — возвращает key
        как есть. Если local_prefix задан, но ключ под него не
        подпадает — возвращает key без изменений (защита от
        неконсистентных данных).
        """
        if self._local_prefix == self._remote_prefix:
            return key
        if not self._local_prefix:
            return f"{self._remote_prefix}/{key}".strip("/")
        if key.startswith(f"{self._local_prefix}/"):
            tail = key[len(self._local_prefix) + 1 :]
            if not self._remote_prefix:
                return tail
            return f"{self._remote_prefix}/{tail}"
        return key


def _parse_s3_uri(uri: str) -> tuple[str, str] | None:
    """``s3://bucket/some/key`` → (``bucket``, ``some/key``)."""
    if not uri.startswith("s3://"):
        return None
    rest = uri[5:]
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        return None
    return bucket, key


__all__ = ["OutboxArtifactStorage"]
