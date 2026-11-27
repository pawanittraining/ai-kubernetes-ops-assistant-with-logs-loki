import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()


class LokiCollector:
    """Read-only Loki HTTP API client. Requires a log shipper to populate labels."""

    def __init__(self, url=None, timeout=None, node_label=None, labels=None):
        self.url = (url or os.getenv("LOKI_URL", "http://localhost:3100")).rstrip("/")
        self.timeout = int(timeout or os.getenv("LOKI_TIMEOUT", "20"))
        self.node_label = node_label or os.getenv("LOKI_NODE_LABEL", "node")
        self.labels = labels or {}
        self.session = requests.Session()

    def health(self):
        response = self.session.get(f"{self.url}/ready", timeout=self.timeout)
        response.raise_for_status()
        return response.text

    @staticmethod
    def _escape(value):
        return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    def query_node_logs(self, node, search="", since_minutes=60, limit=100):
        since_minutes = max(1, min(int(since_minutes), 24 * 60))
        limit = max(1, min(int(limit), 500))
        labels = {**self.labels, "job": "node-os", self.node_label: node}
        selector = "{" + ",".join(
            f'{key}="{self._escape(value)}"' for key, value in labels.items()
        ) + "}"
        logql = selector
        if search:
            # Loki line filter; the value is escaped as a quoted string.
            logql += f' |= "{self._escape(search)}"'
        end_ns = int(time.time() * 1_000_000_000)
        start_ns = end_ns - since_minutes * 60 * 1_000_000_000
        response = self.session.get(
            f"{self.url}/loki/api/v1/query_range",
            params={
                "query": logql,
                "start": str(start_ns),
                "end": str(end_ns),
                "limit": limit,
                "direction": "backward",
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "success":
            raise RuntimeError(payload.get("error", "Loki query failed"))
        entries = []
        for stream in payload.get("data", {}).get("result", []):
            labels = stream.get("stream", {})
            for ts, line in stream.get("values", []):
                entries.append({"timestamp_ns": ts, "labels": labels, "line": line})
        entries.sort(key=lambda x: x["timestamp_ns"], reverse=True)
        return {"query": logql, "entries": entries[:limit]}
