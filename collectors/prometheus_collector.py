import os
import re
import time
import requests
from dotenv import load_dotenv

load_dotenv()


class PrometheusCollector:
    """Read-only Prometheus HTTP API client."""

    def __init__(self, url=None, timeout=None, labels=None):
        self.url = (url or os.getenv("PROMETHEUS_URL", "http://localhost:9090")).rstrip("/")
        self.timeout = int(timeout or os.getenv("PROMETHEUS_TIMEOUT", "15"))
        self.labels = labels or {}
        self.session = requests.Session()

    @staticmethod
    def _escape(value):
        return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    def _selector(self, **labels):
        matchers = {key: value for key, value in labels.items() if value is not None}
        matchers.update(self.labels)
        return ",".join(f'{key}="{self._escape(value)}"'
                        for key, value in matchers.items())

    def _get(self, path, params):
        response = self.session.get(f"{self.url}{path}", params=params, timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "success":
            raise RuntimeError(payload.get("error", "Prometheus query failed"))
        return payload.get("data", {}).get("result", [])

    def query(self, promql):
        return self._get("/api/v1/query", {"query": promql})

    def query_range(self, promql, start, end, step="60s"):
        return self._get("/api/v1/query_range", {
            "query": promql, "start": start, "end": end, "step": step
        })

    def metric_names(self):
        """Return metric names known to this Prometheus target."""
        params = {}
        if self.labels:
            params["match[]"] = "{" + self._selector() + "}"
        response = self.session.get(
            f"{self.url}/api/v1/label/__name__/values",
            params=params,
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "success":
            raise RuntimeError(payload.get("error", "Could not list Prometheus metrics"))
        values = payload.get("data")
        if not isinstance(values, list) or not all(isinstance(x, str) for x in values):
            raise RuntimeError("Prometheus returned an invalid metric-name response")
        return sorted(values)

    def query_metric(self, metric, matchers=None):
        """Query one metric name, scoped by target and user-selected label matchers."""
        if not isinstance(metric, str) or not re.fullmatch(
            r"[a-zA-Z_:][a-zA-Z0-9_:]*", metric
        ):
            raise ValueError(f"Invalid Prometheus metric name: {metric!r}")
        selector = self._selector(**(matchers or {}))
        expression = f"{metric}{{{selector}}}" if selector else metric
        return self.query(expression)

    def query_metric_range(self, metric, start, end, step="60s", matchers=None):
        """Range query for a metric name discovered from this Prometheus target."""
        if not isinstance(metric, str) or not re.fullmatch(
            r"[a-zA-Z_:][a-zA-Z0-9_:]*", metric
        ):
            raise ValueError(f"Invalid Prometheus metric name: {metric!r}")
        selector = self._selector(**(matchers or {}))
        expression = f"{metric}{{{selector}}}" if selector else metric
        return self.query_range(expression, start, end, step)

    def health(self):
        return self.query("up")

    @staticmethod
    def values(result):
        output = []
        for item in result:
            sample = item.get("value", [None, None])
            try:
                value = float(sample[1])
            except (TypeError, ValueError, IndexError):
                value = None
            output.append({"labels": item.get("metric", {}), "value": value})
        return output

    def list_namespaces(self):
        result = self.query(f"count by (namespace) (kube_pod_info{{{self._selector()}}})")
        return sorted({x.get("metric", {}).get("namespace") for x in result
                       if x.get("metric", {}).get("namespace")})

    def list_pods(self, namespace):
        result = self.query(
            f"count by (pod) (kube_pod_info{{{self._selector(namespace=namespace)}}})"
        )
        return sorted({x.get("metric", {}).get("pod") for x in result
                       if x.get("metric", {}).get("pod")})

    def top_pods_cpu(self, namespace=None, limit=5):
        limit = max(1, min(int(limit), 20))
        selector = self._selector(namespace=namespace)
        selector = (selector + "," if selector else "") + 'container!="",image!=""'
        q = (f"topk({limit}, sum by (namespace,pod) "
             f"(rate(container_cpu_usage_seconds_total{{{selector}}}[5m])))")
        return self.values(self.query(q))

    def top_pods_memory(self, namespace=None, limit=5):
        limit = max(1, min(int(limit), 20))
        selector = self._selector(namespace=namespace)
        selector = (selector + "," if selector else "") + 'container!="",image!=""'
        q = (f"topk({limit}, sum by (namespace,pod) "
             f"(container_memory_working_set_bytes{{{selector}}}))")
        return self.values(self.query(q))

    def top_nodes_cpu(self, limit=5):
        limit = max(1, min(int(limit), 20))
        q = (f"topk({limit}, 100 * (1 - avg by (instance) "
             f"(rate(node_cpu_seconds_total{{{self._selector(mode='idle')}}}[5m]))))")
        return self.values(self.query(q))

    def top_nodes_memory(self, limit=5):
        limit = max(1, min(int(limit), 20))
        selector = self._selector()
        q = (f"topk({limit}, 100 * (1 - "
             f"(node_memory_MemAvailable_bytes{{{selector}}} / "
             f"node_memory_MemTotal_bytes{{{selector}}})))")
        return self.values(self.query(q))

    def pod_metrics(self, namespace, pod):
        selector = self._selector(namespace=namespace, pod=pod)
        return {
            "cpu_cores_5m": self.values(self.query(
                f"sum by (namespace,pod) (rate(container_cpu_usage_seconds_total"
                f'{{{selector},container!="",image!=""}}[5m]))')),
            "memory_working_set_bytes": self.values(self.query(
                f"sum by (namespace,pod) (container_memory_working_set_bytes"
                f'{{{selector},container!="",image!=""}})')),
            "restarts_increase_1h": self.values(self.query(
                f"sum by (namespace,pod) (increase(kube_pod_container_status_restarts_total"
                f"{{{selector}}}[1h]))")),
            "ready_containers": self.values(self.query(
                f"sum by (namespace,pod) (kube_pod_container_status_ready{{{selector}}})")),
        }

    def metric_history(self, metric, hours=1, namespace=None, pod=None, node=None, step="60s"):
        """Return a bounded range query for one allow-listed metric."""
        if metric not in {"pod_cpu", "pod_memory", "node_cpu", "node_memory"}:
            raise ValueError(f"Unsupported metric: {metric}")
        if metric == "pod_cpu":
            selector = self._selector(namespace=namespace, pod=pod)
            selector = (selector + "," if selector else "") + 'container!="",image!=""'
            expr = (f"sum by (namespace,pod) (rate(container_cpu_usage_seconds_total"
                    f"{{{selector}}}[5m]))")
        elif metric == "pod_memory":
            selector = self._selector(namespace=namespace, pod=pod)
            selector = (selector + "," if selector else "") + 'container!="",image!=""'
            expr = f"sum by (namespace,pod) (container_memory_working_set_bytes{{{selector}}})"
        elif metric == "node_cpu":
            instance = f".*{self._escape(node)}.*" if node else None
            selector = self._selector(instance=instance, mode="idle")
            expr = (f"100 * (1 - avg by (instance) "
                    f"(rate(node_cpu_seconds_total{{{selector}}}[5m])))")
        else:
            instance = f".*{self._escape(node)}.*" if node else None
            selector = self._selector(instance=instance)
            expr = (f"100 * (1 - (node_memory_MemAvailable_bytes{{{selector}}} / "
                    f"node_memory_MemTotal_bytes{{{selector}}}))")
        hours = max(1, min(int(hours), 24))
        end = int(time.time())
        start = end - hours * 3600
        result = self.query_range(expr, start, end, step)
        series = []
        for item in result:
            points = []
            for timestamp, value in item.get("values", []):
                try:
                    points.append({"timestamp": float(timestamp), "value": float(value)})
                except (TypeError, ValueError):
                    continue
            series.append({"labels": item.get("metric", {}), "points": points[-300:]})
        return {
            "metric": metric,
            "unit": "cores" if metric == "pod_cpu" else
                    "bytes" if metric == "pod_memory" else "percent",
            "hours": hours,
            "series": series,
        }
