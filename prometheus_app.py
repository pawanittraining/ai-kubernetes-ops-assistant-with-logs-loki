import os
from datetime import datetime
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from collectors.prometheus_collector import PrometheusCollector
from collectors.kubernetes_collector import KubernetesCollector
from collectors.loki_collector import LokiCollector
from ai.agent import KubernetesOpsAgent

load_dotenv()
st.set_page_config(page_title="AI Kubernetes Operations Center", page_icon="🛠️", layout="wide")
st.title("🛠️ AI Kubernetes Operations Center")
st.caption("Conversational Kubernetes observability: metrics, pod logs, node status and Loki logs.")

PROM_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
LOKI_URL = os.getenv("LOKI_URL", "http://localhost:3100")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:0.6b")
KUBE_CONTEXT = os.getenv("KUBERNETES_CONTEXT", "") or None


@st.cache_resource
def get_prom(url):
    return PrometheusCollector(url=url)


@st.cache_resource
def get_k8s(context):
    return KubernetesCollector(context=context)


@st.cache_resource
def get_loki(url):
    return LokiCollector(url=url)


@st.cache_resource
def get_agent(prom_url, loki_url, model, host, context):
    return KubernetesOpsAgent(
        prometheus=get_prom(prom_url),
        kubernetes=get_k8s(context),
        loki=get_loki(loki_url),
        model=model,
        host=host,
    )


prom = get_prom(PROM_URL)
try:
    prom.health()
    prom_ok = True
except Exception as exc:
    prom_ok = False
    prom_error = str(exc)

try:
    k8s = get_k8s(KUBE_CONTEXT)
    nodes = k8s.list_nodes()
    k8s_ok = True
except Exception as exc:
    k8s_ok = False
    k8s_error = str(exc)
    k8s = None
    nodes = []

try:
    loki = get_loki(LOKI_URL)
    loki.health()
    loki_ok = True
except Exception as exc:
    loki_ok = False
    loki_error = str(exc)
    loki = None

try:
    agent = get_agent(PROM_URL, LOKI_URL, OLLAMA_MODEL, OLLAMA_HOST, KUBE_CONTEXT)
    agent_ready = True
except Exception as exc:
    agent_ready = False
    agent_error = str(exc)

with st.sidebar:
    st.header("Connections")
    st.write(f"**Prometheus:** {'🟢 Connected' if prom_ok else '🔴 Disconnected'}")
    st.write(f"**Kubernetes:** {'🟢 Connected' if k8s_ok else '🔴 Disconnected'}")
    st.write(f"**Loki:** {'🟢 Connected' if loki_ok else '🟡 Not connected'}")
    st.write(f"**Ollama model:** `{OLLAMA_MODEL}`")
    if not prom_ok:
        st.error(prom_error)
    if not k8s_ok:
        st.warning(f"Kubernetes API unavailable: {k8s_error}")
    if not loki_ok:
        st.info("Loki is optional. Node OS log search is disabled until Loki and a log shipper are configured.")
    st.divider()
    st.subheader("Try asking")
    examples = [
        "Show the top 5 pods by CPU usage.",
        "Show the CPU and memory history for the last 2 hours.",
        "Get the last 100 logs from pod nginx in namespace default.",
        "Show previous logs for pod api in namespace production.",
        "Show events for pod api in namespace production.",
        "Which nodes have MemoryPressure or DiskPressure?",
        "Search node worker-01 OS logs for 'error' in the last 60 minutes.",
    ]
    for i, example in enumerate(examples):
        if st.button(example, key=f"example_{i}", use_container_width=True):
            st.session_state.pending_prompt = example
    if st.button("Clear conversation", use_container_width=True):
        st.session_state.chat_messages = []
        st.session_state.pending_prompt = ""
        st.session_state.chat_charts = []
        st.rerun()

if not prom_ok:
    st.error(f"Prometheus is required for this app: {prom_error}")
    st.stop()
if not agent_ready:
    st.error(f"Could not initialize the AI agent: {agent_error}")
    st.stop()

# Dashboard KPIs
k1, k2, k3, k4 = st.columns(4)
try:
    namespaces = prom.list_namespaces()
except Exception:
    namespaces = []
