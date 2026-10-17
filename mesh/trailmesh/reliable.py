"""TCP-style reliable delivery: sequence numbers, cumulative + selective ACKs,
retransmit on timeout, and in-order delivery to the session."""
from __future__ import annotations

from .wire import ACK, Frame

SACK_BITS = 32


class RetriesExhausted(Exception):
    pass


class ReliableSender:
    def __init__(self, window: int, rto: float, max_retries: int):
        self.window = window
        self.rto = rto
        self.max_retries = max_retries
        self.next_seq = 0
        self._unacked: dict[int, list] = {}  # seq -> [frame, last_sent, tries]

    def can_send(self) -> bool:
        return len(self._unacked) < self.window

    @property
    def idle(self) -> bool:
        return not self._unacked

    def unacked_types(self) -> set:
        return {entry[0].type for entry in self._unacked.values()}

    def send(self, frame: Frame, now: float) -> Frame:
        frame.seq = self.next_seq
        self.next_seq += 1
        self._unacked[frame.seq] = [frame, now, 1]
        return frame

    def on_ack(self, payload: bytes) -> None:
        if len(payload) < ACK.size:
            return
        next_expected, sack = ACK.unpack_from(payload)
        for seq in [s for s in self._unacked if s < next_expected]:
            del self._unacked[seq]
        for i in range(SACK_BITS):
            if sack >> i & 1:
                self._unacked.pop(next_expected + 1 + i, None)

    def due(self, now: float) -> list[Frame]:
        """Frames whose retransmit timer expired. Raises once a frame runs out of retries."""
        out = []
        for entry in self._unacked.values():
            frame, last_sent, tries = entry
            if now - last_sent >= self.rto:
                if tries > self.max_retries:
                    raise RetriesExhausted(frame)
                entry[1] = now
                entry[2] = tries + 1
                out.append(frame)
        return out


class ReliableReceiver:
    def __init__(self, buffer_limit: int = 64):
        self.expected = 0
        self.buffer_limit = buffer_limit
        self._buffer: dict[int, Frame] = {}

    def on_frame(self, frame: Frame) -> list[Frame]:
        """Accept a reliable frame; return the frames now deliverable in order.

        Duplicates and frames too far ahead are ignored, but the caller should
        still send an ACK so the sender stops retransmitting.
        """
        if self.expected <= frame.seq < self.expected + self.buffer_limit:
            self._buffer.setdefault(frame.seq, frame)
        out = []
        while self.expected in self._buffer:
            out.append(self._buffer.pop(self.expected))
            self.expected += 1
        return out

    def ack_payload(self) -> bytes:
        sack = 0
        for i in range(SACK_BITS):
            if self.expected + 1 + i in self._buffer:
                sack |= 1 << i
        return ACK.pack(self.expected, sack)
