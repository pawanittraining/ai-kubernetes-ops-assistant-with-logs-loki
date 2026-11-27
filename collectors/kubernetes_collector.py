import os
from kubernetes import client, config
from kubernetes.config.config_exception import ConfigException
from dotenv import load_dotenv

load_dotenv()


class KubernetesCollector:
    """Read-only Kubernetes API collector for pods, logs, events and nodes."""

    def __init__(self, context=None, in_cluster=False):
        if in_cluster:
            config.load_incluster_config()
        else:
            context = context or os.getenv("KUBERNETES_CONTEXT") or None
            if context:
                config.load_kube_config(context=context)
            else:
                try:
                    config.load_kube_config()
                except ConfigException:
                    # Supports running inside a Kubernetes cluster as well.
                    config.load_incluster_config()
        self.v1 = client.CoreV1Api()

    def list_namespaces(self):
        return sorted(x.metadata.name for x in self.v1.list_namespace().items)

    def list_pods(self, namespace):
        return sorted(x.metadata.name for x in
                      self.v1.list_namespaced_pod(namespace).items)

    def list_containers(self, namespace, pod):
        obj = self.v1.read_namespaced_pod(pod, namespace)
        return sorted(x.name for x in (obj.spec.containers or []))

    def pod_logs(self, namespace, pod, container=None, tail_lines=200, previous=False):
        tail_lines = max(1, min(int(tail_lines), 1000))
        kwargs = {
            "name": pod,
            "namespace": namespace,
            "tail_lines": tail_lines,
            "timestamps": True,
            "previous": bool(previous),
        }
        if container:
            kwargs["container"] = container
        return self.v1.read_namespaced_pod_log(**kwargs)

    def pod_events(self, namespace, pod):
        events = self.v1.list_namespaced_event(
            namespace,
            field_selector=f"involvedObject.name={pod}",
        )
        return [{
            "type": e.type,
            "reason": e.reason,
            "message": e.message,
            "count": e.count,
            "last_timestamp": str(e.last_timestamp or e.event_time or ""),
        } for e in events.items]

    def list_nodes(self):
        result = []
        for node in self.v1.list_node().items:
            conditions = {
                c.type: c.status for c in (node.status.conditions or [])
            }
            result.append({
                "name": node.metadata.name,
                "ready": conditions.get("Ready", "Unknown"),
                "memory_pressure": conditions.get("MemoryPressure", "Unknown"),
                "disk_pressure": conditions.get("DiskPressure", "Unknown"),
                "pid_pressure": conditions.get("PIDPressure", "Unknown"),
                "unschedulable": bool(node.spec.unschedulable),
            })
        return result

    def node_details(self, node_name):
        node = self.v1.read_node(node_name)
        return {
            "name": node.metadata.name,
            "labels": node.metadata.labels or {},
            "capacity": node.status.capacity or {},
            "allocatable": node.status.allocatable or {},
            "conditions": [{
                "type": c.type,
                "status": c.status,
                "reason": c.reason,
                "message": c.message,
            } for c in (node.status.conditions or [])],
        }
