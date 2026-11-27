import os
import json
import time
from datetime import datetime
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from collectors.prometheus_collector import PrometheusCollector
from collectors.kubernetes_collector import KubernetesCollector
from collectors.loki_collector import LokiCollector
from ai.agent import KubernetesOpsAgent
from target_config import load_targets

load_dotenv()
st.set_page_config(page_title="AI Kubernetes Operations Center", page_icon="🛠️", layout="wide")
st.title("🛠️ AI Kubernetes Operations Center")
st.caption("Conversational Kubernetes observability: metrics, pod logs, node status and Loki logs.")

try:
    targets = load_targets()
except ValueError as exc:
    st.error(f"Invalid target configuration: {exc}")
    st.stop()

selected_target = st.sidebar.selectbox(
    "Cluster / environment",
    targets,
    format_func=lambda target: target.name,
)
PROM_URL = selected_target.prometheus_url
LOKI_URL = selected_target.loki_url
PROM_LABELS_JSON = json.dumps(selected_target.prometheus_labels, sort_keys=True)
LOKI_LABELS_JSON = json.dumps(selected_target.loki_labels, sort_keys=True)
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:0.6b")
KUBE_CONTEXT = selected_target.kubernetes_context

messages_key = f"chat_messages::{selected_target.name}"
pending_prompt_key = f"pending_prompt::{selected_target.name}"
st.session_state.setdefault(messages_key, [])
st.session_state.setdefault(pending_prompt_key, "")


@st.cache_resource
def get_prom(url, labels_json):
    return PrometheusCollector(url=url, labels=json.loads(labels_json))


@st.cache_data(ttl=300)
def get_metric_names(url, labels_json):
    return get_prom(url, labels_json).metric_names()


@st.cache_resource
def get_k8s(context, in_cluster):
    return KubernetesCollector(context=context, in_cluster=in_cluster)


@st.cache_resource
def get_loki(url, labels_json):
    if not url:
        return None
    return LokiCollector(url=url, labels=json.loads(labels_json))


@st.cache_resource
def get_agent(prom_url, prom_labels_json, loki_url, loki_labels_json,
              model, host, context, in_cluster):
    return KubernetesOpsAgent(
        prometheus=get_prom(prom_url, prom_labels_json),
        kubernetes=get_k8s(context, in_cluster),
        loki=get_loki(loki_url, loki_labels_json),
        model=model,
        host=host,
    )


prom = get_prom(PROM_URL, PROM_LABELS_JSON)
try:
    prom.health()
    prom_ok = True
except Exception as exc:
    prom_ok = False
    prom_error = str(exc)

try:
    k8s = get_k8s(KUBE_CONTEXT, selected_target.in_cluster)
    nodes = k8s.list_nodes()
    k8s_ok = True
except Exception as exc:
    k8s_ok = False
    k8s_error = str(exc)
    k8s = None
    nodes = []

try:
    loki = get_loki(LOKI_URL, LOKI_LABELS_JSON)
    if loki:
        loki.health()
        loki_ok = True
    else:
        loki_ok = False
        loki_error = "No Loki URL is configured for this target."
except Exception as exc:
    loki_ok = False
    loki_error = str(exc)
    loki = None

try:
    agent = get_agent(
        PROM_URL, PROM_LABELS_JSON, LOKI_URL, LOKI_LABELS_JSON,
        OLLAMA_MODEL, OLLAMA_HOST,
        KUBE_CONTEXT, selected_target.in_cluster,
    )
    agent_ready = True
except Exception as exc:
    agent_ready = False
    agent_error = str(exc)

