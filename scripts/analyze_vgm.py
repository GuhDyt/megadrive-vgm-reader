#!/usr/bin/env python3
"""Decode Mega Drive/Genesis VGM command logs into notes, JSON, Markdown and MIDI.

Standard-library only. Supports .vgm and gzip-compressed .vgz.
The transcription is note-level: it does not emulate YM2612 audio or preserve
FM timbre, PCM/DAC waveforms, or the SN76489 noise waveform.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import struct
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

SAMPLE_RATE = 44100
PPQ = 480
TEMPO_US = 500000
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


class VGMError(Exception):
    pass


def u16(buf: bytes, pos: int) -> int:
    if pos + 2 > len(buf):
        raise VGMError("Header truncado.")
    return struct.unpack_from("<H", buf, pos)[0]


def u32(buf: bytes, pos: int) -> int:
    if pos + 4 > len(buf):
        raise VGMError("Header truncado.")
    return struct.unpack_from("<I", buf, pos)[0]


def hz_to_midi(freq: float) -> float | None:
    if not math.isfinite(freq) or freq <= 0:
        return None
    return 69.0 + 12.0 * math.log2(freq / 440.0)


def midi_name(note: int) -> str:
    return f"{NOTE_NAMES[note % 12]}{note // 12 - 1}"


def midi_float_name(value: float | None) -> str | None:
    if value is None:
        return None
    note = int(round(value))
    cents = int(round((value - note) * 100))
    base = midi_name(note)
    return f"{base}{cents:+d}c" if abs(cents) >= 2 else base


def read_input(path: Path) -> bytes:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise VGMError(f"Não foi possível ler o arquivo: {exc}") from exc
    if path.suffix.lower() == ".vgz" or raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError) as exc:
            raise VGMError(f"Arquivo VGZ/GZip inválido: {exc}") from exc
    if len(raw) < 0x40 or raw[:4] != b"Vgm ":
        raise VGMError("Assinatura Vgm não encontrada. O arquivo não parece ser um VGM válido.")
    return raw


def parse_header(data: bytes) -> dict[str, Any]:
    version = u32(data, 0x08)
    eof_rel = u32(data, 0x04)
    eof_abs = 0x04 + eof_rel if eof_rel else len(data)
    eof_abs = min(eof_abs, len(data))
    gd3_rel = u32(data, 0x14)
    loop_rel = u32(data, 0x1C)
    total_samples = u32(data, 0x18)
    loop_samples = u32(data, 0x20)
    sn_clock = u32(data, 0x0C)
    ym_clock = u32(data, 0x2C)
    data_rel = u32(data, 0x34) if version >= 0x150 and len(data) >= 0x38 else 0
    data_start = (0x34 + data_rel) if data_rel else 0x40
    if data_start < 0x40 or data_start > eof_abs:
        raise VGMError(f"Offset inicial dos comandos inválido: 0x{data_start:X}.")
    return {
        "version": f"{version >> 8}.{version & 0xFF:02X}",
        "version_raw": version,
        "eof_offset": eof_abs,
        "data_start": data_start,
        "total_samples_header": total_samples,
        "loop_samples_header": loop_samples,
        "loop_offset_absolute": (0x1C + loop_rel) if loop_rel else None,
        "loop_present": bool(loop_rel),
        "ym2612_clock_hz": ym_clock & 0x3FFFFFFF,
        "ym2612_dual_chip_flag": bool(ym_clock & 0x40000000),
        "sn76489_clock_hz": sn_clock & 0x3FFFFFFF,
        "sn76489_dual_chip_flag": bool(sn_clock & 0x40000000),
        "command_stream_bytes": max(0, eof_abs - data_start),
        "gd3_present": bool(gd3_rel),
    }


class Analyzer:
    def __init__(self, data: bytes, header: dict[str, Any], max_events: int):
        self.data = data
        self.header = header
        self.max_events = max_events
        self.samples = 0
        self.register_writes = 0
        self.ym_notes: list[dict[str, Any]] = []
        self.psg_notes: list[dict[str, Any]] = []
        self.noise_events: list[dict[str, Any]] = []
        self.dac_events: list[dict[str, Any]] = []
        self.regs: dict[tuple[int, int], int] = {}
        self.fm_active: dict[int, dict[str, Any]] = {}
        self.fm_masks: dict[int, int] = defaultdict(int)
        self.ym_pitch_pending: set[int] = set()
        self.psg_regs = [0x400, 0x400, 0x400, 0]
        self.psg_period_initialized = [False, False, False]
        self.psg_tone_pending: set[int] = set()
        self.psg_vol = [15, 15, 15, 15]
        self.psg_latch = 0
        self.psg_active: dict[int, dict[str, Any]] = {}
        self.noise_active: dict[str, Any] | None = None
        self.dac_enabled = False
        self.dac_write_count = 0
        self.warnings: list[str] = []
        self.command_count = 0

    def _push(self, collection: list, event: dict[str, Any]) -> None:
        if len(self.ym_notes) + len(self.psg_notes) + len(self.noise_events) + len(self.dac_events) >= self.max_events:
            raise VGMError(f"Limite de segurança de {self.max_events} eventos atingido. Use --max-events para aumentar.")
        collection.append(event)

    def _freq_ym(self, channel: int) -> tuple[float | None, int, int]:
        bank = channel // 3
        ch = channel % 3
        lo = self.regs.get((bank, 0xA0 + ch), 0)
        hi = self.regs.get((bank, 0xA4 + ch), 0)
        fnum = ((hi & 0x07) << 8) | lo
        block = (hi >> 3) & 0x07
        clock = self.header["ym2612_clock_hz"]
        if not fnum or not clock:
            return None, fnum, block
        freq = fnum * clock / (144.0 * (2 ** (20 - block)))
        return freq, fnum, block

    def _fm_patch(self, channel: int) -> dict[str, Any]:
        bank = channel // 3
        ch = channel % 3
        b0 = self.regs.get((bank, 0xB0 + ch), 0)
        b4 = self.regs.get((bank, 0xB4 + ch), 0)
        operators = []
        for slot_index, slot in enumerate((0, 4, 8, 12), 1):
            def get(group: int) -> int:
                return self.regs.get((bank, group + slot + ch), 0)
            dtmul = get(0x30)
            operators.append({
                "operator": slot_index,
                "detune": (dtmul >> 4) & 0x07,
                "multiple": dtmul & 0x0F,
                "total_level": get(0x40) & 0x7F,
                "rate_scale": (get(0x50) >> 6) & 0x03,
                "attack_rate": get(0x50) & 0x1F,
                "am_enabled": bool(get(0x60) & 0x80),
                "decay_rate": get(0x60) & 0x1F,
                "second_decay_rate": get(0x70) & 0x1F,
                "sustain_level": (get(0x80) >> 4) & 0x0F,
                "release_rate": get(0x80) & 0x0F,
                "ssg_eg": get(0x90) & 0x0F,
            })
        return {
            "algorithm": b0 & 0x07,
            "feedback": (b0 >> 3) & 0x07,
            "pan_left": bool(b4 & 0x80),
            "pan_right": bool(b4 & 0x40),
            "ams": b4 & 0x03,
            "pms": (b4 >> 4) & 0x07,
            "operators": operators,
        }

    def _start_fm(self, channel: int, operator_mask: int) -> None:
        freq, fnum, block = self._freq_ym(channel)
        midi_float = hz_to_midi(freq) if freq else None
        note = int(round(midi_float)) if midi_float is not None else None
        patch = self._fm_patch(channel)
        self.fm_active[channel] = {
            "start_sample": self.samples,
            "frequency_hz": round(freq, 4) if freq else None,
            "midi_float": round(midi_float, 4) if midi_float is not None else None,
            "midi_note": note,
            "note_name": midi_name(note) if note is not None and 0 <= note <= 127 else None,
            "f_number": fnum,
            "block": block,
            "operator_mask": operator_mask,
            "patch": patch,
        }

    def _close_fm(self, channel: int, reason: str = "key_off") -> None:
        active = self.fm_active.pop(channel, None)
        if active is None:
            return
        start = active["start_sample"]
        active.update({
            "end_sample": self.samples,
            "duration_seconds": round(max(0, self.samples - start) / SAMPLE_RATE, 6),
            "end_reason": reason,
            "channel": channel + 1,
        })
        self._push(self.ym_notes, active)

    def _ym_write(self, bank: int, reg: int, value: int) -> None:
        self.register_writes += 1
        self.regs[(bank, reg)] = value
        if reg == 0x2B and bank == 0:
            self.dac_enabled = bool(value & 0x80)
        elif reg == 0x2A and bank == 0 and self.dac_enabled:
            self.dac_write_count += 1
            if self.dac_write_count == 1 or self.dac_write_count % 2048 == 0:
                self._push(self.dac_events, {
                    "sample": self.samples,
                    "time_seconds": round(self.samples / SAMPLE_RATE, 6),
                    "kind": "dac_data_write",
                    "value": value,
                    "write_index": self.dac_write_count,
                })
        elif reg == 0x28 and bank == 0:
            ch_code = value & 0x03
            bank_select = (value >> 2) & 1
            if ch_code == 3:
                return
            channel = bank_select * 3 + ch_code
            mask = (value >> 4) & 0x0F
            old_mask = self.fm_masks[channel]
            self.fm_masks[channel] = mask
            if mask == 0:
                self._close_fm(channel)
            elif old_mask == 0:
                self._start_fm(channel, mask)
            elif mask != old_mask and channel in self.fm_active:
                # Operator gating can change during a sustained note; preserve one
                # event but record the current mask as a controller-like update.
                self.fm_active[channel]["operator_mask"] = mask
        elif (0xA0 <= reg <= 0xA2) or (0xA4 <= reg <= 0xA6):
            ch_code = reg & 0x03
            if ch_code >= 3:
                return
            channel = bank * 3 + ch_code
            if channel in self.fm_active:
                # Commit after all same-sample A0/A4 writes have landed.
                self.ym_pitch_pending.add(channel)

    def _psg_start(self, channel: int) -> None:
        if channel < 3:
            period = self.psg_regs[channel] or 0x400
            clock = self.header["sn76489_clock_hz"]
            freq = clock / (32.0 * period) if clock else None
            midi_float = hz_to_midi(freq) if freq else None
            note = int(round(midi_float)) if midi_float is not None else None
            self.psg_active[channel] = {
                "start_sample": self.samples,
                "period": period,
                "frequency_hz": round(freq, 4) if freq else None,
                "midi_float": round(midi_float, 4) if midi_float is not None else None,
                "midi_note": note,
                "note_name": midi_name(note) if note is not None and 0 <= note <= 127 else None,
                "volume_attenuation": self.psg_vol[channel],
                "velocity": max(1, min(127, round(127 * (10 ** (-(self.psg_vol[channel] * 2) / 20.0))))) if self.psg_vol[channel] < 15 else 0,
            }

    def _psg_close(self, channel: int, reason: str = "volume_off") -> None:
        active = self.psg_active.pop(channel, None)
        if active is None:
            return
        start = active["start_sample"]
        active.update({
            "end_sample": self.samples,
            "duration_seconds": round(max(0, self.samples - start) / SAMPLE_RATE, 6),
            "end_reason": reason,
            "channel": channel + 1,
        })
        self._push(self.psg_notes, active)

    def _psg_refresh_tone(self, channel: int) -> None:
        # Tone registers are often written low-byte/high-bits at the same VGM
        # timestamp. Defer note reconstruction until the next wait boundary.
        self.psg_tone_pending.add(channel)

    def _commit_pending_registers(self) -> None:
        for channel in sorted(self.ym_pitch_pending):
            active = self.fm_active.get(channel)
            if not active:
                continue
            freq, _, _ = self._freq_ym(channel)
            new_midi = hz_to_midi(freq) if freq else None
            old_midi = active.get("midi_float")
            if new_midi is not None and old_midi is not None and abs(new_midi - old_midi) >= 0.45:
                mask = self.fm_masks[channel]
                self._close_fm(channel, reason="pitch_change")
                self._start_fm(channel, mask)
        self.ym_pitch_pending.clear()

        for channel in sorted(self.psg_tone_pending):
            new_period = self.psg_regs[channel] or 0x400
            if channel in self.psg_active and self.psg_active[channel]["period"] != new_period:
                self._psg_close(channel, reason="period_change")
            if channel not in self.psg_active and self.psg_period_initialized[channel] and self.psg_vol[channel] < 15:
                self._psg_start(channel)
        self.psg_tone_pending.clear()

    def _noise_refresh(self) -> None:
        control = self.psg_regs[3] & 0x07
        attenuation = self.psg_vol[3]
        audible = attenuation < 15
        if audible and self.noise_active is None:
            self.noise_active = {
                "start_sample": self.samples,
                "noise_control": control,
                "noise_type": "white" if (control & 0x04) else "periodic",
                "frequency_mode": control & 0x03,
                "volume_attenuation": attenuation,
            }
        elif not audible and self.noise_active is not None:
            self._close_noise("volume_off")
        elif audible and self.noise_active is not None and self.noise_active["noise_control"] != control:
            self._close_noise("noise_mode_change")
            self._noise_refresh()

    def _close_noise(self, reason: str) -> None:
        if self.noise_active is None:
            return
        event = self.noise_active
        self.noise_active = None
        start = event["start_sample"]
        event.update({
            "end_sample": self.samples,
            "duration_seconds": round(max(0, self.samples - start) / SAMPLE_RATE, 6),
            "end_reason": reason,
            "time_seconds": round(start / SAMPLE_RATE, 6),
        })
        self._push(self.noise_events, event)

    def _psg_write(self, value: int) -> None:
        self.register_writes += 1
        if value & 0x80:
            reg = (value >> 4) & 0x07
            self.psg_latch = reg
            channel = reg // 2
            is_volume = reg & 1
            if is_volume:
                self.psg_vol[channel] = value & 0x0F
                if channel < 3:
                    if self.psg_vol[channel] >= 15:
                        self._psg_close(channel)
                    elif channel in self.psg_active:
                        self.psg_active[channel]["volume_attenuation"] = self.psg_vol[channel]
                        self.psg_active[channel]["velocity"] = max(1, round(127 * (10 ** (-(self.psg_vol[channel] * 2) / 20.0))))
                    elif self.psg_period_initialized[channel]:
                        self._psg_start(channel)
                else:
                    self._noise_refresh()
            elif channel < 3:
                self.psg_regs[channel] = (self.psg_regs[channel] & 0x3F0) | (value & 0x0F)
                self.psg_period_initialized[channel] = True
                self._psg_refresh_tone(channel)
            else:
                self.psg_regs[3] = value & 0x07
                self._noise_refresh()
        else:
            reg = self.psg_latch
            channel = reg // 2
            is_volume = reg & 1
            if is_volume:
                self.psg_vol[channel] = value & 0x0F
                if channel < 3:
                    if self.psg_vol[channel] >= 15:
                        self._psg_close(channel)
                    elif channel in self.psg_active:
                        self.psg_active[channel]["volume_attenuation"] = self.psg_vol[channel]
                        self.psg_active[channel]["velocity"] = max(1, round(127 * (10 ** (-(self.psg_vol[channel] * 2) / 20.0))))
                    elif self.psg_period_initialized[channel]:
                        self._psg_start(channel)
                else:
                    self._noise_refresh()
            elif channel < 3:
                self.psg_regs[channel] = ((value & 0x3F) << 4) | (self.psg_regs[channel] & 0x0F)
                self.psg_period_initialized[channel] = True
                self._psg_refresh_tone(channel)
            else:
                self.psg_regs[3] = value & 0x07
                self._noise_refresh()

    def _wait(self, count: int) -> None:
        self._commit_pending_registers()
        self.samples += max(0, count)

    def parse(self) -> None:
        pos = self.header["data_start"]
        end = self.header["eof_offset"]
        data = self.data
        while pos < end:
            cmd = data[pos]
            pos += 1
            self.command_count += 1
            if cmd == 0x66:
                break
            if cmd == 0x52 or cmd == 0x53:
                if pos + 2 > end:
                    raise VGMError("Comando YM2612 truncado no fim do arquivo.")
                reg, value = data[pos], data[pos + 1]
                pos += 2
                self._ym_write(cmd - 0x52, reg, value)
            elif cmd == 0x50:
                if pos >= end:
                    raise VGMError("Comando SN76489 truncado no fim do arquivo.")
                self._psg_write(data[pos])
                pos += 1
            elif cmd == 0x61:
                if pos + 2 > end:
                    raise VGMError("Comando de espera truncado.")
                self._wait(u16(data, pos))
                pos += 2
            elif cmd == 0x62:
                self._wait(735)
            elif cmd == 0x63:
                self._wait(882)
            elif 0x70 <= cmd <= 0x7F:
                self._wait((cmd & 0x0F) + 1)
            elif 0x80 <= cmd <= 0x8F:
                if self.dac_enabled:
                    self.dac_write_count += 1
                    if self.dac_write_count == 1 or self.dac_write_count % 2048 == 0:
                        self._push(self.dac_events, {
                            "sample": self.samples,
                            "time_seconds": round(self.samples / SAMPLE_RATE, 6),
                            "kind": "dac_stream_write",
                            "write_index": self.dac_write_count,
                        })
                self._wait(cmd & 0x0F)
            elif cmd == 0x67:
                if pos + 6 > end:
                    raise VGMError("Bloco de dados truncado.")
                if data[pos] != 0x66:
                    raise VGMError("Marcador 0x66 ausente após comando de bloco de dados 0x67.")
                size = u32(data, pos + 2)
                pos += 6
                if size > end - pos:
                    raise VGMError("Tamanho do bloco de dados ultrapassa o fim do arquivo.")
                pos += size
            elif cmd == 0x68:
                # Data transfer to chip RAM (fixed 11-byte payload after opcode).
                pos += 11
            elif cmd == 0x64:
                pos += 3
            elif 0x90 <= cmd <= 0x95:
                lengths = {0x90: 4, 0x91: 4, 0x92: 5, 0x93: 10, 0x94: 1, 0x95: 4}
                pos += lengths[cmd]
            elif cmd in (0x30, 0x4F):
                pos += 1
            elif cmd == 0x31:
                pos += 2
            elif 0x51 <= cmd <= 0x5F:
                pos += 2
            elif 0xA0 <= cmd <= 0xBF:
                pos += 2
            elif 0xC0 <= cmd <= 0xDF:
                pos += 3
            elif cmd == 0xE0:
                pos += 4
            else:
                raise VGMError(f"Comando VGM não suportado 0x{cmd:02X} no offset 0x{pos - 1:X}. A análise foi interrompida para evitar dessincronizar o arquivo.")
            if pos > end:
                raise VGMError(f"Comando 0x{cmd:02X} ultrapassa o fim do arquivo.")
        # Commit final same-timestamp register batches before closing active notes.
        self._commit_pending_registers()
        # Close sustained notes at the final logged sample.
        for channel in list(self.fm_active):
            self._close_fm(channel, reason="end_of_log")
        for channel in list(self.psg_active):
            self._psg_close(channel, reason="end_of_log")
        self._close_noise("end_of_log")


def midi_vlq(value: int) -> bytes:
    value = max(0, int(value))
    buffer = value & 0x7F
    out = bytearray()
    while True:
        value >>= 7
        if value:
            buffer <<= 8
            buffer |= ((value & 0x7F) | 0x80)
        else:
            break
    while True:
        out.append(buffer & 0xFF)
        if buffer & 0x80:
            buffer >>= 8
        else:
            break
    return bytes(out)


def samples_to_ticks(sample: int) -> int:
    return round(sample * PPQ * 1_000_000 / (SAMPLE_RATE * TEMPO_US))


def make_track(name: str, channel: int, notes: list[dict[str, Any]], fm: bool) -> bytes:
    events: list[tuple[int, int, bytes]] = []
    name_b = name.encode("utf-8")
    events.append((0, -3, b"\xFF\x03" + midi_vlq(len(name_b)) + name_b))
    guide = b"Note timing guide only; original chip timbre is not embedded."
    events.append((0, -2, b"\xFF\x06" + midi_vlq(len(guide)) + guide))
    for n in notes:
        pitch = n.get("midi_note")
        if pitch is None or not (0 <= int(pitch) <= 127):
            continue
        start = samples_to_ticks(n["start_sample"])
        finish = max(samples_to_ticks(n["end_sample"]), start + 1)
        velocity = 96 if fm else int(n.get("velocity", 90))
        velocity = max(1, min(127, velocity))
        events.append((start, 1, bytes([0x90 | channel, int(pitch), velocity])))
        events.append((max(start, finish), 0, bytes([0x80 | channel, int(pitch), 0])))
    events.sort(key=lambda x: (x[0], x[1]))
    payload = bytearray()
    last_tick = 0
    for tick, _, msg in events:
        payload += midi_vlq(tick - last_tick)
        payload += msg
        last_tick = tick
    payload += b"\x00\xFF\x2F\x00"
    return b"MTrk" + struct.pack(">I", len(payload)) + payload


def write_midi(path: Path, ym_notes: list[dict[str, Any]], psg_notes: list[dict[str, Any]]) -> None:
    tracks = []
    for ch in range(6):
        tracks.append(make_track(f"YM2612 FM {ch + 1} - note guide", ch, [n for n in ym_notes if n["channel"] == ch + 1], True))
    for ch in range(3):
        tracks.append(make_track(f"SN76489 PSG {ch + 1} - note guide", ch + 6, [n for n in psg_notes if n["channel"] == ch + 1], False))
    header = b"MThd" + struct.pack(">IHHH", 6, 1, len(tracks), PPQ)
    path.write_bytes(header + b"".join(tracks))


def summarize_notes(notes: list[dict[str, Any]], channels: int, label: str) -> list[dict[str, Any]]:
    result = []
    for ch in range(1, channels + 1):
        group = [n for n in notes if n["channel"] == ch]
        pitch = [n["midi_note"] for n in group if n.get("midi_note") is not None and 0 <= n["midi_note"] <= 127]
        names = Counter(midi_name(int(p)) for p in pitch)
        result.append({
            "channel": ch,
            "voice_type": label,
            "note_count": len(group),
            "valid_pitched_note_count": len(pitch),
            "lowest_note": midi_name(min(pitch)) if pitch else None,
            "highest_note": midi_name(max(pitch)) if pitch else None,
            "most_common_notes": [{"note": k, "count": v} for k, v in names.most_common(8)],
            "active_seconds": round(sum(n["duration_seconds"] for n in group), 4),
        })
    return result


def patch_summary(notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for n in notes:
        patch = n.get("patch")
        if not patch:
            continue
        key = json.dumps(patch, sort_keys=True)
        if key not in seen:
            seen[key] = {"patch_id": f"FM-{len(seen) + 1:02d}", "patch": patch, "note_count": 0, "channels": set()}
        seen[key]["note_count"] += 1
        seen[key]["channels"].add(n["channel"])
        n["patch_id"] = seen[key]["patch_id"]
    return [{**v, "channels": sorted(v["channels"])} for v in seen.values()]


def make_report(header: dict[str, Any], analyzer: Analyzer, fm_summary: list[dict[str, Any]], psg_summary: list[dict[str, Any]], patches: list[dict[str, Any]]) -> str:
    duration_samples = analyzer.samples
    duration = duration_samples / SAMPLE_RATE
    lines = [
        "# Análise de VGM — Mega Drive / Genesis",
        "",
        "## Resumo",
        f"- Versão VGM: {header['version']}",
        f"- Duração do trecho registrado: {duration:.3f} s ({duration_samples:,} amostras a 44.100 Hz)",
        f"- YM2612 clock: {header['ym2612_clock_hz']:,} Hz" if header["ym2612_clock_hz"] else "- YM2612: clock não informado",
        f"- SN76489 clock: {header['sn76489_clock_hz']:,} Hz" if header["sn76489_clock_hz"] else "- SN76489: clock não informado",
        f"- Comandos lidos: {analyzer.command_count:,}; escritas de registrador: {analyzer.register_writes:,}",
        f"- Eventos de nota FM: {len(analyzer.ym_notes):,}; notas PSG tonais: {len(analyzer.psg_notes):,}",
        f"- Eventos de ruído PSG: {len(analyzer.noise_events):,}; amostras/escritas DAC registradas: {analyzer.dac_write_count:,}",
        f"- Loop presente no cabeçalho: {'sim' if header['loop_present'] else 'não'}" + (f" ({header['loop_samples_header']:,} amostras de loop declaradas)" if header['loop_present'] else ""),
        "",
        "> Esta é uma reconstrução a partir dos comandos dos chips, não uma escuta direta do áudio. A transcrição MIDI representa notas e tempos aproximados; não reproduz o timbre FM original, o ruído PSG nem o áudio DAC.",
        "",
        "## Canais YM2612 (FM)",
        "",
    ]
    for c in fm_summary:
        lines.append(f"- **FM {c['channel']}**: {c['note_count']} eventos; faixa {c['lowest_note'] or 'indeterminada'}–{c['highest_note'] or 'indeterminada'}; notas mais frequentes: " + (", ".join(f"{x['note']} ({x['count']})" for x in c["most_common_notes"][:5]) or "nenhuma nota tonal detectada") + ".")
    lines += ["", "## Canais SN76489 (PSG)", ""]
    for c in psg_summary:
        lines.append(f"- **PSG {c['channel']}**: {c['note_count']} eventos; faixa {c['lowest_note'] or 'indeterminada'}–{c['highest_note'] or 'indeterminada'}; notas mais frequentes: " + (", ".join(f"{x['note']} ({x['count']})" for x in c["most_common_notes"][:5]) or "nenhuma nota tonal detectada") + ".")
    lines += ["", "## Timbres FM encontrados", ""]
    if not patches:
        lines.append("Nenhum snapshot de patch foi capturado em eventos de nota FM.")
    for p in patches:
        patch = p["patch"]
        op_desc = []
        for op in patch["operators"]:
            op_desc.append(f"OP{op['operator']}: MUL {op['multiple']}, DT {op['detune']}, TL {op['total_level']}, AR {op['attack_rate']}, D1R {op['decay_rate']}, D2R {op['second_decay_rate']}, RR {op['release_rate']}")
        lines.append(f"- **{p['patch_id']}** — algoritmo {patch['algorithm']}, feedback {patch['feedback']}, canal(is) {', '.join(map(str, p['channels']))}, usado em {p['note_count']} notas. " + "; ".join(op_desc) + ".")
    lines += ["", "## Ruído PSG", ""]
    if analyzer.noise_events:
        for e in analyzer.noise_events[:20]:
            lines.append(f"- {e['time_seconds']:.3f}s, duração {e['duration_seconds']:.3f}s: {e['noise_type']} noise, modo {e['frequency_mode']}, atenuação {e['volume_attenuation']}/15.")
        if len(analyzer.noise_events) > 20:
            lines.append(f"- ... e mais {len(analyzer.noise_events) - 20} eventos (ver JSON).")
    else:
        lines.append("Nenhuma atividade tonal/noise PSG detectada no canal de ruído.")
    lines += ["", "## Observações e limites", "", "- A função de cada canal (melodia, baixo, acompanhamento, efeitos) deve ser inferida a partir de faixa, ritmo e comportamento; o arquivo não rotula esses papéis.", "- Pitch bends, vibrato, modo especial do canal 3, key-ons parciais, DAC e efeitos podem não virar notas MIDI fiéis.", "- O arquivo MIDI é uma guia de notas e duração, não uma conversão do som do Mega Drive para timbres General MIDI."]
    if analyzer.warnings:
        lines += ["", "## Avisos", ""] + [f"- {w}" for w in analyzer.warnings]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Analisa VGM/VGZ do Mega Drive e gera JSON, Markdown e MIDI.")
    ap.add_argument("input", type=Path, help="Arquivo .vgm ou .vgz")
    ap.add_argument("--outdir", type=Path, default=None, help="Pasta de saída (padrão: <nome>_vgm_analysis)")
    ap.add_argument("--max-events", type=int, default=500000, help="Limite de eventos extraídos (padrão: 500000)")
    args = ap.parse_args()
    if args.max_events < 1:
        ap.error("--max-events precisa ser maior que zero")
    try:
        data = read_input(args.input)
        header = parse_header(data)
        analyzer = Analyzer(data, header, args.max_events)
        analyzer.parse()
        fm_summary = summarize_notes(analyzer.ym_notes, 6, "YM2612 FM")
        psg_summary = summarize_notes(analyzer.psg_notes, 3, "SN76489 PSG")
        patches = patch_summary(analyzer.ym_notes)
        outdir = args.outdir or args.input.with_name(args.input.stem + "_vgm_analysis")
        outdir.mkdir(parents=True, exist_ok=True)
        # Add absolute time to each event for easy reading by an AI assistant.
        for event in analyzer.ym_notes + analyzer.psg_notes:
            event["start_seconds"] = round(event["start_sample"] / SAMPLE_RATE, 6)
            event["end_seconds"] = round(event["end_sample"] / SAMPLE_RATE, 6)
        result = {
            "format": "Mega Drive/Genesis VGM note-level analysis",
            "source_file_name": args.input.name,
            "header": {k: v for k, v in header.items() if k not in ("eof_offset", "data_start")},
            "analysis": {
                "sample_rate_hz": SAMPLE_RATE,
                "duration_seconds": round(analyzer.samples / SAMPLE_RATE, 6),
                "command_count": analyzer.command_count,
                "register_write_count": analyzer.register_writes,
                "ym2612_note_count": len(analyzer.ym_notes),
                "sn76489_tone_note_count": len(analyzer.psg_notes),
                "sn76489_noise_event_count": len(analyzer.noise_events),
                "ym2612_dac_write_count": analyzer.dac_write_count,
            },
            "ym2612_channels": fm_summary,
            "sn76489_channels": psg_summary,
            "fm_patches": patches,
            "ym2612_notes": analyzer.ym_notes,
            "sn76489_tone_notes": analyzer.psg_notes,
            "sn76489_noise_events": analyzer.noise_events,
            "dac_activity_samples": analyzer.dac_events,
            "warnings": analyzer.warnings,
        }
        json_path = outdir / "analysis.json"
        report_path = outdir / "analysis.md"
        midi_path = outdir / "transcription.mid"
        json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        report_path.write_text(make_report(header, analyzer, fm_summary, psg_summary, patches), encoding="utf-8")
        write_midi(midi_path, analyzer.ym_notes, analyzer.psg_notes)
        print(f"Análise concluída: {args.input.name}")
        print(f"Duração: {analyzer.samples / SAMPLE_RATE:.3f} s")
        print(f"Notas YM2612: {len(analyzer.ym_notes)} | notas PSG: {len(analyzer.psg_notes)} | eventos de ruído: {len(analyzer.noise_events)}")
        print(f"Relatório: {report_path}")
        print(f"JSON: {json_path}")
        print(f"MIDI: {midi_path}")
        return 0
    except VGMError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ERRO de arquivo: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
