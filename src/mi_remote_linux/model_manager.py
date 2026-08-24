"""Paraformer model discovery, integrity checks, and safe downloads."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO, Literal

MODEL_REPOSITORY = "csukuangfj/sherpa-onnx-paraformer-zh-2023-09-14"
MODEL_REVISION = "def027084691107096b5ebba69785756d63de6c5"
MODEL_BASE_URL = f"https://huggingface.co/{MODEL_REPOSITORY}/resolve/{MODEL_REVISION}"


@dataclass(frozen=True)
class ModelFileSpec:
    name: str
    size: int
    sha256: str


MODEL_FILES = (
    ModelFileSpec(
        "model.int8.onnx",
        243_371_218,
        "f36a0433bcf096bd6d6f11b80a3ac8bed110bdca632fe0d731df8d1a84475945",
    ),
    ModelFileSpec(
        "tokens.txt",
        75_756,
        "59aba8873a2ed1e122c25fee421e25f283b63290efbde85c1f01a853d83cb6e6",
    ),
)

FileState = Literal["missing", "valid", "invalid"]
ProgressCallback = Callable[[str, int, int | None], None]


class ModelError(RuntimeError):
    """A model operation failed without leaving a partial installation."""


@dataclass(frozen=True)
class ModelFileStatus:
    name: str
    state: FileState
    expected_size: int
    actual_size: int | None
    expected_sha256: str
    actual_sha256: str | None


@dataclass(frozen=True)
class ModelStatus:
    target: Path
    repository: str
    revision: str
    files: tuple[ModelFileStatus, ...]

    @property
    def ready(self) -> bool:
        return all(item.state == "valid" for item in self.files)

    def to_json(self) -> str:
        return json.dumps(
            {
                "target": str(self.target),
                "repository": self.repository,
                "revision": self.revision,
                "ready": self.ready,
                "files": [asdict(item) for item in self.files],
            },
            ensure_ascii=False,
            indent=2,
        )


@dataclass(frozen=True)
class DownloadResult:
    target: Path
    downloaded: tuple[str, ...]
    skipped: tuple[str, ...]


def default_model_dir(environment: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environment is None else environment
    data_home = Path(env.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data_home.expanduser() / "mi-remote-linux/models/paraformer-zh"


def model_files_present(path: Path) -> bool:
    return all((path / item.name).is_file() for item in MODEL_FILES)


def resolve_model_dir(
    configured: str | Path | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the active model, retaining compatibility with Voxtype installs."""
    if configured is not None:
        return Path(configured).expanduser()

    env = os.environ if environment is None else environment
    project_dir = default_model_dir(env)
    data_home = project_dir.parents[2]
    candidates = [project_dir, data_home / "voxtype/models/paraformer-zh"]
    env_dir = env.get("MI_REMOTE_PARAFORMER_MODEL_DIR")
    if env_dir:
        candidates.insert(0, Path(env_dir).expanduser())
    return next(
        (candidate for candidate in candidates if model_files_present(candidate)), candidates[0]
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class ModelManager:
    def __init__(
        self,
        *,
        target: str | Path | None = None,
        files: Sequence[ModelFileSpec] = MODEL_FILES,
        base_url: str = MODEL_BASE_URL,
        opener: Callable[..., BinaryIO] = urllib.request.urlopen,
    ):
        self.target = resolve_model_dir() if target is None else Path(target).expanduser().resolve()
        self.files = tuple(files)
        self.base_url = base_url.rstrip("/")
        self.opener = opener
        for item in self.files:
            if Path(item.name).name != item.name or item.size < 0:
                raise ValueError(f"invalid model file specification: {item.name}")

    def status(self) -> ModelStatus:
        statuses = []
        for item in self.files:
            path = self.target / item.name
            if not path.is_file():
                statuses.append(
                    ModelFileStatus(item.name, "missing", item.size, None, item.sha256, None)
                )
                continue
            actual_size = path.stat().st_size
            actual_sha256 = _sha256(path) if actual_size == item.size else None
            state: FileState = (
                "valid" if actual_size == item.size and actual_sha256 == item.sha256 else "invalid"
            )
            statuses.append(
                ModelFileStatus(
                    item.name,
                    state,
                    item.size,
                    actual_size,
                    item.sha256,
                    actual_sha256,
                )
            )
        return ModelStatus(self.target, MODEL_REPOSITORY, MODEL_REVISION, tuple(statuses))

    def download(
        self,
        *,
        force: bool = False,
        progress: ProgressCallback | None = None,
    ) -> DownloadResult:
        initial = self.status()
        invalid = [item.name for item in initial.files if item.state == "invalid"]
        if invalid and not force:
            names = "、".join(invalid)
            raise ModelError(f"现有模型文件校验失败：{names}；确认覆盖请加 --force")

        self.target.mkdir(parents=True, exist_ok=True)
        downloaded = []
        skipped = []
        for item, state in zip(self.files, initial.files, strict=True):
            if state.state == "valid":
                skipped.append(item.name)
                continue
            self._download_file(item, progress)
            downloaded.append(item.name)
        return DownloadResult(self.target, tuple(downloaded), tuple(skipped))

    def _download_file(
        self,
        item: ModelFileSpec,
        progress: ProgressCallback | None,
    ) -> None:
        destination = self.target / item.name
        fd, partial_name = tempfile.mkstemp(
            dir=self.target,
            prefix=f".{item.name}.part-",
        )
        os.close(fd)
        partial = Path(partial_name)
        request = urllib.request.Request(
            f"{self.base_url}/{item.name}",
            headers={"User-Agent": "mi-remote-linux"},
        )
        try:
            with self.opener(request, timeout=60) as response, partial.open("wb") as output:
                header_value = getattr(response, "headers", {}).get("Content-Length")
                total = int(header_value) if header_value else None
                received = 0
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
                    received += len(chunk)
                    if progress:
                        progress(item.name, received, total)

            actual_size = partial.stat().st_size
            actual_sha256 = _sha256(partial) if actual_size == item.size else None
            if actual_size != item.size or actual_sha256 != item.sha256:
                raise ModelError(f"{item.name} 校验失败（大小 {actual_size}/{item.size} 字节）")
            os.replace(partial, destination)
        except ModelError:
            raise
        except Exception as exc:
            raise ModelError(f"{item.name} 下载失败：{exc}") from exc
        finally:
            partial.unlink(missing_ok=True)


def render_model_status(status: ModelStatus) -> str:
    symbols = {"valid": "✓", "missing": "-", "invalid": "✗"}
    labels = {"valid": "校验通过", "missing": "缺失", "invalid": "校验失败"}
    lines = [f"Paraformer 模型：{status.target}", f"来源：{MODEL_REPOSITORY}", ""]
    for item in status.files:
        size = f"{item.actual_size:,} 字节" if item.actual_size is not None else "未安装"
        lines.append(f"{symbols[item.state]} {item.name}: {labels[item.state]}（{size}）")
    lines.append("")
    lines.append("状态：可用" if status.ready else "状态：不可用")
    return "\n".join(lines)
