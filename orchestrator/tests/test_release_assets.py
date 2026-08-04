"""Static release asset and shell entrypoint tests."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

from provisioning.validate_release import validate

ROOT = Path(__file__).resolve().parents[2]


def test_release_asset_validator_passes() -> None:
    validate()


def test_shell_entrypoints_parse() -> None:
    scripts = [
        ROOT / "install.sh",
        ROOT / "provisioning" / "pull_models.sh",
        ROOT / "provisioning" / "verify_release.sh",
        ROOT / "provisioning" / "package_release.sh",
    ]
    process = subprocess.run(
        ["bash", "-n", *(str(path) for path in scripts)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0, process.stderr


def test_all_yaml_and_json_assets_parse() -> None:
    yaml_paths = [
        ROOT / "docker-compose.yml",
        ROOT / "k3s" / "swarm.yaml",
        ROOT / "monitoring" / "prometheus.yml",
        *sorted((ROOT / "orchestrator" / "profiles").glob("*.yaml")),
        ROOT / "orchestrator" / "agents.yaml",
        ROOT / "provisioning" / "license_manifest.yaml",
    ]
    for path in yaml_paths:
        documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        assert documents, path

    dashboard = (
        ROOT
        / "monitoring"
        / "grafana"
        / "dashboards"
        / "swarm-dashboard.json"
    )
    assert json.loads(dashboard.read_text(encoding="utf-8"))["uid"] == "llm-swarm"


def test_release_scripts_do_not_package_runtime_secrets() -> None:
    script = (ROOT / "provisioning" / "package_release.sh").read_text(
        encoding="utf-8"
    )
    assert 'excluded_names = {".env", ".DS_Store"}' in script
    assert '".git"' in script
    assert '"data"' in script
    assert '"dist"' in script


def test_release_checksum_uses_portable_basename() -> None:
    script = (ROOT / "provisioning" / "package_release.sh").read_text(
        encoding="utf-8"
    )
    assert 'cd "${dist}"' in script
    assert 'sha256sum "${name}.zip" > "${name}.zip.sha256"' in script
    assert 'sha256sum "${archive}" > "${archive}.sha256"' not in script
