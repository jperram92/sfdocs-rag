# sfdocs-rag container: runs the test suite by default, or the batch job / MCP server.
#
#   docker build -t sfdocs .
#   docker run --rm sfdocs                                  # full test suite incl. real-browser Playwright tests
#   docker compose run --rm live-test                       # tests against the real Salesforce site
#   docker compose run --rm pipeline                        # the real batch job, data kept in ./data
#
# The default base is Microsoft's Playwright image, which ships Chromium and its system
# libraries. PLAYWRIGHT_VERSION must match the base image tag so the browsers line up.
ARG BASE=mcr.microsoft.com/playwright/python:v1.55.0-noble
FROM ${BASE}
ARG PLAYWRIGHT_VERSION=1.55.0

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    SFDOCS_REQUIRE_BROWSER=1

WORKDIR /app
COPY pyproject.toml ./
COPY sfdocs ./sfdocs
RUN python3 -m pip install --no-cache-dir --break-system-packages -e ".[test]" "playwright==${PLAYWRIGHT_VERSION}"

COPY tests ./tests
COPY config.yaml CLAUDE.md.snippet README.md ./
COPY scripts ./scripts

# Real runs keep their data on a volume; LM Studio on the host is reachable at host.docker.internal.
ENV SFDOCS_DATA_DIR=/data \
    LMSTUDIO_BASE_URL=http://host.docker.internal:1234/v1
VOLUME /data

CMD ["python3", "-m", "pytest", "-v"]
