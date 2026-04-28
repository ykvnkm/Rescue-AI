{{- define "rescue-ai-sync-worker.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "rescue-ai-sync-worker.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "rescue-ai-sync-worker.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "rescue-ai-sync-worker.labels" -}}
helm.sh/chart: {{ include "rescue-ai-sync-worker.chart" . }}
{{ include "rescue-ai-sync-worker.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: rescue-ai
app.kubernetes.io/component: sync-worker
{{- end -}}

{{- define "rescue-ai-sync-worker.selectorLabels" -}}
app.kubernetes.io/name: {{ include "rescue-ai-sync-worker.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "rescue-ai-sync-worker.kubeSecretName" -}}
{{- if .Values.secrets.kube.existingSecret -}}
{{- .Values.secrets.kube.existingSecret -}}
{{- else -}}
{{- printf "%s-secret" (include "rescue-ai-sync-worker.fullname" .) -}}
{{- end -}}
{{- end -}}