with st.sidebar:
    st.header("Connections")
    st.caption(f"Selected target: **{selected_target.name}**")
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
            st.session_state[pending_prompt_key] = example
    if st.button("Clear conversation", use_container_width=True):
        st.session_state[messages_key] = []
        st.session_state[pending_prompt_key] = ""
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
    latest_rows = []
    memory_metric = history.get("unit") == "bytes"
    cpu_metric = history.get("unit") == "cores"
    for item in series:
        labels = item.get("labels", {})
        label = (labels.get("pod") or labels.get("instance") or
                 labels.get("node") or json.dumps(labels, sort_keys=True) or "series")
        for point in item.get("points", [])[-1:]:
            latest_rows.append({
                "Series": label,
                "Timestamp": datetime.fromtimestamp(point["timestamp"]).strftime("%Y-%m-%d %H:%M:%S"),
                "Value": format_metric_value(point["value"], "bytes" if memory_metric else
                                             "cpu" if cpu_metric else "raw"),
            })
        for point in item.get("points", []):
            stamp = datetime.fromtimestamp(point["timestamp"])
            value = point["value"] / (1024 ** 2) if memory_metric else point["value"]
            frames.setdefault(stamp, {})[label] = value
    if frames:
        df = pd.DataFrame.from_dict(frames, orient="index").sort_index()
        st.line_chart(df, use_container_width=True)
        unit = "MiB" if memory_metric else history.get("unit")
        st.caption(f'{history.get("metric")} · {history.get("hours")} hour(s) · unit: {unit}')
        if latest_rows:
            st.caption("Latest sample for each series")
            st.dataframe(pd.DataFrame(latest_rows), use_container_width=True, hide_index=True)
    else:
        st.warning("No historical samples returned for this metric.")


def format_metric_value(value, unit):
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if unit == "bytes":
        absolute = abs(number)
        if absolute >= 1024 ** 3:
            return f"{number / (1024 ** 3):,.2f} GiB"
        if absolute >= 1024 ** 2:
            return f"{number / (1024 ** 2):,.2f} MiB"
        if absolute >= 1024:
            return f"{number / 1024:,.2f} KiB"
        return f"{number:,.0f} B"
    if unit == "cpu":
        return f"{number:,.4f} cores ({number * 1000:,.1f} mCPU)"
    if unit == "percent":
        return f"{number:,.2f}%"
    if unit == "seconds":
        return f"{number:,.2f} s"
    return f"{number:,.6g}"


def render_metric_results(results, unit):
    rows = []
    for item in results:
        value = item.get("value")
        rows.append({
            "Labels": json.dumps(item.get("labels", {}), sort_keys=True),
            "Raw value": value,
            "Readable value": format_metric_value(value, unit),
        })
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info("No samples returned for this query.")


def render_metric_range(results, unit):
    chart_data = {}
    latest_rows = []
    for index, item in enumerate(results):
        labels = item.get("metric", {})
        series_name = (labels.get("pod") or labels.get("instance") or
                       labels.get("node") or json.dumps(labels, sort_keys=True) or f"series {index + 1}")
        values = item.get("values", [])
        if values:
            timestamp, latest_value = values[-1]
            latest_rows.append({
                "Series": series_name,
                "Timestamp": datetime.fromtimestamp(float(timestamp)).strftime("%Y-%m-%d %H:%M:%S"),
                "Value": format_metric_value(latest_value, unit),
            })
        for timestamp, value in values:
            try:
                stamp = datetime.fromtimestamp(float(timestamp))
                numeric_value = float(value)
            except (TypeError, ValueError, OSError):
                continue
            chart_value = numeric_value / (1024 ** 2) if unit == "bytes" else numeric_value
            chart_data.setdefault(stamp, {})[series_name] = chart_value
    if chart_data:
        frame = pd.DataFrame.from_dict(chart_data, orient="index").sort_index()
        st.line_chart(frame, use_container_width=True)
        chart_unit = "MiB" if unit == "bytes" else unit
        st.caption(f"Time-series values · chart unit: {chart_unit}")
        if latest_rows:
            st.caption("Latest sample for each series")
            st.dataframe(pd.DataFrame(latest_rows), use_container_width=True, hide_index=True)
    else:
        st.info("No time-series samples returned for this query.")

