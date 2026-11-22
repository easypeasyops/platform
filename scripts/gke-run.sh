#!/usr/bin/env bash
# The weekly GKE run's steps around Terraform (PT-120, .github/workflows/gke-e2e.yml), on
# the current kube context and gcloud project:
#
#   scripts/gke-run.sh check                # kind e2e's checks on every app namespace
#   scripts/gke-run.sh drain                # what Kubernetes made in GCP, deleted and gone
#   scripts/gke-run.sh leftovers <run_id> <since>   # what the run left in GCP; non-zero if any
#   STATE_BUCKET=<b> scripts/gke-run.sh reap [--dry-run]   # every run's, older than 3 hours
#
# check: each namespace the apps ApplicationSet labelled, through the front door's load
# balancer (the Gateway's address), with scripts/lib/e2e-checks.sh, the checks kind's e2e
# runs. A failed check prints that namespace's state and fails.
#
# drain: Terraform knows nothing of what controllers inside the cluster made in GCP (the
# front door's load balancer, the PersistentVolumes' disks), and destroying the cluster
# first strands them. So, before `terraform destroy`: Argo CD stops (nothing re-creates
# what is deleted), the Gateways and every LoadBalancer Service go, then each namespace
# with a PVC and each app namespace, and it waits until no LoadBalancer Service and no
# PersistentVolume is left. Both carry a finalizer that GKE removes only once the load
# balancer or the disk is deleted in GCP, so their absence is GCP's.
#
# leftovers: after `terraform destroy`, what still exists in GCP: the run's own resources by
# their label run-id=<run_id> (clusters, disks, addresses, forwarding rules, buckets), and
# what Kubernetes made since <since> (RFC 3339), which carries no label: forwarding rules
# and disks whose description names a Kubernetes object. The project holds nothing but
# runs (DESIGN.md, The GKE run), one at a time (the workflow's concurrency group).
#
# reap (.github/workflows/gke-reaper.yml): what a run that never reached its teardown left,
# by gcloud alone: anything older than REAP_AFTER_HOURS (3; a run's job times out after 2)
# that carries a run-id label, that Kubernetes made, or that is named platform-<run>. In
# dependency order: clusters, load balancers (forwarding rules, target pools, backend
# services), addresses, disks, secrets, buckets, then the VPC (firewall rules, routers,
# subnets, the network), and last the runs' Terraform state in STATE_BUCKET. It prints each
# deletion, and exits 1 when it deleted anything, so the run is red and its email says so.
# --dry-run lists what it would delete and exits 0.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# e2e_checks, e2e_diagnostics, pass, die
# shellcheck source=scripts/lib/e2e-checks.sh
source "$ROOT/scripts/lib/e2e-checks.sh"

APP_LABEL=easypeasyops.github.io/app # apps/applicationset.yaml's namespace label
DRAIN_TIMEOUT=900                    # seconds; a load balancer goes in about a minute

CHECKING="" # the namespace being checked, whose state a failed check prints
diagnostics() {
  [[ $? -ne 0 && -n "$CHECKING" ]] || return 0
  e2e_diagnostics "$CHECKING" "$(kubectl get ns "$CHECKING" -o json | jq -r --arg l "$APP_LABEL" '.metadata.labels[$l]')"
}

check() {
  local ip namespaces
  ip=$(kubectl -n gateway get gateway front-door -o jsonpath='{.status.addresses[0].value}')
  [[ -n "$ip" ]] || die "the front door's Gateway has no address"
  namespaces=$(kubectl get ns -l "$APP_LABEL" -o name | sed 's|^namespace/||')
  [[ -n "$namespaces" ]] || die "no app namespace (label $APP_LABEL): did install run with ARGOCD_APPS=0?"
  trap diagnostics EXIT
  for CHECKING in $namespaces; do
    echo "== $CHECKING, through the front door at $ip"
    e2e_checks "$CHECKING" "http://$ip"
  done
  CHECKING=""
  pass "every app namespace: $(echo "$namespaces" | xargs)"
}

drain() {
  local lbs namespaces left
  kubectl -n argocd scale statefulset/argocd-application-controller deployment/argocd-applicationset-controller --replicas=0
  kubectl delete gateways.gateway.networking.k8s.io -A --all --wait=false
  lbs=$(kubectl get svc -A -o json | jq -r '.items[] | select(.spec.type == "LoadBalancer") | "\(.metadata.namespace) \(.metadata.name)"')
  while read -r ns name; do
    [[ -z "$ns" ]] || kubectl -n "$ns" delete svc "$name" --wait=false
  done <<<"$lbs"
  namespaces=$({
    kubectl get pvc -A -o jsonpath='{range .items[*]}{.metadata.namespace}{"\n"}{end}'
    kubectl get ns -l "$APP_LABEL" -o name | sed 's|^namespace/||'
  } | sort -u | xargs)
  # shellcheck disable=SC2086 # one namespace per word
  [[ -z "$namespaces" ]] || kubectl delete ns $namespaces --wait=false
  for ((i = 0; i < DRAIN_TIMEOUT; i += 10)); do
    left=$({
      kubectl get svc -A -o json | jq -r '.items[] | select(.spec.type == "LoadBalancer") | "service/\(.metadata.namespace)/\(.metadata.name)"'
      kubectl get pv -o name
    } | xargs)
    if [[ -z "$left" ]]; then
      pass "drained: no LoadBalancer Service, no PersistentVolume (namespaces deleted: ${namespaces:-none})"
      return 0
    fi
    sleep 10
  done
  kubectl get svc -A --field-selector spec.type=LoadBalancer || true
  kubectl get pv,pvc -A || true
  die "after ${DRAIN_TIMEOUT}s still in the cluster, so still in GCP: $left"
}

