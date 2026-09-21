{{- define "olly.labels" -}}
app.kubernetes.io/part-of: observability
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
{{- define "olly.spread" -}}
topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: topology.kubernetes.io/zone
    whenUnsatisfiable: DoNotSchedule
    labelSelector:
      matchLabels:
        app: {{ . }}
  - maxSkew: 1
    topologyKey: kubernetes.io/hostname
    whenUnsatisfiable: DoNotSchedule
    labelSelector:
      matchLabels:
        app: {{ . }}
{{- end }}
{{- define "olly.security" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
fsGroup: 10001
seccompProfile:
  type: RuntimeDefault
{{- end }}

{{/* Shared pipeline controls. Arrow deliberately excludes trace-ID routing. */}}
{{- define "olly.interconnect" -}}
{{- if eq .Values.collectors.interconnect "arrow" }}otelarrow{{ else }}otlp{{ end -}}
{{- end }}
{{- define "olly.exportQueue" -}}
sending_queue:
  queue_size: {{ .Values.collectors.queueSize }}
  num_consumers: {{ .Values.collectors.queueConsumers }}
retry_on_failure:
  enabled: true
  max_elapsed_time: 300s
{{- end }}
{{- define "olly.arrowExport" -}}
{{- if eq .Values.collectors.interconnect "arrow" }}
arrow:
  disable_downgrade: true
  num_streams: {{ .Values.collectors.arrowStreams }}
  max_stream_lifetime: 30s
{{- end }}
{{- end }}
{{- define "olly.batch" -}}
timeout: {{ .Values.collectors.batchTimeout }}
send_batch_size: {{ .Values.collectors.batchSize }}
send_batch_max_size: {{ .Values.collectors.batchMaxSize }}
{{- end }}
{{/* Use Mimir's drop policy on OTLP attributes too, before export normalization.
     Never remove identifying labels from a series without aggregation. */}}
{{- define "olly.metricFilter" -}}
error_mode: propagate
metric_conditions:
  {{- range .Values.mimir.mimir.structuredConfig.limits.metric_relabel_configs }}
  {{- range .source_labels }}
  - 'datapoint.attributes[{{ . | toJson }}] != nil'
  {{- if contains "_" . }}
  - 'datapoint.attributes[{{ replace "_" "." . | toJson }}] != nil'
  {{- end }}
  {{- end }}
  {{- end }}
  - 'Len(datapoint.attributes) > {{ .Values.mimir.mimir.structuredConfig.limits.max_label_names_per_series }}'
  {{- if .Values.collectors.requireMetricIdentity }}
  - 'resource.attributes["service.instance.id"] == nil and resource.attributes["k8s.pod.uid"] == nil and resource.attributes["k8s.node.name"] == nil'
  {{- end }}
{{- end }}

{{- define "olly.interconnectQueue" -}}
{{- if eq .Values.collectors.interconnect "arrow" }}
sending_queue:
  sizer: items
  queue_size: {{ .Values.collectors.arrowQueueItems | int }}
  num_consumers: {{ .Values.collectors.queueConsumers | int }}
  block_on_overflow: false
  batch:
    flush_timeout: {{ .Values.collectors.batchTimeout }}
    min_size: {{ .Values.collectors.batchSize | int }}
    max_size: {{ .Values.collectors.batchMaxSize | int }}
retry_on_failure:
  enabled: true
  max_elapsed_time: 300s
{{- else }}
{{- include "olly.exportQueue" . }}
{{- end }}
{{- end }}
