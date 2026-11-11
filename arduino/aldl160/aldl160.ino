// aldl160.ino - reads the 160 baud ALDL stream from a GM OBD1 ECM
// (e.g. the 1988 Pontiac Fiero 2.8 V6) and sends it to a computer over USB.
//
// Wiring (see ../../WIRING.md):  ALDL pin E -> 10k resistor -> Arduino D2
//                                ALDL pin A -> Arduino GND
//
// Output over USB serial at 115200 baud, one line per message:
//   D,<len>,<hex bytes>     a decoded data frame, e.g. D,20,0A1B...
//   P,<level>,<us>          a raw pulse (pulse mode only)
//   S,<stats>               error counters, once every 5 seconds
//   I,<text>                info
//
// Commands you can type in the Serial Monitor (or the Python app sends):
//   p  = pulse mode (print every raw pulse, for checking the signal)
//   d  = data mode (default, print decoded frames)
//   i  = invert: swap which line level counts as "active"
//   b  = swap bit order (MSB first <-> LSB first)
//   u  = toggle the internal pull-up resistor on D2

#include "aldl_decoder.h"

const uint8_t DATA_PIN = 2;   // must be an interrupt pin (D2 or D3 on Uno/Nano)

Aldl160Decoder decoder;

// Ring buffer filled by the interrupt with (level, duration) pairs.
const uint8_t RB_SIZE = 64;
volatile uint8_t rbLevel[RB_SIZE];
volatile uint32_t rbUs[RB_SIZE];
volatile uint8_t rbHead = 0, rbTail = 0;
volatile uint32_t lastEdgeUs = 0;
volatile uint32_t rbDropped = 0;

bool pulseMode = false;
bool pullup = false;
uint32_t lastStats = 0;
uint32_t framesOut = 0;

void onEdge() {
  uint32_t now = micros();
  uint8_t levelNow = digitalRead(DATA_PIN);
  uint8_t next = (uint8_t)((rbHead + 1) % RB_SIZE);
  if (next == rbTail) { rbDropped++; lastEdgeUs = now; return; }
  rbLevel[rbHead] = !levelNow;          // the level that just ENDED
  rbUs[rbHead] = now - lastEdgeUs;      // how long it lasted
  rbHead = next;
  lastEdgeUs = now;
}

void printHex(uint8_t b) {
  const char *h = "0123456789ABCDEF";
  Serial.write(h[b >> 4]);
  Serial.write(h[b & 0x0F]);
}

void printSettings() {
  Serial.print(F("I,active="));
  Serial.print(decoder.activeLevel ? F("high") : F("low"));
  Serial.print(F(" order="));
  Serial.print(decoder.msbFirst ? F("msb") : F("lsb"));
  Serial.print(F(" pullup="));
  Serial.print(pullup ? F("on") : F("off"));
  Serial.print(F(" mode="));
  Serial.println(pulseMode ? F("pulse") : F("data"));
}

void handleCommand(char c) {
  switch (c) {
    case 'p': pulseMode = true; break;
    case 'd': pulseMode = false; break;
    case 'i': decoder.activeLevel ^= 1; decoder.reset(); break;
    case 'b': decoder.msbFirst = !decoder.msbFirst; decoder.reset(); break;
    case 'u':
      pullup = !pullup;
      pinMode(DATA_PIN, pullup ? INPUT_PULLUP : INPUT);
      break;
    default: return;
  }
  printSettings();
}

void setup() {
  Serial.begin(115200);
  pinMode(DATA_PIN, INPUT);
  lastEdgeUs = micros();
  attachInterrupt(digitalPinToInterrupt(DATA_PIN), onEdge, CHANGE);
  Serial.println(F("I,aldl160 ready"));
  printSettings();
}

void loop() {
  while (Serial.available()) handleCommand((char)Serial.read());

  while (rbTail != rbHead) {
    noInterrupts();
    uint8_t level = rbLevel[rbTail];
    uint32_t us = rbUs[rbTail];
    rbTail = (uint8_t)((rbTail + 1) % RB_SIZE);
    interrupts();

    if (pulseMode) {
      Serial.print(F("P,"));
      Serial.print(level);
      Serial.print(',');
      Serial.println(us);
      continue;
    }
    if (decoder.feedPulse(level, us)) {
      framesOut++;
      Serial.print(F("D,"));
      Serial.print(decoder.frameLen);
      Serial.print(',');
      for (uint8_t i = 0; i < decoder.frameLen; i++) printHex(decoder.frame[i]);
      Serial.println();
    }
  }

  // If the line has been quiet for a long time, tell the decoder.
  noInterrupts();
  uint32_t quietUs = micros() - lastEdgeUs;
  interrupts();
  if (quietUs > 3 * decoder.bitPeriodUs) decoder.feedPulse(!decoder.activeLevel, quietUs);

  if (millis() - lastStats > 5000) {
    lastStats = millis();
    Serial.print(F("S,frames="));   Serial.print(framesOut);
    Serial.print(F(" noise="));     Serial.print(decoder.noisePulses);
    Serial.print(F(" bad="));       Serial.print(decoder.badPulses);
    Serial.print(F(" gaps="));      Serial.print(decoder.gapResets);
    Serial.print(F(" overflow="));  Serial.print(decoder.overflows);
    Serial.print(F(" dropped="));   Serial.println(rbDropped);
  }
}
