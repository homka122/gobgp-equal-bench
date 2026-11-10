# The benchmark in a container: the Go toolchain and the Python harness. The
# gobgpd binary is mounted at /gobgpd; blaster and sink are built when it
# runs, /cache keeps the Go caches between the runs.
FROM golang:1.27-bookworm

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends python3 util-linux && rm -rf /var/lib/apt/lists/*

WORKDIR /bench
ENV UV_PYTHON_DOWNLOADS=never UV_PYTHON=python3 GOCACHE=/cache/go-build GOMODCACHE=/cache/mod
COPY harness/pyproject.toml harness/uv.lock harness/
RUN cd harness && uv sync --frozen
COPY scripts scripts
COPY live live
COPY harness harness
COPY bench ./
ENTRYPOINT ["./bench"]
