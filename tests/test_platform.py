"""The platform under Argo CD (deploy/argocd/platform/, links DESIGN: GitOps with Argo CD).

The chart renders the child Applications; these checks hold for every way it is rendered:
  - every chart is pulled by digest, from a registry the platform project allows
  - every git source deploys the revision the root asks for, from this repository
  - the children stay inside the platform project's namespaces, sync in the right waves,
    and orphan (not delete) their resources if the Application goes
  - the vendored metrics-server manifest is byte for byte the one that was pinned
and (integration) each chart's version still resolves to the digest pinned for it.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CHART = ROOT / "deploy/argocd/platform"
VALUES = yaml.safe_load((CHART / "values.yaml").read_text())
NODE_EXPORTER = yaml.safe_load((ROOT / "deploy/kind/node-exporter-values.yaml").read_text())
PROJECT = yaml.safe_load((ROOT / "deploy/argocd/platform-project.yaml").read_text())["spec"]
REPO = "https://github.com/easypeasyops/platform.git"
WAVE_1 = {"kyverno-policies"}  # needs Kyverno's webhook serving, not just its CRDs
WAVE_FIRST = {"platform-project"}  # wave -2: the AppProject the children are checked against
WAVE_BEFORE = {"external-secrets", "cert-manager"}  # wave -1: wave 0 needs their Secrets


def _render(*sets: str) -> list[dict[str, Any]]:
    if shutil.which("helm") is None:
        if os.environ.get("CI"):
            pytest.fail("helm is required in CI")
        pytest.skip("helm not installed")
    args = [arg for s in ("arch=amd64", "revision=0123abc", *sets) for arg in ("--set", s)]
    out = subprocess.run(
        ["helm", "template", "platform", str(CHART), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d and d["kind"] == "Application"]


ALL_ON = ()
ALL_OFF = ("components.monitoring=false", "components.policies=false", "components.backups=false")


def _sources(app: dict[str, Any]) -> list[dict[str, Any]]:
    spec = app["spec"]
    return spec.get("sources") or [spec["source"]]


@pytest.mark.parametrize("sets", [ALL_ON, ALL_OFF], ids=["all", "minimal"])
def test_children_are_pinned_and_stay_in_the_platform_project(sets: tuple[str, ...]) -> None:
    allowed_repos = set(PROJECT["sourceRepos"])
    allowed_ns = {d["namespace"] for d in PROJECT["destinations"]}
    for app in _render(*sets):
        name = app["metadata"]["name"]
        assert app["kind"] == "Application"
        assert app["metadata"]["namespace"] == "argocd"
        assert app["spec"]["project"] == "platform", name
        assert app["spec"]["destination"]["namespace"] in allowed_ns, name
        # Orphan, don't cascade: a deleted Application must not take the platform with it.
        assert "finalizers" not in app["metadata"], name
        sync = app["spec"]["syncPolicy"]
        assert sync["automated"] == {"prune": True, "selfHeal": True}, name
        assert "ServerSideApply=true" in sync["syncOptions"], name
        for source in _sources(app):
            assert source["repoURL"] in allowed_repos, (name, source["repoURL"])
            if source["repoURL"] == REPO:
                assert source["targetRevision"] == "0123abc", name  # the root's revision
            else:  # an OCI chart: by digest, never a tag
                assert re.fullmatch(r"sha256:[0-9a-f]{64}", source["targetRevision"]), name
            for values_file in source.get("helm", {}).get("valueFiles", []):
                assert (ROOT / values_file.removeprefix("$values/")).is_file(), values_file


def test_waves_put_crd_users_after_the_charts_that_install_the_crds() -> None:
    waves = {
        a["metadata"]["name"]: a["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]
        for a in _render()
    }
    assert {name for name, wave in waves.items() if wave == "1"} == WAVE_1
    assert {name for name, wave in waves.items() if wave == "-1"} == WAVE_BEFORE
    assert {name for name, wave in waves.items() if wave == "-2"} == WAVE_FIRST
    assert set(waves.values()) == {"-2", "-1", "0", "1"}


def test_components_can_be_switched_off() -> None:
    names = {a["metadata"]["name"] for a in _render(*ALL_OFF)}
    assert names == {
        "platform-project",
        "external-secrets",
        "secret-store",
        "cert-manager",
        "cloudnative-pg",
        "barman-cloud",
        "metrics-server",
        "envoy-gateway",
        "argo-rollouts",
        "gateway",
        "tool-routes",
    }
    routes = next(a for a in _render(*ALL_OFF) if a["metadata"]["name"] == "tool-routes")
    assert routes["spec"]["source"]["directory"]["include"] == "argocd.yaml"


@pytest.mark.parametrize("arch", ["amd64", "arm64"])
def test_rollouts_plugin_matches_the_node_architecture(arch: str) -> None:
    app = next(a for a in _render(f"arch={arch}") if a["metadata"]["name"] == "argo-rollouts")
    (plugin,) = app["spec"]["sources"][0]["helm"]["valuesObject"]["controller"][
        "trafficRouterPlugins"
    ]
    assert plugin["location"].endswith(f"-linux-{arch}")
    assert plugin["sha256"] == VALUES["gatewayAPIPlugin"]["sha256"][arch]


def test_helm_releases_handed_over_are_the_ones_argo_cd_renders() -> None:
    """cluster.sh deletes these Helm records once Argo CD owns the resources: each name must
    be one Argo CD renders (charts added later, like External Secrets, were never Helm's)."""
    script = (ROOT / "scripts/cluster.sh").read_text()
    match = re.search(r"^HELM_RELEASES=\(([^)]*)\)", script, re.M)
    assert match
    handed_over = set(match[1].split())
    rendered = {
        f"{a['spec']['destination']['namespace']}/{a['spec']['sources'][0]['helm']['releaseName']}"
        for a in _render()
        if "helm" in a["spec"].get("sources", [{}])[0]  # a chart (backups is git only)
    }
    assert handed_over <= rendered


def test_one_application_sets_each_namespaces_labels() -> None:
    """Two Applications in one namespace (CloudNativePG and its plugin): only one of them may
    manage the namespace's metadata, or each sync would undo the other's."""
    managers: dict[str, list[str]] = {}
    for app in _render():
        if "managedNamespaceMetadata" in app["spec"]["syncPolicy"]:
            ns = app["spec"]["destination"]["namespace"]
            managers.setdefault(ns, []).append(app["metadata"]["name"])
    assert {ns: names for ns, names in managers.items() if len(names) > 1} == {}
    assert managers["cnpg-system"] == ["cloudnative-pg"]


def test_logs_ship_with_monitoring_and_leave_its_namespace_labels_alone() -> None:
    """VictoriaLogs lives in kube-prometheus-stack's namespace: installed and skipped with
    it (cluster.sh's MONITORING knob), and the Pod Security label stays the monitoring
    Application's to set."""
    (logs,) = [a for a in _render() if a["metadata"]["name"] == "logs"]
    assert logs["spec"]["destination"]["namespace"] == "monitoring"
    assert "managedNamespaceMetadata" not in logs["spec"]["syncPolicy"]
    chart = logs["spec"]["sources"][0]
    assert chart["repoURL"] == VALUES["charts"]["victoriaLogs"]["repo"]
    assert chart["helm"]["valueFiles"] == ["$values/deploy/kind/victoria-logs-values.yaml"]
    without = {a["metadata"]["name"] for a in _render("components.monitoring=false")}
    assert not {"logs", "monitoring"} & without


def test_slack_is_wired_only_where_the_store_has_the_webhooks() -> None:
    """The webhooks are loaded by hand, so a fresh cluster has none: with their
    ExternalSecret the monitoring Application would wait on it for good and take the root
    with it (links #157's e2e). Without `components.slack` neither the ExternalSecret nor
    Alertmanager's mount of its Secret is there; cluster.sh sets it from the store."""

    def monitoring(*sets: str) -> tuple[str, dict[str, Any]]:
        (app,) = [a for a in _render(*sets) if a["metadata"]["name"] == "monitoring"]
        chart, _, extra = app["spec"]["sources"]
        assert extra["path"] == "deploy/secrets"
        return extra["directory"]["include"], chart["helm"].get("valuesObject", {})

    assert monitoring() == ("{monitoring.yaml,alertmanager-slack.yaml}", {})
    assert monitoring("components.slack=false") == (
        "monitoring.yaml",
        {"alertmanager": {"alertmanagerSpec": {"secrets": []}}},
    )
    # What the switch removes is all there is: the mount in the values file, one
    # ExternalSecret in the file, and the store Secret cluster.sh looks for by that name.
    values = yaml.safe_load((ROOT / "deploy/kind/monitoring-values.yaml").read_text())
    assert values["alertmanager"]["alertmanagerSpec"]["secrets"] == ["alertmanager-slack"]
    (es,) = yaml.safe_load_all((ROOT / "deploy/secrets/alertmanager-slack.yaml").read_text())
    assert {ref["remoteRef"]["key"] for ref in es["spec"]["data"]} == {"alertmanager-slack"}
    script = (ROOT / "scripts/cluster.sh").read_text()
    assert "-n secret-store get secret alertmanager-slack --ignore-not-found" in script
    assert '{"name":"components.slack","value":"%s"}' in script


def test_privileged_namespaces_hold_one_application_each() -> None:
    """Reading the node's files takes a hostPath volume, which Pod Security allows only at
    "privileged": the log collector and node-exporter each have a namespace at that level
    that holds nothing else, and the collector's is the one the store's NetworkPolicy lets
    write."""
    privileged = {
        a["spec"]["destination"]["namespace"]
        for a in _render()
        if "privileged"
        in a["spec"]["syncPolicy"].get("managedNamespaceMetadata", {}).get("labels", {}).values()
    }
    assert privileged == {"log-collector", "node-exporter"}
    without_monitoring = {a["metadata"]["name"] for a in _render("components.monitoring=false")}
    for ns in privileged:
        in_it = [a for a in _render() if a["spec"]["destination"]["namespace"] == ns]
        assert [a["metadata"]["name"] for a in in_it] == [ns]
        assert ns not in without_monitoring
    store = yaml.safe_load((ROOT / "deploy/kind/victoria-logs-values.yaml").read_text())
    (rule,) = store["networkPolicy"]["ingress"]
    assert store["networkPolicy"]["enabled"] is True
    assert rule["ports"] == [{"port": 9428, "protocol": "TCP"}]
    assert rule["from"] == [
        {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "log-collector"}}},
        {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "grafana"}}},
        {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "prometheus"}}},
    ]


