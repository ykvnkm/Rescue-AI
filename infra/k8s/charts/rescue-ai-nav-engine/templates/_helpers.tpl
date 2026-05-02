{{- define "rescue-ai-nav-engine.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "rescue-ai-nav-engine.fullname" -}}
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

{{- define "rescue-ai-nav-engine.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "rescue-ai-nav-engine.labels" -}}
helm.sh/chart: {{ include "rescue-ai-nav-engine.chart" . }}
{{ include "rescue-ai-nav-engine.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: rescue-ai
app.kubernetes.io/component: nav-engine
{{- end -}}

{{- define "rescue-ai-nav-engine.selectorLabels" -}}
app.kubernetes.io/name: {{ include "rescue-ai-nav-engine.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
