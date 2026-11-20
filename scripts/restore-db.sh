#!/usr/bin/env bash
# Restore an app's database from its backups into a fresh Cluster, never in place (PT-102).
#
#   scripts/restore-db.sh <app> <env>                        # the latest moment in the archive
#   scripts/restore-db.sh <app> <env> --target-time "2026-11-18 21:30:00-08"   # as it was then
#   scripts/restore-db.sh <app> <env> --keep                 # leave the copy to look at
#   scripts/restore-db.sh <app> <env> --check-sql "SELECT count(*) FROM links"   # print it
#   scripts/restore-db.sh <app> <env> --from s3://cnpg-backups/<ns>-lost-<yyyymmdd>   # moved aside
#   scripts/restore-db.sh --drill <namespace>/<cluster>      # the daily drill, in the cluster
#
# The source is the service chart's database (`database.cluster`): the Cluster `<app>-db` in
# namespace `<app>-<env>` (SOURCE_NAMESPACE overrides it), and its ObjectStore. In namespace
# `restore-drill` (the one the bucket's NetworkPolicy lets restores in) it creates a Cluster
# `<app>-db-restore` bootstrapped by `recovery` from that ObjectStore: the operator fetches
# the newest base backup before the target and replays the archived WAL after it. The copy
# archives nowhere and the source is only read. Then it checks the copy: healthy, and
# without --target-time the same tables as the source. It prints how long it took.
#
# --from reads the backups from another folder of the bucket instead of the ObjectStore's
# own: a lost database's, moved aside so its recreated Cluster can archive (docs/RUNBOOK.md,
# The backup bucket, A lost database). The folder holds `<app>-db/`, as the source's does;
# the tables aren't compared, since the live source no longer holds that data.
#
# --drill is what the daily restore drill runs, inside the cluster as the ServiceAccount
# restore-drill/restore-drill (deploy/restore-drill/, PT-44): the source is named by its
# namespace and Cluster, the copy is `drill-<namespace>`, and the bucket's keys are the
# Secret `drill-backup` already there. It only reads the source's Cluster and ObjectStore:
# no Secret is copied, the tables aren't compared, and it doesn't create the namespace.
#
# Copying the restored data into the live database is a separate, deliberate step:
# docs/RUNBOOK.md, Restore an app database.
set -euo pipefail

die() { echo "restore-db: $*" >&2; exit 1; }
usage() { sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }

DRILL=0
if [[ ${1:-} == --drill ]]; then
  [[ ${2:-} == */* ]] || usage
  DRILL=1 SOURCE=${2%/*} CLUSTER=${2#*/}
  APP=${CLUSTER%-db}
  shift 2
else
  [[ $# -ge 2 ]] || usage
  APP=$1 ENV=$2
  SOURCE=${SOURCE_NAMESPACE:-$APP-$ENV} CLUSTER=$APP-db
  shift 2
fi
TARGET_TIME="" KEEP=0 CHECK_SQL="" FROM=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --target-time) TARGET_TIME=${2:?--target-time needs a time}; shift 2 ;;
    --keep) KEEP=1; shift ;;
    --check-sql) CHECK_SQL=${2:?--check-sql needs a query}; shift 2 ;;
    --from) FROM=${2:?--from needs an s3:// folder}; shift 2 ;;
    *) usage ;;
  esac
done

NS=restore-drill
COPY=$CLUSTER-restore KEYS=$CLUSTER-restore-backup
[[ $DRILL == 1 ]] && COPY=drill-$SOURCE KEYS=drill-backup
PLUGIN=barman-cloud.cloudnative-pg.io

kubectl -n "$SOURCE" get cluster "$CLUSTER" >/dev/null 2>&1 \
  || die "no Cluster $CLUSTER in $SOURCE (database.cluster.enabled in the app's values)"
kubectl -n "$SOURCE" get objectstore "$CLUSTER" >/dev/null 2>&1 \
  || die "no ObjectStore $CLUSTER in $SOURCE: its backups are off (database.cluster.backup.enabled)"

jp() { kubectl -n "$SOURCE" get "$1" "$2" -o jsonpath="$3"; }
db=$(jp cluster "$CLUSTER" '{.spec.bootstrap.initdb.database}')
owner=$(jp cluster "$CLUSTER" '{.spec.bootstrap.initdb.owner}')
db=${db:-$APP} owner=${owner:-$APP}
image=$(jp cluster "$CLUSTER" '{.spec.imageName}')
dest=$(jp objectstore "$CLUSTER" '{.spec.configuration.destinationPath}')
endpoint=$(jp objectstore "$CLUSTER" '{.spec.configuration.endpointURL}')