leftovers() {
  [[ $# -eq 2 ]] || die "usage: $0 leftovers <run_id> <since, RFC 3339>"
  local run=$1 since=$2 label made found=""
  label="labels.run-id=$run"
  made="description~'kubernetes.io/|networking.gke.io/' AND creationTimestamp>='$since'"
  found+=$(gcloud container clusters list --filter="resourceLabels.run-id=$run" --format='value(name)' | sed 's/^/cluster /')
  found+=$'\n'$(gcloud compute disks list --filter="$label OR ($made)" --format='value(name)' | sed 's/^/disk /')
  found+=$'\n'$(gcloud compute addresses list --filter="$label" --format='value(name)' | sed 's/^/address /')
  found+=$'\n'$(gcloud compute forwarding-rules list --filter="$label OR ($made)" --format='value(name)' | sed 's/^/forwarding-rule /')
  found+=$'\n'$(gcloud storage buckets list --filter="$label" --format='value(name)' | sed 's/^/bucket /')
  found=$(echo "$found" | sed '/^$/d')
  if [[ -n "$found" ]]; then
    echo "$found"
    die "run $run left $(echo "$found" | wc -l | tr -d ' ') resources in GCP (RUNBOOK, The weekly GKE run: clean up by hand)"
  fi
  pass "run $run left nothing: no cluster, disk, address, forwarding rule or bucket"
}

REAPED=()
DRY=0
# reap_each <kind> <delete command...>: delete each ID (a name or a URI) read on stdin. Fed
# a here-string of a list already made (ids=$(list)), not a pipe, so REAPED is this
# shell's and a refused list fails the reap rather than reading as empty.
reap_each() {
  local kind=$1 id
  shift
  while read -r id; do
    [[ -n "$id" ]] || continue
    echo "reap $kind $id"
    REAPED+=("$kind $id")
    if ((DRY == 0)); then "$@" "$id" --quiet </dev/null; fi
  done
}

reap() {
  [[ "${1:-}" != --dry-run ]] || DRY=1
  : "${STATE_BUCKET:?set STATE_BUCKET, the state bucket: <project_id>-tfstate}"
  local cutoff old ours kind name where ids
  cutoff=$(REAP_AFTER_HOURS="${REAP_AFTER_HOURS:-3}" python3 -c 'import datetime as d, os
print((d.datetime.now(d.timezone.utc) - d.timedelta(hours=float(os.environ["REAP_AFTER_HOURS"]))).strftime("%Y-%m-%dT%H:%M:%SZ"))')
  echo "reaping what runs made before $cutoff"
  old="creationTimestamp<'$cutoff'"
  ours="labels.run-id:* OR name~^platform- OR description~'kubernetes.io/|networking.gke.io/'"

  ids=$(gcloud container clusters list --filter="resourceLabels.run-id:* AND createTime<'$cutoff'" --format='value(name,location)')
  while read -r name where; do
    [[ -n "$name" ]] || continue
    echo "reap cluster $name ($where)"
    REAPED+=("cluster $name")
    if ((DRY == 0)); then gcloud container clusters delete "$name" --location "$where" --quiet </dev/null; fi
  done <<<"$ids"
  for kind in forwarding-rules target-pools backend-services addresses disks; do
    ids=$(gcloud compute "$kind" list --filter="$old AND ($ours)" --uri)
    reap_each "$kind" gcloud compute "$kind" delete <<<"$ids"
  done
  ids=$(gcloud secrets list --filter="labels.run-id:* AND createTime<'$cutoff'" --format='value(name)')
  reap_each secret gcloud secrets delete <<<"$ids"
  ids=$(gcloud storage buckets list --filter="labels.run-id:* AND creation_time<'$cutoff'" --format='value(name)' | sed 's|^|gs://|')
  reap_each bucket gcloud storage rm -r --all-versions <<<"$ids"
  # The VPC last, in its order: what refers to the network goes before it.
  ids=$(gcloud compute firewall-rules list --filter="$old AND network~/networks/platform-" --uri)
  reap_each firewall-rule gcloud compute firewall-rules delete <<<"$ids"
  ids=$(gcloud compute routers list --filter="$old AND name~^platform-" --uri)
  reap_each router gcloud compute routers delete <<<"$ids"
  ids=$(gcloud compute networks subnets list --filter="$old AND name~^platform-" --uri)
  reap_each subnet gcloud compute networks subnets delete <<<"$ids"
  ids=$(gcloud compute networks list --filter="$old AND name~^platform-" --uri)
  reap_each network gcloud compute networks delete <<<"$ids"
  ids=$(gcloud storage objects list "gs://$STATE_BUCKET" --filter="name~^gke/ AND update_time<'$cutoff'" --format='value(name)' | sed "s|^|gs://$STATE_BUCKET/|")
  reap_each state gcloud storage rm <<<"$ids"

  if ((${#REAPED[@]} == 0)); then
    pass "nothing older than $cutoff left by a run"
  elif ((DRY)); then
    echo "dry run: would reap ${#REAPED[@]}, deleted nothing"
  else
    die "reaped ${#REAPED[@]} resources a run left (RUNBOOK, The weekly GKE run: which run, and why its teardown did not)"
  fi
}

case "${1:-}" in
  check) check ;;
  drain) drain ;;
  leftovers) shift; leftovers "$@" ;;
  reap) shift; reap "$@" ;;
  *) die "usage: $0 check | drain | leftovers <run_id> <since> | reap [--dry-run]" ;;
esac
