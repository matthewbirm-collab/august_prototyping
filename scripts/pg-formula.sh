#!/usr/bin/env bash
# Print the Postgres formula (e.g. postgresql@18) the Homebrew TimescaleDB build targets.
set -euo pipefail
brew info --json=v2 timescale/tap/timescaledb | python3 -c '
import json, re, sys
formula = json.load(sys.stdin)["formulae"][0]
deps = formula["dependencies"] + formula["build_dependencies"]
print(next((d for d in deps if re.fullmatch(r"postgresql@\d+", d)), ""))
'
