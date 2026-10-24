{{- define "nico-bmc-target.labels" -}}
app.kubernetes.io/name: nico-bmc-target
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "nico-bmc-target.config" -}}
{{- $clusterUID := required "clusterUID is required" .Values.clusterUID -}}
{{- $cluster := lookup "v1" "Namespace" "" "kube-system" -}}
{{- if and $cluster (ne $cluster.metadata.uid $clusterUID) -}}
{{- fail "clusterUID differs from this cluster's kube-system UID" -}}
{{- end -}}
{{- dict "clusterUID" $clusterUID "owner" (printf "%s/%s" .Release.Namespace .Release.Name)
    "nodeName" (required "nodeName is required" .Values.nodeName)
    "architecture" .Values.architecture "instanceIndex" .Values.target.instanceIndex
    "memoryMiB" .Values.target.memoryMiB "cpus" .Values.target.cpus "diskGiB" .Values.target.diskGiB
    "bridgeAddress" .Values.network.bridgeAddress "bmcPort" .Values.service.targetPort
    "bmcAddress" (required "service.clusterIP is required" .Values.service.clusterIP) | toJson -}}
{{- end -}}
