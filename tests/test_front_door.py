"""The kind front door (deploy/kind/gateway.yaml, deploy/kind/tools/, scripts/cluster.sh).

The users' listener and the dev tools' listener must stay apart, and the laptop ports in
scripts/cluster.sh must forward to the node ports the Gateway's Service actually pins.
Its metrics (the platform chart's templates/monitoring.yaml): Prometheus must find the
proxies and the controller where Envoy Gateway runs them.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
# The app's listener admits every app's namespaces, by the label the apps ApplicationSet
# gives them (platform plan 5.7); the tools' listener, its two namespaces by name.
APP_NAMESPACES = {"key": "easypeasyops.github.io/app", "operator": "Exists"}
TOOL_NAMESPACES = {"argocd", "monitoring"}
LISTENER_NAMESPACES = {"http": APP_NAMESPACES, "tools": TOOL_NAMESPACES}
CHART = ROOT / "deploy/argocd/platform"


def _docs(path: Path) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def _gateway_docs() -> dict[str, dict[str, Any]]:
    return {d["kind"]: d for d in _docs(ROOT / "deploy/kind/gateway.yaml")}


def test_each_listener_admits_only_its_namespaces() -> None:
    listeners = _gateway_docs()["Gateway"]["spec"]["listeners"]
    assert {listener["name"] for listener in listeners} == set(LISTENER_NAMESPACES)
    for listener in listeners:
        (expr,) = listener["allowedRoutes"]["namespaces"]["selector"]["matchExpressions"]
        if listener["name"] == "http":
            assert expr == APP_NAMESPACES
        else:
            assert expr["key"] == "kubernetes.io/metadata.name"
            assert expr["operator"] == "In"
            assert set(expr["values"]) == TOOL_NAMESPACES


def test_tool_routes_attach_to_the_tools_listener_only() -> None:
    routes = [d for f in sorted((ROOT / "deploy/kind/tools").glob("*.yaml")) for d in _docs(f)]
    assert {r["metadata"]["name"] for r in routes} == {
        "argocd",
        "grafana",
        "prometheus",
        "alertmanager",
    }
    for route in routes:
        assert route["metadata"]["namespace"] in TOOL_NAMESPACES
        assert route["spec"]["parentRefs"] == [
            {"name": "shortly", "namespace": "gateway", "sectionName": "tools"}
        ]
        assert route["spec"]["hostnames"] == [f"{route['metadata']['name']}.localhost"]


def test_front_door_ports_match_the_pinned_node_ports() -> None:
    listeners = {
        listener["port"]: listener["name"]
        for listener in _gateway_docs()["Gateway"]["spec"]["listeners"]
    }
    service = _gateway_docs()["EnvoyProxy"]["spec"]["provider"]["kubernetes"]["envoyService"]
    assert service["type"] == "NodePort"
    pinned = {p["nodePort"]: p["port"] for p in service["patch"]["value"]["spec"]["ports"]}
    assert set(pinned.values()) == set(listeners), "every listener needs a pinned node port"

    script = (ROOT / "scripts/cluster.sh").read_text()
    match = re.search(r"^FRONT_DOOR_PORTS=\(([^)]*)\)", script, re.M)
    assert match, "scripts/cluster.sh has no FRONT_DOOR_PORTS"
    forwarded = {int(host): int(node) for host, node in re.findall(r'"(\d+):(\d+)"', match[1])}
    assert set(forwarded.values()) == set(pinned), "a forwarder points at an unpinned port"
    # The docs and the Argo CD / Grafana settings use 8080 for users, 8090 for the tools.
    by_listener = {listeners[pinned[node]]: host for host, node in forwarded.items()}
    assert by_listener == {"http": 8080, "tools": 8090}


def _platform(*sets: str) -> list[dict[str, Any]]:
    """What the platform chart renders: the child Applications and the root's own resources."""
    if shutil.which("helm") is None:
        if os.environ.get("CI"):
            pytest.fail("helm is required in CI")
        pytest.skip("helm not installed")
    args = [arg for s in ("arch=amd64", *sets) for arg in ("--set", s)]
    out = subprocess.run(
        ["helm", "template", "platform", str(CHART), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def _monitors(*sets: str) -> dict[str, dict[str, Any]]:
    return {
        d["kind"]: d
        for d in _platform(*sets)
        if d["kind"] in {"PodMonitor", "ServiceMonitor"} and "envoy" in d["metadata"]["name"]
    }


def test_the_front_door_is_scraped_where_envoy_gateway_runs() -> None:
    """The monitors live in the root's namespace, which always exists, and select across to
    the namespace the envoy-gateway Application deploys into; without monitoring (no
    Prometheus Operator CRDs) they aren't rendered."""
    apps = {d["metadata"]["name"]: d for d in _platform() if d["kind"] == "Application"}
    runs_in = apps["envoy-gateway"]["spec"]["destination"]["namespace"]
    root = yaml.safe_load((ROOT / "deploy/argocd/platform-root.yaml").read_text())
    monitors = _monitors()
    assert set(monitors) == {"PodMonitor", "ServiceMonitor"}
    for monitor in monitors.values():
        assert monitor["metadata"]["namespace"] == root["spec"]["destination"]["namespace"]
        assert monitor["spec"]["namespaceSelector"] == {"matchNames": [runs_in]}
    # The proxy: Envoy's own Prometheus endpoint (EnvoyProxy's default, not switched off).
    (endpoint,) = monitors["PodMonitor"]["spec"]["podMetricsEndpoints"]
    assert (endpoint["port"], endpoint["path"]) == ("metrics", "/stats/prometheus")
    proxy = _gateway_docs()["EnvoyProxy"]["spec"]
    assert "telemetry" not in proxy, "check metrics.prometheus is still on, then update this"
    assert not _monitors("components.monitoring=false")


def test_a_routes_series_say_which_route() -> None:
    """Envoy's name for a route's backends becomes route_namespace and route; its own
    clusters (xds_cluster, prometheus_stats) get neither. Prometheus anchors the regex."""
    (endpoint,) = _monitors()["PodMonitor"]["spec"]["podMetricsEndpoints"]
    relabelings = {r["targetLabel"]: r for r in endpoint["metricRelabelings"]}
    assert set(relabelings) == {"route_namespace", "route"}
    for label, want in (("route_namespace", "aipostit-prod"), ("route", "aipostit")):
        rule = relabelings[label]
        assert rule["sourceLabels"] == ["envoy_cluster_name"]
        match = re.fullmatch(rule["regex"], "httproute/aipostit-prod/aipostit/rule/0")
        assert match
        assert match.expand(rule["replacement"].replace("$", "\\")) == want
        assert not re.fullmatch(rule["regex"], "xds_cluster")


@pytest.mark.integration
def test_the_controllers_service_has_the_port_the_monitor_scrapes() -> None:
    """The ServiceMonitor names a label and a port of the Service Envoy Gateway's chart
    renders: a chart bump that renames either must fail here, not as a missing target.
    (The proxy's pods are made by the controller, not rendered: the cluster shows those.)"""
    monitor = _monitors()["ServiceMonitor"]["spec"]
    values = yaml.safe_load((CHART / "values.yaml").read_text())["charts"]["envoyGateway"]
    out = subprocess.run(
        [
            *("helm", "template", "eg", f"{values['repo']}@{values['digest']}"),
            *("--namespace", "envoy-gateway-system"),
            *("--values", str(ROOT / "deploy/kind/envoy-gateway-values.yaml")),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    selected = [
        d
        for d in yaml.safe_load_all(out)
        if d
        and d["kind"] == "Service"
        and monitor["selector"]["matchLabels"].items() <= d["metadata"]["labels"].items()
    ]
    (service,) = selected
    (endpoint,) = monitor["endpoints"]
    assert endpoint["port"] in {port["name"] for port in service["spec"]["ports"]}


def test_the_dashboard_ships_with_monitoring_and_the_runbook_links_it() -> None:
    def dashboards(*sets: str) -> list[dict[str, Any]]:
        return [
            d
            for d in _platform(*sets)
            if d["kind"] == "ConfigMap" and d["metadata"]["labels"].get("grafana_dashboard") == "1"
        ]

    (config_map,) = dashboards()
    (text,) = config_map["data"].values()
    dashboard = json.loads(text)  # the chart must hand Grafana the file as it is
    assert dashboard == json.loads((CHART / "files/front-door-dashboard.json").read_text())
    values = yaml.safe_load((ROOT / "deploy/kind/monitoring-values.yaml").read_text())
    link = f"{values['grafana']['grafana.ini']['server']['root_url']}/d/{dashboard['uid']}"
    assert link in (ROOT / "docs/RUNBOOK.md").read_text()
    assert not dashboards("components.monitoring=false")


def test_the_dashboard_reads_the_series_the_monitors_make() -> None:
    """Every query selects the proxies' or the controller's job, or kube-state-metrics' for
    the routes' status, and breaks a route's series down by the labels the PodMonitor adds:
    a renamed label must fail here, not as an empty panel. The three things the ticket asks
    for are there, per route, and the routes with no backend."""
    monitors = _monitors()
    # The proxies' job: the one the PodMonitor's jobLabel gives them.
    proxies = monitors["PodMonitor"]["spec"]
    assert proxies["selector"]["matchLabels"][proxies["jobLabel"]] == "envoy"
    (endpoint,) = monitors["PodMonitor"]["spec"]["podMetricsEndpoints"]
    route_labels = {r["targetLabel"] for r in endpoint["metricRelabelings"]}
    dashboard = json.loads((CHART / "files/front-door-dashboard.json").read_text())
    queries = {
        panel["title"]: target["expr"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
    }
    for title, expr in queries.items():
        assert re.search(r'job="(envoy(-gateway)?|kube-state-metrics)"', expr), title
        for label in re.findall(r"\broute\w*", expr):
            assert label in route_labels, f"{title}: {label}"
    for title, metric in (
        ("Request rate", "envoy_cluster_upstream_rq_total"),
        ("Error rate (5xx share)", 'envoy_cluster_upstream_rq_xx{job="envoy",route_namespace'),
        ("Latency, p95", "envoy_cluster_upstream_rq_time_bucket"),
        ("Routes with no backend", _route_status_series() + '{job="kube-state-metrics"'),
    ):
        assert metric in queries[title], title
        assert "by (route_namespace, route" in queries[title], title


def _route_status_series() -> str:
    """The series kube-state-metrics makes of every HTTPRoute's conditions (custom resource
    state, deploy/kind/monitoring-values.yaml): it may read the routes, and labels each
    series as the PodMonitor labels a route's Envoy series, plus the condition's own."""
    values = yaml.safe_load((ROOT / "deploy/kind/monitoring-values.yaml").read_text())
    ksm = values["kube-state-metrics"]
    assert ksm["customResourceState"]["enabled"] is True
    (resource,) = ksm["customResourceState"]["config"]["spec"]["resources"]
    assert resource["groupVersionKind"]["kind"] == "HTTPRoute"
    assert {
        "apiGroups": [resource["groupVersionKind"]["group"]],
        "resources": ["httproutes"],
        "verbs": ["list", "watch"],
    } in ksm["rbac"]["extraRules"]
    assert resource["labelsFromPath"] == {
        "route_namespace": ["metadata", "namespace"],
        "route": ["metadata", "name"],
    }
    (metric,) = resource["metrics"]
    gauge = metric["each"]["gauge"]
    assert gauge["labelsFromPath"] == {"type": ["type"], "reason": ["reason"]}
    assert gauge["valueFrom"] == ["status"]  # "True" is 1, "False" 0
    return f"{resource['metricNamePrefix']}_{metric['name']}"


@pytest.mark.integration
def test_the_stack_hands_kube_state_metrics_the_route_config() -> None:
    """The values are the kube-state-metrics subchart's: a stack bump that renames one must
    fail here, not as an alert that can never fire. Rendered, kube-state-metrics is started
    with the config and may list and watch HTTPRoutes."""
    series = _route_status_series()
    chart = yaml.safe_load((CHART / "values.yaml").read_text())["charts"]["kubePrometheusStack"]
    out = subprocess.run(
        [
            *("helm", "template", "monitoring", f"{chart['repo']}@{chart['digest']}"),
            *("--namespace", "monitoring"),
            *("--values", str(ROOT / "deploy/kind/monitoring-values.yaml")),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    docs = [
        d for d in yaml.safe_load_all(out) if d and "kube-state-metrics" in d["metadata"]["name"]
    ]
    (deployment,) = [d for d in docs if d["kind"] == "Deployment"]
    (container,) = deployment["spec"]["template"]["spec"]["containers"]
    (flag,) = [a for a in container["args"] if a.startswith("--custom-resource-state-config-file=")]
    (config_map,) = [d for d in docs if d["kind"] == "ConfigMap"]
    (resource,) = yaml.safe_load(config_map["data"][Path(flag.split("=")[1]).name])["spec"][
        "resources"
    ]
    assert f"{resource['metricNamePrefix']}_{resource['metrics'][0]['name']}" == series
    (role,) = [d for d in docs if d["kind"] == "ClusterRole"]
    assert {
        "apiGroups": ["gateway.networking.k8s.io"],
        "resources": ["httproutes"],
        "verbs": ["list", "watch"],
    } in role["rules"]


def test_the_front_door_alerts_ship_as_the_rules_file_says() -> None:
    (rule,) = [
        d
        for d in _platform()
        if d["kind"] == "PrometheusRule" and d["metadata"]["name"] == "front-door"
    ]
    assert rule["spec"] == yaml.safe_load((CHART / "files/front-door-rules.yaml").read_text())
    alerts = {a["alert"]: a for group in rule["spec"]["groups"] for a in group["rules"]}
    # The proxies and the controller: absent from the jobs their monitors give them, so a
    # pod that is gone (no target left to be down) counts as down.
    proxies = _monitors()["PodMonitor"]["spec"]
    job = proxies["selector"]["matchLabels"][proxies["jobLabel"]]
    assert alerts.pop("FrontDoorDown")["expr"] == f'absent(up{{job="{job}"}} == 1)'
    controller = alerts.pop("FrontDoorControllerDown")
    assert controller["expr"] == 'absent(up{job="envoy-gateway"} == 1)'
    # The rest read the routes' status, and name the route: no backend, with or without
    # traffic.
    assert alerts
    for alert in alerts.values():
        assert _route_status_series() + '{job="kube-state-metrics"' in alert["expr"]
        assert "by (route_namespace, route, type, reason)" in alert["expr"]
        assert (
            "{{ $labels.route_namespace }}/{{ $labels.route }}" in (alert["annotations"]["summary"])
        )
