import json
import unittest

import yaml

from scripts.tuning import validate_tuning
from tests import test_pipeline_controls


class Red(unittest.TestCase):
    def resources(self, tuning=None):
        return test_pipeline_controls.PipelineControls().resources(tuning)

    def agent(self, resources):
        return yaml.safe_load(resources["ConfigMap", "otel-agent"]["data"]["collector.yaml"])

    def test_red_branches_before_sampling_and_preserves_original_traces(self):
        d = self.resources()
        agent = self.agent(d)
        pipelines = agent["service"]["pipelines"]
        self.assertEqual(pipelines["traces/red"]["receivers"], ["otlp"])
        self.assertEqual(pipelines["traces/red"]["exporters"], ["spanmetrics/red"])
        self.assertNotIn("tail_sampling", pipelines["traces/red"]["processors"])
        self.assertNotIn("transform/red", pipelines["traces"]["processors"])
        self.assertEqual(pipelines["traces"]["exporters"], ["otelarrow/traces"])
        self.assertEqual(pipelines["metrics/red"]["exporters"], ["otelarrow/metrics"])
        self.assertIn("filter/cardinality", pipelines["metrics/red"]["processors"])
        env = d["DaemonSet", "otel-agent"]["spec"]["template"]["spec"]["containers"][0]["env"]
        uid = next(e for e in env if e["name"] == "K8S_POD_UID")
        self.assertEqual(uid["valueFrom"]["fieldRef"]["fieldPath"], "metadata.uid")
        transform = str(agent["processors"]["transform/red"])
        self.assertIn("${env:K8S_POD_UID}", transform)
        self.assertIn('set(trace_state, "")', transform)

    def test_defaults_are_bounded_and_optional_routes_do_not_enable_raw_span_names(self):
        connector = self.agent(self.resources())["connectors"]["spanmetrics/red"]
        self.assertEqual(connector["exclude_dimensions"], ["span.name"])
        self.assertEqual(connector["dimensions"], [{"name": "service.namespace"}])
        self.assertEqual(connector["aggregation_cardinality_limit"], 64)
        self.assertEqual(connector["resource_metrics_cache_size"], 128)
        self.assertEqual(connector["histogram"]["unit"], "s")
        self.assertFalse(connector["exemplars"]["enabled"])
        self.assertFalse(connector["events"]["enabled"])
        d = self.resources(
            {
                "spanMetrics": {
                    "includeHttpRoute": True,
                    "spanKinds": ["SERVER", "CONSUMER"],
                    "histogramBucketsSeconds": [0.01, 0.3, 1.2],
                    "aggregationCardinalityLimit": 12,
                }
            }
        )
        agent = self.agent(d)
        connector = agent["connectors"]["spanmetrics/red"]
        self.assertEqual(connector["aggregation_cardinality_limit"], 12)
        self.assertEqual(connector["histogram"]["explicit"]["buckets"], ["0.01s", "0.3s", "1.2s"])
        self.assertIn({"name": "http.route", "default": "unknown"}, connector["dimensions"])
        self.assertEqual(
            agent["processors"]["filter/red"]["traces"]["span"][0],
            "kind != SPAN_KIND_SERVER and kind != SPAN_KIND_CONSUMER",
        )

    def test_disable_removes_pipeline_dashboard_and_active_rules(self):
        d = self.resources(
            {"spanMetrics": {"enabled": False}, "collectors": {"interconnect": "otlp"}}
        )
        agent = self.agent(d)
        self.assertNotIn("connectors", agent)
        self.assertNotIn("traces/red", agent["service"]["pipelines"])
        self.assertNotIn("metrics/red", agent["service"]["pipelines"])
        self.assertNotIn("service-red.json", d["ConfigMap", "olly-dashboards"]["data"])
        self.assertEqual(
            yaml.safe_load(d["ConfigMap", "olly-rules"]["data"]["red.yaml"]), {"groups": []}
        )
        # Keep the projected file even when disabled so the ruler volume still mounts.
        items = next(
            v
            for v in d["Deployment", "mimir-ruler"]["spec"]["template"]["spec"]["volumes"]
            if v["name"] == "git-rules"
        )["configMap"]["items"]
        self.assertIn({"key": "red.yaml", "path": "platform/red.yaml"}, items)

    def test_dashboard_and_rules_are_provisioned_and_aggregate_writers_before_quantiles(self):
        d = self.resources()
        dashboard = json.loads(d["ConfigMap", "olly-dashboards"]["data"]["service-red.json"])
        self.assertEqual(dashboard["uid"], "service-red")
        self.assertEqual(
            {v["name"] for v in dashboard["templating"]["list"]},
            {"region", "cluster", "namespace", "service", "kind"},
        )
        expressions = [
            t["expr"] for p in dashboard["panels"] for t in p.get("targets", []) if "expr" in t
        ]
        self.assertEqual(sum("histogram_quantile" in expr for expr in expressions), 3)
        self.assertTrue(
            all("sum by" in expr for expr in expressions if "histogram_quantile" in expr)
        )
        rules = yaml.safe_load(d["ConfigMap", "olly-rules"]["data"]["red.yaml"])["groups"][0][
            "rules"
        ]
        self.assertTrue(
            all("instance" not in r["expr"].split("(rate(")[0] for r in rules if "record" in r)
        )
        self.assertTrue(any(r.get("alert") == "SpanMetricsCardinalityOverflow" for r in rules))

    def test_bad_tuning_is_rejected_by_chart_and_installer(self):
        for bad in [
            {"aggregationCardinalityLimit": 0},
            {"resourceMetricsCacheSize": 0},
            {"spanKinds": ["CLIENT"]},
            {"histogramBucketsSeconds": [1, 0.1]},
            {"histogramBucketsSeconds": [0.1, 0.1]},
            {"histogramBucketsSeconds": [0, 1]},
            {"flushIntervalSeconds": 60, "expirationSeconds": 30},
        ]:
            with self.subTest(bad=bad):
                tuning = {"spanMetrics": bad}
                with self.assertRaises(ValueError):
                    validate_tuning(tuning)
                self.assertNotEqual(
                    test_pipeline_controls.PipelineControls().render(tuning).returncode, 0
                )
        with self.assertRaises(ValueError):
            validate_tuning({"spanMetrics": {"dimensions": ["user.id"]}})
