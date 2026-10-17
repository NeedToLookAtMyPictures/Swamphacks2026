"""Open a hardware link by name and get the Config that suits it (used by the examples and the app)."""
from __future__ import annotations

from .espnow_serial import EspNowSerialLink
from .lora import LoRaParams
from .lora_serial import LoRaSerialLink
from .node import Config

LINKS = ("espnow", "lora")


def open_link(kind: str, port: str, *, sf: int = 7, log=print) -> EspNowSerialLink:
    """kind "espnow": firmware/espnow_bridge. kind "lora": firmware/lora_bridge (915 MHz, 500 kHz, `sf`)."""
    if kind == "lora":
        return LoRaSerialLink(port, LoRaParams(sf=sf), log=log)
    if kind == "espnow":
        return EspNowSerialLink(port, log=log)
    raise ValueError(f"unknown link {kind!r}; use one of {LINKS}")


def config_for(link, **overrides) -> Config:
    if isinstance(link, LoRaSerialLink):
        return Config.lora(link.params, **overrides)
    return Config(**overrides)
