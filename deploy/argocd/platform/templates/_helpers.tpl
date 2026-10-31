{{/*
Everything a platform child Application shares. `wave` orders the root's sync: Argo CD
applies a wave, waits until its Applications are Healthy (argocd-cm's Application health
check, deploy/kind/argocd-values.yaml), then starts the next.
  wave -1: External Secrets, so the Secrets wave 0's charts read can be made from git;
           cert-manager, so the Certificates wave 0's Barman Cloud plugin makes can be issued
  wave 0: the charts and the CRDs they ship; the secret store; also what needs only a fast chart's CRDs
          (the Gateway, the tool routes: the sync retry covers the few seconds' race)
  wave 1: what needs a slow chart running (the Kyverno policies: Kyverno's webhook; the
          alerts on Argo CD: kube-prometheus-stack's CRDs)
On a fresh cluster the waves overlap: Argo CD waits only for children that report
Progressing, and a child that has never synced reports Missing, so the next wave's children
are created at once (CI, links #113). What orders them then is the sync retry below.
*/}}
{{- define "platform.meta" -}}
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: {{ .name }}
  namespace: argocd
  labels:
    app.kubernetes.io/part-of: platform
  annotations:
    argocd.argoproj.io/sync-wave: {{ .wave | quote }}
    # Diff against what the API server would store, not against the raw manifest: API
    # defaults and fields other controllers own (webhook caBundles, aggregated ClusterRole
    # rules) are not drift.
    argocd.argoproj.io/compare-options: ServerSideDiff=true
  # No resources-finalizer, on purpose: deleting a child Application (or the root) orphans
  # its resources instead of deleting the monitoring stack or the admission webhook. Remove
  # a component by deleting its resources, then its Application.
{{- end }}

{{/*
Automated sync with pruning and self-heal, like the app's environments. Server-side apply:
several of these charts ship CRDs too large for client-side apply's last-applied
annotation (kube-prometheus-stack, Kyverno, Envoy Gateway). `labels` become the
namespace's labels (Pod Security levels), set by Argo CD when it creates the namespace.
*/}}
{{- define "platform.syncPolicy" -}}
syncPolicy:
  automated: { prune: true, selfHeal: true }
  syncOptions:
    - CreateNamespace=true
    - ServerSideApply=true
  {{- with .labels }}
  managedNamespaceMetadata:
    labels:
      {{- toYaml . | nindent 6 }}
  {{- end }}
  # A webhook that isn't serving yet (Kyverno's, External Secrets') or a CRD that doesn't
  # exist yet refuses the first apply: retry instead of failing the sync. Often, and capped
  # at 30 s: a child that depends on another (secret-store on External Secrets' webhook,
  # backups on the store) starts at most 30 s after it could, not up to 2 minutes, and a
  # chain of them adds up (CI, links #113). 20 tries cover about 9 minutes.
  retry:
    limit: 20
    backoff: { duration: 5s, factor: 2, maxDuration: 30s }
{{- end }}

{{/* A chart from an OCI registry, pinned by digest, with its values file from git;
`extra`: one more file from git synced with it (the ExternalSecret it reads). */}}
{{- define "platform.chartSources" -}}
sources:
  - repoURL: {{ .chart.repo }}
    targetRevision: {{ .chart.digest }} # {{ .chart.version }}
    path: .
    helm:
      releaseName: {{ .release }} # the name scripts/cluster.sh's helm install used
      valueFiles:
        - $values/{{ .values }}
      {{- with .valuesObject }}
      valuesObject:
        {{- toYaml . | nindent 8 }}
      {{- end }}
  - repoURL: {{ .root.Values.repoURL }}
    targetRevision: {{ .root.Values.revision }}
    ref: values
  {{- with .extra }}
  - repoURL: {{ $.root.Values.repoURL }}
    targetRevision: {{ $.root.Values.revision }}
    path: {{ dir . }}
    directory: { include: {{ base . }} }
  {{- end }}
{{- end }}
