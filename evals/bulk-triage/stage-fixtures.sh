#!/bin/bash
# claude plugin eval runs this as `bash <script>` in the run's empty
# workspace (with --scaffold). Copy this case's fixtures in, read-only.
set -euo pipefail
cp -R "$(dirname "$0")/tickets" .
find tickets -type f -exec chmod a-w {} +
