"""
modules/07_ml/evacuation.py - which road, and is there time.

The whole product exists to answer one question: "this village, four hours,
evacuate along this road". Everything upstream produces the four hours. This
module produces the road.

The problem is not an ordinary shortest path, because the network changes
underneath you. A road that is dry now is impassable in forty minutes, and a
route that is shortest in distance may cross the channel ahead of the wave and
strand everybody. So we solve a TIME-DEPENDENT shortest path: an edge may only
be used if you can be on it before the water arrives there, with a safety
margin.

    python -m modules.07_ml.evacuation --run outputs/teesta_overtop_fast_041

A note on what this is NOT. The project plan listed a graph neural network for
this. A GNN would need labelled evacuation outcomes to learn from - real
routes, real timings, real decisions - and no such dataset exists for Indian
dam breaks. Training one on synthetic labels would be inventing evidence. What
is implemented here is an exact algorithm on a real road graph, which is both
defensible and, for this problem, better: we do not need to LEARN the shortest
safe path when we can compute it.

Owner: captain (module 07).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

from shared.contract import WET_THRESHOLD_M
from shared.geo import Grid, haversine_km

REPO_ROOT = Path(__file__).resolve().parents[2]

WALK_SPEED_KMH = 4.5
"""Evacuation on foot, over rough ground, with children and belongings.

