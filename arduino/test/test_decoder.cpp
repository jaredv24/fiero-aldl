// PC-side test for aldl_decoder.h. Build and run:
//   g++ -std=c++11 -I../aldl160 test_decoder.cpp -o test_decoder && ./test_decoder
#include <cstdio>
#include <cstdlib>
#include <vector>
#include "aldl_decoder.h"

static int failures = 0;
#define CHECK(c) do { if (!(c)) { printf("FAIL line %d: %s\n", __LINE__, #c); failures++; } } while (0)

// Build the bit sequence for one frame: 9 sync ones, then 0 + 8 bits per byte.
static void addFrame(std::vector<int> &bits, const std::vector<uint8_t> &data, bool msb) {
  for (int i = 0; i < 9; i++) bits.push_back(1);
  for (uint8_t b : data) {
    bits.push_back(0);
    for (int i = 0; i < 8; i++) bits.push_back(msb ? (b >> (7 - i)) & 1 : (b >> i) & 1);
  }
}

// Turn bits into (level, duration) pulses the way the wire would look.
// Active pulse: 1 -> ~4400us, 0 -> ~400us; rest of the 6250us bit is idle.
static void play(Aldl160Decoder &d, const std::vector<int> &bits, uint8_t active,
                 std::vector<std::vector<uint8_t>> &out, int jitter) {
  for (int bit : bits) {
    int on = bit ? 4400 : 400;
    if (jitter) on += (rand() % (2 * jitter + 1)) - jitter;
    int off = 6250 - on;
    if (d.feedPulse(active, on)) out.push_back(std::vector<uint8_t>(d.frame, d.frame + d.frameLen));
    if (d.feedPulse(!active, off)) out.push_back(std::vector<uint8_t>(d.frame, d.frame + d.frameLen));
  }
}

int main() {
  srand(1);
  std::vector<uint8_t> f1, f2;
  for (int i = 0; i < 20; i++) { f1.push_back((uint8_t)rand()); f2.push_back((uint8_t)rand()); }
  f1[3] = 0xFF; f2[5] = 0x00;  // edge cases: all ones / all zeros bytes

  for (int active = 0; active <= 1; active++) {
    for (int msb = 0; msb <= 1; msb++) {
      Aldl160Decoder d;
      d.activeLevel = active;
      d.msbFirst = msb;
      std::vector<int> bits;
      // Start mid-frame with junk to make sure it syncs properly.
      for (int i = 0; i < 13; i++) bits.push_back(i % 3 == 0);
      addFrame(bits, f1, msb);
      addFrame(bits, f2, msb);
      addFrame(bits, f1, msb);  // its end is only known when the next sync starts
      for (int i = 0; i < 9; i++) bits.push_back(1);
      std::vector<std::vector<uint8_t>> got;
      play(d, bits, active, got, 200);  // keep 0-pulses above the noise filter
      CHECK(got.size() == 3);
      if (got.size() == 3) {
        CHECK(got[0] == f1);
        CHECK(got[1] == f2);
        CHECK(got[2] == f1);
      }
      CHECK(d.badPulses == 0);
    }
  }

  // A long gap mid-frame must discard the partial frame.
  {
    Aldl160Decoder d;
    std::vector<int> bits;
    addFrame(bits, f1, true);
    std::vector<std::vector<uint8_t>> got;
    play(d, std::vector<int>(bits.begin(), bits.begin() + 40), 0, got, 0);
    d.feedPulse(1, 100000);  // line idle for 100ms
    CHECK(d.gapResets == 1);
    bits.clear();
    addFrame(bits, f2, true);
    for (int i = 0; i < 9; i++) bits.push_back(1);
    play(d, bits, 0, got, 0);
    CHECK(got.size() == 1 && got[0] == f2);
  }

  // Short glitches are ignored.
  {
    Aldl160Decoder d;
    std::vector<std::vector<uint8_t>> got;
    CHECK(!d.feedPulse(0, 20));
    CHECK(d.noisePulses == 1);
  }

  printf(failures ? "%d FAILURE(S)\n" : "all decoder tests passed\n", failures);
  return failures ? 1 : 0;
}
