import copy
import subprocess
import unittest
from pathlib import Path

import yaml

from scripts.platform_config import load_platform, validate_platform
from scripts.preflight import validate_network_provider
from scripts.regional_values import values
from scripts.tuning import validate_tuning

ROOT = Path(__file__).resolve().parents[1]


class ProfilingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.objects = {
            (d["kind"], d["metadata"]["name"]): d
            for d in yaml.safe_load_all(
                subprocess.check_output(
                    [
                        "helm",
                        "template",
                        "olly",
                        "charts/platform",
                        "-n",
                        "observability",
                        "--set",
                        "profiling.enabled=true",
                        "--set",
                        "hubble.enabled=true",
                    ],
                    text=True,
                )
            )
            if d
        }

    def test_enabled_components_have_az_separation_and_persistent_quorum(self):
        for name in ["metastore", "segment-writer"]:
            obj = self.objects["StatefulSet", "pyroscope-" + name]
            self.assertEqual(obj["spec"]["replicas"], 3)
            self.assertEqual(
                obj["spec"]["volumeClaimTemplates"][0]["spec"]["storageClassName"], "gp3-encrypted"
            )
            self.assertEqual(obj["spec"]["template"]["spec"]["serviceAccountName"], "pyroscope")
        meta = self.objects["StatefulSet", "pyroscope-metastore"]["spec"]["template"]["spec"]
        self.assertEqual(
            meta["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][
                0
            ]["topologyKey"],
            "topology.kubernetes.io/zone",
        )
        self.assertNotIn(("Rollout", "pyroscope-metastore"), self.objects)

    def test_profile_selection_limits_and_native_v2_retention(self):
        config = self.objects["ConfigMap", "alloy-profiles"]["data"]["config.alloy"]
        self.assertIn("spec.nodeName=", config)
        self.assertIn("observability/profile=true", config)
        self.assertIn("sample_rate = 19", config)
        self.assertIn("max_backoff_retries = 3", config)
        self.assertIn("http://pyroscope-distributor:4040", config)
        pyro = yaml.safe_load(self.objects["ConfigMap", "pyroscope-config"]["data"]["config.yaml"])
        self.assertEqual(pyro["limits"]["retention_period"], "168h")
        self.assertNotIn("compactor_blocks_retention_period", pyro["limits"])
        self.assertNotIn("max_global_series_per_tenant", pyro["limits"])
        ds = self.objects["DaemonSet", "alloy-profiles"]["spec"]["template"]["spec"]
        self.assertTrue(ds["hostPID"])
        self.assertTrue(ds["containers"][0]["securityContext"]["privileged"])
        roles = [
            d
            for (kind, _), d in self.objects.items()
            if kind == "Role" and d["metadata"]["name"] == "olly-profile-discovery"
        ]
        self.assertEqual(roles[0]["metadata"]["namespace"], "applications")

    def test_hubble_scrape_is_explicit_and_bounded(self):
        jobs = self.objects["OpenTelemetryCollector", "otel-scrape"]["spec"]["config"]["receivers"][
            "prometheus"
        ]["config"]["scrape_configs"]
        job = next(j for j in jobs if j["job_name"] == "hubble")
        self.assertEqual(job["sample_limit"], 5000)
        self.assertEqual(job["metric_relabel_configs"][0]["action"], "keep")
        self.assertNotIn("http", job["metric_relabel_configs"][0]["regex"])

    def test_schema_and_contract_reject_invalid_settings(self):
        for tuning in [
            {"profiling": {"sampleRate": 0}},
            {"profiling": {"enabled": True}},
            {"profiling": {"namespaces": ["*"]}},
            {
                "pyroscope": {
                    "pyroscope": {"structuredConfig": {"limits": {"retention_period": "0h"}}}
                }
            },
        ]:
            with self.assertRaises(ValueError):
                validate_tuning(tuning)
        p = load_platform(ROOT / "examples/existing-platform.yaml")
        p["profiling"] = {"enabled": True}
        with self.assertRaises(ValueError):
            validate_platform(p)
        p["buckets"]["pyroscope"] = "example-regional-profiles"
        p["role_arns"]["pyroscope"] = p["role_arns"]["tempo"]
        p = validate_platform(p)
        v = values(p, "registry/quota@sha256:" + "a" * 64)
        self.assertEqual(
            v["pyroscope"]["pyroscope"]["structuredConfig"]["storage"]["s3"]["region"], p["region"]
        )
        p["tuning"] = {"hubble": {"enabled": True}}
        with self.assertRaises(ValueError):
            values(p, "registry/quota@sha256:" + "a" * 64)

    def test_cilium_preflight_fails_closed_without_migrating(self):
        cilium = {
            "metadata": {"name": "cilium"},
            "status": {"desiredNumberScheduled": 6, "numberReady": 6},
        }
        config = {
            "data": {
                "enable-policy": "default",
                "enable-hubble": "true",
                "hubble-metrics-server": ":9965",
            }
        }
        calls = []

        def kube(*args):
            calls.append(args)
            return config

        p = {"network_policy_provider": "cilium", "tuning": {"hubble": {"enabled": True}}}
        validate_network_provider(p, [cilium], kube)
        self.assertEqual(calls, [("configmap", "cilium-config", "-n", "kube-system")])
        bad = copy.deepcopy(cilium)
        bad["status"]["numberReady"] = 5
        with self.assertRaises(ValueError):
            validate_network_provider(p, [bad], kube)
        aws = {
            "metadata": {"name": "aws-node"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {"name": "aws-eks-nodeagent", "args": ["--enable-network-policy=true"]}
                        ]
                    }
                }
            },
        }
        with self.assertRaises(ValueError):
            validate_network_provider(p, [cilium, aws], kube)
        aws["spec"]["template"]["spec"]["containers"][0]["args"] = ["--enable-network-policy=false"]
        validate_network_provider(p, [cilium, aws], kube)
        with self.assertRaises(ValueError):
            validate_network_provider({"network_policy_provider": "aws"}, [aws], kube)
        config["data"]["enable-policy"] = "never"
        with self.assertRaises(ValueError):
            validate_network_provider(p, [cilium], kube)
