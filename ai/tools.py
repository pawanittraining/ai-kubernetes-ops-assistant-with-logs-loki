import json


class OpsTools:
    """Allow-listed, read-only tools exposed to the local LLM."""

    def __init__(self, prometheus, kubernetes=None, loki=None):
        self.prom = prometheus
        self.k8s = kubernetes
        self.loki = loki

    def definitions(self):
        tools = [
            self._tool("get_top_pods_cpu", "Rank pods by CPU usage over five minutes. CPU is in cores.",
                       {"namespace": {"type": ["string", "null"]}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}},
                       []),
            self._tool("get_top_pods_memory", "Rank pods by current memory working set. Memory is in bytes.",
                       {"namespace": {"type": ["string", "null"]}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}},
                       []),
            self._tool("get_top_nodes_cpu", "Rank nodes by CPU utilization percentage. Requires node-exporter metrics.",
                       {"limit": {"type": "integer", "minimum": 1, "maximum": 20}}, []),
            self._tool("get_top_nodes_memory", "Rank nodes by memory utilization percentage. Requires node-exporter metrics.",
                       {"limit": {"type": "integer", "minimum": 1, "maximum": 20}}, []),
            self._tool("get_pod_metrics", "Get current CPU, memory, restart increase and ready-container metrics for a pod.",
                       {"namespace": {"type": "string"}, "pod": {"type": "string"}},
                       ["namespace", "pod"]),
            self._tool("get_metric_history", "Get historical time-series data for pod CPU, pod memory, node CPU or node memory. Use for chart/trend questions.",
                       {"metric": {"type": "string", "enum": ["pod_cpu", "pod_memory", "node_cpu", "node_memory"]},
                        "hours": {"type": "integer", "minimum": 1, "maximum": 24},
                        "namespace": {"type": ["string", "null"]},
                        "pod": {"type": ["string", "null"]},
                        "node": {"type": ["string", "null"]}},
                       ["metric"]),
        ]
        if self.k8s:
            tools += [
                self._tool("get_pod_logs", "Retrieve recent or previous logs for a Kubernetes pod container. Use previous=true for the previous terminated container instance.",
                           {"namespace": {"type": "string"}, "pod": {"type": "string"},
                            "container": {"type": ["string", "null"]},
                            "tail_lines": {"type": "integer", "minimum": 1, "maximum": 500},
                            "previous": {"type": "boolean"}},
                           ["namespace", "pod"]),
                self._tool("get_pod_events", "Retrieve Kubernetes events associated with a pod.",
                           {"namespace": {"type": "string"}, "pod": {"type": "string"}},
                           ["namespace", "pod"]),
                self._tool("get_node_status", "Retrieve node Ready, MemoryPressure, DiskPressure and PIDPressure conditions.",
                           {"node": {"type": "string"}}, ["node"]),
            ]
        if self.loki:
            tools.append(
                self._tool("get_node_os_logs", "Search centralized node operating-system logs in Loki. Requires Fluent Bit or another shipper configured with job=node-os and a node label.",
                           {"node": {"type": "string"}, "search": {"type": ["string", "null"]},
                            "since_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 300}},
                           ["node"])
            )
        return tools

    @staticmethod
    def _tool(name, description, properties, required):
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

    def execute(self, name, args):
        args = args or {}
        dispatch = {
            "get_top_pods_cpu": lambda: self.prom.top_pods_cpu(**args),
            "get_top_pods_memory": lambda: self.prom.top_pods_memory(**args),
            "get_top_nodes_cpu": lambda: self.prom.top_nodes_cpu(**args),
            "get_top_nodes_memory": lambda: self.prom.top_nodes_memory(**args),
            "get_pod_metrics": lambda: self.prom.pod_metrics(**args),
            "get_metric_history": lambda: self.prom.metric_history(**args),
        }
        if name == "get_pod_logs" and self.k8s:
            return self.k8s.pod_logs(**args)
        if name == "get_pod_events" and self.k8s:
            return self.k8s.pod_events(**args)
        if name == "get_node_status" and self.k8s:
            return self.k8s.node_details(args["node"])
        if name == "get_node_os_logs" and self.loki:
            return self.loki.query_node_logs(**args)
        if name not in dispatch:
            raise ValueError(f"Tool is not enabled: {name}")
        return dispatch[name]()


def json_for_model(value):
    return json.dumps(value, ensure_ascii=False, default=str)
