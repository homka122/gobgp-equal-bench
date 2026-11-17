// blaster is the sender: the upstream peer of the gobgpd under test. It
// builds every UPDATE itself, so it can announce a route again with the same
// content in another encoding, which gobgpd as a peer never does.
//
// It reads commands from stdin, one per line:
//
//	load        announce all routes once
//	storm R     announce all routes R more times, as -resend says
//	mark K      announce the marker route 198.18.K.0/24
//	probe R S   announce R probe routes per second for S seconds: each is
//	            a /32 of its own in 198.19.0.0/16 and carries the time it
//	            was sent (withStamp)
//	eor         send the End-of-RIB marker
//	down        close the TCP connection without a NOTIFICATION
//	up          connect again as a restarting speaker and announce all
//	            routes as the load did
//	quit
//
// and prints one JSON line on stdout when a command has been sent.
//
// With -gr-restart-time the OPEN carries the Graceful Restart Capability,
// with -llgr-stale-time also the Long-Lived Graceful Restart Capability, both
// with the N bit and for the family of the routes. After up the R bit and the
// F bits are set: the speaker has restarted and kept its forwarding state.
package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"math/rand/v2"
	"net/netip"
	"os"
	"slices"
	"strings"
	"time"

	"github.com/osrg/gobgp/v4/pkg/packet/bgp"

	"gobgp-equal-bench/live/speaker"
)

const (
	localAS = 100
	label   = 100
)

var (
	family    = flag.String("family", "ipv4", "ipv4 or vpnv4")
	routes    = flag.Int("routes", 10000, "number of routes")
	perUpdate = flag.Int("per-update", 1, "routes per UPDATE")
	local     = flag.String("local", "127.0.0.1", "local address")
	remote    = flag.String("remote", "127.0.0.1:10179", "gobgpd address")
	standard  = flag.Int("standard", 0, "COMMUNITIES values per route")
	extended  = flag.Int("extended", 0, "route-target extended communities per route")
	large     = flag.Int("large", 0, "large communities per route")
	order     = flag.String("order", "sorted", "order of the community values: sorted, random or reversed")
	seed      = flag.Uint64("seed", 1, "seed of the random order")
	unique    = flag.Bool("unique", false, "give every UPDATE a large community of its own")
	groups    = flag.Int("groups", 0, "put every route into one of that many groups at random, once for the run: "+
		"the routes of a group share a large community of their own, and each time the routes are announced it is one "+
		"by one in a new random order; 0: no groups")
	resend   = flag.String("resend", "same", "what a storm round sends: "+strings.Join(resends, ", "))
	hold     = flag.Int("hold", 90, "Hold Time to offer, s")
	grTime   = flag.Int("gr-restart-time", 0, "Restart Time of the Graceful Restart Capability, s; 0: no capability")
	llgrTime = flag.Int("llgr-stale-time", 0, "Long-Lived Stale Time, s; 0: no Long-Lived Graceful Restart Capability")
)

