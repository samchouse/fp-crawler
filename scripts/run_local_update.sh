#!/usr/bin/env bash
set -euo pipefail

script_path="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/$(basename -- "${BASH_SOURCE[0]}")"
repo_root="$(cd -- "$(dirname -- "$script_path")/.." && pwd)"

if [[ -z "${TMDB_ACCESS_TOKEN:-}" && -z "${TMDB_API_KEY:-}" ]]; then
  echo "Set TMDB_ACCESS_TOKEN or TMDB_API_KEY before running this script." >&2
  exit 2
fi

run_dir="$(mktemp -d "${TMPDIR:-/tmp}/fp-crawler-local.XXXXXX")"
cp -a "$repo_root/catalogs" "$run_dir/catalogs"

echo "Running Canada update in Ubuntu Docker; output and report are under $run_dir"
docker run --rm --init --ipc=host \
  --env TMDB_ACCESS_TOKEN \
  --env TMDB_API_KEY \
  --volume "$repo_root:/workspace:ro" \
  --volume "$run_dir:/run-output" \
  --workdir /workspace \
  ubuntu:24.04 \
  bash -lc '
    set -euo pipefail
    export DEBIAN_FRONTEND=noninteractive

    apt-get update
    apt-get install -y --no-install-recommends \
      ca-certificates libatomic1 python3 python3-pip python3-venv xvfb

    mkdir -p /tmp/fp-crawler
    cp -a /workspace/pyproject.toml /workspace/README.md \
      /workspace/flixpatrol_scraper /workspace/scripts /tmp/fp-crawler/
    cd /tmp/fp-crawler

    python3 -m venv /tmp/fp-venv
    /tmp/fp-venv/bin/pip install --no-cache-dir -e ".[solver]"
    /tmp/fp-venv/bin/scrapling install --force
    /tmp/fp-venv/bin/python -m patchright install --with-deps chromium

    export FLIXPATROL_SOLVE_CF=1
    export FLIXPATROL_SOLVE_HEADED=1
    xvfb-run -a /tmp/fp-venv/bin/python scripts/update_catalogs.py \
      --region "${FP_REGION:-canada}" \
      --force \
      --output /run-output/catalogs \
      --state-dir /run-output/state \
      --report /run-output/report.json \
      "$@"
  ' fp-crawler-local "$@"
