"""Distances along trails.

A hiker can't walk in a straight line to a locale, so distances are measured
the way they would walk it: from where they are straight to the nearest point
on a trail, along the trails, then from the trail straight to the locale.

The trails are the global trails (global_database.py), each a list of lines of
[lat, lon] points parsed from its GPX. Trails are connected where they share a
point, and where the end of one comes within JOIN_DISTANCE of another.
"""

import heapq
import math
from collections import defaultdict

EARTH_RADIUS = 6_371_000  # meters
CELL = 0.005              # spatial index cell size, in degrees (about 500 m)
JOIN_DISTANCE = 30        # meters: a trail ending this close to another joins it
PRECISION = 5             # decimal places: points this close (about 1 m) are the same


def haversine(a, b):
    """Great-circle distance in meters between two (lat, lon) points."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_RADIUS * math.asin(math.sqrt(h))


class Snap:
    """The nearest point on a trail to some point.

    The trail point lies on segment `seg` (from node a to node b) at fraction t.
    """

    def __init__(self, seg, t, trail_point, offset):
        self.seg = seg
        self.t = t
        self.trail_point = trail_point
        self.offset = offset  # meters from the point to the trail


class TrailNetwork:
    def __init__(self, lines):
        """`lines`: lists of (lat, lon) points, e.g. every segment of every global trail."""
        self.nodes = {}                    # node key -> (lat, lon)
        self.segments = []                 # (a, b, length in meters, line), None once split
        self.adjacent = defaultdict(dict)  # node -> {neighbor: meters}
        self.grid = defaultdict(list)      # (cell x, cell y) -> segment indexes
        self.lines_at = defaultdict(set)   # node -> lines that pass through it
        ends = []                          # (node, line) for the two ends of every line
        for line, points in enumerate(lines):
            keys = [self._node(p) for p in points]
            keys = [k for i, k in enumerate(keys) if i == 0 or k != keys[i - 1]]
            if len(keys) < 2:
                continue
            for a, b in zip(keys, keys[1:]):
                self._add_segment(a, b, line)
            ends += [(keys[0], line), (keys[-1], line)]
        self._update_bounds()
        for node, line in ends:
            self._join(node, line)
        self._update_bounds()

    @classmethod
    def from_trails(cls, trails):
        """Network of the trails GlobalDatabase.trails() returns."""
        return cls([points for trail in trails for points in trail["segments"]])

    def _node(self, point):
        key = (round(point[0], PRECISION), round(point[1], PRECISION))
        self.nodes.setdefault(key, (float(point[0]), float(point[1])))
        return key

    def _add_segment(self, a, b, line):
        length = haversine(self.nodes[a], self.nodes[b])
        index = len(self.segments)
        self.segments.append((a, b, length, line))
        self._connect(a, b, length)
        self.lines_at[a].add(line)
        self.lines_at[b].add(line)
        (lat1, lon1), (lat2, lon2) = self.nodes[a], self.nodes[b]
        for x in range(self._cell(min(lon1, lon2)), self._cell(max(lon1, lon2)) + 1):
            for y in range(self._cell(min(lat1, lat2)), self._cell(max(lat1, lat2)) + 1):
                self.grid[x, y].append(index)

    def _connect(self, a, b, length):
        if length < self.adjacent[a].get(b, math.inf):
            self.adjacent[a][b] = self.adjacent[b][a] = length

    def _join(self, node, line):
        """Connect a line's end to the nearest other trail, if it's close and not already."""
        if self.lines_at[node] - {line}:
            return  # already meets another trail here
        snap = self.snap(self.nodes[node], skip_line=line)
        if snap is None or snap.offset > JOIN_DISTANCE:
            return
        self._connect(node, self._split(snap), snap.offset)

    def _split(self, snap):
        """Make the snapped point a node, splitting its segment in two; return the node."""
        a, b, length, line = self.segments[snap.seg]
        if snap.t * length < 1:
            return a
        if (1 - snap.t) * length < 1:
            return b
        middle = self._node(snap.trail_point)
        self.segments[snap.seg] = None
        del self.adjacent[a][b], self.adjacent[b][a]
        self._add_segment(a, middle, line)
        self._add_segment(middle, b, line)
        return middle

    def _update_bounds(self):
        xs, ys = [x for x, _ in self.grid], [y for _, y in self.grid]
        self.bounds = (min(xs), min(ys), max(xs), max(ys)) if self.grid else None

    @staticmethod
    def _cell(degrees):
        return math.floor(degrees / CELL)

    # -- nearest point on a trail ---------------------------------------------------

    def snap(self, point, skip_line=None):
        """The nearest point on any trail to `point`, or None if there are no trails."""
        if self.bounds is None:
            return None
        lat, lon = point
        cx, cy = self._cell(lon), self._cell(lat)
        # Smallest distance covered by one ring of cells, so we know when to stop
        ring = CELL * math.pi / 180 * EARTH_RADIUS * min(1.0, math.cos(math.radians(lat)))
        # Past this many rings every cell with a segment in it has been checked
        min_x, min_y, max_x, max_y = self.bounds
        last_ring = max(cx - min_x, max_x - cx, cy - min_y, max_y - cy)
        best = None
        for k in range(last_ring + 1):
            # Everything within k-1 rings has been checked; nothing further can beat best
            if best is not None and best.offset <= (k - 1) * ring:
                break
            for cell in self._ring(cx, cy, k):
                for index in self.grid.get(cell, ()):
                    segment = self.segments[index]
                    if segment is None or segment[3] == skip_line:
                        continue
                    snap = self._snap_to_segment(point, index)
                    if best is None or snap.offset < best.offset:
                        best = snap
        return best

    @staticmethod
    def _ring(cx, cy, k):
        """The cells exactly k cells away from (cx, cy)."""
        if k == 0:
            yield cx, cy
            return
        for x in range(cx - k, cx + k + 1):
            yield x, cy - k
            yield x, cy + k
        for y in range(cy - k + 1, cy + k):
            yield cx - k, y
            yield cx + k, y

    def _snap_to_segment(self, point, index):
        a, b, _, _ = self.segments[index]
        # Flat projection around the point; fine at the scale of one segment
        scale = math.cos(math.radians(point[0]))

        def xy(p):
            return (p[1] - point[1]) * scale, p[0] - point[0]

        (ax, ay), (bx, by) = xy(self.nodes[a]), xy(self.nodes[b])
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 == 0 else min(1.0, max(0.0, -(ax * dx + ay * dy) / length2))
        (lat1, lon1), (lat2, lon2) = self.nodes[a], self.nodes[b]
        trail_point = (lat1 + t * (lat2 - lat1), lon1 + t * (lon2 - lon1))
        return Snap(index, t, trail_point, haversine(point, trail_point))

    # -- distances --------------------------------------------------------------------

    def distances(self, origin, targets):
        """Walking distance from `origin` to each of `targets`, all (lat, lon) points.

        Returns one dict per target, in order:
            distance     total meters: to_trail + along_trail + from_trail
            to_trail     meters from origin to the nearest point on a trail
            along_trail  meters along the trails
            from_trail   meters from the trail to the target
            straight     straight-line meters, for comparison
            path         [(lat, lon), ...] the route, origin to target
        The trail fields are None when there are no trails or the target's trail
        isn't connected to the origin's.
        """
        start = self.snap(origin)
        if start is None:
            return [self._unreachable(origin, t) for t in targets]
        snaps = [self.snap(t) for t in targets]

        # Dijkstra from the origin's trail point, until every target's segment ends are settled
        a, b, length, _ = self.segments[start.seg]
        dist = {a: start.t * length, b: (1 - start.t) * length}
        prev = {a: None, b: None}
        wanted = {n for s in snaps for n in self.segments[s.seg][:2]}
        queue = [(d, n) for n, d in dist.items()]
        heapq.heapify(queue)
        settled = set()
        while queue and not wanted <= settled:
            d, node = heapq.heappop(queue)
            if node in settled:
                continue
            settled.add(node)
            for neighbor, length in self.adjacent[node].items():
                if d + length < dist.get(neighbor, math.inf):
                    dist[neighbor] = d + length
                    prev[neighbor] = node
                    heapq.heappush(queue, (d + length, neighbor))

        return [self._route(origin, target, start, snap, dist, prev)
                for target, snap in zip(targets, snaps)]

    def _route(self, origin, target, start, end, dist, prev):
        c, d, length, _ = self.segments[end.seg]
        options = [  # (meters along the trail, last trail node before the target, or None)
            (dist.get(c, math.inf) + end.t * length, c),
            (dist.get(d, math.inf) + (1 - end.t) * length, d),
        ]
        if end.seg == start.seg:  # both on the same segment: no nodes in between
            options.append((abs(end.t - start.t) * length, None))
        along, last = min(options, key=lambda o: o[0])
        if along == math.inf:
            return self._unreachable(origin, target)

        nodes = []
        while last is not None:
            nodes.append(self.nodes[last])
            last = prev[last]
        path = [origin, start.trail_point, *reversed(nodes), end.trail_point, target]
        return {
            "distance": start.offset + along + end.offset,
            "to_trail": start.offset,
            "along_trail": along,
            "from_trail": end.offset,
            "straight": haversine(origin, target),
            "path": [list(p) for p in path],
        }

    @staticmethod
    def _unreachable(origin, target):
        return {"distance": None, "to_trail": None, "along_trail": None, "from_trail": None,
                "straight": haversine(origin, target), "path": None}
