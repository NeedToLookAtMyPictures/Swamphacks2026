"""Delay-tolerant gossip protocol for trail updates over ESP-NOW (LoRa later)."""
from .link import Link, RxFrame
from .node import Config, Node
from .records import Kind, Record
from .store import RecordStore

__all__ = ["Config", "Kind", "Link", "Node", "Record", "RecordStore", "RxFrame"]
