"""零依赖（只用 numpy + 标准库）的小型作曲/合成工具。

音符串语法（空白分隔）::

    C4            四分音符 C4（默认 1 拍）
    D#5:0.5       半拍升 D5；降号写 b：Eb4
    R:2           休止 2 拍
    [C4 E4 G4]:2  和弦，2 拍
    Cmaj7:4       和弦名（根音默认 4 组），4 拍

快速上手::

    from yawnkit import music
    song = music.Song(bpm=96)
    song.track("C4 E4 G4 C5:2 B4 G4 E4:2", instrument="piano")
    song.track(music.progression("C G Am F", beats=4, octave=3), instrument="pad", volume=0.5)
    song.drums(kick="x...x...x...x...", snare="....x.......x...", hat="x.x.x.x.x.x.x.x.", bars=4)
    song.save("song.wav", midi="song.mid")
"""

from __future__ import annotations

import re
import struct
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

SAMPLE_RATE = 44100
MAX_SECONDS = 180
_NOTE_OFFSETS = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_NOTE_PATTERN = re.compile(r"^([A-Ga-g])([#b]?)(-?\d)?$")
_CHORD_PATTERN = re.compile(r"^([A-G])([#b]?)(maj7|m7|7|dim|aug|sus2|sus4|m|maj)?$")
_CHORD_INTERVALS = {
    "": (0, 4, 7),
    "maj": (0, 4, 7),
    "m": (0, 3, 7),
    "7": (0, 4, 7, 10),
    "maj7": (0, 4, 7, 11),
    "m7": (0, 3, 7, 10),
    "dim": (0, 3, 6),
    "aug": (0, 4, 8),
    "sus2": (0, 2, 7),
    "sus4": (0, 5, 7),
}
SCALES = {
    "major": (0, 2, 4, 5, 7, 9, 11),
    "minor": (0, 2, 3, 5, 7, 8, 10),
    "pentatonic": (0, 2, 4, 7, 9),
    "minor_pentatonic": (0, 3, 5, 7, 10),
    "blues": (0, 3, 5, 6, 7, 10),
    "dorian": (0, 2, 3, 5, 7, 9, 10),
    "chinese": (0, 2, 4, 7, 9),  # 宫商角徵羽
}
INSTRUMENTS = ("sine", "triangle", "square", "saw", "piano", "pluck", "bell", "pad", "bass")


def note_to_midi(name: str, default_octave: int = 4) -> int:
    """``C4`` → 60，``A4`` → 69，``Eb3`` → 51。"""

    match = _NOTE_PATTERN.match(name.strip())
    if not match:
        raise ValueError(f"无法识别的音符: {name!r}")
    letter, accidental, octave = match.groups()
    value = _NOTE_OFFSETS[letter.upper()]
    value += 1 if accidental == "#" else -1 if accidental == "b" else 0
    octave_number = int(octave) if octave is not None else default_octave
    return 12 * (octave_number + 1) + value


def midi_to_freq(midi: int) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12)


def chord(name: str, octave: int = 4) -> list[int]:
    """和弦名 → MIDI 音高列表：``chord("Am7", 3)``。"""

    match = _CHORD_PATTERN.match(name.strip())
    if not match:
        raise ValueError(f"无法识别的和弦: {name!r}")
    letter, accidental, quality = match.groups()
    root = note_to_midi(f"{letter}{accidental}{octave}")
    return [root + interval for interval in _CHORD_INTERVALS[quality or ""]]


def scale(root: str = "C4", kind: str = "major", octaves: int = 1) -> list[int]:
    base = note_to_midi(root)
    steps = SCALES[kind]
    return [base + 12 * octave + step for octave in range(octaves) for step in steps] + [
        base + 12 * octaves
    ]


def progression(chords: str, *, beats: float = 4, octave: int = 3) -> str:
    """``progression("C G Am F")`` → 可直接传给 ``Song.track`` 的和弦音符串。"""

    return " ".join(f"{name}@{octave}:{beats:g}" for name in chords.split())


@dataclass(slots=True)
class Event:
    start: float  # 拍
    length: float  # 拍
    pitches: tuple[int, ...]  # 空元组 = 休止
    velocity: float = 0.8


