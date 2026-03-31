.PHONY: help install install-dev test test-verbose test-cov lint format clean

help: ## Show this help message
	@echo 'Usage: make [target]'
	@echo ''
	@echo 'Targets:'
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-15s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Install production dependencies
	pip install -r requirements.txt

install-dev: ## Install development dependencies
	pip install -r requirements-dev.txt
	pip install -r requirements.txt

install-playwright: ## Install Playwright browsers
	playwright install

test: ## Run tests
	PYTHONPATH=. pytest tests/

test-verbose: ## Run tests with verbose output
	PYTHONPATH=. pytest tests/ -v

test-cov: ## Run tests with coverage
	PYTHONPATH=. pytest tests/ --cov=sync_octopus_tado --cov-report=html --cov-report=term

lint: ## Run linting (flake8)
	flake8 sync_octopus_tado.py tests/

format: ## Format code with black and isort
	black sync_octopus_tado.py tests/
	isort sync_octopus_tado.py tests/

type-check: ## Run mypy type checking
	mypy sync_octopus_tado.py

check-all: lint type-check test ## Run all checks (lint, type-check, test)

clean: ## Clean up cache files
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name "*.pyc" -delete
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
	find . -type d -name ".mypy_cache" -exec rm -rf {} +

setup: install-dev install-playwright ## Full development setup
