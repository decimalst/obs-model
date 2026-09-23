{{- define "olly.graphProcessors" -}}
filter/servicegraph:
  error_mode: propagate
  traces:
    span:
      - 'kind != SPAN_KIND_CLIENT and kind != SPAN_KIND_SERVER'
      # An explicit set bounds the connector's otherwise unbounded series map.
      - {{ printf "not ContainsValue(%s, resource.attributes[\"service.name\"])" (toJson .Values.serviceGraph.services) | quote }}
transform/servicegraph:
  error_mode: propagate
  trace_statements:
    - context: span
      statements:
        # Mutate only the independent graph branch. Disable database/peer-name
        # inference and dimensions so payload-controlled names cannot grow series.
        - 'keep_keys(resource.attributes, ["service.name"])'
        - 'keep_keys(attributes, [])'
resource/servicegraph:
  attributes:
    - {key: service.name, value: olly-servicegraph, action: upsert}
    - {key: service.instance.id, value: '${env:K8S_POD_UID}', action: upsert}
{{- end }}

{{- define "olly.graphConnector" -}}
servicegraph:
  store:
    ttl: {{ .Values.serviceGraph.storeTTLSeconds }}s
    max_items: {{ .Values.serviceGraph.storeMaxItems }}
  metrics_flush_interval: {{ .Values.serviceGraph.flushIntervalSeconds }}s
  cache_loop: {{ .Values.serviceGraph.cacheLoopSeconds }}s
  store_expiration_loop: 1s
  dimensions: []
  virtual_node_peer_attributes: []
  database_name_attributes: [olly.disabled.database]
  latency_histogram_buckets:
    {{- range .Values.serviceGraph.histogramBucketsSeconds }}
    - {{ printf "%vs" . | quote }}
    {{- end }}
{{- end }}
