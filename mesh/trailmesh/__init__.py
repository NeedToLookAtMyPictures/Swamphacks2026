"""Delay-tolerant epidemic protocol for trail updates over ESP-NOW (LoRa later)."""
from .link import Link, RxFrame
from .node import Config, Node
from .records import Kind, Record
from .store import AppStore

__all__ = ["AppStore", "Config", "Kind", "Link", "Node", "Record", "RxFrame"]
