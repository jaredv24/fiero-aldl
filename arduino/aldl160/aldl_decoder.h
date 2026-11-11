// aldl_decoder.h - decoder for GM's 160 baud ALDL data stream.
//
// Plain C++ (no Arduino calls) so it can be unit tested on a PC.
//
// How the 160 baud stream works (as commonly documented for early GM ECMs):
//   - One bit every 6.25 ms (160 bits per second).
//   - Every bit starts with the line going to its "active" level. A SHORT
//     active pulse is a 0, a LONG active pulse is a 1.
//   - A frame starts with a sync of nine 1 bits in a row.
//   - Each data byte is sent as a 0 "start" bit followed by 8 data bits.
//     Because every byte begins with a 0, nine 1s in a row can only be sync.
//
// Things that vary or that you may need to confirm on your car (settings below):
//   - which line level is "active" (low or high)
//   - the short/long threshold (default: half a bit period)
//   - bit order inside a byte (default: most significant bit first)

#ifndef ALDL_DECODER_H
#define ALDL_DECODER_H

#include <stdint.h>
#include <string.h>

class Aldl160Decoder {
 public:
  static const uint8_t MAX_BYTES = 64;
  static const uint8_t SYNC_ONES = 9;

  // Settings (change before use if needed).
  uint8_t activeLevel = 0;        // 0 = active-low, 1 = active-high
  uint32_t bitPeriodUs = 6250;    // 160 baud
  uint32_t thresholdUs = 3125;    // active pulse longer than this = 1
  uint32_t minPulseUs = 150;      // shorter = noise, ignored
  bool msbFirst = true;

  // Result of the last completed frame.
  uint8_t frame[MAX_BYTES];
  uint8_t frameLen = 0;

  // Error counters, handy for checking wiring/settings.
  uint32_t noisePulses = 0;
  uint32_t badPulses = 0;     // active pulse longer than a whole bit
  uint32_t gapResets = 0;
  uint32_t overflows = 0;

  void reset() {
    state_ = HUNT;
    ones_ = 0;
    len_ = 0;
    cur_ = 0;
    nbits_ = 0;
  }

  // Feed one measured pulse: the line sat at `level` for `us` microseconds.
  // Returns true when a complete frame is ready in frame[]/frameLen.
  bool feedPulse(uint8_t level, uint32_t us) {
    if (level == activeLevel) {
      if (us < minPulseUs) { noisePulses++; return false; }
      if (us > bitPeriodUs + bitPeriodUs / 2) { badPulses++; reset(); return false; }
      return feedBit(us > thresholdUs ? 1 : 0);
    }
    // Idle level. A gap much longer than a bit means the stream stopped.
    if (us > 3 * bitPeriodUs) { gapResets++; reset(); }
    return false;
  }

  // Feed one already-decoded bit. Returns true when a frame is complete.
  bool feedBit(uint8_t bit) {
    switch (state_) {
      case HUNT:
        if (bit) {
          if (ones_ < 255) ones_++;
        } else {
          if (ones_ >= SYNC_ONES) startByte();  // this 0 is the first start bit
          ones_ = 0;
        }
        return false;

      case DATA:
        if (msbFirst) cur_ = (uint8_t)((cur_ << 1) | bit);
        else cur_ = (uint8_t)(cur_ | (bit << nbits_));
        if (++nbits_ == 8) {
          if (len_ >= MAX_BYTES) { overflows++; reset(); return false; }
          buf_[len_++] = cur_;
          state_ = EXPECT_START;
        }
        return false;

      case EXPECT_START:
        if (!bit) { startByte(); return false; }
        // A 1 where a start bit should be = the next sync has begun.
        {
          bool ok = len_ > 0;
          if (ok) { memcpy(frame, buf_, len_); frameLen = len_; }
          reset();
          ones_ = 1;
          return ok;
        }
    }
    return false;
  }

 private:
  enum State { HUNT, DATA, EXPECT_START };
  State state_ = HUNT;
  uint8_t ones_ = 0;
  uint8_t buf_[MAX_BYTES];
  uint8_t len_ = 0;
  uint8_t cur_ = 0;
  uint8_t nbits_ = 0;

  void startByte() {
    if (state_ == HUNT) len_ = 0;
    state_ = DATA;
    cur_ = 0;
    nbits_ = 0;
  }
};

#endif