func main() {
	flag.Parse()
	speaker.HoldTime = uint16(*hold)
	set, err := newCommunitySet(*standard, *extended, *large, *order, *seed)
	if err != nil {
		log.Fatal(err)
	}
	if err := set.supports(*resend); err != nil {
		log.Fatal(err)
	}
	fam := bgp.RF_IPv4_UC
	if *family == "vpnv4" {
		fam = bgp.RF_IPv4_VPN
	}
	s := dial(fam, false)
	nlris := make([]bgp.PathNLRI, *routes)
	for i := range *routes {
		// 10.x.y.0/24, then 11.x.y.0/24 after 65536 routes
		nlris[i] = nlri(fam, netip.PrefixFrom(netip.AddrFrom4([4]byte{byte(10 + i>>16), byte(i >> 8), byte(i), 0}), 24))
	}
	if *groups > 0 {
		if *unique || *perUpdate != 1 {
			log.Fatal("-groups needs -per-update 1 and no -unique")
		}
		rnd = rand.New(rand.NewPCG(*seed, *seed^0x5bd1e995))
		group = make([]int, *routes)
		for i := range group {
			group[i] = rnd.IntN(*groups)
		}
	}

	out := json.NewEncoder(os.Stdout)
	out.Encode(map[string]any{"ready": true})
	round := 0
	in := bufio.NewScanner(os.Stdin)
	for in.Scan() {
		var cmd string
		var arg int
		fmt.Sscan(in.Text(), &cmd, &arg)
		start := time.Now()
		updates := 0
		reply := map[string]any{}
		switch cmd {
		case "load":
			updates = announce(s, fam, nlris, set.attrs(fam, *resend, round))
			round++
		case "eor":
			if err := s.Send(bgp.NewEndOfRib(fam)); err != nil {
				log.Fatal(err)
			}
			if err := s.Flush(); err != nil {
				log.Fatal(err)
			}
			updates = 1
		case "down":
			s.Close()
		case "up":
			s = dial(fam, true)
			reply["connect_seconds"] = time.Since(start).Seconds()
			updates = announce(s, fam, nlris, set.attrs(fam, *resend, 0))
		case "storm":
			for range arg {
				updates += announce(s, fam, nlris, set.attrs(fam, *resend, round))
				round++
			}
		case "mark":
			// gobgpd handles the UPDATEs of a peer in the order they come
			// and sends to each peer through a FIFO queue: when a receiver
			// has the marker, gobgpd has handled every UPDATE before it. What
			// they caused it sends in one pass with the marker at the latest,
			// so everything is sent when the next marker arrives.
			p := netip.PrefixFrom(netip.AddrFrom4([4]byte{198, 18, byte(arg), 0}), 24)
			updates = announceMarker(s, fam, nlri(fam, p), set.attrs(fam, "same", 0))
		case "probe":
			var rate, secs int
			fmt.Sscan(in.Text(), &cmd, &rate, &secs)
			updates = probe(s, fam, set.attrs(fam, "same", 0), rate, secs)
		case "quit":
			s.Close()
			return
		default:
			log.Fatalf("unknown command %q", strings.TrimSpace(in.Text()))
		}
		reply["cmd"], reply["updates"], reply["seconds"] = cmd, updates, time.Since(start).Seconds()
		out.Encode(reply)
	}
}

// dial connects to gobgpd and drains whatever it sends.
func dial(fam bgp.Family, restarted bool) *speaker.Session {
	var caps []bgp.ParameterCapabilityInterface
	if *grTime > 0 {
		caps = append(caps, bgp.NewCapGracefulRestart(restarted, true, uint16(*grTime),
			[]*bgp.CapGracefulRestartTuple{bgp.NewCapGracefulRestartTuple(fam, restarted)}))
	}
	if *grTime > 0 && *llgrTime > 0 {
		caps = append(caps, bgp.NewCapLongLivedGracefulRestart(
			[]*bgp.CapLongLivedGracefulRestartTuple{bgp.NewCapLongLivedGracefulRestartTuple(fam, restarted, uint32(*llgrTime))}))
	}
	// gobgpd closes a connection that comes while the peer is Idle, for a
	// few seconds after a session ends: try again, as a BGP speaker does.
	var s *speaker.Session
	var err error
	for deadline := time.Now().Add(time.Minute); ; time.Sleep(100 * time.Millisecond) {
		if s, err = speaker.Dial(*local, *remote, localAS, netip.MustParseAddr("1.1.1.1"), []bgp.Family{fam}, caps...); err == nil {
			break
		}
		if time.Now().After(deadline) {
			log.Fatal(err)
		}
	}
	go func() {
		for {
			if _, err := s.ReadRaw(); err != nil {
				if !s.Closed() {
					log.Fatalf("session closed: %v", err)
				}
				return
			}
		}
	}()
	return s
}

