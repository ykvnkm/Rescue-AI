"""Unit tests for RpiClient HTTP integration adapter."""

from __future__ import annotations

from rescue_ai.config import RpiSettings
from rescue_ai.infrastructure.rpi_client import RpiClient, _build_gt_sequence_from_coco


class _Response:
    def __init__(self, payload: dict[str, object], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


def test_rpi_client_health_catalog_and_session_calls(monkeypatch) -> None:
    calls: list[tuple[str, str, dict[str, object] | None, float]] = []

    def _fake_get(url: str, timeout: float):
        calls.append(("GET", url, None, timeout))
        if url.endswith("/health"):
            return _Response({"status": "ok"})
        if url.endswith("/mission/catalog"):
            return _Response(
                {
                    "missions": [
                        {
                            "id": "m1",
                            "name": "Mission 1",
                            "images_dir": "/missions/m1/images",
                            "annotations_json": "/missions/m1/ann.json",
                        }
                    ]
                }
            )
        if "/source/session/" in url:
            return _Response({"processed": 10})
        raise AssertionError(f"Unexpected URL: {url}")

    def _fake_post(url: str, json=None, timeout: float = 0):
        calls.append(("POST", url, json, timeout))
        if url.endswith("/source/start"):
            return _Response({"session_id": "sess-1"})
        if "/source/stop/" in url:
            return _Response({"stopped": True})
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr("rescue_ai.infrastructure.rpi_client.httpx.get", _fake_get)
    monkeypatch.setattr("rescue_ai.infrastructure.rpi_client.httpx.post", _fake_post)

    client = RpiClient(
        RpiSettings(
            RPI_BASE_URL="http://192.168.0.118:9100",
            RPI_MISSIONS_DIR="/home/ykvnkm/Documents/missions",
            RPI_RTSP_PORT=8554,
            RPI_RTSP_PATH_PREFIX="live",
        )
    )

    health = client.health(timeout_sec=1.0)
    assert health["status"] == "ok"

    catalog = client.catalog(timeout_sec=2.0)
    assert len(catalog.missions) == 1
    assert catalog.missions[0].mission_id == "m1"

    session = client.start_stream("m1", target_fps=6.0, timeout_sec=3.0)
    assert session.session_id == "sess-1"
    assert session.rtsp_url == "rtsp://192.168.0.118:8554/live/sess-1"

    stop_payload = client.stop_stream("sess-1", timeout_sec=4.0)
    assert stop_payload["stopped"] is True

    stats = client.session_stats("sess-1", timeout_sec=5.0)
    assert stats["processed"] == 10
    assert client.base_url == "http://192.168.0.118:9100"

    start_call = [item for item in calls if item[1].endswith("/source/start")][0]
    assert start_call[2] is not None
    assert start_call[2]["source"] == "/home/ykvnkm/Documents/missions/m1"


def test_load_gt_sequence_from_raw_file(monkeypatch) -> None:
    def _fake_get(url: str, timeout: float, params=None):
        if url.endswith("/mission/catalog"):
            return _Response(
                {
                    "missions": [
                        {
                            "id": "m1",
                            "name": "Mission 1",
                            "images_dir": "/missions/m1/images",
                            "annotations_json": "/missions/m1/ann.json",
                        }
                    ]
                }
            )
        if url.endswith("/source/raw_file"):
            assert params == {"path": "/missions/m1/ann.json"}
            return _Response(
                {
                    "images": [
                        {"id": 1, "file_name": "0001.jpg"},
                        {"id": 2, "file_name": "0002.jpg"},
                        {"id": 3, "file_name": "0003.jpg"},
                    ],
                    "categories": [{"id": 1, "name": "person"}],
                    "annotations": [
                        {"id": 10, "image_id": 1, "category_id": 1},
                        {"id": 11, "image_id": 3, "category_id": 1},
                    ],
                }
            )
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr("rescue_ai.infrastructure.rpi_client.httpx.get", _fake_get)
    client = RpiClient(
        RpiSettings(
            RPI_BASE_URL="http://192.168.0.118:9100",
            RPI_MISSIONS_DIR="/home/ykvnkm/Documents/missions",
            RPI_RTSP_PORT=8554,
            RPI_RTSP_PATH_PREFIX="live",
        )
    )

    gt = client.load_gt_sequence("m1", timeout_sec=2.0)
    assert gt == [True, False, True]


def test_build_gt_sequence_filters_non_person_annotations() -> None:
    payload = {
        "images": [
            {"id": 2, "file_name": "0002.jpg"},
            {"id": 1, "file_name": "0001.jpg"},
        ],
        "categories": [{"id": 1, "name": "person"}, {"id": 2, "name": "car"}],
        "annotations": [
            {"image_id": 2, "category_id": 2},
            {"image_id": 1, "category_id": 1},
        ],
    }

    assert _build_gt_sequence_from_coco(payload) == [True, False]


def test_build_gt_sequence_uses_numeric_filename_gaps_as_negatives() -> None:
    payload = {
        "images": [
            {"id": 1, "file_name": "000002.jpg"},
            {"id": 2, "file_name": "000007.jpg"},
            {"id": 3, "file_name": "000011.jpg"},
        ],
        "categories": [{"id": 1, "name": "person"}],
        "annotations": [
            {"image_id": 1, "category_id": 1},
            {"image_id": 3, "category_id": 1},
        ],
    }

    # Sequence spans 2..11; only frames 2 and 11 are positive.
    seq = _build_gt_sequence_from_coco(payload)
    assert seq is not None
    assert len(seq) == 10
    assert seq[0] is True   # frame 2
    assert seq[9] is True   # frame 11
    assert sum(1 for item in seq if item) == 2
