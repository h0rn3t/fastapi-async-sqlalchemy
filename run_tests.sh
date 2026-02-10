#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${YELLOW}=== Running all tests ===${NC}"
echo ""

# Run pytest with coverage
.venv/bin/python -m pytest tests/ \
    -v \
    --tb=short \
    --cov=fastapi_async_sqlalchemy \
    --cov-report=term-missing \
    --cov-report=html:htmlcov \
    -x \
    "$@"

EXIT_CODE=$?

echo ""
if [ $EXIT_CODE -eq 0 ]; then
    echo -e "${GREEN}=== All tests passed ===${NC}"
else
    echo -e "${RED}=== Tests failed (exit code: $EXIT_CODE) ===${NC}"
fi

exit $EXIT_CODE
