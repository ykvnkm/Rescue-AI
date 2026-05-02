{{- define "rescue-ai-detection.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "rescue-ai-detection.fullname" -}}
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

{{- define "rescue-ai-detection.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "rescue-ai-detection.labels" -}}
helm.sh/chart: {{ include "rescue-ai-detection.chart" . }}
{{ include "rescue-ai-detection.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: rescue-ai
app.kubernetes.io/component: detection
{{- end -}}

{{- define "rescue-ai-detection.selectorLabels" -}}
app.kubernetes.io/name: {{ include "rescue-ai-detection.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
