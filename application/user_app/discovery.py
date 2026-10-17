"""Find the sync server on the same wifi, with no address typed in.

The server answers a UDP broadcast on DISCOVERY_PORT with the port its HTTP API
listens on. The device broadcasts a question, and whoever answers is a server; its
address is the one the answer came from.

    device  --  "swamphacks-discover/1"  -->  255.255.255.255:8765  (everyone on the wifi)
    server  --  {"service": ..., "port": 8000}  -->  device
    device then syncs with http://<server-ip>:8000

Only the standard library is used, so neither app gets an extra dependency.
Some networks (many campus and hotel wifis) block devices from talking to each
other; there, pass the server's address with --server instead.
"""

import json
import socket
import threading
import time

DISCOVERY_PORT = 8765
QUESTION = b"swamphacks-discover/1"
SERVICE = "swamphacks-server"


def start_responder(http_port, port=DISCOVERY_PORT):
    """Server side: answer every discovery question with our HTTP port, in a
    background thread. Raises OSError if the UDP port can't be opened."""
    answer = json.dumps({"service": SERVICE, "port": http_port}).encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", port))

    def respond_forever():
        while True:
            question, address = sock.recvfrom(512)
            if question.strip() == QUESTION:
                sock.sendto(answer, address)

    thread = threading.Thread(target=respond_forever, name="lan-discovery", daemon=True)
    thread.start()
    return thread


def find_servers(timeout=1.5, port=DISCOVERY_PORT, address="<broadcast>"):
    """Device side: ask the wifi for servers. Returns their URLs, first answer first."""
    urls = []
    deadline = time.monotonic() + timeout
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.sendto(QUESTION, (address, port))
        except OSError:
            return urls  # no network at all
        while (left := deadline - time.monotonic()) > 0:
            sock.settimeout(left)
            try:
                answer, (ip, _) = sock.recvfrom(512)
            except (socket.timeout, OSError):
                return urls
            try:
                info = json.loads(answer)
                url = f"http://{ip}:{int(info['port'])}"
            except (ValueError, KeyError, TypeError):
                continue
            if info.get("service") == SERVICE and url not in urls:
                urls.append(url)
    return urls
