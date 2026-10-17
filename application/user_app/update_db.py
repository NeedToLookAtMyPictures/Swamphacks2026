import time

from database import Database

# Two separate devices, each with its own file
ben = Database("data/test_ben.db")
justin = Database("data/user.db")

if ben.get_device() is None:
    ben.init_device("Ben")
if justin.get_device() is None:
    justin.init_device("justin")

# Each device records its own user's position
ben.set_location(29.6516, -82.3248)
justin.set_location(29.6436, -82.3549)

now = time.time()
ben.merge({"geolocations": [
    {"user": "Nathan", "origin_id": "nathan-device", "time_created": now - 600,
     "latitude": 29.6480, "longitude": -82.3400},     # 10 minutes ago
    {"user": "Bayan", "origin_id": "bayan-device", "time_created": now - 3600,
     "latitude": 29.6600, "longitude": -82.3300},     # 1 hour ago
    {"user": "James", "origin_id": "james-device", "time_created": now - 7200,
     "latitude": 29.6350, "longitude": -82.3700},     # 2 hours ago
]})

ben.add_message("justin", "Campsites")
ben.add_message("*", "Hurricane is coming")

print("merged:", justin.merge(ben.export()))

# Justin's inbox
me = justin.get_device()["name"]
for m in justin.list("messages"):
    if m["receiver"].lower() == me.lower() or m["receiver"] == "*":
        print(f'{m["sender"]} -> {m["receiver"]}: {m["content"]}')

# Who Justin knows the position of now
for g in justin.list("geolocations"):
    print(f'{g["user"]} at {g["latitude"]:.4f}, {g["longitude"]:.4f}')