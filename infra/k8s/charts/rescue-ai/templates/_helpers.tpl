{{- define "rescue-ai.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "rescue-ai.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: rescue-ai
{{- end -}}

{{- define "rescue-ai.localPostgresql.name" -}}
{{ include "rescue-ai.fullname" . }}-postgresql
{{- end -}}

{{- define "rescue-ai.localMinio.name" -}}
{{ include "rescue-ai.fullname" . }}-minio
{{- end -}}
