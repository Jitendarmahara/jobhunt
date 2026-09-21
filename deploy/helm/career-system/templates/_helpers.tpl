{{- define "career-system.name" -}}
career-system
{{- end -}}
{{- define "career-system.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "career-system.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
