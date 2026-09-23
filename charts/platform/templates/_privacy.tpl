{{/* Fixed, reviewed patterns avoid evaluating user-provided VRL. */}}
{{- define "olly.logPrivacy" -}}
# The normalized envelope is the only payload allowed onto the durable log path.
. = filter(object!(.)) -> |key, _value| {
  includes(["cluster_name", "event_id", "timestamp", "service_key", "service_name", "namespace", "level", "message", "trace_id", "debug_allowed"], key)
}
message = string!(.message)
{{ range .Values.privacy.logs.patterns -}}
{{- if eq . "email" }}
pattern = r'(?i)[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}'
{{- else if eq . "bearer" }}
pattern = r'(?i)bearer\s+[a-z0-9._~+/=-]+'
{{- else }}
pattern = r'(?i)"?(password|passwd|api[_-]?key|access[_-]?token|secret)"?\s*[=:]\s*("[^"]*"|[^\s,;]+)'
{{- end }}
{{ if eq $.Values.privacy.logs.mode "drop" -}}
if match(message, pattern) { abort }
{{ else -}}
message = replace(message, pattern, "[REDACTED]")
{{ end -}}
{{ end -}}
.message = message
{{- end }}

{{- define "olly.tracePrivacy" -}}
filter/privacy:
  error_mode: propagate
  traces:
    # Pinned OTTL cannot clear links from a literal empty array. Dropping linked
    # spans avoids silently leaking link attributes; this is explicitly opt-in.
    span: ['Len(links) > 0']
    spanevent: ['true']
transform/privacy:
  error_mode: propagate
  trace_statements:
    - context: span
      statements:
        - {{ printf "keep_keys(resource.attributes, %s)" (toJson .Values.privacy.traces.allowedResourceAttributes) | quote }}
        - {{ printf "keep_keys(attributes, %s)" (toJson .Values.privacy.traces.allowedSpanAttributes) | quote }}
        - 'keep_keys(instrumentation_scope.attributes, [])'
        - 'set(instrumentation_scope.name, "redacted")'
        - 'set(instrumentation_scope.version, "")'
        {{- if not .Values.privacy.traces.keepSpanName }}
        - 'set(name, "[REDACTED]")'
        {{- end }}
        - 'set(status.message, "")'
        - 'set(trace_state, "")'
{{- end }}
