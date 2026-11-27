import json
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse


@dataclass(frozen=True)
class ObservabilityTarget:
    name: str
    prometheus_url: str
    loki_url: str | None = None
    kubernetes_context: str | None = None
    in_cluster: bool = False
    prometheus_labels: dict[str, str] = field(default_factory=dict)
    loki_labels: dict[str, str] = field(default_factory=dict)


def _validate_url(value, field, target_name, required=False):
    if value is None or value == "":
        if required:
            raise ValueError(f"Target '{target_name}' requires {field}.")
        return None
    if not isinstance(value, str):
        raise ValueError(f"Target '{target_name}' has a non-string {field}.")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(
            f"Target '{target_name}' has an invalid {field}; use an http(s) URL."
        )
    if parsed.username or parsed.password:
        raise ValueError(
            f"Target '{target_name}' must not put credentials in {field}."
        )
    return value.rstrip("/")


def _validate_labels(value, field, target_name):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"Target '{target_name}' has non-object {field}.")
    labels = {}
    for key, label_value in value.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", key):
            raise ValueError(f"Target '{target_name}' has an invalid label in {field}.")
        if not isinstance(label_value, str) or not label_value:
            raise ValueError(f"Target '{target_name}' label values in {field} must be strings.")
        labels[key] = label_value
    return labels


def _from_json(path):
    try:
        with open(path, encoding="utf-8") as config_file:
            config = json.load(config_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not load targets configuration '{path}': {exc}") from exc

    entries = config.get("targets") if isinstance(config, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError(
            f"Targets configuration '{path}' must contain a non-empty 'targets' list."
        )

    targets = []
    names = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Each item in the targets list must be an object.")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Every target must have a non-empty string 'name'.")
        name = name.strip()
        if name in names:
            raise ValueError(f"Duplicate target name: '{name}'.")
        names.add(name)

        context = entry.get("kubernetes_context") or None
        if context is not None and not isinstance(context, str):
            raise ValueError(f"Target '{name}' has a non-string Kubernetes context.")
        in_cluster = entry.get("in_cluster", False)
        if not isinstance(in_cluster, bool):
            raise ValueError(f"Target '{name}' has a non-boolean 'in_cluster' value.")
        if context and in_cluster:
            raise ValueError(
                f"Target '{name}' cannot set both 'kubernetes_context' and 'in_cluster'."
            )

        targets.append(ObservabilityTarget(
            name=name,
            prometheus_url=_validate_url(
                entry.get("prometheus_url"), "prometheus_url", name, required=True
            ),
            loki_url=_validate_url(entry.get("loki_url"), "loki_url", name),
            kubernetes_context=context.strip() if context else None,
            in_cluster=in_cluster,
            prometheus_labels=_validate_labels(
                entry.get("prometheus_labels"), "prometheus_labels", name
            ),
            loki_labels=_validate_labels(entry.get("loki_labels"), "loki_labels", name),
        ))
    return targets


def load_targets():
    """Load configured targets, retaining compatibility with the original .env setup."""
    config_path = os.getenv("TARGETS_CONFIG")
    if config_path:
        return _from_json(config_path)

    name = os.getenv("TARGET_NAME", "default")
    return [ObservabilityTarget(
        name=name,
        prometheus_url=_validate_url(
            os.getenv("PROMETHEUS_URL", "http://localhost:9090"),
            "PROMETHEUS_URL",
            name,
            required=True,
        ),
        loki_url=_validate_url(os.getenv("LOKI_URL", "http://localhost:3100"),
                               "LOKI_URL", name),
        kubernetes_context=os.getenv("KUBERNETES_CONTEXT") or None,
        loki_labels=(
            {os.getenv("LOKI_CLUSTER_LABEL", "cluster"): os.getenv("LOKI_CLUSTER_VALUE")}
            if os.getenv("LOKI_CLUSTER_VALUE") else {}
        ),
    )]
