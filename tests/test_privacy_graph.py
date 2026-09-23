import unittest

import yaml

from scripts.tuning import validate_tuning
from tests import test_pipeline_controls


class PrivacyGraph(unittest.TestCase):
    def resources(self, tuning=None):
        return test_pipeline_controls.PipelineControls().resources(tuning)

    def config(self, docs, name):
        return yaml.safe_load(next(iter(docs["ConfigMap", name]["data"].values())))

    def test_off_is_non_mutating_and_graph_requires_names(self):
        docs = self.resources()
        agent = self.config(docs, "otel-agent")
        self.assertNotIn("filter/privacy", agent["processors"])
        self.assertNotIn("privacy", self.config(docs, "vector-agent")["transforms"])
        sampler = self.config(docs, "otel-traces")
        self.assertNotIn("connectors", sampler)
        bad = {"serviceGraph": {"enabled": True}}
        with self.assertRaises(ValueError):
            validate_tuning(bad)
        self.assertNotEqual(test_pipeline_controls.PipelineControls().render(bad).returncode, 0)

    def test_graph_is_trace_affine_bounded_and_uses_independent_writers(self):
        for transport in ["arrow", "otlp"]:
            docs = self.resources(
                {
                    "serviceGraph": {"enabled": True, "services": ["api", "db"]},
                    "collectors": {"interconnect": transport},
                }
            )
            sampler = self.config(docs, "otel-traces")
            pipelines = sampler["service"]["pipelines"]
            self.assertNotIn("tail_sampling", pipelines["traces/servicegraph"]["processors"])
            self.assertNotIn("transform/servicegraph", pipelines["traces"]["processors"])
            connector = sampler["connectors"]["servicegraph"]
            self.assertEqual(connector["store"], {"ttl": "30s", "max_items": 10000})
            self.assertEqual(connector["virtual_node_peer_attributes"], [])
            self.assertEqual(connector["dimensions"], [])
            self.assertIn("${env:K8S_POD_UID}", str(sampler["processors"]["resource/servicegraph"]))
            self.assertIn("filter/cardinality", pipelines["metrics/servicegraph"]["processors"])
            expected = "otelarrow/graph" if transport == "arrow" else "otlp/graph"
            self.assertEqual(pipelines["metrics/servicegraph"]["exporters"], [expected])
            self.assertEqual(
                self.config(docs, "otel-router")["exporters"]["loadbalancing"]["routing_key"],
                "traceID",
            )
            env = docs["StatefulSet", "otel-traces"]["spec"]["template"]["spec"]["containers"][0][
                "env"
            ]
            self.assertIn(
                {"name": "K8S_POD_UID", "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}},
                env,
            )

    def test_privacy_runs_before_durable_buffers_and_sampling(self):
        docs = self.resources(
            {
                "privacy": {"logs": {"mode": "drop"}, "traces": {"mode": "allowlist"}},
                "serviceGraph": {"enabled": True, "services": ["api"]},
            }
        )
        for name, sink in [("vector-agent", "quota"), ("vector-gateway", "kafka")]:
            config = self.config(docs, name)
            self.assertTrue(config["transforms"]["privacy"]["drop_on_error"])
            self.assertTrue(config["transforms"]["privacy"]["drop_on_abort"])
            final_input = "cleanup" if name == "vector-agent" else "privacy"
            self.assertEqual(config["sinks"][sink]["inputs"], [final_input])
        for name in ["otel-agent", "otel-router", "otel-traces"]:
            config = self.config(docs, name)
            for signal, pipeline in config["service"]["pipelines"].items():
                if signal.startswith("traces"):
                    processors = pipeline["processors"]
                    self.assertIn("filter/privacy", processors)
                    self.assertIn("transform/privacy", processors)
                    if "tail_sampling" in processors:
                        self.assertLess(
                            processors.index("transform/privacy"), processors.index("tail_sampling")
                        )
            self.assertEqual(
                config["processors"]["filter/privacy"]["traces"]["spanevent"], ["true"]
            )
            self.assertEqual(config["processors"]["filter/privacy"]["error_mode"], "propagate")

    def test_invalid_dimensions_names_or_unsafe_identity_removal_rejected(self):
        for bad in [
            {"serviceGraph": {"services": ["api"] * 65}},
            {"serviceGraph": {"services": ["api", "api"]}},
            {"serviceGraph": {"services": ["private@example.org"]}},
            {"serviceGraph": {"histogramBucketsSeconds": [1, 0.5]}},
            {"serviceGraph": {"storeMaxItems": 0}},
            {"serviceGraph": {"dimensions": ["user.id"]}},
            {"privacy": {"logs": {"mode": "accept-on-error"}}},
            {"privacy": {"logs": {"patterns": ["custom-regex"]}}},
            {"privacy": {"traces": {"mode": "allowlist", "allowedResourceAttributes": []}}},
        ]:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    validate_tuning(bad)
                self.assertNotEqual(
                    test_pipeline_controls.PipelineControls().render(bad).returncode, 0
                )