@pytest.mark.integration
def test_the_log_collector_gets_less_than_its_namespace_allows() -> None:
    """The chart, rendered with our values: no host network, PID or IPC, no privileged
    container or capability, the node's disk read-only except its state directory, and no
    stream field beyond where a line came from (a per-request one would be a stream each)."""
    chart = VALUES["charts"]["logCollector"]
    _render()  # skips without helm
    out = subprocess.run(
        [
            *("helm", "template", "log-collector", f"{chart['repo']}@{chart['digest']}"),
            *("--namespace", "log-collector"),
            *("--values", str(ROOT / "deploy/kind/log-collector-values.yaml")),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    docs = [d for d in yaml.safe_load_all(out) if d]
    (daemonset,) = [d for d in docs if d["kind"] == "DaemonSet"]
    pod = daemonset["spec"]["template"]["spec"]
    assert not {"hostNetwork", "hostPID", "hostIPC", "initContainers"} & set(pod)
    (container,) = pod["containers"]
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "readOnlyRootFilesystem": True,
    }
    host_paths = {v["name"]: v["hostPath"]["path"] for v in pod["volumes"]}
    assert set(host_paths.values()) == {"/var/log", "/var/lib/vl-collector"}
    assert len(host_paths) == len(pod["volumes"])  # no other kind of volume either
    writable = {m["name"] for m in container["volumeMounts"] if not m.get("readOnly")}
    assert {host_paths[name] for name in writable} == {"/var/lib/vl-collector"}
    args = container["args"]
    assert "--remoteWrite.url=http://victoria-logs.monitoring.svc:9428/insert/native" in args
    assert (
        "--kubernetesCollector.streamFields="
        "kubernetes.container_name,kubernetes.pod_name,kubernetes.pod_namespace"
    ) in args
    # What it may read from the API: where pods run, nothing of theirs.
    (role,) = [d for d in docs if d["kind"] == "ClusterRole"]
    assert role["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["nodes", "namespaces", "pods"],
            "verbs": ["get", "list", "watch"],
        }
    ]


