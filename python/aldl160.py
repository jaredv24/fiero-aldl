"""GM 160 baud ALDL decoding in Python, for USB ALDL cables (no Arduino).

How a USB cable is read
-----------------------
A typical 160 baud ALDL cable is a USB-serial chip whose receive line is wired
to ALDL pin E. The ECM sends one bit every 6.25 ms as a pulse: short = 0,
long = 1. If the serial port is opened at a much higher baud (2400 by default),
each time the line goes low the serial chip "receives" one byte. The longer the
line stayed low, the more zero bits that byte starts with (serial bytes arrive
least-significant bit first), and a low lasting longer than a whole byte comes
in as 0x00. So every byte gives back the length of one low period, and from
that, one ALDL bit.

Depending on the cable, the low periods are either the ALDL pulses themselves
(normal) or the gaps between them (inverted, if the cable has an inverting
transistor). cable_check.py finds out which applies to your cable.

Status: tested against a simulated serial chip, not a real cable yet.
  - Normal cables: works in simulation at 2400 and 4800 baud.
  - Inverted cables: EXPERIMENTAL. Short pulses can slip between the serial
    chip's samples, so bits get lost. How well it works depends on the
    real cable's chip; a capture from cable_check.py is needed to fix it.
"""

import time

BIT_PERIOD_US = 6250        # 160 baud
THRESHOLD_US = BIT_PERIOD_US / 2
DEFAULT_CABLE_BAUD = 2400


class Decoder160:
    """Turns a stream of ALDL bits into frames (same logic as the Arduino).

    A frame starts with nine 1 bits (sync); each byte is a 0 start bit then
    8 data bits.
    """

    SYNC_ONES = 9
    MAX_BYTES = 64

    def __init__(self, msb_first=True):
        self.msb_first = msb_first
        self.overflows = 0
        self.reset()

    def reset(self):
        self._state = "hunt"
        self._ones = 0
        self._buf = bytearray()
        self._cur = 0
        self._nbits = 0

    def _start_byte(self):
        if self._state == "hunt":
            self._buf = bytearray()
        self._state = "data"
        self._cur = 0
        self._nbits = 0

    def feed_bit(self, bit):
        """Feed one bit (0/1). Returns a complete frame (bytes) or None."""
        if self._state == "hunt":
            if bit:
                self._ones += 1
            else:
                if self._ones >= self.SYNC_ONES:
                    self._start_byte()
                self._ones = 0
            return None

        if self._state == "data":
            if self.msb_first:
                self._cur = ((self._cur << 1) | bit) & 0xFF
            else:
                self._cur |= bit << self._nbits
            self._nbits += 1
            if self._nbits == 8:
                if len(self._buf) >= self.MAX_BYTES:
                    self.overflows += 1
                    self.reset()
                    return None
                self._buf.append(self._cur)
                self._state = "start"
            return None

        # Expecting a start bit (0). A 1 means the next sync has begun.
        if not bit:
            self._start_byte()
            return None
        frame = bytes(self._buf)
        self.reset()
        self._ones = 1
        return frame or None


def low_time_us(byte, baud):
    """How long the line was low, judged from one received serial byte.

    Returns None for 0x00, meaning "at least a whole byte long".
    """
    if byte == 0:
        return None
    zeros = 0
    while not (byte >> zeros) & 1:
        zeros += 1
    return (1 + zeros) * 1_000_000 / baud   # start bit + leading zero data bits


def byte_to_bit(byte, baud, invert=False):
    """Turn one received serial byte into one ALDL bit."""
    t = low_time_us(byte, baud)
    long_low = t is None or t > THRESHOLD_US
    # Normal: the low period IS the pulse, long = 1.
    # Inverted: the low period is the gap after the pulse, long gap = short pulse = 0.
    return 0 if long_low == invert else 1


def byte_to_bits(byte, baud, invert=False):
    """Turn one received serial byte into the ALDL bit(s) it covers.

    Inverted cables: the low period is the gap AFTER a pulse. A short gap (bit 1)
    can be followed by a short pulse (bit 0) that starts and ends while the
    serial chip is still busy with this byte, so the chip never starts a byte
    for it. It still shows up inside this byte as ones followed by zeros, so
    we add the missing 0 bit here.
    """
    bits = [byte_to_bit(byte, baud, invert)]
    if invert and byte != 0:
        zeros = 0
        while not (byte >> zeros) & 1:
            zeros += 1
        rest, left = byte >> zeros, 8 - zeros
        while left and rest & 1:
            rest >>= 1
            left -= 1
        if left:  # line went low again before the byte ended
            bits.append(0)
    return bits


class CableDecoder:
    """Serial bytes from a USB ALDL cable -> ALDL frames."""

    def __init__(self, baud=DEFAULT_CABLE_BAUD, invert=False, msb_first=True):
        self.baud, self.invert = baud, invert
        self.decoder = Decoder160(msb_first)
        self.bytes_in = 0
        self.frames = 0

    def feed(self, data):
        """Feed received bytes. Returns a list of complete frames."""
        out = []
        for b in data:
            self.bytes_in += 1
            for bit in byte_to_bits(b, self.baud, self.invert):
                frame = self.decoder.feed_bit(bit)
                if frame:
                    self.frames += 1
                    out.append(frame)
        return out

    def gap(self):
        """Call when the line has been quiet for a while."""
        self.decoder.reset()


def decode_capture(chunks, baud, invert=False, msb_first=True):
    """Decode a list of received byte chunks. Returns the list of frames."""
    cd = CableDecoder(baud, invert, msb_first)
    frames = []
    for chunk in chunks:
        if chunk is None:
            cd.gap()
        else:
            frames.extend(cd.feed(chunk))
    return frames


def frame_quality(frames):
    """(most common frame length, fraction of frames with that length)."""
    if not frames:
        return 0, 0.0
    counts = {}
    for f in frames:
        counts[len(f)] = counts.get(len(f), 0) + 1
    length, n = max(counts.items(), key=lambda kv: kv[1])
    return length, n / len(frames)


class CableSource:
    """Data source for aldl_logger.py that reads a USB ALDL cable directly."""

    def __init__(self, port, baud=DEFAULT_CABLE_BAUD, invert=False, msb_first=True):
        try:
            import serial
        except ImportError:
            raise SystemExit("pyserial is not installed. Run:  pip install pyserial")
        self.ser = serial.Serial(port, baud, timeout=0.2)
        self.ser.reset_input_buffer()
        self.cd = CableDecoder(baud, invert, msb_first)

    def send(self, chars):
        pass  # nothing to configure on a plain cable

    def lines(self):
        yield (f"I,cable mode {self.cd.baud} baud"
               f"{' inverted' if self.cd.invert else ''}\n")
        last_data = last_stats = time.time()
        while True:
            data = self.ser.read(256)
            now = time.time()
            if data:
                last_data = now
                for frame in self.cd.feed(data):
                    yield f"D,{len(frame)},{frame.hex().upper()}\n"
            elif now - last_data > 0.1:
                self.cd.gap()
            if now - last_stats > 5:
                last_stats = now
                yield f"S,frames={self.cd.frames} bytes={self.cd.bytes_in}\n"

    def close(self):
        self.ser.close()
