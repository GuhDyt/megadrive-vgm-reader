---
name: megadrive-vgm-reader
description: Analyze Sega Mega Drive/Genesis VGM and VGZ music logs by decoding YM2612 FM and SN76489 PSG register writes into note events, timing, channel activity, and instrument parameters. Use whenever the user uploads or asks to inspect a .vgm or .vgz file, transcribe its notes, identify its musical structure, or explain its chiptune instrumentation.
---

# Mega Drive VGM Reader

You are analyzing a **sequence of sound-chip commands**, not a conventional recorded audio file. A Mega Drive `.vgm` contains timed register writes. Decode those writes before drawing conclusions about the music.

## Workflow

1. Locate the user's attached `.vgm` or `.vgz` file. Do not ask the user to convert it first.
2. Run `scripts/analyze_vgm.py` with Python 3. The script uses only the Python standard library; it does not need pip packages or internet access.
   - Example: `python scripts/analyze_vgm.py "/path/to/song.vgm" --outdir "/mnt/data/vgm-analysis"`
   - It creates `analysis.md`, `analysis.json`, and `transcription.mid` in the output folder.
3. Read `analysis.md` and inspect `analysis.json` when finer detail is needed. The JSON is the machine-readable source of truth for note events and patch snapshots. Do not infer musical details from a filename.
4. Explain the music in natural language. Use the decoded events to discuss melody, bass, accompaniment, rhythm, repetition, register, note lengths, channel roles, and changes in timbre. Distinguish direct observations from interpretations.
5. If the user requests a note transcription, provide note names and octave, grouped by channel/voice and in chronological order. Use the note-on/note-off times from the JSON. For very long tracks, give a representative section and clearly state the time range rather than silently truncating.
6. Treat `transcription.mid` as a **note-and-timing guide**, not an authentic Mega Drive sound reproduction. Standard MIDI cannot preserve the exact YM2612 FM patches, PSG noise, DAC playback, chip effects, or all pitch/envelope behavior.

## What the analyzer extracts

- VGM header information: version, duration, loop metadata, YM2612 clock, and SN76489 clock.
- YM2612: six FM channels; key-on/key-off events; pitch from F-number/block; algorithm and feedback; stereo pan; operator detune/multiplier, total level, and envelope-rate parameters.
- SN76489: three tone channels; tone periods; note and volume activity; a separate event stream for the noise channel.
- DAC activity: marks when YM2612 DAC mode and DAC data writes occur. It does not pretend to transcribe arbitrary PCM samples into notes.
- Outputs: a readable Markdown report, detailed JSON, and a multi-track MIDI note guide.

## Important interpretation rules

- Do not claim to have literally listened to the VGM. Say that you decoded the chip commands and reconstructed the musical events.
- Do not identify the song, game, composer, or source based only on GD3 tags, filenames, or embedded metadata. Ignore GD3 metadata unless the user explicitly asks about metadata.
- The YM2612 is a four-operator FM synthesizer. A patch's algorithm determines which operators are modulators and carriers. Describe parameter values accurately; avoid pretending that a numeric patch snapshot alone uniquely tells you the perceived instrument.
- The SN76489 contributes three square-wave tone channels and one noise channel. Noise is represented as activity/type/rate, not as a pitched melodic note.
- The YM2612 has six logical FM channels, but special channel-3 mode, DAC playback, rapid arpeggios, pitch modulation, and sound effects can make simple note transcription approximate.
- VGM waits are measured in 44.1 kHz samples. Convert sample positions to seconds using 44100 samples per second.
- VGM files can contain a loop point. The report describes the logged pass and reports loop metadata; do not assume the file itself contains multiple repeated passes.
- If a register stream is unsupported or malformed, report the limitation instead of guessing or silently skipping unknown commands.

## Suggested response style

Start with a compact overview: duration, active chips, number of voices, and the broad musical character supported by the event data. Then cover the most important channels and their roles. Include timestamps for specific examples. When a role such as “bass” or “lead” is inferred from pitch range and activity rather than encoded in the file, call it an interpretation.

If the user asks “what does this music sound like?”, describe the musical behavior from the extracted events and clearly distinguish that from an actual audio listen. If they need an audio render, explain that this Skill's built-in output is a note-level reconstruction; it does not claim a faithful WAV emulation.
