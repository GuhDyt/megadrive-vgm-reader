# VGM analysis notes

The analyzer targets the Mega Drive/Genesis sound pair:

- Yamaha YM2612 / OPN2: six FM channels, four operators per channel.
- Texas Instruments SN76489-compatible PSG: three tone channels plus noise.

The VGM stream uses 44,100 samples per second for wait commands. Common commands handled include YM2612 writes (`0x52`, `0x53`), SN76489 writes (`0x50`), fixed and variable waits (`0x61`, `0x62`, `0x63`, `0x70`–`0x7F`), YM2612 DAC writes (`0x80`–`0x8F`), data blocks (`0x67`), and end (`0x66`). The parser skips common other-chip writes using their defined command lengths. It fails explicitly on unknown commands rather than desynchronizing the stream.

## Pitch formulas

YM2612 approximate fundamental:

`frequency_hz = f_number * ym2612_clock / (144 * 2 ** (20 - block))`

SN76489 tone:

`frequency_hz = sn76489_clock / (32 * period)`

These formulas yield the chip oscillator pitch. FM carrier spectra, modulation, detune, envelopes, DAC playback, and noise can make the perceived pitch or timbre differ from a simple MIDI note.
