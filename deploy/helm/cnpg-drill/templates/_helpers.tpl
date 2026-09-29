{{- define "cnpg-drill.name" -}}
{{- printf "%s-cnpg-drill" .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
