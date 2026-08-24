import hashlib
import io
import json
from pathlib import Path

import pytest

from mi_remote_linux.model_manager import (
    ModelError,
    ModelFileSpec,
    ModelManager,
    default_model_dir,
    render_model_status,
    resolve_model_dir,
)


def spec(name: str, payload: bytes) -> ModelFileSpec:
    return ModelFileSpec(
        name=name,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def fake_opener(payloads: dict[str, bytes], opened: list[str]):
    def open_url(request, *, timeout):
        assert timeout == 60
        url = request.full_url
        opened.append(url)
        return io.BytesIO(payloads[url.rsplit("/", 1)[-1]])

    return open_url


def test_default_and_resolved_model_dirs_follow_xdg_and_existing_voxtype(tmp_path: Path):
    environment = {"XDG_DATA_HOME": str(tmp_path)}
    expected_default = tmp_path / "mi-remote-linux/models/paraformer-zh"
    voxtype = tmp_path / "voxtype/models/paraformer-zh"
    voxtype.mkdir(parents=True)
    (voxtype / "model.int8.onnx").write_bytes(b"model")
    (voxtype / "tokens.txt").write_bytes(b"tokens")

    assert default_model_dir(environment) == expected_default
    assert resolve_model_dir(environment=environment) == voxtype


def test_environment_model_dir_has_priority(tmp_path: Path):
    configured = tmp_path / "configured"
    environment = {
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "MI_REMOTE_PARAFORMER_MODEL_DIR": str(configured),
    }

    assert resolve_model_dir(environment=environment) == configured


def test_status_reports_missing_valid_and_invalid_files(tmp_path: Path):
    files = (spec("model.bin", b"model"), spec("tokens.txt", b"tokens"))
    manager = ModelManager(target=tmp_path, files=files)
    (tmp_path / "model.bin").write_bytes(b"model")
    (tmp_path / "tokens.txt").write_bytes(b"broken")

    status = manager.status()

    assert status.ready is False
    assert [item.state for item in status.files] == ["valid", "invalid"]
    assert status.files[1].actual_size == 6
    assert json.loads(status.to_json())["ready"] is False
    assert "校验失败" in render_model_status(status)


def test_download_skips_verified_files_and_fetches_only_missing(tmp_path: Path):
    payloads = {"model.bin": b"model", "tokens.txt": b"tokens"}
    files = tuple(spec(name, payload) for name, payload in payloads.items())
    (tmp_path / "model.bin").write_bytes(payloads["model.bin"])
    opened: list[str] = []
    manager = ModelManager(
        target=tmp_path,
        files=files,
        base_url="https://example.invalid/model",
        opener=fake_opener(payloads, opened),
    )

    result = manager.download()

    assert result.downloaded == ("tokens.txt",)
    assert result.skipped == ("model.bin",)
    assert opened == ["https://example.invalid/model/tokens.txt"]
    assert manager.status().ready is True
    assert not list(tmp_path.glob("*.part-*"))


def test_download_refuses_existing_invalid_file_before_network_without_force(tmp_path: Path):
    payload = b"expected"
    (tmp_path / "model.bin").write_bytes(b"broken")
    opened: list[str] = []
    manager = ModelManager(
        target=tmp_path,
        files=(spec("model.bin", payload),),
        opener=fake_opener({"model.bin": payload}, opened),
    )

    with pytest.raises(ModelError, match="--force"):
        manager.download()

    assert opened == []
    assert (tmp_path / "model.bin").read_bytes() == b"broken"


def test_force_download_atomically_replaces_invalid_file(tmp_path: Path):
    payload = b"expected"
    (tmp_path / "model.bin").write_bytes(b"broken")
    manager = ModelManager(
        target=tmp_path,
        files=(spec("model.bin", payload),),
        opener=fake_opener({"model.bin": payload}, []),
    )

    result = manager.download(force=True)

    assert result.downloaded == ("model.bin",)
    assert (tmp_path / "model.bin").read_bytes() == payload


def test_failed_checksum_keeps_existing_file_and_removes_partial(tmp_path: Path):
    expected = b"expected"
    original = b"original"
    (tmp_path / "model.bin").write_bytes(original)
    manager = ModelManager(
        target=tmp_path,
        files=(spec("model.bin", expected),),
        opener=fake_opener({"model.bin": b"wrong-data"}, []),
    )

    with pytest.raises(ModelError, match="校验失败"):
        manager.download(force=True)

    assert (tmp_path / "model.bin").read_bytes() == original
    assert not list(tmp_path.glob("*.part-*"))


def test_download_cleans_partial_after_network_failure(tmp_path: Path):
    def failing_opener(_request, *, timeout):
        assert timeout == 60
        raise OSError("offline")

    manager = ModelManager(
        target=tmp_path,
        files=(spec("model.bin", b"expected"),),
        opener=failing_opener,
    )

    with pytest.raises(ModelError, match="下载失败"):
        manager.download()

    assert not (tmp_path / "model.bin").exists()
    assert not list(tmp_path.glob("*.part-*"))
