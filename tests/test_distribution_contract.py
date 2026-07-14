"""Distribution and operator-documentation contract tests.

These checks keep the install/config surface aligned with the packages and
Hermes plugin API that operators actually use.  They intentionally fail on
the retired distribution name and on copy-paste instructions that point at a
nonexistent public package or the wrong platform config key.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent


def _project_metadata() -> dict:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]


def test_runtime_dependency_uses_canonical_distribution() -> None:
    dependencies = _project_metadata()["dependencies"]

    assert "molecules-workspace-runtime>=0.4.4" in dependencies
    assert all("molecule-ai-workspace-runtime" not in item for item in dependencies)


def test_pip_entrypoint_targets_plugin_package() -> None:
    entrypoints = _project_metadata()["entry-points"]["hermes_agent.plugins"]

    assert entrypoints == {"molecule": "hermes_channel_molecule"}


def test_plugin_manifest_requires_external_workspace_credentials() -> None:
    manifest = yaml.safe_load(
        (REPO_ROOT / "hermes_channel_molecule" / "plugin.yaml").read_text(
            encoding="utf-8"
        )
    )

    assert manifest["requires_env"] == [
        "MOLECULE_WORKSPACE_ID",
        "MOLECULE_WORKSPACE_TOKEN",
        "MOLECULE_PLATFORM_URL",
    ]


def test_readme_uses_real_source_and_private_runtime_install_paths() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "pip install hermes-channel-molecule" not in readme
    assert "git+https://git.moleculesai.app/molecule-ai/hermes-channel-molecule.git" in readme
    assert "pip download --no-deps" in readme
    assert "molecules-workspace-runtime==0.4.4" in readme
    assert "--index-url https://pypi.org/simple/" in readme
    assert "molecule-ai-workspace-runtime" not in readme


def test_readme_uses_current_hermes_config_and_plugin_opt_in() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "plugins:\n  enabled:\n    - molecule" in readme
    assert "\nplatforms:\n  molecule:\n    enabled: true" in readme
    assert "gateway:\n  platforms:" not in readme
    assert "plugin_platforms:" not in readme
    assert "hermes plugins enable molecule" in readme


def test_readme_describes_current_workspace_credential_lifecycle() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "External Connection" in readme
    assert "shown only once" in readme
    assert "Rotate credentials" in readme
    assert "canvas → Tokens tab" not in readme
    assert "server=molecule" in readme
    assert "server=a2a-delegation" not in readme