k1.metric("Namespaces discovered", len(namespaces))
k2.metric("Kubernetes nodes", len(nodes) if k8s_ok else "N/A")
try:
    topcpu = prom.top_pods_cpu(limit=1)
    k3.metric("Highest pod CPU", f'{topcpu[0]["value"]:.3f} cores' if topcpu and topcpu[0]["value"] is not None else "N/A")
except Exception:
    k3.metric("Highest pod CPU", "N/A")
try:
    topmem = prom.top_pods_memory(limit=1)
    k4.metric("Highest pod memory", f'{topmem[0]["value"] / (1024**2):.1f} MiB' if topmem and topmem[0]["value"] is not None else "N/A")
except Exception:
    k4.metric("Highest pod memory", "N/A")

tab_chat, tab_logs, tab_nodes, tab_metrics, tab_help = st.tabs([
    "💬 AI Assistant", "📜 Logs Explorer", "🖥️ Nodes", "📈 Metrics Explorer", "ℹ️ Setup"
])

def render_history_chart(history):
    series = history.get("series", [])
    frames = {}
    for item in series:
        labels = item.get("labels", {})
        label = labels.get("pod") or labels.get("instance") or labels.get("node") or "series"
        for point in item.get("points", []):
            stamp = datetime.fromtimestamp(point["timestamp"])
            frames.setdefault(stamp, {})[label] = point["value"]
    if frames:
        df = pd.DataFrame.from_dict(frames, orient="index").sort_index()
        st.line_chart(df, use_container_width=True)
        st.caption(f'{history.get("metric")} · {history.get("hours")} hour(s) · unit: {history.get("unit")}')
    else:
        st.warning("No historical samples returned for this metric.")

with tab_chat:
    st.subheader("Ask your Kubernetes SRE assistant")
    st.caption("The assistant can call Prometheus, Kubernetes and Loki read-only tools. Follow-up questions use recent chat context.")
    if "chat_messages" not in st.session_state:
        st.session_state.chat_messages = []
    if "chat_charts" not in st.session_state:
        st.session_state.chat_charts = []
    if "pending_prompt" not in st.session_state:
        st.session_state.pending_prompt = ""

    for idx, msg in enumerate(st.session_state.chat_messages):
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg["role"] == "assistant" and msg.get("charts"):
                for history in msg["charts"]:
                    with st.expander(f'Historical chart: {history.get("metric")}', expanded=True):
                        render_history_chart(history)

    prompt = st.chat_input("Ask about metrics, pod logs, node conditions or OS logs...")
    if st.session_state.pending_prompt:
        prompt = st.session_state.pending_prompt
        st.session_state.pending_prompt = ""
    if prompt:
        st.session_state.chat_messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            with st.spinner("Collecting evidence and analyzing..."):
                try:
                    result = agent.respond(st.session_state.chat_messages)
                    answer = result["answer"]
                    charts = result.get("charts", [])
                except Exception as exc:
                    answer = f"**Agent error:** {exc}\n\nCheck Ollama, model availability, Kubernetes access and service endpoints."
                    charts = []
            st.markdown(answer)
            for history in charts:
                with st.expander(f'Historical chart: {history.get("metric")}', expanded=True):
                    render_history_chart(history)
        st.session_state.chat_messages.append({"role": "assistant", "content": answer, "charts": charts})