Deliberately conservative. 5 km/h is the usual flat-ground walking figure;
crowds moving uphill in panic do worse, and a route plan that assumes an
optimistic speed fails exactly when it matters. Cited as an assumption, not a
measurement."""

VEHICLE_SPEED_KMH = 25.0
"""Hill roads, at night, with traffic. Not highway speed."""

SAFETY_MARGIN_HR = 0.25
"""You must clear an edge at least this long before the water reaches it.
15 minutes. Reduce it and the plan starts recommending routes that arrive at a
bridge as the flood does."""

DANGEROUS_DEPTH_M = 0.30
"""Above this, a road is impassable to small vehicles and unsafe on foot.
Australian Disaster Resilience Guideline 7-3 (2017) hazard class H2 - the same
threshold used for roads_cut in module 01."""


# ==========================================================================
# Graph construction
# ==========================================================================


def build_road_graph(roads: list[dict], grid: Grid, arrival_hr, max_depth):
    """Turn OSM road geometry into a graph annotated with flood timing.

    Nodes are rounded coordinates, so ways that share an intersection share a
    node. Rounding to 5 decimal places is about 1 m - fine enough that distinct
    junctions stay distinct, coarse enough that the same junction digitised
    twice collapses into one.

    Each edge carries:
        length_km    great-circle length
        flood_hr     when water arrives on it (inf if it never floods)
        max_depth_m  the worst depth along it

    Returns a networkx.Graph.
    """
    import networkx as nx

    G = nx.Graph()

    def node_of(lon, lat):
        return (round(lon, 5), round(lat, 5))

    for road in roads:
        coords = road.get("coords") or []
        for (lon1, lat1), (lon2, lat2) in zip(coords, coords[1:]):
            a, b = node_of(lon1, lat1), node_of(lon2, lat2)
            if a == b:
                continue

            mid_lon, mid_lat = 0.5 * (lon1 + lon2), 0.5 * (lat1 + lat2)
            if grid.contains(mid_lon, mid_lat):
                r, c = grid.rowcol(mid_lon, mid_lat)
                depth = float(max_depth[r, c])
                t = float(arrival_hr[r, c])
                flood_hr = t if np.isfinite(t) and depth >= DANGEROUS_DEPTH_M else np.inf
            else:
                # Outside the model domain: unmodelled, so treated as dry. This
                # is an assumption and it is the optimistic one - flag it rather
                # than let a route escape the domain and be declared safe.
                depth, flood_hr = 0.0, np.inf

            length = haversine_km(lon1, lat1, lon2, lat2)
            if G.has_edge(a, b):
                continue
            G.add_edge(
                a, b,
                length_km=length,
                flood_hr=flood_hr,
                max_depth_m=depth,
                name=road.get("name", ""),
                highway=road.get("highway", ""),
                in_domain=grid.contains(mid_lon, mid_lat),
            )

    for n in G.nodes:
        lon, lat = n
        if grid.contains(lon, lat):
            r, c = grid.rowcol(lon, lat)
            G.nodes[n]["flood_hr"] = float(arrival_hr[r, c])
            G.nodes[n]["max_depth_m"] = float(max_depth[r, c])
            G.nodes[n]["in_domain"] = True
        else:
            G.nodes[n]["flood_hr"] = float("inf")
            G.nodes[n]["max_depth_m"] = 0.0
            G.nodes[n]["in_domain"] = False
    return G


def nearest_node(G, lon: float, lat: float):
    """Graph node closest to a point. None if the graph is empty."""
    best, best_d = None, float("inf")
    for n in G.nodes:
        d = (n[0] - lon) ** 2 + (n[1] - lat) ** 2
        if d < best_d:
            best, best_d = n, d
    return best


def at_risk_node(G, lon: float, lat: float, radius_m: float = 400.0):
    """The road node near a settlement that the water reaches FIRST.

    Starting a route from the merely-nearest node is wrong and produced
    nonsense: in a narrow gorge the closest node is usually already dry, the
    router declares it safe, and the answer comes back as a 0 km route that
    looks like a plan and is actually "you are already fine".

    The question worth answering is "the water is about to cut this village's
    road access - where do people go", so the route starts from the threatened
    node. Returns (node, flood_hr), or (None, None) when nothing within the
    radius floods at all, which is itself a useful answer.
    """
    deg = radius_m / 111_320.0
    best, best_t = None, float("inf")
    for n, d in G.nodes(data=True):
        if abs(n[0] - lon) > deg or abs(n[1] - lat) > deg:
            continue
        t = d.get("flood_hr", float("inf"))
        if np.isfinite(t) and t < best_t:
            best, best_t = n, t
    return best, (best_t if best is not None else None)


# ==========================================================================
# Time-dependent shortest path
# ==========================================================================


def safe_route(
    G,
    start,
    speed_kmh: float = WALK_SPEED_KMH,
    margin_hr: float = SAFETY_MARGIN_HR,
    start_time_hr: float = 0.0,
):
    """Earliest-arrival path from `start` to any node the flood never reaches.

    Dijkstra on elapsed time, with one extra rule: an edge is only traversable
    if you finish crossing it at least `margin_hr` before the water arrives on
    it. That single constraint is what separates this from a road-distance
    query - it will refuse a short route that crosses the channel ahead of the
    wave and take a longer one uphill instead.

    Returns (path, arrival_time_hr) or (None, None) if no safe node is
    reachable in time. None is a real answer here and an important one: it
    means this settlement cannot walk out, and needs boats or helicopters. We
    report that rather than inventing a route.
    """
    import heapq

    if start not in G:
        return None, None

    best = {start: start_time_hr}
    prev: dict = {}
    heap = [(start_time_hr, start)]

    while heap:
        t, node = heapq.heappop(heap)
        if t > best.get(node, float("inf")):
            continue

        # A node the flood never reaches, outside the wet area, is the goal.
        if not np.isfinite(G.nodes[node].get("flood_hr", float("inf"))):
            path = [node]
            while path[-1] in prev:
                path.append(prev[path[-1]])
            return list(reversed(path)), t

        for nbr in G.neighbors(node):
            e = G[node][nbr]
            travel = e["length_km"] / max(speed_kmh, 0.1)
            arrive = t + travel
            if arrive + margin_hr > e["flood_hr"]:
                continue  # water gets there first
            if arrive < best.get(nbr, float("inf")):
                best[nbr] = arrive
                prev[nbr] = node
                heapq.heappush(heap, (arrive, nbr))

    return None, None


def plan_evacuation(
    run_dir: str | Path,
    exposure: dict,
    speed_kmh: float = WALK_SPEED_KMH,
    margin_hr: float = SAFETY_MARGIN_HR,
) -> dict:
    """Evacuation route for every affected settlement in a run.

    Writes evacuation.json into the run folder and returns it. For each
    settlement it reports the route, how long the walk takes, how long the
    water takes, and the difference - which is the number a district officer
    actually acts on.
    """
    from shared.io import read_grid, read_json, write_json

    run_dir = Path(run_dir)
    arrival, grid = read_grid(run_dir, "arrival_time")
    depth, _ = read_grid(run_dir, "max_depth")

    roads = exposure.get("roads") or []
    if not roads:
        raise RuntimeError(
            "no road geometry in the exposure bundle - run "
            "modules.01_geodata.exposure.build_exposure() with with_roads=True"
        )

    G = build_road_graph(roads, grid, arrival, depth)

    try:
        impact = read_json(run_dir, "impact.json")
        settlements = impact.get("settlements", [])
    except FileNotFoundError:
        settlements = []

    plans = []
    for s in settlements:
        water_hr = s.get("arrival_hr")

        start, node_flood_hr = at_risk_node(G, s["lon"], s["lat"])
        if start is None:
            # Nothing on this settlement's road network floods. Say that
            # plainly instead of returning an empty route that reads like a plan.
            plans.append(
                {
                    "name": s["name"],
                    "status": "access_not_inundated",
                    "reachable": True,
                    "water_arrives_hr": water_hr,
                    "note": (
                        "No road within 400 m of this settlement is inundated "
                        "above 0.30 m. The settlement is flagged as affected "
                        "because water reaches within the sampled footprint, "
                        "but its road access stays open - shelter in place or "
                        "move locally uphill; no evacuation route is required."
                    ),
                }
            )
            continue

        path, arrive_hr = safe_route(
            G, start, speed_kmh, margin_hr
        )

        if path is None:
            plans.append(
                {
                    "name": s["name"],
                    "status": "no_safe_route",
                    "reachable": False,
                    "water_arrives_hr": water_hr,
                    "road_floods_at_hr": round(node_flood_hr, 3),
                    "note": (
                        "No road route reaches unflooded ground before the water, "
                        "at the assumed speed. This settlement needs assisted "
                        "evacuation, not a route."
                    ),
                }
            )
            continue

        walk_hr = arrive_hr
        plans.append(
            {
                "name": s["name"],
                "status": "route_found",
                "reachable": True,
                "road_floods_at_hr": round(node_flood_hr, 3),
                "walk_time_hr": round(walk_hr, 3),
                "water_arrives_hr": water_hr,
                "margin_hr": round(water_hr - walk_hr, 3) if water_hr is not None else None,
                "route_km": round(
                    sum(
                        G[a][b]["length_km"] for a, b in zip(path, path[1:])
                    ),
                    3,
                ),
                "route": [[lon, lat] for lon, lat in path],
                "exit_point": list(path[-1]),
            }
        )

    order = {"no_safe_route": 0, "route_found": 1, "access_not_inundated": 2}
    plans.sort(key=lambda p: (order.get(p["status"], 9), p.get("margin_hr") or 1e9))

    payload = {
        "run_id": run_dir.name,
        "assumptions": {
            "speed_kmh": speed_kmh,
            "safety_margin_hr": margin_hr,
            "dangerous_depth_m": DANGEROUS_DEPTH_M,
            "note": (
                "Speeds and margin are assumptions, not measurements. Roads "
                "outside the model domain are treated as dry, which is the "
                "optimistic direction - a route that leaves the domain is "
                "unverified beyond its edge."
            ),
        },
        "graph": {"nodes": G.number_of_nodes(), "edges": G.number_of_edges()},
        "method": (
            "Time-dependent Dijkstra on the OSM road graph. Not a learned "
            "model - see the module docstring for why a GNN was not used."
        ),
        "settlements": plans,
    }
    write_json(run_dir, "evacuation.json", payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m modules.07_ml.evacuation")
    ap.add_argument("--run", required=True)
    ap.add_argument("--site", default="teesta")
    ap.add_argument("--speed", type=float, default=WALK_SPEED_KMH)
    args = ap.parse_args(argv)

    exposure_path = REPO_ROOT / "data" / "exposure" / args.site / "exposure.json"
    exposure = json.loads(exposure_path.read_text(encoding="utf-8"))

    out = plan_evacuation(args.run, exposure, speed_kmh=args.speed)
    print(f"graph: {out['graph']['nodes']} nodes, {out['graph']['edges']} edges")
    for p in out["settlements"]:
        if p["status"] == "route_found":
            print(
                f"  {p['name']:<16} road floods {p['road_floods_at_hr']:>5.2f} hr  "
                f"walk {p['walk_time_hr']:>5.2f} hr  margin {p['margin_hr']:>+6.2f} hr  "
                f"({p['route_km']} km)"
            )
        elif p["status"] == "no_safe_route":
            print(f"  {p['name']:<16} NO SAFE ROUTE - assisted evacuation required")
        else:
            print(f"  {p['name']:<16} road access not inundated - no route needed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
