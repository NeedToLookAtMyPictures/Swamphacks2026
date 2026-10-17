"""Open a board and build a Node for one radio mode (used by the examples and the app).

Boards:
  "espnow"  an ESP32 running firmware/espnow_bridge (ESP-NOW only)
  "heltec"  a Heltec WiFi LoRa 32 V3 running firmware/heltec_bridge (LoRa + ESP-NOW)

Radio modes (Heltec only):
  "auto"    discover and handshake over LoRa; exchange over ESP-NOW when the peer
            is within 60 m (both positions known), otherwise over LoRa
  "lora"    LoRa only, at any distance (to test LoRa with the boards side by side)
  "espnow"  ESP-NOW only, like espnow_bridge (to test ESP-NOW on its own)
"""
from __future__ import annotations

from .espnow_serial import EspNowSerialLink
from .heltec_serial import HeltecSerialLink
from .lora import LoRaParams
from .node import Config, Node

BOARDS = ("espnow", "heltec")
RADIOS = ("auto", "lora", "espnow")


def open_board(kind: str, port: str, *, sf: int = 7, log=print) -> EspNowSerialLink:
    if kind == "heltec":
        return HeltecSerialLink(port, LoRaParams(sf=sf), log=log)
    if kind == "espnow":
        return EspNowSerialLink(port, log=log)
    raise ValueError(f"unknown board {kind!r}; use one of {BOARDS}")


def radio_link(board: EspNowSerialLink, radio: str):
    """The link that carries discovery for this mode (for scripts that don't run a Node)."""
    if isinstance(board, HeltecSerialLink) and radio in ("auto", "lora"):
        return board.lora
    return board


def build_node(board: EspNowSerialLink, store, *, radio: str = "auto", config: dict | None = None, **node_kwargs) -> Node:
    """config: Config overrides (e.g. always_on=True), applied to whichever radio does discovery."""
    config = config or {}
    if radio not in RADIOS:
        raise ValueError(f"unknown radio mode {radio!r}; use one of {RADIOS}")
    if not isinstance(board, HeltecSerialLink):
        if radio == "lora":
            raise ValueError("an espnow_bridge board has no LoRa radio; flash heltec_bridge to a Heltec V3")
        return Node(board, store, Config(**config), **node_kwargs)
    if radio == "espnow":
        return Node(board, store, Config(**config), **node_kwargs)
    lora_cfg = Config.lora(board.params, **config)
    if radio == "lora":
        return Node(board.lora, store, lora_cfg, **node_kwargs)
    # ESP-NOW starts only after the LoRa handshake, so allow for its ~0.1-1 s startup.
    fast_cfg = Config(idle_timeout=6.0, max_retries=20)
    return Node(board.lora, store, lora_cfg, fast_link=board, fast_config=fast_cfg, **node_kwargs)
