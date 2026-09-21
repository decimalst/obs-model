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
deps:
	helm dependency build charts/operators
	helm dependency build charts/platform
	helm dependency build charts/flink-operator
render:
	$(PYTHON) scripts/render.py
lint:
	terraform fmt -recursive -check terraform
	helm lint charts/operators --strict
	helm lint charts/platform --strict
	helm lint charts/debug-grants --strict
	helm lint charts/flink-operator --strict
	$(PYTHON) -m ruff check scripts tests
	$(PYTHON) -m ruff format --check scripts tests
	@for script in scripts/*.sh; do bash -n "$$script"; done
	cd services/log-quota && $(GO) vet ./...
test: render
	$(PYTHON) -m unittest discover -s tests -v
	cd services/log-quota && $(GO) test -race ./...
schemas: render
	$(KUBECONFORM) -strict -summary -kubernetes-version 1.35.0 -ignore-missing-schemas build/platform.yaml build/operators.yaml build/debug-grants.yaml build/flink-operator.yaml
check: lint test schemas
terraform-init:
	terraform -chdir=terraform/modules/platform-services init -backend=false
	terraform -chdir=terraform/live/existing-cluster init -backend=false
	terraform -chdir=terraform/modules/region init -backend=false
	terraform -chdir=terraform/live/us-east-1 init -backend=false
	terraform -chdir=terraform/live/us-west-2 init -backend=false
terraform-test:
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
image:
	docker build -t observability/log-quota:0.1.0 services/log-quota
.PHONY: integration-test
integration-test:
	$(PYTHON) scripts/integration_test.py
