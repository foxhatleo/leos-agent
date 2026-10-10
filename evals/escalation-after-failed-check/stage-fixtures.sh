#!/bin/bash
# claude plugin eval runs this as `bash <script>` in the run's empty
# workspace (with --scaffold). Copy this case's fixtures in, read-only.
set -euo pipefail
cp -R "$(dirname "$0")/postmortems" .
find postmortems -type f -exec chmod a-w {} +
