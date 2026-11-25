import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()


class PrometheusCollector:
    """Read-only Prometheus HTTP API client."""

    def __init__(self, url=None, timeout=None):
        self.url = (url or os.getenv("PROMETHEUS_URL", "http://localhost:9090")).rstrip("/")
        self.timeout = int(timeout or os.getenv("PROMETHEUS_TIMEOUT", "15"))
        self.session = requests.Session()

    @staticmethod
    def _escape(value):
        return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    @classmethod
    def _selector(cls, **labels):
        return ",".join(f'{k}="{cls._escape(v)}"' for k, v in labels.items() if v is not None)

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
        result = self.query("count by (namespace) (kube_pod_info)")
        return sorted({x.get("metric", {}).get("namespace") for x in result
                       if x.get("metric", {}).get("namespace")})

    def list_pods(self, namespace):
        selector = self._selector(namespace=namespace)
        result = self.query(f"count by (pod) (kube_pod_info{{{selector}}})")
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
             f"(rate(node_cpu_seconds_total{{mode='idle'}}[5m]))))")
        return self.values(self.query(q))

    def top_nodes_memory(self, limit=5):
        limit = max(1, min(int(limit), 20))
        q = (f"topk({limit}, 100 * (1 - "
             f"(node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)))")
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
        expressions = {
            "pod_cpu": 'sum by (namespace,pod) (rate(container_cpu_usage_seconds_total{container!="",image!=""}[5m]))',
            "pod_memory": 'sum by (namespace,pod) (container_memory_working_set_bytes{container!="",image!=""})',
            "node_cpu": "100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{mode='idle'}[5m])))",
            "node_memory": "100 * (1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes))",
        }
        if metric not in expressions:
            raise ValueError(f"Unsupported metric: {metric}")
        expr = expressions[metric]
        matchers = []
        if namespace:
            matchers.append(f'namespace="{self._escape(namespace)}"')
        if pod:
            matchers.append(f'pod="{self._escape(pod)}"')
        if node:
            # node exporter commonly labels the target as instance, not node.
            matchers.append(f'instance=~".*{self._escape(node)}.*"')
        if matchers and metric.startswith("pod_"):
            expr = expr.replace("{", "{" + ",".join(matchers) + ",", 1)
        elif node and metric.startswith("node_"):
            expr = expr.replace("{", "{" + ",".join(matchers) + ",", 1)
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
