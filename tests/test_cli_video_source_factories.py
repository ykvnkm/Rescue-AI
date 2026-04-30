"""Coverage tests for CLI video-source factory helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from rescue_ai.interfaces.cli import auto_video_source_factory as auto_factory_mod
from rescue_ai.interfaces.cli import video_source_factory as factory_mod


def test_plain_factory_local_and_rtsp_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: dict[str, tuple[object, ...]] = {}

    class _FakeFileSource:
        def __init__(self, path: str, fps_override: float) -> None:
            calls["file"] = (path, fps_override)

    class _FakeFolderSource:
        def __init__(self, path: str, fps: float) -> None:
            calls["frames"] = (path, fps)

    class _FakeRtspSource:
        def __init__(self, url: str) -> None:
            calls["rtsp"] = (url,)

    monkeypatch.setattr(factory_mod, "FileVideoSource", _FakeFileSource)
    monkeypatch.setattr(factory_mod, "FolderFramesSource", _FakeFolderSource)
    monkeypatch.setattr(factory_mod, "RTSPVideoSource", _FakeRtspSource)

    video_path = tmp_path / "sample.mp4"
    video_path.write_bytes(b"0")
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()

    src, resolved = factory_mod.auto_video_source_factory("video", str(video_path), 7.5)
    assert isinstance(src, _FakeFileSource)
    assert resolved == str(video_path)
    assert calls["file"] == (str(video_path), 7.5)

    src, resolved = factory_mod.auto_video_source_factory(
        "frames", str(frames_dir), 3.0
    )
    assert isinstance(src, _FakeFolderSource)
    assert resolved == str(frames_dir)
    assert calls["frames"] == (str(frames_dir), 3.0)

    src, resolved = factory_mod.auto_video_source_factory(
        "rtsp", "rtsp://cam/stream", 2.0
    )
    assert isinstance(src, _FakeRtspSource)
    assert resolved == "rtsp://cam/stream"
    assert calls["rtsp"] == ("rtsp://cam/stream",)


def test_plain_factory_validation_errors(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        factory_mod.auto_video_source_factory(
            "video", str(tmp_path / "missing.mp4"), 1.0
        )
    with pytest.raises(FileNotFoundError):
        factory_mod.auto_video_source_factory("frames", str(tmp_path / "missing"), 1.0)
    with pytest.raises(ValueError):
        factory_mod.auto_video_source_factory("rtsp", "", 1.0)
    with pytest.raises(ValueError):
        factory_mod.auto_video_source_factory("unknown", "x", 1.0)


def test_auto_factory_rpi_stream_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeRpiClient:
        def __init__(self, settings) -> None:
            self.settings = settings

    class _FakeRemoteSource:
        def __init__(self, *, rpi_client, mission_id: str, target_fps: float) -> None:
            self.client = rpi_client
            self.mission_id = mission_id
            self.target_fps = target_fps
            self.session_id = "sess-1"

    monkeypatch.setattr(auto_factory_mod, "RpiClient", _FakeRpiClient)
    monkeypatch.setattr(auto_factory_mod, "RemoteRpiVideoSource", _FakeRemoteSource)
    monkeypatch.setattr(
        auto_factory_mod,
        "get_settings",
        lambda: SimpleNamespace(rpi=SimpleNamespace(base_url="http://rpi")),
    )

    src, resolved = auto_factory_mod.auto_video_source_factory(
        "video", "", 4.0, rpi_mission_id="m-1"
    )
    assert isinstance(src, _FakeRemoteSource)
    assert resolved == "rpi:m-1:sess-1"


def test_auto_factory_rpi_stream_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError):
        auto_factory_mod.auto_video_source_factory("rtsp", "", 1.0, rpi_mission_id="m")

    monkeypatch.setattr(
        auto_factory_mod,
        "get_settings",
        lambda: SimpleNamespace(rpi=SimpleNamespace(base_url="")),
    )
    with pytest.raises(RuntimeError):
        auto_factory_mod.auto_video_source_factory("video", "", 1.0, rpi_mission_id="m")


def test_auto_factory_local_modes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: dict[str, tuple[object, ...]] = {}

    class _FakeFileSource:
        def __init__(self, path: str, fps_override: float, loop: bool) -> None:
            calls["file"] = (path, fps_override, loop)

    class _FakeFolderSource:
        def __init__(self, path: str, fps: float) -> None:
            calls["frames"] = (path, fps)

    class _FakeRtspSource:
        def __init__(self, url: str) -> None:
            calls["rtsp"] = (url,)

    monkeypatch.setattr(auto_factory_mod, "FileVideoSource", _FakeFileSource)
    monkeypatch.setattr(auto_factory_mod, "FolderFramesSource", _FakeFolderSource)
    monkeypatch.setattr(auto_factory_mod, "RTSPVideoSource", _FakeRtspSource)

    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"1")
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()

    src, resolved = auto_factory_mod.auto_video_source_factory(
        "video", str(video_path), 8.0, demo_loop=True
    )
    assert isinstance(src, _FakeFileSource)
    assert resolved == str(video_path)
    assert calls["file"] == (str(video_path), 8.0, True)

    src, resolved = auto_factory_mod.auto_video_source_factory(
        "frames", str(frames_dir), 5.0
    )
    assert isinstance(src, _FakeFolderSource)
    assert resolved == str(frames_dir)
    assert calls["frames"] == (str(frames_dir), 5.0)

    src, resolved = auto_factory_mod.auto_video_source_factory("rtsp", "rtsp://x", 2.0)
    assert isinstance(src, _FakeRtspSource)
    assert resolved == "rtsp://x"


def test_auto_factory_local_validation_errors(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        auto_factory_mod.auto_video_source_factory(
            "video", str(tmp_path / "none.mp4"), 1.0
        )
    with pytest.raises(FileNotFoundError):
        auto_factory_mod.auto_video_source_factory(
            "frames", str(tmp_path / "none"), 1.0
        )
    with pytest.raises(ValueError):
        auto_factory_mod.auto_video_source_factory("rtsp", "", 1.0)
    with pytest.raises(ValueError):
        auto_factory_mod.auto_video_source_factory("xxx", "a", 1.0)