primary() { kubectl -n "$1" get cluster "$2" -o jsonpath='{.status.currentPrimary}'; }
sql() { kubectl -n "$1" exec "$(primary "$1" "$2")" -c postgres -- psql -d "$db" -tAc "$3"; }
tables="SELECT schemaname || '.' || tablename FROM pg_tables
        WHERE schemaname NOT IN ('pg_catalog', 'information_schema') ORDER BY 1"

remove_copy() {
  kubectl -n "$NS" delete cluster "$COPY" --ignore-not-found --wait=true --timeout=120s >/dev/null
  kubectl -n "$NS" delete objectstore "$COPY-source" --ignore-not-found >/dev/null
  [[ $DRILL == 1 ]] || kubectl -n "$NS" delete secret "$KEYS" --ignore-not-found >/dev/null
}
cleanup() {
  if [[ $KEEP == 1 ]]; then
    echo "kept:  kubectl -n $NS exec -it $COPY-1 -c postgres -- psql -d $db"
    echo "then:  kubectl -n $NS delete cluster/$COPY objectstore/$COPY-source secret/$KEYS"
  else
    remove_copy
  fi
}

# A clean slate: an earlier copy of the same database goes first. The drill's namespace is
# Argo CD's (deploy/restore-drill/); by hand, it may not be there yet.
if [[ $DRILL == 0 ]]; then
  kubectl get namespace "$NS" >/dev/null 2>&1 || kubectl create namespace "$NS" >/dev/null
  kubectl label namespace "$NS" pod-security.kubernetes.io/enforce=restricted --overwrite >/dev/null
fi
remove_copy
trap cleanup EXIT

# The bucket's keys. The drill's are there already (Secret drill-backup, from the
# backup-bucket store); by hand they're copied from the source's (the chart's
# ExternalSecret), piped, never printed or put in an argument.
if [[ $DRILL == 0 ]]; then
  kubectl -n "$SOURCE" get secret "$CLUSTER-backup" -o json \
    | python3 -c '
import json, sys
s = json.load(sys.stdin)
print(json.dumps({"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                  "metadata": {"name": sys.argv[1]}, "data": s["data"]}))' "$KEYS" \
    | kubectl -n "$NS" apply -f - >/dev/null
fi
# The source's bucket and folder (unless --from), read with those keys; the server name
# stays the source's Cluster.
dest=${FROM:-$dest}
dest=${dest%/}

start=$SECONDS
kubectl -n "$NS" apply -f - >/dev/null <<YAML
apiVersion: barmancloud.cnpg.io/v1
kind: ObjectStore
metadata: { name: $COPY-source }
spec:
  configuration:
    destinationPath: $dest
    endpointURL: $endpoint
    s3Credentials:
      accessKeyId: { name: $KEYS, key: ACCESS_KEY_ID }
      secretAccessKey: { name: $KEYS, key: ACCESS_SECRET_KEY }
---
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata: { name: $COPY }
spec:
  instances: 1
  imageName: $image
  storage: { size: 1Gi }
  resources:
    requests: { cpu: 10m, memory: 128Mi }
    limits: { memory: 512Mi }
  enablePDB: false
  bootstrap:
    recovery:
      source: origin
      database: $db
      owner: $owner
$( [[ -n $TARGET_TIME ]] && printf '      recoveryTarget: { targetTime: "%s" }\n' "$TARGET_TIME" )
  externalClusters:
    - name: origin
      plugin:
        name: $PLUGIN
        parameters:
          barmanObjectName: $COPY-source
          serverName: $CLUSTER
YAML

phase=""
for _ in $(seq 1 150); do
  phase=$(kubectl -n "$NS" get cluster "$COPY" -o jsonpath='{.status.phase}' 2>/dev/null || true)
  [[ $phase == "Cluster in healthy state" ]] && break
  sleep 2
done
if [[ $phase != "Cluster in healthy state" ]]; then
  kubectl -n "$NS" get pods -o wide || true
  kubectl -n "$NS" logs -l cnpg.io/cluster="$COPY" --all-containers --tail=30 2>/dev/null || true
  die "the copy never became healthy (phase '$phase'; logs above)"
fi
took=$((SECONDS - start))

got=$(sql "$NS" "$COPY" "$tables")
if [[ -z $TARGET_TIME && -z $FROM && $DRILL == 0 ]]; then
  want=$(sql "$SOURCE" "$CLUSTER" "$tables")
  [[ $got == "$want" ]] || die "the copy's tables differ from the source's:
$(diff <(echo "$want") <(echo "$got") || true)"
fi
if [[ -n $CHECK_SQL ]]; then
  echo "check: $(sql "$NS" "$COPY" "$CHECK_SQL" | paste -sd ' ' -)"
fi
echo "ok:    $SOURCE/$CLUSTER restored${FROM:+ from $FROM}${TARGET_TIME:+ to $TARGET_TIME} into $NS/$COPY in ${took}s, $(grep -c . <<<"$got") tables"