func nlri(fam bgp.Family, p netip.Prefix) bgp.PathNLRI {
	var n bgp.NLRI
	var err error
	if fam == bgp.RF_IPv4_VPN {
		n, err = bgp.NewLabeledVPNIPAddrPrefix(p, *bgp.NewMPLSLabelStack(label), bgp.NewRouteDistinguisherTwoOctetAS(65000, 1))
	} else {
		n, err = bgp.NewIPAddrPrefix(p)
	}
	if err != nil {
		log.Fatal(err)
	}
	return bgp.PathNLRI{NLRI: n}
}

// withByType returns a with x inserted before the first attribute of a
// higher type code, so a list sorted by type stays sorted.
func withByType(a []bgp.PathAttributeInterface, x bgp.PathAttributeInterface) []bgp.PathAttributeInterface {
	i := slices.IndexFunc(a, func(y bgp.PathAttributeInterface) bool { return y.GetType() > x.GetType() })
	if i < 0 {
		i = len(a)
	}
	return slices.Insert(slices.Clone(a), i, x)
}

// With -groups: the group of every route, and the random source of the
// groups and of the order the routes are announced in.
var (
	group []int
	rnd   *rand.Rand
)

// announce sends all routes with the attributes a, *perUpdate to an UPDATE.
func announce(s *speaker.Session, fam bgp.Family, nlris []bgp.PathNLRI, a []bgp.PathAttributeInterface) int {
	updates := 0
	if group != nil {
		for _, i := range rnd.Perm(len(nlris)) {
			send(s, fam, nlris[i:i+1], withOwn(a, group[i]))
			updates++
		}
	} else {
		for i := 0; i < len(nlris); i += *perUpdate {
			a := a
			if *unique {
				a = withOwn(a, i)
			}
			send(s, fam, nlris[i:min(i+*perUpdate, len(nlris))], a)
			updates++
		}
	}
	if err := s.Flush(); err != nil {
		log.Fatal(err)
	}
	return updates
}

// announceMarker sends the marker route n; with -unique it carries the
// community of the first route, with -groups none of a group.
func announceMarker(s *speaker.Session, fam bgp.Family, n bgp.PathNLRI, a []bgp.PathAttributeInterface) int {
	if *unique {
		a = withOwn(a, 0)
	}
	send(s, fam, []bgp.PathNLRI{n}, a)
	if err := s.Flush(); err != nil {
		log.Fatal(err)
	}
	return 1
}

// probe announces rate probe routes per second for secs seconds on a fixed
// schedule, whatever gobgpd does with them.
func probe(s *speaker.Session, fam bgp.Family, a []bgp.PathAttributeInterface, rate, secs int) int {
	n := rate * secs
	if rate <= 0 || n > 1<<16 {
		log.Fatalf("probe %d %d: want a rate above 0 and at most 65536 probes", rate, secs)
	}
	start := time.Now()
	for i := range n {
		time.Sleep(time.Until(start.Add(time.Duration(i) * time.Second / time.Duration(rate))))
		p := netip.PrefixFrom(netip.AddrFrom4([4]byte{198, 19, byte(i >> 8), byte(i)}), 32)
		send(s, fam, []bgp.PathNLRI{nlri(fam, p)}, withStamp(a, time.Now()))
		if err := s.Flush(); err != nil {
			log.Fatal(err)
		}
	}
	return n
}

func send(s *speaker.Session, fam bgp.Family, batch []bgp.PathNLRI, a []bgp.PathAttributeInterface) {
	var m *bgp.BGPMessage
	if fam == bgp.RF_IPv4_UC {
		m = bgp.NewBGPUpdateMessage(nil, a, batch)
	} else {
		mp, err := bgp.NewPathAttributeMpReachNLRI(fam, batch, netip.MustParseAddr("127.0.0.1"))
		if err != nil {
			log.Fatal(err)
		}
		m = bgp.NewBGPUpdateMessage(nil, withByType(a, mp), nil)
	}
	if err := s.Send(m); err != nil {
		log.Fatal(err)
	}
}
