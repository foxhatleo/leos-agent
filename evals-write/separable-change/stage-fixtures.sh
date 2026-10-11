#!/bin/bash
# claude plugin eval runs this as `bash <script>` in the run's empty
# workspace (with --scaffold). Copy this case's fixtures in, writable, except
# the acceptance tests, which the run must not change.
set -euo pipefail
cp -R "$(dirname "$0")/shop" "$(dirname "$0")/tests" .
chmod a-w tests/test_contract.py
