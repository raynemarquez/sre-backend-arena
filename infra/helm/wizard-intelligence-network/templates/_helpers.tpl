{{/*
Expand the name of the chart.
*/}}
{{- define "wizard-intelligence-network.name" -}}
{{- default .Chart.Name .Values.nameOverride -}}
{{- end -}}

{{/*
Create a default fully qualified app name.
We truncate this at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec ).
If release name contains .chart (e.g. wizard-intelligence-network.chart), we trim it.
*/}}
{{- define "wizard-intelligence-network.fullname" -}}
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

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "wizard-intelligence-network.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels
*/}}
{{- define "wizard-intelligence-network.labels" -}}
helm.sh/chart: {{ include "wizard-intelligence-network.chart" . }}
{{ include "wizard-intelligence-network.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Selector labels
*/}}
{{- define "wizard-intelligence-network.selectorLabels" -}}
app.kubernetes.io/name: {{ include "wizard-intelligence-network.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Create the name of the service account to use
*/}}
{{- define "wizard-intelligence-network.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
    {{- default (include "wizard-intelligence-network.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
    {{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}