def parse(sequence: str | list, *, default_octave: int = 4) -> list[Event]:
    """把音符串（或 ``[(音符, 拍数), ...]`` 列表）解析成事件。"""

    events: list[Event] = []
    position = 0.0
    if isinstance(sequence, list):
        tokens = [f"{item[0]}:{item[1]}" if isinstance(item, tuple) else str(item) for item in sequence]
    else:
        # 和弦方括号里的空格先换成逗号，便于按空白切分。
        tokens = re.sub(r"\[([^\]]*)\]", lambda m: "[" + ",".join(m.group(1).split()) + "]", sequence).split()
    for token in tokens:
        body, _, length_text = token.partition(":")
        length = float(length_text) if length_text else 1.0
        if length <= 0:
            raise ValueError(f"时值必须为正: {token!r}")
        if body.upper() in {"R", "REST", "-"}:
            pitches: tuple[int, ...] = ()
        elif body.startswith("["):
            pitches = tuple(
                note_to_midi(item, default_octave) for item in body.strip("[]").split(",") if item
            )
        elif "@" in body:
            name, _, octave = body.partition("@")
            pitches = tuple(chord(name, int(octave)))
        elif _CHORD_PATTERN.match(body) and not _NOTE_PATTERN.match(body):
            pitches = tuple(chord(body, default_octave))
        else:
            pitches = (note_to_midi(body, default_octave),)
        events.append(Event(position, length, pitches))
        position += length
    return events


def _envelope(count: int, rate: int, attack: float, decay: float, sustain: float, release: float) -> np.ndarray:
    env = np.full(count, sustain, dtype=np.float32)
    a = min(int(attack * rate), count)
    d = min(int(decay * rate), max(count - a, 0))
    r = min(int(release * rate), count)
    if a:
        env[:a] = np.linspace(0, 1, a, endpoint=False)
    if d:
        env[a : a + d] = np.linspace(1, sustain, d, endpoint=False)
    if r:
        env[-r:] *= np.linspace(1, 0, r)
    return env


