#!/usr/bin/env bash
# Builds blaster, sink and flapper into work/bin.
set -euo pipefail
cd "$(dirname "$0")/../live"
go build -trimpath -buildvcs=false -o ../work/bin/ ./cmd/blaster ./cmd/sink ./cmd/flapper
