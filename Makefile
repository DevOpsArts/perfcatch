.PHONY: build install dev test lint clean docker-build docker-push deploy undeploy helm-install helm-upgrade helm-uninstall

NAMESPACE ?= perfcatch
DOCKER_REPO ?= devopsart1/perfcatch
IMAGE_TAG ?= latest
IMAGE ?= $(DOCKER_REPO):$(IMAGE_TAG)

install:
	pip install -e .

dev:
	pip install -e ".[dev]"

test:
	pytest --cov=perfcatch tests/

lint:
	ruff check src/ tests/
	ruff format --check src/ tests/

format:
	ruff format src/ tests/

docker-build:
	docker build -t $(IMAGE) -f deploy/Dockerfile .

docker-push: docker-build
	docker push $(IMAGE)

helm-install:
	helm install perfcatch charts/perfcatch \
		--namespace $(NAMESPACE) --create-namespace \
		--set image.repository=$(DOCKER_REPO) \
		--set image.tag=$(IMAGE_TAG)

helm-upgrade:
	helm upgrade perfcatch charts/perfcatch \
		--namespace $(NAMESPACE) \
		--set image.repository=$(DOCKER_REPO) \
		--set image.tag=$(IMAGE_TAG)

helm-uninstall:
	helm uninstall perfcatch --namespace $(NAMESPACE)

deploy:
	kubectl apply -f deploy/namespace.yaml
	kubectl apply -f deploy/rbac.yaml
	kubectl apply -f deploy/configmap.yaml
	kubectl apply -f deploy/daemonset.yaml

undeploy:
	kubectl delete -f deploy/daemonset.yaml --ignore-not-found
	kubectl delete -f deploy/configmap.yaml --ignore-not-found
	kubectl delete -f deploy/rbac.yaml --ignore-not-found
	kubectl delete -f deploy/namespace.yaml --ignore-not-found

clean:
	rm -rf build/ dist/ *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