def _voice(freq: float, seconds: float, instrument: str, rate: int) -> np.ndarray:
    count = max(int(seconds * rate), 1)
    t = np.arange(count, dtype=np.float32) / rate
    phase = 2 * np.pi * freq * t
    if instrument == "sine":
        return np.sin(phase) * _envelope(count, rate, 0.01, 0.05, 0.9, 0.05)
    if instrument == "triangle":
        wave_ = 2 / np.pi * np.arcsin(np.sin(phase))
        return wave_ * _envelope(count, rate, 0.01, 0.05, 0.85, 0.05)
    if instrument == "square":
        return 0.5 * np.sign(np.sin(phase)) * _envelope(count, rate, 0.005, 0.05, 0.7, 0.04)
    if instrument == "saw":
        wave_ = 2 * (t * freq - np.floor(0.5 + t * freq))
        return 0.6 * wave_ * _envelope(count, rate, 0.005, 0.1, 0.7, 0.05)
    if instrument == "bass":
        wave_ = np.sin(phase) + 0.3 * np.sin(2 * phase)
        return wave_ * _envelope(count, rate, 0.005, 0.15, 0.6, 0.05)
    if instrument == "piano":
        harmonics = sum(np.sin(k * phase) * (0.6**k) for k in range(1, 6))
        decay = np.exp(-t * (2.5 + freq / 600))
        return harmonics * decay * _envelope(count, rate, 0.004, 0.0, 1.0, 0.03)
    if instrument == "bell":
        wave_ = np.sin(phase + 2.0 * np.sin(phase * 1.4))
        return wave_ * np.exp(-t * 3.0) * _envelope(count, rate, 0.002, 0.0, 1.0, 0.05)
    if instrument == "pad":
        detune = np.sin(phase) + np.sin(phase * 1.005) + np.sin(phase * 0.995)
        return detune / 3 * _envelope(count, rate, 0.3, 0.2, 0.8, 0.4)
    if instrument == "pluck":
        # Karplus-Strong：一段噪声在延迟线里反复平均，衰减成拨弦声。
        period = max(int(rate / freq), 2)
        rng = np.random.default_rng(int(freq * 1000))
        buffer = rng.uniform(-1, 1, period).astype(np.float32)
        chunks = []
        for _ in range(count // period + 1):
            chunks.append(buffer)
            buffer = 0.996 * 0.5 * (buffer + np.roll(buffer, -1))
        return np.concatenate(chunks)[:count]
    raise ValueError(f"未知音色 {instrument!r}，可选：{', '.join(INSTRUMENTS)}")


def _drum(kind: str, rate: int) -> np.ndarray:
    rng = np.random.default_rng(7)
    if kind == "kick":
        count = int(0.35 * rate)
        t = np.arange(count) / rate
        freq = 120 * np.exp(-t * 18) + 40
        return np.sin(2 * np.pi * np.cumsum(freq) / rate) * np.exp(-t * 9)
    if kind == "snare":
        count = int(0.2 * rate)
        t = np.arange(count) / rate
        noise = rng.uniform(-1, 1, count)
        return (0.7 * noise + 0.3 * np.sin(2 * np.pi * 190 * t)) * np.exp(-t * 22)
    if kind == "hat":
        count = int(0.06 * rate)
        t = np.arange(count) / rate
        noise = rng.uniform(-1, 1, count)
        noise = np.diff(noise, prepend=0)  # 粗略高通
        return 0.4 * noise * np.exp(-t * 60)
    raise ValueError(f"未知鼓件 {kind!r}，可选 kick/snare/hat")


@dataclass(slots=True)
class Track:
    events: list[Event]
    instrument: str = "piano"
    volume: float = 0.8
    pan: float = 0.0  # -1 左 … 1 右
    channel: int = 0
    program: int = 0


@dataclass(slots=True)
class DrumTrack:
    patterns: dict[str, str]
    bars: int = 1
    volume: float = 0.8
    steps_per_beat: int = 4


_GM_PROGRAMS = {
    "piano": 0,
    "bell": 14,
    "pluck": 25,
    "bass": 33,
    "pad": 89,
    "square": 80,
    "saw": 81,
    "sine": 79,
    "triangle": 79,
}
_GM_DRUMS = {"kick": 36, "snare": 38, "hat": 42}


@dataclass(slots=True)
class Song:
    bpm: float = 120
    sample_rate: int = SAMPLE_RATE
    tracks: list[Track] = field(default_factory=list)
    drum_tracks: list[DrumTrack] = field(default_factory=list)

    @property
    def beat_seconds(self) -> float:
        return 60.0 / self.bpm

    def track(
        self,
        notes: str | list,
        *,
        instrument: str = "piano",
        volume: float = 0.8,
        pan: float = 0.0,
        octave: int = 4,
        start: float = 0.0,
    ) -> Song:
        events = parse(notes, default_octave=octave)
        for event in events:
            event.start += start
        if instrument not in INSTRUMENTS:
            raise ValueError(f"未知音色 {instrument!r}，可选：{', '.join(INSTRUMENTS)}")
        self.tracks.append(
            Track(
                events,
                instrument,
                volume,
                max(-1.0, min(pan, 1.0)),
                channel=min(len(self.tracks), 8),
                program=_GM_PROGRAMS.get(instrument, 0),
            )
        )
        return self

    def drums(self, *, bars: int = 1, volume: float = 0.8, **patterns: str) -> Song:
        """鼓点：``x`` 击打、``.`` 空，一个字符 = 1/4 拍（16 分音符）。"""

        unknown = set(patterns) - set(_GM_DRUMS)
        if unknown:
            raise ValueError(f"未知鼓件 {sorted(unknown)}，可选 kick/snare/hat")
        self.drum_tracks.append(DrumTrack(dict(patterns), max(bars, 1), volume))
        return self

    @property
    def total_beats(self) -> float:
        ends = [event.start + event.length for track in self.tracks for event in track.events]
        ends += [
            max(len(pattern) for pattern in drum.patterns.values()) / drum.steps_per_beat * drum.bars
            for drum in self.drum_tracks
            if drum.patterns
        ]
        return max(ends, default=0.0)

    def render(self) -> np.ndarray:
        """返回 (样本数, 2) 的 float32 立体声，峰值归一到 0.9。"""

        rate = self.sample_rate
        seconds = self.total_beats * self.beat_seconds + 1.0
        if seconds > MAX_SECONDS:
            raise ValueError(f"曲子太长（{seconds:.0f} 秒），上限 {MAX_SECONDS} 秒")
        mix = np.zeros((int(seconds * rate) + 1, 2), dtype=np.float32)
        for track in self.tracks:
            left = (1 - track.pan) / 2
            right = (1 + track.pan) / 2
            for event in track.events:
                if not event.pitches:
                    continue
                begin = int(event.start * self.beat_seconds * rate)
                # 音尾多留 0.3 拍，让钢琴/拨弦自然衰减。
                length = (event.length + 0.3) * self.beat_seconds
                for pitch in event.pitches:
                    voice = _voice(midi_to_freq(pitch), length, track.instrument, rate)
                    voice = voice * track.volume * event.velocity / max(len(event.pitches) ** 0.5, 1)
                    end = min(begin + len(voice), len(mix))
                    mix[begin:end, 0] += voice[: end - begin] * left * 2
                    mix[begin:end, 1] += voice[: end - begin] * right * 2
        for drum in self.drum_tracks:
            step_seconds = self.beat_seconds / drum.steps_per_beat
            for kind, pattern in drum.patterns.items():
                sample = _drum(kind, rate) * drum.volume
                for bar in range(drum.bars):
                    for index, char in enumerate(pattern):
                        if char.lower() != "x":
                            continue
                        begin = int((bar * len(pattern) + index) * step_seconds * rate)
                        end = min(begin + len(sample), len(mix))
                        mix[begin:end] += sample[: end - begin, None]
        peak = float(np.abs(mix).max()) or 1.0
        return mix / peak * 0.9

    def save(self, path: str = "song.wav", *, midi: str | None = None) -> str:
        audio = self.render()
        save_wav(path, audio, self.sample_rate)
        if midi:
            self.save_midi(midi)
        print(f"[音乐已保存] {path}（{len(audio) / self.sample_rate:.1f} 秒）")
        return path

    def save_midi(self, path: str = "song.mid") -> str:
        write_midi(path, self)
        return path


def save_wav(path: str, audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> str:
    data = np.clip(audio, -1, 1)
    if data.ndim == 1:
        data = data[:, None]
    pcm = (data * 32767).astype("<i2")
    with wave.open(str(Path(path)), "wb") as handle:
        handle.setnchannels(pcm.shape[1])
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())
    return path


def _varlen(value: int) -> bytes:
    buffer = value & 0x7F
    out = bytearray()
    value >>= 7
    while value:
        buffer <<= 8
        buffer |= (value & 0x7F) | 0x80
        value >>= 7
    while True:
        out.append(buffer & 0xFF)
        if buffer & 0x80:
            buffer >>= 8
        else:
            break
    return bytes(out)


def write_midi(path: str, song: Song, *, ticks_per_beat: int = 480) -> None:
    """写 SMF format 1：一个速度轨 + 每个音轨一条 + 鼓（通道 10）。"""

    tempo = int(60_000_000 / song.bpm)
    tracks: list[bytes] = [b"\x00\xff\x51\x03" + tempo.to_bytes(3, "big") + b"\x00\xff\x2f\x00"]

    def _pack(messages: list[tuple[int, bytes]]) -> bytes:
        messages.sort(key=lambda item: (item[0], item[1][0] & 0xF0 == 0x90))
        out = bytearray()
        last = 0
        for tick, payload in messages:
            out += _varlen(tick - last) + payload
            last = tick
        out += b"\x00\xff\x2f\x00"
        return bytes(out)

    for track in song.tracks:
        channel = track.channel
        messages: list[tuple[int, bytes]] = [(0, bytes([0xC0 | channel, track.program]))]
        for event in track.events:
            start = int(event.start * ticks_per_beat)
            end = int((event.start + event.length) * ticks_per_beat)
            velocity = max(1, min(int(event.velocity * track.volume * 127), 127))
            for raw_pitch in event.pitches:
                pitch = max(0, min(raw_pitch, 127))
                messages.append((start, bytes([0x90 | channel, pitch, velocity])))
                messages.append((end, bytes([0x80 | channel, pitch, 0])))
        tracks.append(_pack(messages))
    for drum in song.drum_tracks:
        messages = []
        step = ticks_per_beat // drum.steps_per_beat
        for kind, pattern in drum.patterns.items():
            note = _GM_DRUMS[kind]
            for bar in range(drum.bars):
                for index, char in enumerate(pattern):
                    if char.lower() == "x":
                        tick = (bar * len(pattern) + index) * step
                        messages.append((tick, bytes([0x99, note, 100])))
                        messages.append((tick + step // 2, bytes([0x89, note, 0])))
        tracks.append(_pack(messages))
    with open(path, "wb") as handle:
        handle.write(b"MThd" + struct.pack(">IHHH", 6, 1, len(tracks), ticks_per_beat))
        handle.writelines(b"MTrk" + struct.pack(">I", len(data)) + data for data in tracks)


def melody(notes: str, *, bpm: float = 120, instrument: str = "piano", path: str = "melody.wav") -> str:
    """一行出声：``music.melody("C4 D4 E4 C4", instrument="bell")``。"""

    return Song(bpm=bpm).track(notes, instrument=instrument).save(path)


__all__ = [
    "INSTRUMENTS",
    "SCALES",
    "Song",
    "chord",
    "melody",
    "midi_to_freq",
    "note_to_midi",
    "parse",
    "progression",
    "save_wav",
    "scale",
    "write_midi",
]