with tab_chat:
    st.subheader("Ask your Kubernetes SRE assistant")
    st.caption("The assistant can call Prometheus, Kubernetes and Loki read-only tools. Follow-up questions use recent chat context.")
    chat_messages = st.session_state[messages_key]

    for idx, msg in enumerate(chat_messages):
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg["role"] == "assistant" and msg.get("charts"):
                for history in msg["charts"]:
                    with st.expander(f'Historical chart: {history.get("metric")}', expanded=True):
                        render_history_chart(history)

    prompt = st.chat_input("Ask about metrics, pod logs, node conditions or OS logs...")
    if st.session_state[pending_prompt_key]:
        prompt = st.session_state[pending_prompt_key]
        st.session_state[pending_prompt_key] = ""
    if prompt:
        chat_messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            with st.spinner("Collecting evidence and analyzing..."):
                try:
                    result = agent.respond(chat_messages)
                    answer = result["answer"]
                    charts = result.get("charts", [])
                except Exception as exc:
                    answer = f"**Agent error:** {exc}\n\nCheck Ollama, model availability, Kubernetes access and service endpoints."
                    charts = []
            st.markdown(answer)
            for history in charts:
                with st.expander(f'Historical chart: {history.get("metric")}', expanded=True):
                    render_history_chart(history)
        chat_messages.append({"role": "assistant", "content": answer, "charts": charts})

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
    st.subheader("Prometheus Metrics Explorer")
    explore_mode = st.radio(
        "Metric source",
        ["Curated Kubernetes metrics", "All Prometheus metrics"],
        horizontal=True,
    )
    if explore_mode == "Curated Kubernetes metrics":
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
    else:
        st.caption(
            "Browse every metric name exposed by this Prometheus target, query its "
            "current samples, or graph its raw time series. Use the label filter to "
            "narrow high-cardinality metrics."
        )
        try:
            metric_names = get_metric_names(PROM_URL, PROM_LABELS_JSON)
        except Exception as exc:
            st.error(f"Could not list Prometheus metrics: {exc}")
            metric_names = []

        if metric_names:
            metric_search = st.text_input("Filter metric names")
            filtered_metrics = [
                name for name in metric_names
                if metric_search.casefold() in name.casefold()
            ]
            if not filtered_metrics:
                st.warning("No metric names match that filter.")
            else:
                selected_name = st.selectbox(
                    f"Metric name ({len(filtered_metrics)} available)",
                    filtered_metrics,
                )
                col_label, col_value = st.columns(2)
                label_name = col_label.text_input(
                    "Optional label name", placeholder="namespace"
                ).strip()
                label_value = col_value.text_input(
                    "Optional exact label value", placeholder="default"
                )
                matchers = {}
                if label_name and label_value:
                    matchers[label_name] = label_value
                elif label_name or label_value:
                    st.info("Enter both a label name and value to apply the filter.")

                unit = st.selectbox(
                    "Display unit",
                    ["Raw number", "CPU cores", "Bytes", "Percent", "Seconds"],
                    help=(
                        "Prometheus metrics do not consistently publish unit metadata. "
                        "Choose how numeric values should be formatted."
                    ),
                )
                unit_key = {
                    "Raw number": "raw",
                    "CPU cores": "cpu",
                    "Bytes": "bytes",
                    "Percent": "percent",
                    "Seconds": "seconds",
                }[unit]
                metric_col, history_col = st.columns(2)
                if metric_col.button("Query current values"):
                    try:
                        results = prom.query_metric(selected_name, matchers=matchers)
                        render_metric_results(prom.values(results), unit_key)
                    except Exception as exc:
                        st.error(f"Prometheus instant query failed: {exc}")

                hours = st.slider("History range (hours)", 1, 24, 2)
                if history_col.button("Plot time series"):
                    try:
                        end = int(time.time())
                        start = end - int(hours * 3600)
                        results = prom.query_metric_range(
                            selected_name,
                            start,
                            end,
                            step="60s",
                            matchers=matchers,
                        )
                        render_metric_range(results, unit_key)
                    except Exception as exc:
                        st.error(f"Prometheus range query failed: {exc}")
        else:
            st.info("No metric names were returned by Prometheus.")

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