with tab_logs:
    st.subheader("Logs Explorer")
    if not k8s_ok:
        st.warning("Kubernetes API connection is required for pod logs.")
    else:
        try:
            log_ns = st.selectbox("Namespace", k8s.list_namespaces(), key="log_namespace")
            pods = k8s.list_pods(log_ns) if log_ns else []
            pod = st.selectbox("Pod", pods, key="log_pod") if pods else None
            containers = k8s.list_containers(log_ns, pod) if pod else []
            container = st.selectbox("Container", containers, key="log_container") if containers else None
            col1, col2 = st.columns(2)
            tail = col1.number_input("Tail lines", min_value=10, max_value=1000, value=200, step=50)
            previous = col2.checkbox("Previous terminated container")
            if st.button("Fetch pod logs", type="primary", disabled=not (pod and container)):
                try:
                    content = k8s.pod_logs(log_ns, pod, container, int(tail), previous)
                    st.code(content or "(No log lines returned)", language="text")
                except Exception as exc:
                    st.error(f"Could not retrieve pod logs: {exc}")
            if pod and st.button("Fetch pod events"):
                try:
                    st.json(k8s.pod_events(log_ns, pod))
                except Exception as exc:
                    st.error(f"Could not retrieve events: {exc}")
        except Exception as exc:
            st.error(f"Logs explorer error: {exc}")

    st.divider()
    st.subheader("Node operating-system logs (Loki)")
    if not loki_ok:
        st.info("Loki is not connected. Configure Loki and a node log shipper; see the Setup tab and observability/ files.")
    else:
        node_names = [n["name"] for n in nodes] if nodes else []
        node_name = st.selectbox("Node", node_names, key="loki_node") if node_names else st.text_input("Node label value")
        search_text = st.text_input("Search text", value="error")
        since = st.selectbox("Look back", [15, 30, 60, 180, 360], index=2)
        if st.button("Search node OS logs", disabled=not node_name):
            try:
                result = loki.query_node_logs(node_name, search_text, since, 200)
                if not result["entries"]:
                    st.warning("No matching logs. Check the shipper, Loki labels and selected node.")
                else:
                    for entry in result["entries"]:
                        st.text(f'{entry["timestamp_ns"]}  {entry["line"]}')
            except Exception as exc:
                st.error(f"Loki query failed: {exc}")

with tab_nodes:
    st.subheader("Kubernetes node status")
    if not k8s_ok:
        st.warning(k8s_error)
    elif not nodes:
        st.info("No nodes returned by the Kubernetes API.")
    else:
        st.dataframe(pd.DataFrame(nodes), use_container_width=True, hide_index=True)
        selected_node = st.selectbox("Inspect node", [n["name"] for n in nodes])
        if st.button("Load node details"):
            try:
                st.json(k8s.node_details(selected_node))
            except Exception as exc:
                st.error(str(exc))

with tab_metrics:
    st.subheader("Historical Metrics Explorer")
    metric_label_map = {
        "Pod CPU (cores)": "pod_cpu",
        "Pod memory (bytes)": "pod_memory",
        "Node CPU (%)": "node_cpu",
        "Node memory (%)": "node_memory",
    }
    metric_label = st.selectbox("Metric", list(metric_label_map))
    selected_metric = metric_label_map[metric_label]
    hours = st.slider("Time range (hours)", 1, 24, 2)
    ns_filter = None
    pod_filter = None
    node_filter = None
    if selected_metric.startswith("pod_"):
        ns_options = ["All"] + namespaces
        ns_choice = st.selectbox("Namespace filter", ns_options)
        ns_filter = None if ns_choice == "All" else ns_choice
        if ns_filter:
            pod_options = ["All"] + prom.list_pods(ns_filter)
            pod_choice = st.selectbox("Pod filter", pod_options)
            pod_filter = None if pod_choice == "All" else pod_choice
    else:
        node_options = ["All"] + [n["name"] for n in nodes]
        node_choice = st.selectbox("Node filter", node_options)
        node_filter = None if node_choice == "All" else node_choice
    if st.button("Plot historical metrics", type="primary"):
        try:
            history = prom.metric_history(
                selected_metric, hours, namespace=ns_filter, pod=pod_filter, node=node_filter
            )
            render_history_chart(history)
        except Exception as exc:
            st.error(f"Historical query failed: {exc}")

with tab_help:
    st.markdown("""
### Capabilities in this build
- AI chat with read-only tool calling.
- Prometheus instant and range queries.
- Historical charts rendered in the chat when the assistant requests historical data.
- Kubernetes pod logs, previous-container logs, pod events and node conditions.
- Loki node OS log search, when a log shipper is configured.

### Log-source distinction
- **Pod logs:** fetched directly from the Kubernetes API (`read_namespaced_pod_log`).
- **Node OS logs:** queried from Loki. Loki does not collect logs by itself; a shipper such as Fluent Bit must send node logs with labels `job="node-os"` and `node="<Kubernetes node name>"`.
- Kubernetes `kubectl logs` does not provide full host journald, kernel or kubelet logs.

### Prometheus prerequisites
- kube-state-metrics: pod metadata and restart/readiness metrics.
- kubelet/cAdvisor: container CPU and memory.
- Node Exporter: node CPU and memory metrics.
- Metric labels differ between installations; empty results require checking Prometheus targets and actual labels.

### Safety
All application tools are read-only. No pod deletion, restart, scaling, exec, shell or infrastructure changes are performed by the assistant.
""")