def test_the_event_exporter_reads_events_and_nothing_else() -> None:
    """Kubernetes events into the logs (PT-24): one pod, in a namespace of its own at
    "restricted", that may list and watch events and nothing else, writes them to its stdout
    for the log collector, and admits no connection."""
    docs = list(yaml.safe_load_all((ROOT / "deploy/kind/event-exporter.yaml").read_text()))
    by_kind = {d["kind"]: d for d in docs}
    assert len(by_kind) == len(docs)  # one of each
    assert set(by_kind) == {
        *("ServiceAccount", "ClusterRole", "ClusterRoleBinding"),
        *("ConfigMap", "Deployment", "NetworkPolicy"),
    }
    assert by_kind["ClusterRole"]["rules"] == [
        {"apiGroups": [""], "resources": ["events"], "verbs": ["list", "watch"]}
    ]
    binding = by_kind["ClusterRoleBinding"]
    assert binding["roleRef"]["name"] == by_kind["ClusterRole"]["metadata"]["name"]
    sa = by_kind["ServiceAccount"]["metadata"]["name"]
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": sa, "namespace": "event-exporter"}
    ]

    deployment = by_kind["Deployment"]["spec"]
    assert deployment["replicas"] == 1  # two would write every event twice
    pod = deployment["template"]["spec"]
    assert pod["serviceAccountName"] == sa
    assert not any(pod.get(k) for k in ("hostNetwork", "hostPID", "hostIPC", "initContainers"))
    assert [v["name"] for v in pod["volumes"] if set(v) != {"name", "configMap"}] == []
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["runAsUser"] != 0
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    (container,) = pod["containers"]
    assert re.fullmatch(r"[a-z0-9./-]+@sha256:[0-9a-f]{64}", container["image"])
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "readOnlyRootFilesystem": True,
    }
    assert "ports" not in container
    policy = by_kind["NetworkPolicy"]["spec"]
    assert policy == {"podSelector": {}, "policyTypes": ["Ingress"]}  # no one may connect

    # Only to its stdout, never asking for the object an event is about (the role has no
    # `get`), and with the fields RUNBOOK's queries search by.
    config = yaml.safe_load(by_kind["ConfigMap"]["data"]["config.yaml"])
    assert config["omitLookup"] is True
    assert "leaderElection" not in config  # it would need leases
    (receiver,) = config["receivers"]
    assert set(receiver) == {"name", "stdout"}
    layout = receiver["stdout"]["layout"]
    assert {"message", "reason", "type"} <= set(layout)
    assert {"kind", "namespace", "name"} <= set(layout["involvedObject"])
    runbook = (ROOT / "docs/RUNBOOK.md").read_text()
    for field in ("involvedObject.kind", "involvedObject.name", "reason", "type"):
        assert f"{field}:=" in runbook, field

    # Its Application: with the logs it writes for, into that namespace alone, "restricted".
    (app,) = [a for a in _render() if a["metadata"]["name"] == "event-exporter"]
    assert app["spec"]["destination"]["namespace"] == "event-exporter"
    assert app["spec"]["source"]["directory"] == {"include": "event-exporter.yaml"}
    assert app["spec"]["syncPolicy"]["managedNamespaceMetadata"]["labels"] == {
        "pod-security.kubernetes.io/enforce": "restricted"
    }
    in_it = [a for a in _render() if a["spec"]["destination"]["namespace"] == "event-exporter"]
    assert in_it == [app]
    assert "event-exporter" not in {
        a["metadata"]["name"] for a in _render("components.monitoring=false")
    }


