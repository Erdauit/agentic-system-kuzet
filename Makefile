# Qoima Vision — команды разработчика. CI вызывает те же цели (см. .github/workflows/ci.yml).

PACKAGES := -p shared -p cli -p core_api -p vision_pipeline -p agents -p workers

.PHONY: help sync lint format typecheck test test-integration e2e license-gate schemas \
        ui-install ui-build ui-typecheck compose-config ci clean

help:
	@echo "sync              — установить окружение разработки (uv sync)"
	@echo "lint / format     — ruff check / ruff format"
	@echo "typecheck         — mypy (strict для shared/)"
	@echo "test              — юнит и контрактные тесты"
	@echo "test-integration  — интеграционные тесты (нужны QOIMA_TEST_DATABASE_URL, QOIMA_TEST_VALKEY_URL)"
	@echo "e2e               — приёмка Фазы 1 на запущенной системе с симулятором камер"
	@echo "license-gate      — лицензионный гейт Python + npm"
	@echo "schemas           — обновить снапшоты JSON Schema контрактов"
	@echo "ui-build          — собрать UI"
	@echo "compose-config    — проверить все комбинации compose-профилей"
	@echo "ci                — всё, что проверяет CI"

sync:
	uv sync --frozen

lint:
	uv run ruff check src tests tools
	uv run ruff format --check src tests tools

format:
	uv run ruff format src tests
	uv run ruff check --fix src tests

typecheck:
	uv run mypy $(PACKAGES)

test:
	uv run pytest tests/unit tests/contract

test-integration:
	uv run pytest tests/integration -m integration

# Приёмка Фазы 1: нужна запущенная система и симулятор (qoima up, qoima sim up --cameras 4).
e2e:
	QOIMA_E2E_URL=$${QOIMA_E2E_URL:-https://localhost:8443} uv run pytest tests/e2e -m e2e

license-gate:
	uv run qoima-license-gate python
	uv run qoima-license-gate npm ui/package-lock.json

schemas:
	uv run python -m shared.schemas.export tests/contract/snapshots

ui-install:
	cd ui && npm ci --no-audit --no-fund

ui-typecheck:
	cd ui && npm run typecheck

ui-build:
	cd ui && npm run build

# Проверка синтаксиса и интерполяции compose во всех профилях (без запуска контейнеров).
compose-config:
	@set -e; export QOIMA_DATA_ROOT=/tmp/qoima-data QOIMA_SECRETS_DIR=/nonexistent; \
	for extra in "" "-f compose/compose.gpu1.yml" "-f compose/compose.gpu2.yml" \
	             "-f compose/compose.gpu1.yml -f compose/compose.engineering.yml"; do \
	  echo "compose.yml $$extra"; \
	  docker compose -f compose/compose.yml $$extra config --quiet; \
	done

ci: lint typecheck test license-gate ui-typecheck compose-config

clean:
	rm -rf .mypy_cache .ruff_cache .pytest_cache ui/dist
