# InfoGrep — convenience wrapper. Run `make` (or `make help`) to list targets.
.DEFAULT_GOAL := help
.PHONY: help install uninstall purge sync app test lint shellcheck

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Install InfoGrep (app + login agents + MCP) — see ./install.sh
	./install.sh

uninstall: ## Remove the app, login agents and MCP (keeps indexes)
	./uninstall.sh

purge: ## Uninstall AND delete all indexes (~/.infogrep)
	./uninstall.sh --purge

sync: ## Create/refresh the dev virtualenv (uv sync --extra dev)
	uv sync --extra dev

app: ## Build the macOS menu-bar app (macos/InfoGrep.app)
	cd macos && ./build.sh

test: ## Run the test suite
	uv run pytest

lint: ## Lint Python (ruff) and shell scripts (shellcheck if installed)
	uv run ruff check .
	@command -v shellcheck >/dev/null 2>&1 \
	  && shellcheck install.sh uninstall.sh macos/build.sh \
	  || echo "shellcheck not installed — skipping (brew install shellcheck)"

shellcheck: ## Lint just the shell scripts
	shellcheck install.sh uninstall.sh macos/build.sh
