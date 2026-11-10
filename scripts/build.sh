#!/usr/bin/env bash
# Builds blaster and sink into work/bin.
set -euo pipefail
cd "$(dirname "$0")/../live"
go build -trimpath -buildvcs=false -o ../work/bin/ ./cmd/blaster ./cmd/sink
