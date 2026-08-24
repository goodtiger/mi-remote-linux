from pathlib import Path
from types import SimpleNamespace

import pytest

from mi_remote_linux.service_manager import (
    GENERATED_MARKER,
    UDEV_RULES,
    ServiceError,
    ServiceManager,
)


def successful_runner(commands):
    def run(argv):
        commands.append(tuple(argv))
        verb = argv[2] if len(argv) > 2 else ""
        output = {"is-enabled": "enabled\n", "is-active": "active\n"}.get(verb, "")
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    return run


def test_service_render_uses_resolved_executable_and_safe_percent_escaping(tmp_path: Path):
    manager = ServiceManager(
        executable="/opt/Mi Remote/bin/mi-remote%test",
        config_home=tmp_path,
        command_runner=successful_runner([]),
    )

    unit = manager.render(address="AA:BB:CC:DD:EE:FF")

    assert unit.startswith(GENERATED_MARKER)
    assert 'ExecStart="/opt/Mi Remote/bin/mi-remote%%test" "voice"' in unit
    assert '"--config" "--inject"' in unit
    assert '"--address" "AA:BB:CC:DD:EE:FF"' in unit


def test_install_is_preview_only_without_apply(tmp_path: Path):
    commands = []
    manager = ServiceManager(
        executable="/usr/bin/mi-remote",
        config_home=tmp_path,
        command_runner=successful_runner(commands),
    )

    result = manager.install(apply=False)

    assert result.applied is False
    assert result.changed is False
    assert not manager.service_path.exists()
    assert commands == []
    assert "ExecStart" in result.unit


def test_apply_installs_and_starts_user_service(tmp_path: Path):
    commands = []
    manager = ServiceManager(
        executable="/usr/bin/mi-remote",
        config_home=tmp_path,
        command_runner=successful_runner(commands),
    )

    result = manager.install(apply=True)

    assert result.applied is True
    assert result.changed is True
    assert manager.service_path.read_text().startswith(GENERATED_MARKER)
    assert commands == [
        ("systemctl", "--user", "daemon-reload"),
        ("systemctl", "--user", "enable", "--now", "mi-remote.service"),
    ]


def test_install_refuses_to_overwrite_existing_unit_without_force(tmp_path: Path):
    manager = ServiceManager(
        executable="/usr/bin/mi-remote",
        config_home=tmp_path,
        command_runner=successful_runner([]),
    )
    manager.service_path.parent.mkdir(parents=True)
    manager.service_path.write_text("# user owned\n")

    with pytest.raises(ServiceError, match="--force"):
        manager.install(apply=True)

    assert manager.service_path.read_text() == "# user owned\n"


def test_uninstall_requires_apply_and_removes_only_generated_unit(tmp_path: Path):
    commands = []
    manager = ServiceManager(
        executable="/usr/bin/mi-remote",
        config_home=tmp_path,
        command_runner=successful_runner(commands),
    )
    manager.install(apply=True)
    commands.clear()

    preview = manager.uninstall(apply=False)
    assert preview.applied is False
    assert manager.service_path.exists()

    result = manager.uninstall(apply=True)
    assert result.changed is True
    assert not manager.service_path.exists()
    assert commands == [
        ("systemctl", "--user", "disable", "--now", "mi-remote.service"),
        ("systemctl", "--user", "daemon-reload"),
    ]


def test_status_is_read_only_and_reports_systemd_state(tmp_path: Path):
    commands = []
    manager = ServiceManager(
        executable="/usr/bin/mi-remote",
        config_home=tmp_path,
        command_runner=successful_runner(commands),
    )
    manager.service_path.parent.mkdir(parents=True)
    manager.service_path.write_text(manager.render())

    status = manager.status()

    assert status.installed is True
    assert status.enabled == "enabled"
    assert status.active == "active"
    assert commands == [
        ("systemctl", "--user", "is-enabled", "mi-remote.service"),
        ("systemctl", "--user", "is-active", "mi-remote.service"),
    ]


def test_packaged_udev_rules_are_scoped_to_rc003_and_uinput():
    assert 'ATTRS{idVendor}=="2717"' in UDEV_RULES
    assert 'ATTRS{idProduct}=="32b8"' in UDEV_RULES
    assert 'KERNEL=="uinput"' in UDEV_RULES
