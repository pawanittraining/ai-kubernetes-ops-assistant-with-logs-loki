#### This document assume you have already kubernetes cluster installed, prometheus is deployed already, and loki deployed or you can follow for loki below
# AI Kubernetes Operations Center — Phase 2

Adds Kubernetes pod logs/events/node status, Loki node OS log search, historical Prometheus queries and charts rendered in chat.

## 1. Install Python dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

The default `.env.example` uses `qwen3:0.6b`. You can change `OLLAMA_MODEL` to any model installed locally.

## 2. Start Ollama

```bash
ollama pull qwen3:0.6b
ollama list
```

If the Ollama macOS app is running, you usually do not need a separate `ollama serve`.

## 3. Prometheus

Ensure Prometheus is reachable at `http://localhost:9090` (or update `.env`).

```bash
kubectl get svc -n monitoring
kubectl port-forward -n monitoring svc/prometheus-operated 9090:9090
```

Service name may differ; use the actual Prometheus service shown by `kubectl get svc`.

## 4. Kubernetes API access

The app uses your local kubeconfig. Check:

```bash
kubectl config current-context
kubectl get nodes
kubectl get pods -A
```

The user running Streamlit needs read access to pods, pods/log, events, nodes and namespaces. For a local learning cluster, your existing kubeconfig user may already have sufficient access. In a shared cluster, use a least-privilege read-only Role/ClusterRole; do not grant cluster-admin just for this app.

Optional `.env`:
```env
KUBERNETES_CONTEXT=kind-kind
```

Leave blank to use the current kubeconfig context.

## 5. Loki for node OS logs (optional)

Pod logs work directly through the Kubernetes API. Node OS logs need a log collector; Loki stores and queries logs but does not collect them by itself.

Install a small single-binary Loki for a local learning cluster:

```bash
helm repo add grafana https://grafana.github.io/helm-charts
helm repo update
kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -
helm upgrade --install loki grafana/loki \
  --namespace monitoring \
  -f observability/loki-values.yaml
```

The values mount a writable `emptyDir` at `/var/loki`, where Loki stores its
filesystem data and ruler state. This is suitable for a local learning cluster;
data is lost when the pod is replaced. For data that must survive pod
recreation, configure `singleBinary.persistence.enabled: true` and provide a
working default or explicit StorageClass instead.

Wait for Loki to become ready:
```bash
kubectl rollout status statefulset/loki -n monitoring
kubectl get pods -n monitoring -l app.kubernetes.io/name=loki
```

Check the service:
```bash
kubectl get svc -n monitoring | grep loki
```

Forward the service:
```bash
kubectl port-forward -n monitoring svc/loki 3100:3100
```

If the chart creates a different service name, use that service. Confirm:
```bash
curl http://localhost:3100/ready
```

### Ship node logs

Review `observability/fluent-bit-node-logs.yaml` before applying. This Kind example reads common host log files under `/var/log` and the systemd journal under `/run/log/journal`, then sends them to Loki with labels:
- `job="node-os"`
- `node="<Kubernetes node name>"`

Apply only after confirming the host log paths and Loki service DNS are correct for your cluster:

```bash
kubectl apply -f observability/fluent-bit-node-logs.yaml
kubectl get pods -n monitoring -l app=fluent-bit-node-logs
kubectl logs -n monitoring -l app=fluent-bit-node-logs --tail=100
```

**Kind limitation:** Kind nodes are Docker containers, not physical hosts. Their OS log paths and journald availability vary by Kind node image. The current `devops-lab` nodes have `/run/log/journal` (not `/var/log/journal`); verify this path on other clusters and node images. This sample does not guarantee kubelet/kernel logs will be present. Check Loki labels and sample queries before relying on it.

Test Loki:
```bash
curl -G 'http://localhost:3100/loki/api/v1/query' \
  --data-urlencode 'query={job="node-os"}'
```

Update `.env`:
```env
LOKI_URL=http://localhost:3100
LOKI_NODE_LABEL=node
```

If you do not install Loki, the rest of the application still works; node OS log search is disabled.

## 6. Run the application

```bash
streamlit run prometheus_app.py
```

## 7. Try these chat questions

```text
Show me the top 5 pods by CPU usage.
Show CPU history for the last 2 hours.
Show memory history for pod api in namespace production for 6 hours.
Get the last 100 logs from pod nginx in namespace default.
Show previous logs for pod api in namespace production.
Show Kubernetes events for pod api in namespace production.
Show the node conditions for worker-01.
Search node worker-01 OS logs for error in the last 60 minutes.
```

Follow-up examples:
```text
Plot memory for that same pod over the last 4 hours.
Do the logs support the memory-pressure hypothesis?
What evidence did you use?
```

## 8. Important behavior and limitations

- Historical chat charts are rendered when the assistant calls `get_metric_history`.
- Prometheus history is limited to 1–24 hours per tool call and a 60-second sampling step by default.
- Node CPU/memory queries need Node Exporter metrics.
- Pod logs use Kubernetes `read_namespaced_pod_log`; previous logs are only available if a previous container instance exists.
- Node OS logs use Loki LogQL selector `{job="node-os",node="<node>"}`. Your shipper must use those exact labels, or adjust `LOKI_NODE_LABEL` and the Loki collector.
- This build is read-only. It does not run `kubectl exec`, restart pods, delete resources or change cluster configuration from the chat.
- Logs can contain secrets. Use only test/non-sensitive workloads while learning; consider redaction and access controls before using this with production data.
