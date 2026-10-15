module gobgp-equal-bench/live

go 1.25.0

require github.com/osrg/gobgp/v4 v4.0.0

// pkg/packet/bgp of the base variant built by scripts/build.sh
replace github.com/osrg/gobgp/v4 => ../work/variants/base
