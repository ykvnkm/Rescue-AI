{{/* Common chart helpers (k8s standard pattern). */}}

{{- define "rescue-ai-api.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "rescue-ai-api.fullname" -}}
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

{{- define "rescue-ai-api.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "rescue-ai-api.labels" -}}
helm.sh/chart: {{ include "rescue-ai-api.chart" . }}
{{ include "rescue-ai-api.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: rescue-ai
{{- end -}}

{{- define "rescue-ai-api.selectorLabels" -}}
app.kubernetes.io/name: {{ include "rescue-ai-api.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "rescue-ai-api.serviceAccountName" -}}
{{ include "rescue-ai-api.fullname" . }}
{{- end -}}

{{/*
Резолвер секрет-источника. Возвращает имя Secret, на который ссылается
envFrom. Если source=kube и existingSecret пустой — используем чартовый.
Если source=vault — секреты не монтируются как Secret (агент пишет
их в файлы), и шаблон должен пропустить envFrom.
*/}}
{{- define "rescue-ai-api.kubeSecretName" -}}
{{- if .Values.secrets.kube.existingSecret -}}
{{- .Values.secrets.kube.existingSecret -}}
{{- else -}}
{{- printf "%s-secret" (include "rescue-ai-api.fullname" .) -}}
{{- end -}}
{{- end -}}
