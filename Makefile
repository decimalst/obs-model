PYTHON ?= .venv/bin/python
GO ?= $(if $(wildcard .cache/tools/go/bin/go),$(CURDIR)/.cache/tools/go/bin/go,go)
KUBECONFORM ?= $(if $(wildcard .cache/tools/kubeconform),$(CURDIR)/.cache/tools/kubeconform,kubeconform)
export GOCACHE := $(CURDIR)/.cache/go-build
export GOMODCACHE := $(CURDIR)/.cache/go-mod
export GO

.PHONY: setup deps render check lint test terraform-init terraform-test runtime-test schemas image
setup:
	python3 -m venv .venv
	$(PYTHON) -m pip install -r requirements-dev.txt
	npm ci --ignore-scripts
deps:
	$(PYTHON) scripts/helm_dependencies.py charts/operators charts/platform charts/flink-operator integrations/cilium
render:
	$(PYTHON) scripts/render.py
lint:
	terraform fmt -recursive -check terraform
	helm lint charts/operators --strict
	helm lint charts/platform --strict
	helm lint charts/debug-grants --strict
	helm lint charts/flink-operator --strict
	$(PYTHON) -m ruff check scripts tests charts/platform/files/support
	$(PYTHON) -m ruff format --check scripts tests charts/platform/files/support
	@for script in scripts/*.sh; do bash -n "$$script"; done
	cd services/log-quota && $(GO) vet ./...
test: render
	$(PYTHON) -m unittest discover -s tests -v
	cd services/log-quota && $(GO) test -race ./...
schemas: render
	$(KUBECONFORM) -strict -summary -kubernetes-version 1.35.0 -ignore-missing-schemas build/platform.yaml build/operators.yaml build/debug-grants.yaml build/flink-operator.yaml
check: studio-test lint test schemas service-test
terraform-init:
	terraform -chdir=terraform/live/lab init -backend=false
	terraform -chdir=terraform/modules/platform-services init -backend=false
	terraform -chdir=terraform/live/existing-cluster init -backend=false
	terraform -chdir=terraform/modules/region init -backend=false
	terraform -chdir=terraform/live/us-east-1 init -backend=false
	terraform -chdir=terraform/live/us-west-2 init -backend=false
terraform-test:
	terraform -chdir=terraform/live/lab validate
	terraform -chdir=terraform/live/lab test
	terraform -chdir=terraform/modules/platform-services validate
	terraform -chdir=terraform/modules/platform-services test
	terraform -chdir=terraform/live/existing-cluster validate
	terraform -chdir=terraform/live/existing-cluster test
	terraform -chdir=terraform/modules/region validate
	terraform -chdir=terraform/modules/region test
	terraform -chdir=terraform/live/us-east-1 validate
	terraform -chdir=terraform/live/us-west-2 validate
runtime-test:
	$(PYTHON) scripts/runtime_test.py
.PHONY: mimir-startup-test
mimir-startup-test:
	$(PYTHON) scripts/mimir_startup_test.py
.PHONY: startup-gate-test
startup-gate-test:
	$(PYTHON) scripts/startup_gate_test.py
image:
	docker build -t observability/log-quota:0.1.0 services/log-quota
.PHONY: integration-test
integration-test:
	$(PYTHON) scripts/integration_test.py
.PHONY: collector-test
collector-test:
	$(PYTHON) scripts/collector_test.py
.PHONY: rules-test
rules-test:
	docker run --rm --network none --entrypoint /bin/promtool -v $(CURDIR)/charts/platform/files/rules:/rules:ro -v $(CURDIR)/tests/rules:/tests:ro prom/prometheus:v3.5.0 check rules /rules/platform.yaml /rules/red.yaml /rules/catalog.yaml /rules/support.yaml /tests/catalog.fixture.yaml
	docker run --rm --network none --entrypoint /bin/promtool -v $(CURDIR)/charts/platform/files/rules:/rules:ro -v $(CURDIR)/tests/rules:/tests:ro prom/prometheus:v3.5.0 test rules /tests/platform.test.yaml /tests/red.test.yaml /tests/catalog.test.yaml /tests/support.test.yaml

.PHONY: sharding-test
sharding-test:
	$(PYTHON) scripts/sharding_test.py

.PHONY: consumer-test
consumer-test:
	$(PYTHON) scripts/consumer_test.py

.PHONY: red-test
red-test:
	$(PYTHON) scripts/red_test.py

.PHONY: dashboard-test
dashboard-test:
	$(PYTHON) scripts/dashboard_test.py

NODE ?= $(if $(wildcard .cache/tools/node/bin/node),$(CURDIR)/.cache/tools/node/bin/node,node)
.PHONY: catalog studio-test profiling-test service-test
catalog:
	$(PYTHON) scripts/service_catalog.py compile
studio-test:
	$(NODE) --check site/app.mjs
	$(NODE) node_modules/prettier/bin/prettier.cjs --check site scripts/*.mjs tests/*.mjs
	$(NODE) --test tests/studio*.test.mjs
	$(PYTHON) -m unittest discover -s tests -p test_catalog.py -v
	$(PYTHON) -m unittest discover -s tests -p test_studio_advanced.py -v
profiling-test:
	$(PYTHON) scripts/profiling_test.py
service-test:
	cd templates/service && $(GO) test -race ./...
	cd templates/service && $(GO) vet ./...

.PHONY: studio-terraform-test
studio-terraform-test: studio-test
	# Studio exports retain the exact provider pin shared with the lab root.
	cp terraform/live/lab/.terraform.lock.hcl build/studio-new/.terraform.lock.hcl
	terraform -chdir=build/studio-new init -backend=false -lockfile=readonly
	terraform -chdir=build/studio-new validate
	cp terraform/live/lab/.terraform.lock.hcl build/studio-iam/.terraform.lock.hcl
	terraform -chdir=build/studio-iam init -backend=false -lockfile=readonly
	terraform -chdir=build/studio-iam validate

.PHONY: support-test
support-test:
	$(PYTHON) scripts/support_test.py
	$(PYTHON) scripts/kafka_metrics_test.py

.PHONY: privacy-graph-test security-test qualification-test
privacy-graph-test:
	$(PYTHON) scripts/privacy_graph_test.py
security-test:
	$(PYTHON) scripts/security_test.py
qualification-test:
	$(PYTHON) -m unittest discover -s tests -p test_qualification.py -v