@pytest.mark.integration
def test_node_exporter_gets_less_than_its_namespace_allows() -> None:
    """The chart, rendered with our values: the node's /proc, /sys and / read-only and
    nothing else of the node's (no host network, PID or IPC), not root, no privileged
    container or capability, no token and no role in the API, and a NetworkPolicy that
    admits Prometheus alone. On the pod network, or that policy wouldn't apply to it."""
    chart = VALUES["charts"]["nodeExporter"]
    _render()  # skips without helm
    out = subprocess.run(
        [
            *("helm", "template", "node-exporter", f"{chart['repo']}@{chart['digest']}"),
            *("--namespace", "node-exporter"),
            *("--values", str(ROOT / "deploy/kind/node-exporter-values.yaml")),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    docs = [d for d in yaml.safe_load_all(out) if d]
    assert {d["kind"] for d in docs} == {
        "DaemonSet",
        "NetworkPolicy",
        "PodMonitor",
        "ServiceAccount",
    }
    (daemonset,) = [d for d in docs if d["kind"] == "DaemonSet"]
    pod = daemonset["spec"]["template"]["spec"]
    assert not any(pod.get(k) for k in ("hostNetwork", "hostPID", "hostIPC", "initContainers"))
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["runAsUser"] != 0
    (container,) = pod["containers"]
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "readOnlyRootFilesystem": True,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert "hostPort" not in str(container["ports"])
    host_paths = {v["name"]: v["hostPath"]["path"] for v in pod["volumes"]}
    assert set(host_paths.values()) == {"/proc", "/sys", "/"}
    assert len(host_paths) == len(pod["volumes"])  # no other kind of volume either
    assert {m["name"] for m in container["volumeMounts"]} == set(host_paths)
    assert all(m["readOnly"] is True for m in container["volumeMounts"])
    (policy,) = [d["spec"] for d in docs if d["kind"] == "NetworkPolicy"]
    assert (
        policy["podSelector"]["matchLabels"].items()
        <= daemonset["spec"]["template"]["metadata"]["labels"].items()
    )
    assert policy["policyTypes"] == ["Egress", "Ingress"]
    assert "egress" not in policy
    assert policy["ingress"] == [
        {
            "from": [
                {
                    "namespaceSelector": {
                        "matchLabels": {"kubernetes.io/metadata.name": "monitoring"}
                    },
                    "podSelector": {"matchLabels": {"app.kubernetes.io/name": "prometheus"}},
                }
            ],
            "ports": [{"port": 9100, "protocol": "TCP"}],
        }
    ]
    # The stack's node rules and dashboards select this job.
    (monitor,) = [d for d in docs if d["kind"] == "PodMonitor"]
    job_label = monitor["spec"]["jobLabel"]
    assert daemonset["spec"]["template"]["metadata"]["labels"][job_label] == "node-exporter"
    # Our filesystem arguments, in place of the chart's (not beside them).
    exclude = [a for a in container["args"] if a.startswith("--collector.filesystem.")]
    assert exclude == NODE_EXPORTER["extraArgs"]


def test_node_exporter_reports_the_nodes_disk_once() -> None:
    """The kind node's disk is /var, and Docker binds /etc/hostname, /etc/hosts and
    /etc/resolv.conf into the node from the same disk: reported, each disk alert fires four
    times (docs/RUNBOOK.md, The node's disk is filling up). Our list replaces the chart's,
    so it keeps the chart's own exclusions (chart 5.1.0) and adds the three files."""
    args = dict(a.split("=", 1) for a in NODE_EXPORTER["extraArgs"])
    assert set(args) == {
        "--collector.filesystem.mount-points-exclude",
        "--collector.filesystem.fs-types-exclude",
    }
    mounts = re.compile(args["--collector.filesystem.mount-points-exclude"])
    for left_out in (
        *("/etc/hostname", "/etc/hosts", "/etc/resolv.conf"),
        # the chart's own
        *("/dev", "/dev/pts", "/proc", "/sys/fs/cgroup"),
        "/run/containerd/io.containerd.runtime.v2.task/k8s.io/0abc/rootfs",
        "/var/lib/kubelet/pods/0abc/volumes/kubernetes.io~empty-dir/cache",
        "/var/lib/docker/overlay2/0abc/merged",
    ):
        assert mounts.search(left_out), left_out
    for kept in ("/var", "/", "/etc", "/etc/hostsx", "/var/local-path-provisioner"):
        assert not mounts.search(kept), kept
    fs_types = re.compile(args["--collector.filesystem.fs-types-exclude"])
    assert all(fs_types.search(t) for t in ("overlay", "proc", "sysfs", "nsfs"))
    assert not fs_types.search("ext4")


@pytest.mark.integration
@pytest.mark.parametrize(
    ("name", "release", "values"),
    [
        ("kubePrometheusStack", "monitoring", "monitoring-values.yaml"),
        ("victoriaLogs", "logs", "victoria-logs-values.yaml"),
    ],
)
def test_monitoring_pods_meet_the_restricted_level(name: str, release: str, values: str) -> None:
    """The monitoring namespace enforces Pod Security "restricted": a pod below it is refused
    at its next start. Every pod the two charts there render with our values meets it,
    their install hooks' Jobs too. (Prometheus's and Alertmanager's pods are made by the
    operator, not rendered: the cluster's e2e sees those.)"""
    (monitoring,) = [a for a in _render() if a["metadata"]["name"] == "monitoring"]
    assert monitoring["spec"]["syncPolicy"]["managedNamespaceMetadata"]["labels"] == {
        "pod-security.kubernetes.io/enforce": "restricted"
    }
    chart = VALUES["charts"][name]
    out = subprocess.run(
        [
            *("helm", "template", release, f"{chart['repo']}@{chart['digest']}"),
            *("--namespace", "monitoring"),
            *("--values", str(ROOT / "deploy/kind" / values)),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    pods = {
        f"{d['kind']}/{d['metadata']['name']}": d["spec"]
        if d["kind"] == "Pod"
        else d["spec"]["template"]["spec"]
        for d in yaml.safe_load_all(out)
        if d and d["kind"] in {"Pod", "Deployment", "StatefulSet", "DaemonSet", "Job"}
    }
    assert pods
    for owner, pod in pods.items():
        assert not any(pod.get(k) for k in ("hostNetwork", "hostPID", "hostIPC")), owner
        assert all("hostPath" not in v for v in pod.get("volumes") or []), owner
        of_pod = pod.get("securityContext") or {}
        for container in pod["containers"] + (pod.get("initContainers") or []):
            where = f"{owner}, container {container['name']}"
            own = container.get("securityContext") or {}
            assert own.get("allowPrivilegeEscalation") is False, where
            assert own.get("capabilities") == {"drop": ["ALL"]}, where
            assert not own.get("privileged"), where
            assert (of_pod | own).get("runAsNonRoot") is True, where
            assert (of_pod | own).get("runAsUser") != 0, where
            assert (of_pod | own).get("seccompProfile") == {"type": "RuntimeDefault"}, where


@pytest.mark.integration
def test_the_stack_ships_node_rules_and_dashboards_but_no_exporter_of_its_own() -> None:
    """node-exporter comes from its own chart, in its own namespace; the stack still ships
    the rules and dashboards that read it, selecting the job that exporter has, and the
    disk alerts have their entry in the runbook."""
    chart = VALUES["charts"]["kubePrometheusStack"]
    _render()  # skips without helm
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
    docs = [d for d in yaml.safe_load_all(out) if d]
    assert not [d for d in docs if d["kind"] == "DaemonSet"]
    names = {(d["kind"], d["metadata"]["name"]) for d in docs}
    assert {
        ("PrometheusRule", "kps-node-exporter"),
        ("PrometheusRule", "kps-node-exporter.rules"),
        ("PrometheusRule", "kps-node.rules"),
        ("ConfigMap", "kps-nodes"),
    } <= names
    assert ("ConfigMap", "kps-nodes-darwin") not in names
    (rules,) = [d for d in docs if d["metadata"]["name"] == "kps-node-exporter"]
    alerts = {r["alert"]: r["expr"] for g in rules["spec"]["groups"] for r in g["rules"]}
    runbook = (ROOT / "docs/RUNBOOK.md").read_text()
    for alert in ("NodeFilesystemSpaceFillingUp", "NodeFilesystemAlmostOutOfSpace"):
        assert 'job="node-exporter"' in alerts[alert]
        assert f"`{alert}`" in runbook
    assert "NodeClockNotSynchronising" not in alerts  # never true on the Docker VM


def test_grafana_reads_the_logs_from_the_store_that_admits_it() -> None:
    """Grafana's data source, the store's Service and the store's NetworkPolicy are three
    values files' worth of names: a rename on one side must fail here, not in Explore."""
    grafana = yaml.safe_load((ROOT / "deploy/kind/monitoring-values.yaml").read_text())["grafana"]
    store = yaml.safe_load((ROOT / "deploy/kind/victoria-logs-values.yaml").read_text())
    (logs,) = [a for a in _render() if a["metadata"]["name"] == "logs"]
    (rule,) = store["networkPolicy"]["ingress"]
    (port,) = rule["ports"]
    (source,) = grafana["additionalDataSources"]
    service = store["server"]["fullnameOverride"]
    namespace = logs["spec"]["destination"]["namespace"]
    assert source["url"] == f"http://{service}.{namespace}.svc:{port['port']}"
    assert source["access"] == "proxy"  # from Grafana's pod: the policy admits no browser
    # The policy admits Grafana's pods by the subchart's name label, in the store's namespace.
    assert {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "grafana"}}} in rule["from"]
    assert "nameOverride" not in grafana
    (monitoring,) = [a for a in _render() if a["metadata"]["name"] == "monitoring"]
    assert monitoring["spec"]["destination"]["namespace"] == namespace
    # The plugin the data source needs, at a pinned version.
    (plugin,) = grafana["plugins"]
    assert re.fullmatch(rf"{re.escape(source['type'])}@\d+\.\d+\.\d+", plugin)
    # Kept on a claim these values declare, mounted where Grafana installs and loads plugins
    # (the chart's GF_PATHS_PLUGINS): a start with no route to grafana.com still has it.
    values = yaml.safe_load((ROOT / "deploy/kind/monitoring-values.yaml").read_text())
    (volume,) = grafana["extraVolumes"]
    assert grafana["extraVolumeMounts"] == [
        {"name": volume["name"], "mountPath": "/var/lib/grafana/plugins", "readOnly": False}
    ]
    (claim,) = [m for m in values["extraManifests"] if m["kind"] == "PersistentVolumeClaim"]
    assert volume["existingClaim"] == claim["metadata"]["name"]


def test_logs_are_kept_for_a_time_and_a_size_that_fit_the_volume() -> None:
    """The size cap is what keeps the volume from filling: it must stay under the claim."""
    server = yaml.safe_load((ROOT / "deploy/kind/victoria-logs-values.yaml").read_text())["server"]
    assert server["retentionPeriod"] == "30d"
    assert server["persistentVolume"]["size"].endswith("Gi")
    assert server["retentionDiskSpaceUsage"] < int(server["persistentVolume"]["size"][:-2])


def test_the_project_is_kept_from_git() -> None:
    """The child that syncs the AppProject syncs the very file cluster.sh bootstraps with,
    into the namespace the project lives in."""
    (app,) = [a for a in _render() if a["metadata"]["name"] == "platform-project"]
    source = app["spec"]["source"]
    assert (source["path"], source["directory"]["include"]) == (
        "deploy/argocd",
        "platform-project.yaml",
    )
    project = yaml.safe_load((ROOT / "deploy/argocd/platform-project.yaml").read_text())
    assert app["spec"]["destination"]["namespace"] == project["metadata"]["namespace"]
    assert "deploy/argocd/platform-project.yaml" in (ROOT / "scripts/cluster.sh").read_text()


def test_root_application_points_at_the_chart() -> None:
    root = yaml.safe_load((ROOT / "deploy/argocd/platform-root.yaml").read_text())
    assert root["spec"]["project"] == "platform"
    assert root["spec"]["source"]["repoURL"] == REPO
    assert root["spec"]["source"]["path"] == "deploy/argocd/platform"
    assert REPO in PROJECT["sourceRepos"]


def test_argo_cd_alerts_ship_only_with_monitoring() -> None:
    def kinds(*sets: str) -> list[str]:
        args = [a for x in ("arch=amd64", *sets) for a in ("--set", x)]
        out = subprocess.run(
            ["helm", "template", "platform", str(CHART), *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return [d["kind"] for d in yaml.safe_load_all(out) if d]

    _render()  # skips without helm
    assert {"ServiceMonitor", "PrometheusRule"} <= set(kinds())
    # Their CRDs arrive in wave 0: Argo CD's dry run must not fail the root's whole sync.
    out = subprocess.run(
        ["helm", "template", "platform", str(CHART), "--set", "arch=amd64"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for doc in (
        d for d in yaml.safe_load_all(out) if d and d["apiVersion"] == "monitoring.coreos.com/v1"
    ):
        options = doc["metadata"]["annotations"]["argocd.argoproj.io/sync-options"]
        assert "SkipDryRunOnMissingResource=true" in options, doc["kind"]
    assert not {"ServiceMonitor", "PrometheusRule"} & set(kinds("components.monitoring=false"))


def test_kyverno_alerts_ship_only_with_kyverno() -> None:
    """They fire when Kyverno isn't scraped: without the policies they would fire for good.
    And in the root's own namespace: Kyverno's doesn't exist when the root is first synced."""

    def monitors(*sets: str) -> list[tuple[str, str]]:
        args = [a for x in ("arch=amd64", *sets) for a in ("--set", x)]
        out = subprocess.run(
            ["helm", "template", "platform", str(CHART), *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return [
            (d["kind"], d["metadata"]["namespace"])
            for d in yaml.safe_load_all(out)
            if d and d["kind"] != "Application" and d["metadata"]["name"] == "kyverno"
        ]

    _render()  # skips without helm
    assert sorted(monitors()) == [("PrometheusRule", "argocd"), ("ServiceMonitor", "argocd")]
    assert monitors("components.policies=false") == []
    assert monitors("components.monitoring=false") == []


def test_the_operators_monitor_is_the_platforms_not_the_charts() -> None:
    """The CloudNativePG chart renders its PodMonitor whether or not the CRD exists, and the
    operator is installed on every cluster: with the chart's flag on, a cluster without
    monitoring could never sync it. The platform's own goes with the monitoring switch."""
    values = yaml.safe_load((ROOT / "deploy/kind/cloudnative-pg-values.yaml").read_text())
    assert values["monitoring"]["podMonitorEnabled"] is False
    _render()  # skips without helm
    out = subprocess.run(
        ["helm", "template", "platform", str(CHART), "--set", "arch=amd64"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    (monitor,) = [
        d
        for d in yaml.safe_load_all(out)
        if d and d["kind"] == "PodMonitor" and d["metadata"]["name"] == "cloudnative-pg"
    ]
    assert monitor["metadata"]["namespace"] == "argocd"
    assert monitor["spec"]["namespaceSelector"] == {"matchNames": ["cnpg-system"]}


def test_vendored_metrics_server_is_the_pinned_release() -> None:
    folder = ROOT / "deploy/platform/metrics-server"
    want, name = (folder / "SHA256SUMS").read_text().split()
    assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == want


# ------------------------------------------------------------------- real registries
def _registry_digest(repo: str, tag: str) -> str:
    """The digest a registry serves for repo:tag (anonymous pull token, HEAD manifest)."""
    host, path = repo.removeprefix("oci://").split("/", 1)
    token_url = {
        "ghcr.io": f"https://ghcr.io/token?scope=repository:{path}:pull",
        "registry-1.docker.io": "https://auth.docker.io/token?service=registry.docker.io"
        f"&scope=repository:{path}:pull",
        "quay.io": f"https://quay.io/v2/auth?service=quay.io&scope=repository:{path}:pull",
    }[host]
    with urllib.request.urlopen(token_url, timeout=20) as resp:  # noqa: S310 (fixed hosts)
        token = json.load(resp)["token"]
    request = urllib.request.Request(
        f"https://{host}/v2/{path}/manifests/{tag}",
        method="HEAD",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.oci.image.manifest.v1+json",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as resp:  # noqa: S310
        return str(resp.headers["Docker-Content-Digest"])


@pytest.mark.integration
@pytest.mark.parametrize("name", sorted(VALUES["charts"]))
def test_chart_versions_still_resolve_to_their_pinned_digests(name: str) -> None:
    chart = VALUES["charts"][name]
    assert _registry_digest(chart["repo"], chart["version"]) == chart["digest"], (
        f"{name} {chart['version']} no longer resolves to the pinned digest: re-pushed tag?"
    )


def test_waits_read_a_fresh_comparison() -> None:
    """cluster.sh's wait_synced asks Argo CD for a refresh before it trusts Synced/Healthy:
    right after a spec change the status is the previous comparison's (links #108, on the
    laptop: "platform ready" before Argo CD had looked at the new commit)."""
    script = (ROOT / "scripts/cluster.sh").read_text()
    start = script.index("wait_synced() {")
    body = script[start : script.index("\n}\n", start)]
    refresh = body.index("argocd.argoproj.io/refresh=normal")
    assert refresh < body.index("while ((SECONDS - t0 < timeout))")
