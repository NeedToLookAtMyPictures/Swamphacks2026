"""Run with: python -m unittest test_trail_distance"""

import tempfile
import unittest
from pathlib import Path

from flask import Flask

from api import api
from database import Database
from test_database import FakeServer
from global_database import GlobalDatabase
from trail_distance import TrailNetwork, haversine

# A U-shaped trail made of three lines that share their corners:
#
#   N2 ---- N3
#   |        |
#   |        |
#   N1       N4        (N1 and N4 are about 1 km apart; each side is about 2 km)
#
N1, N2, N3, N4 = (29.60, -82.40), (29.62, -82.40), (29.62, -82.39), (29.60, -82.39)
U = [[N1, N2], [N2, N3], [N3, N4]]
ISLAND = [[(29.70, -82.30), (29.71, -82.30)]]  # a trail nobody can reach from the U


def gpx(*lines):
    segments = "".join(
        "<trkseg>" + "".join(f'<trkpt lat="{lat}" lon="{lon}"/>' for lat, lon in line) + "</trkseg>"
        for line in lines)
    return f'<gpx xmlns="http://www.topografix.com/GPX/1/1"><trk>{segments}</trk></gpx>'


class TrailNetworkTest(unittest.TestCase):
    def setUp(self):
        self.trails = TrailNetwork(U + ISLAND)

    def test_snap_finds_nearest_point_on_a_trail(self):
        snap = self.trails.snap((29.61, -82.401))  # just west of the west side
        self.assertAlmostEqual(snap.trail_point[0], 29.61, places=6)
        self.assertAlmostEqual(snap.trail_point[1], -82.40, places=6)
        self.assertAlmostEqual(snap.offset, haversine((29.61, -82.401), (29.61, -82.40)))

    def test_snap_far_from_every_trail(self):
        self.assertIsNotNone(self.trails.snap((25.0, -80.0)))

    def test_follows_the_trail_not_a_straight_line(self):
        origin = (29.60, -82.4005)  # just off the bottom of the west side
        target = (29.60, -82.3895)  # just off the bottom of the east side
        [route] = self.trails.distances(origin, [target])

        along = haversine(N1, N2) + haversine(N2, N3) + haversine(N3, N4)
        self.assertAlmostEqual(route["along_trail"], along, delta=0.01)
        self.assertAlmostEqual(route["to_trail"], haversine(origin, N1), delta=0.01)
        self.assertAlmostEqual(route["from_trail"], haversine(target, N4), delta=0.01)
        self.assertAlmostEqual(route["distance"],
                               route["to_trail"] + route["along_trail"] + route["from_trail"])
        self.assertGreater(route["distance"], 4 * route["straight"])
        self.assertEqual(route["path"][0], list(origin))
        self.assertEqual(route["path"][-1], list(target))
        self.assertEqual(route["path"][2:-2], [list(N2), list(N3)])

    def test_same_segment(self):
        [route] = self.trails.distances((29.605, -82.40), [(29.615, -82.40)])
        self.assertAlmostEqual(route["along_trail"], haversine((29.605, -82.40), (29.615, -82.40)),
                               delta=0.01)
        self.assertAlmostEqual(route["to_trail"], 0)
        self.assertEqual(len(route["path"]), 4)  # no nodes in between

    def test_unconnected_trail_is_unreachable(self):
        near_u, near_island = (29.61, -82.40), (29.705, -82.30)
        reachable, unreachable = self.trails.distances(near_u, [(29.62, -82.395), near_island])
        self.assertIsNotNone(reachable["distance"])
        self.assertIsNone(unreachable["distance"])
        self.assertIsNone(unreachable["path"])
        self.assertAlmostEqual(unreachable["straight"], haversine(near_u, near_island))

    def test_no_trails(self):
        [route] = TrailNetwork([]).distances((29.6, -82.4), [(29.7, -82.3)])
        self.assertIsNone(route["distance"])
        self.assertGreater(route["straight"], 0)

    def test_trail_ending_near_another_joins_it(self):
        # A spur ending 10 m east of the middle of the west side, without touching it
        spur = [(29.61, -82.39), (29.61, -82.3999)]
        gap = haversine(spur[1], (29.61, -82.40))
        trails = TrailNetwork(U + [spur])
        [route] = trails.distances((29.60, -82.40), [spur[0]])
        expected = haversine(N1, (29.61, -82.40)) + gap + haversine(*spur)
        self.assertAlmostEqual(route["along_trail"], expected, delta=0.5)

    def test_trail_ending_far_from_another_stays_apart(self):
        spur = [(29.61, -82.39), (29.61, -82.3990)]  # ends about 100 m from the west side
        trails = TrailNetwork([[N1, N2], spur])
        [route] = trails.distances(N1, [spur[0]])
        self.assertIsNone(route["distance"])


class DistancesApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app = Flask(__name__)
        app.config["DATABASE"] = Database(Path(self.tmp.name) / "api.db")
        app.config["GLOBAL_DATABASE"] = GlobalDatabase(Path(self.tmp.name) / "global.db")
        app.register_blueprint(api)
        FakeServer().install(app, self)
        self.client = app.test_client()
        self.client.post("/api/device", json={"name": "alice"})

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, trails):
        def locale(id, lat, lon):
            return {"id": id, "category": "water", "name": id, "latitude": lat, "longitude": lon}
        self.client.post("/api/global/load", json={
            "locales": [locale("far", 29.60, -82.3895), locale("near", 29.62, -82.3995),
                        locale("island", 29.705, -82.30)],
            "trails": trails,
        })

    def test_distances(self):
        c = self.client
        self.load([{"id": "u", "name": "U", "gpx": gpx(*U)},
                   {"id": "i", "name": "Island", "gpx": gpx(*ISLAND)}])
        self.assertEqual(c.get("/api/distances").status_code, 409)  # no position yet
        self.assertEqual(c.get("/api/distances?from=nowhere").status_code, 400)
        self.assertEqual(c.get("/api/distances?from=1,2&to=nope").status_code, 404)

        c.put("/api/location", json={"latitude": 29.60, "longitude": -82.4005})
        r = c.get("/api/distances").json
        self.assertTrue(r["trails"])
        self.assertEqual([l["id"] for l in r["locales"]], ["near", "far", "island"])
        self.assertGreater(r["locales"][1]["distance"], 5000)  # around the U, not 1 km across
        self.assertIsNone(r["locales"][2]["distance"])

        # Between two locales, and to just one
        r = c.get("/api/distances?from=near&to=far").json
        self.assertEqual([l["id"] for l in r["locales"]], ["far"])
        expected = (haversine((29.62, -82.3995), N3) + haversine(N3, N4)
                    + haversine(N4, (29.60, -82.3895)))
        self.assertAlmostEqual(r["locales"][0]["distance"], expected, delta=0.5)

    def test_reloading_global_data_rebuilds_the_network(self):
        c = self.client
        self.load([])
        r = c.get("/api/distances?from=29.60,-82.4005").json
        self.assertFalse(r["trails"])
        self.load([{"id": "u", "name": "U", "gpx": gpx(*U)}])
        r = c.get("/api/distances?from=29.60,-82.4005").json
        self.assertTrue(r["trails"])


if __name__ == "__main__":
    unittest.main()
