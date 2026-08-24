import re
import subprocess
import sys
from pathlib import Path

import tomllib

from mi_remote_linux import __version__

ROOT = Path(__file__).resolve().parents[1]


def test_project_uses_single_dynamic_version_source():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert "version" not in project["project"]
    assert project["project"]["dynamic"] == ["version"]
    assert project["tool"]["setuptools"]["dynamic"]["version"] == {
        "attr": "mi_remote_linux.__version__"
    }
    assert re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", __version__)


def test_release_check_accepts_only_matching_stable_tag():
    script = ROOT / "scripts/check_release.py"

    accepted = subprocess.run(
        [sys.executable, script, f"v{__version__}"],
        check=False,
        capture_output=True,
        text=True,
    )
    mismatch = subprocess.run(
        [sys.executable, script, "v9.9.9"],
        check=False,
        capture_output=True,
        text=True,
    )
    malformed = subprocess.run(
        [sys.executable, script, __version__],
        check=False,
        capture_output=True,
        text=True,
    )

    assert accepted.returncode == 0
    assert f"v{__version__}" in accepted.stdout
    assert mismatch.returncode == 1
    assert "不匹配" in mismatch.stderr
    assert malformed.returncode == 1
    assert "vMAJOR.MINOR.PATCH" in malformed.stderr


def test_release_workflow_has_publish_guards_and_artifact_checks():
    workflow = (ROOT / ".github/workflows/release.yml").read_text()

    assert "tags:" in workflow
    assert '"v*.*.*"' in workflow
    assert "workflow_dispatch" not in workflow
    assert "python scripts/check_release.py" in workflow
    assert "pytest -q" in workflow
    assert "twine check dist/*" in workflow
    assert "sha256sum" in workflow
    assert "install_release.py dist/install-mi-remote.py" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "gh release create" in workflow
    assert "--verify-tag" in workflow
    assert "contents: write" in workflow
    assert workflow.index("python scripts/check_release.py") < workflow.index("gh release create")
