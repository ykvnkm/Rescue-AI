{{- define "obs.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: rescue-ai
app.kubernetes.io/contour: {{ .Values.contour | quote }}
{{- end -}}

{{- define "obs.prometheus.name" -}}
{{ .Release.Name }}-prometheus
{{- end -}}

{{- define "obs.alertmanager.name" -}}
{{ .Release.Name }}-alertmanager
{{- end -}}

{{- define "obs.grafana.name" -}}
{{ .Release.Name }}-grafana
{{- end -}}
