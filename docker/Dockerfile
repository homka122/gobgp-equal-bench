# Everything in one container: the gobgpd variants, blaster, sink and the
# Python harness. Results are written to /bench/results (mount it).
FROM golang:1.27-bookworm

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends python3 util-linux && rm -rf /var/lib/apt/lists/*

WORKDIR /bench
COPY versions.env ./
COPY scripts scripts
COPY live live
RUN scripts/build.sh && cd work/variants/base && go mod download

ENV UV_PYTHON_DOWNLOADS=never UV_PYTHON=python3
COPY harness/pyproject.toml harness/uv.lock harness/
RUN cd harness && uv sync --frozen
COPY micro micro
COPY harness harness
COPY Makefile ./
ENTRYPOINT ["make"]
CMD ["all"]
