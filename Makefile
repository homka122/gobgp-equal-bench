# make image          build the container (clones gobgp, builds all variants)
# make docker-quick   few-minutes run in the container, results in results/quick
# make docker-all     full run in the container, results in results/full
#
# Inside the container (or locally after scripts/build.sh):
# make quick | make all

RUN_ARGS ?=
PY = cd harness && uv run -q python

.PHONY: build quick all micro live plots image docker-quick docker-all

build:
	scripts/build.sh

quick: OUT = $(CURDIR)/results/quick
quick:
	$(PY) micro.py --count 3 --benchtime 200ms --out $(OUT)
	$(PY) run.py --n 2000 --rounds 4 --reps 2 --out $(OUT) $(RUN_ARGS)
	$(PY) plots.py --results $(OUT)

all: OUT = $(CURDIR)/results/full
all:
	$(PY) micro.py --count 10 --out $(OUT)
	$(PY) run.py --n 10000 --rounds 10 --reps 5 --out $(OUT) $(RUN_ARGS)
	$(PY) plots.py --results $(OUT)

image:
	docker build -f docker/Dockerfile -t gobgp-equal-bench .

docker-quick docker-all: docker-%: image
	docker run --rm -v $(CURDIR)/results:/bench/results gobgp-equal-bench $* RUN_ARGS="--sink-addr 127.0.0.2"
