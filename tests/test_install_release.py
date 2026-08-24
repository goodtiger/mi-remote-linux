import hashlib
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/install_release.py"
SPEC = importlib.util.spec_from_file_location("install_release", SCRIPT)
assert SPEC and SPEC.loader
install_release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(install_release)


def test_release_asset_names_and_urls_are_deterministic():
    assets = install_release.release_assets("v0.4.0")

    assert assets.tag == "v0.4.0"
    assert assets.wheel_name == "mi_remote_linux-0.4.0-py3-none-any.whl"
    assert assets.wheel_url.endswith("/releases/download/v0.4.0/" + assets.wheel_name)
    assert assets.checksums_url.endswith("/releases/download/v0.4.0/SHA256SUMS")


@pytest.mark.parametrize("tag", ["0.4.0", "v0.4", "v01.2.3", "latest", "v1.2.3rc1"])
def test_release_assets_reject_ambiguous_versions(tag: str):
    with pytest.raises(ValueError, match="vMAJOR.MINOR.PATCH"):
        install_release.release_assets(tag)


def test_parse_checksums_and_verify_file(tmp_path: Path):
    wheel = tmp_path / "mi_remote_linux-0.4.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel payload")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    checksums = install_release.parse_checksums(f"{digest}  {wheel.name}\n")

    assert checksums == {wheel.name: digest}
    install_release.verify_file(wheel, checksums[wheel.name])

    wheel.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="SHA-256"):
        install_release.verify_file(wheel, checksums[wheel.name])


def test_parse_checksums_rejects_unsafe_or_duplicate_names():
    digest = "a" * 64
    with pytest.raises(ValueError, match="不安全"):
        install_release.parse_checksums(f"{digest}  ../package.whl\n")
    with pytest.raises(ValueError, match="重复"):
        install_release.parse_checksums(f"{digest}  a.whl\n{digest}  a.whl\n")


def test_dry_run_does_not_create_installation(tmp_path: Path):
    environment = {
        "HOME": str(tmp_path / "home"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_BIN_HOME": str(tmp_path / "bin"),
    }

    result = subprocess.run(
        [sys.executable, SCRIPT, "--version", "v0.4.0", "--dry-run"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0
    assert "不会修改系统" in result.stdout
    assert "v0.4.0" in result.stdout
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / "bin").exists()
