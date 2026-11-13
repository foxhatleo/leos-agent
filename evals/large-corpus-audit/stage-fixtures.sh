#!/bin/bash
# claude plugin eval runs this as `bash <script>` in the run's empty
# workspace (with --scaffold). Generate this case's corpus in, read-only.
# generate.py is deterministic (fixed seed, standard library only), so every
# run reads the same files and nothing large is committed.
set -euo pipefail
python3 "$(dirname "$0")/generate.py" corpus
find corpus -type f -exec chmod a-w {} +
