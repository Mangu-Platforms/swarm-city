#!/usr/bin/env python3
"""Validate static release assets without requiring Docker or live models."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = ROOT / "orchestrator" / "profiles"
ENV_DEFAULT_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-([^}]*)\}")
MODEL_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-([^}]+)\}")


class ValidationFailure(RuntimeError):
    """Raised when a release invariant is not satisfied."""


def _load_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValidationFailure(
            f"invalid YAML in {path.relative_to(ROOT)}: {exc}"
        ) from exc


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationFailure(message)


def _default(value: object) -> str:
    text = str(value)
    match = ENV_DEFAULT_RE.fullmatch(text)
    return match.group(1) if match else text


def _profile_models(path: Path) -> set[str]:
    document = _load_yaml(path)
    agents = document.get("agents") if isinstance(document, dict) else None
    _require(isinstance(agents, list) and agents, f"{path.name} has no agents")
    roles: set[str] = set()
    models: set[str] = set()
    for index, raw in enumerate(agents, start=1):
        _require(isinstance(raw, dict), f"{path.name} agent {index} is not an object")
        role = str(raw.get("role", "")).strip()
        model = str(raw.get("model", "")).strip()
        count = raw.get("count", 1)
        roles.add(role)
        match = MODEL_RE.fullmatch(model)
        models.add(match.group(1) if match else model)
        if isinstance(count, int):
            _require(count > 0, f"{path.name} agent {index} count must be positive")
        else:
            _require(
                bool(ENV_DEFAULT_RE.fullmatch(str(count))),
                f"{path.name} agent {index} count must be an integer or bounded env default",
            )
    _require("draft" in roles, f"{path.name} is missing draft agents")
    _require("critic" in roles, f"{path.name} is missing quality critics")
    _require("security" in roles, f"{path.name} is missing security critics")
    _require("finalizer" in roles, f"{path.name} is missing a local finalizer")
    _require(all(models), f"{path.name} contains an empty model tag")
    return models


def _validate_profiles_and_licenses() -> None:
    profiles = sorted(PROFILE_DIR.glob("agents-*.yaml"))
    _require(
        {path.name for path in profiles}
        == {"agents-light.yaml", "agents-balanced.yaml", "agents-quality.yaml"},
        "the release must contain exactly light, balanced, and quality profiles",
    )
    all_models: set[str] = set()
    for path in [ROOT / "orchestrator" / "agents.yaml", *profiles]:
        all_models.update(_profile_models(path))

    manifest = _load_yaml(ROOT / "provisioning" / "license_manifest.yaml")
    entries = manifest.get("models") if isinstance(manifest, dict) else None
    _require(isinstance(entries, list), "license manifest has no models list")
    declared: dict[str, dict] = {}
    for raw in entries:
        _require(isinstance(raw, dict), "license manifest entry is not an object")
        name = str(raw.get("name", "")).strip()
        _require(
            name and name not in declared, f"duplicate or empty license entry: {name}"
        )
        _require(bool(raw.get("license")), f"license missing for {name}")
        _require(
            raw.get("commercial_use") is True, f"commercial use not approved for {name}"
        )
        _require(
            str(raw.get("source", "")).startswith("https://"),
            f"source missing for {name}",
        )
        declared[name] = raw
    _require(
        all_models == set(declared),
        "profile/license model mismatch: "
        f"missing={sorted(all_models - set(declared))}, "
        f"unused={sorted(set(declared) - all_models)}",
    )


def _validate_compose() -> None:
    compose = _load_yaml(ROOT / "docker-compose.yml")
    services = compose.get("services") if isinstance(compose, dict) else None
    _require(isinstance(services, dict), "docker-compose.yml has no services")
    _require(
        set(services) == {"ollama", "orchestrator", "prometheus", "grafana"},
        "Compose service set is incomplete or unexpected",
    )
    for name, service in services.items():
        image = service.get("image")
        if image:
            _require(":latest" not in str(image), f"{name} uses a latest image tag")
        for port in service.get("ports", []) or []:
            _require(
                str(port).startswith("127.0.0.1:"),
                f"{name} publishes a port beyond localhost: {port}",
            )
        security_opt = service.get("security_opt", []) or []
        _require(
            "no-new-privileges:true" in security_opt,
            f"{name} does not enable no-new-privileges",
        )

    orchestrator = services["orchestrator"]
    _require(
        orchestrator.get("read_only") is True,
        "orchestrator root filesystem is writable",
    )
    _require(
        "ALL" in (orchestrator.get("cap_drop") or []),
        "orchestrator keeps Linux capabilities",
    )
    environment = orchestrator.get("environment") or {}
    _require(
        _default(environment.get("REQUIRE_API_TOKEN")) == "true",
        "API auth is not required by default",
    )
    _require(
        _default(environment.get("ENABLE_GIT_APPLY")) == "false",
        "git apply is enabled by default",
    )
    _require(
        _default(environment.get("ALLOW_UNSANDBOXED_TESTS")) == "false",
        "unsandboxed tests are enabled by default",
    )
    _require(
        _default(environment.get("REQUIRE_SECURITY_REVIEW")) == "true",
        "security review is not required",
    )
    _require(
        _default(environment.get("REQUIRE_HIGH_RISK_APPROVAL")) == "true",
        "high-risk approval is not required",
    )
    _require(
        "/repo:${REPO_MOUNT_MODE:-ro}"
        in "\n".join(str(item) for item in orchestrator.get("volumes", [])),
        "repository mount does not default to read-only",
    )


def _pod_specs(documents: list) -> list[tuple[str, dict]]:
    """Return (deployment name, pod spec) for every Deployment in the manifest."""

    specs = []
    for document in documents:
        if not isinstance(document, dict) or document.get("kind") != "Deployment":
            continue
        name = (document.get("metadata") or {}).get("name", "<unnamed>")
        spec = ((document.get("spec") or {}).get("template") or {}).get("spec")
        if isinstance(spec, dict):
            specs.append((name, spec))
    return specs


def _validate_kubernetes() -> None:
    """Assert the manifest's posture per container, not by substring.

    A whole-file substring test passes on a manifest with the invariant
    inverted: one container saying `readOnlyRootFilesystem: true` satisfies it
    for every other container, and a stray `value: "true"` anywhere satisfies
    the auth check no matter what REQUIRE_API_TOKEN is actually set to.
    """

    documents = list(
        yaml.safe_load_all((ROOT / "k3s" / "swarm.yaml").read_text(encoding="utf-8"))
    )
    kinds = {
        document.get("kind") for document in documents if isinstance(document, dict)
    }
    required = {
        "Namespace",
        "Deployment",
        "Service",
        "PersistentVolumeClaim",
        "NetworkPolicy",
    }
    _require(
        required.issubset(kinds),
        f"Kubernetes manifest is missing: {sorted(required - kinds)}",
    )

    specs = _pod_specs(documents)
    _require(bool(specs), "Kubernetes manifest declares no pod specs")
    auth_required = False
    for deployment, spec in specs:
        pod_security = spec.get("securityContext") or {}
        _require(
            "seccompProfile" in pod_security,
            f"{deployment} pod lacks an explicit seccomp profile",
        )
        _require(
            pod_security.get("runAsNonRoot") is True,
            f"{deployment} pod does not require a non-root user",
        )
        containers = spec.get("containers") or []
        _require(bool(containers), f"{deployment} declares no containers")
        for container in containers:
            name = container.get("name", "<unnamed>")
            image = str(container.get("image", ""))
            tag = image.rsplit("/", 1)[-1].partition("@")[0]
            _require(
                ":" in tag and not tag.endswith(":latest"),
                f"{deployment}/{name} image is untagged or uses latest: {image}",
            )
            security = container.get("securityContext") or {}
            _require(
                security.get("readOnlyRootFilesystem") is True,
                f"{deployment}/{name} has a writable root filesystem",
            )
            _require(
                security.get("allowPrivilegeEscalation") is False,
                f"{deployment}/{name} allows privilege escalation",
            )
            _require(
                "ALL" in ((security.get("capabilities") or {}).get("drop") or []),
                f"{deployment}/{name} keeps Linux capabilities",
            )
            for variable in container.get("env") or []:
                if variable.get("name") != "REQUIRE_API_TOKEN":
                    continue
                _require(
                    str(variable.get("value")).lower() == "true",
                    f"{deployment}/{name} does not require API authentication",
                )
                auth_required = True
    _require(auth_required, "no Kubernetes container sets REQUIRE_API_TOKEN=true")


def _validate_dependencies_and_assets() -> None:
    requirement_files = [
        ROOT / "orchestrator" / "requirements.txt",
        ROOT / "orchestrator" / "requirements-dev.txt",
    ]
    for path in requirement_files:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("-r "):
                continue
            _require(
                "==" in stripped,
                f"dependency is not exactly pinned in {path.name}: {stripped}",
            )

    dockerfile = (ROOT / "orchestrator" / "Dockerfile").read_text(encoding="utf-8")
    _require(
        "USER 1000:1000" in dockerfile, "orchestrator image does not run as non-root"
    )
    _require(
        "--no-server-header" in dockerfile, "Uvicorn server header is not disabled"
    )

    dashboard = ROOT / "monitoring" / "grafana" / "dashboards" / "swarm-dashboard.json"
    try:
        parsed = json.loads(dashboard.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationFailure(f"invalid Grafana dashboard JSON: {exc}") from exc
    _require(
        parsed.get("uid") == "llm-swarm", "Grafana dashboard UID changed unexpectedly"
    )
    expressions = json.dumps(parsed)
    for metric in (
        "swarm_active_tasks",
        "swarm_queued_tasks",
        "swarm_queue_rejections_total",
        "swarm_release_gates_total",
        "swarm_git_transactions_total",
    ):
        _require(metric in expressions, f"Grafana dashboard is missing {metric}")


def validate() -> None:
    _validate_profiles_and_licenses()
    _validate_compose()
    _validate_kubernetes()
    _validate_dependencies_and_assets()


def main() -> int:
    try:
        validate()
    except ValidationFailure as exc:
        print(f"release validation failed: {exc}", file=sys.stderr)
        return 1
    print("release asset validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
