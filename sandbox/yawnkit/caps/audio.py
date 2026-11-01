"""audio 包：音频信息、剪切拼接、音量、变速、格式转换、频谱/波形图、合成音、旋律（WAV/FLAC/OGG/MP3，靠 libsndfile）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import CapError, as_list, cap, dest, human, ref, src

FORMATS = {".wav": "WAV", ".flac": "FLAC", ".ogg": "OGG", ".mp3": "MP3"}


def _read(path: str) -> tuple[Any, int]:
    import soundfile as sf

    target = src(path, kind="音频")
    try:
        data, rate = sf.read(str(target), always_2d=True, dtype="float32")
    except (RuntimeError, sf.LibsndfileError) as exc:
        raise CapError(
            f"读不了这个音频（支持 wav/flac/ogg/mp3；m4a/aac/视频要先转换）：{exc}"
        ) from exc
    return data, int(rate)


def _write(data: Any, rate: int, output: str, default: str) -> dict[str, Any]:
    import numpy as np
    import soundfile as sf

    target = dest(output, default)
    suffix = target.suffix.lower()
    if suffix not in FORMATS:
        target = target.with_suffix(".wav")
        suffix = ".wav"
    clipped = np.clip(data, -1.0, 1.0)
    sf.write(str(target), clipped, rate, format=FORMATS[suffix])
    return {
        "file": ref(target),
        "seconds": round(len(clipped) / rate, 2),
        "size": human(target.stat().st_size),
    }


def _stem(path: str) -> str:
    return Path(str(path).removeprefix("sandbox:")).stem


@cap("audio.info")
def info(path: str) -> dict[str, Any]:
    import soundfile as sf

    target = src(path, kind="音频")
    try:
        meta = sf.info(str(target))
    except (RuntimeError, sf.LibsndfileError) as exc:
        raise CapError(f"读不了这个音频：{exc}") from exc
    return {
        "seconds": round(meta.duration, 2),
        "sample_rate": meta.samplerate,
        "channels": meta.channels,
        "format": meta.format,
        "subtype": meta.subtype,
        "size": human(target.stat().st_size),
    }


@cap("audio.trim")
def trim(
    path: str, start: float = 0, end: float = 0, output: str = ""
) -> dict[str, Any]:
    data, rate = _read(path)
    first = max(0, int(float(start) * rate))
    last = int(float(end) * rate) if float(end) > 0 else len(data)
    if last <= first:
        raise CapError("end 要大于 start（秒）")
    return _write(
        data[first:last],
        rate,
        output,
        f"{_stem(path)}_剪切{Path(path).suffix or '.wav'}",
    )


@cap("audio.concat")
def concat(paths: list[str], gap: float = 0, output: str = "") -> dict[str, Any]:
    import numpy as np

    items = [_read(p) for p in as_list(paths)]
    if len(items) < 2:
        raise CapError("至少两个音频")
    rate = items[0][1]
    channels = max(d.shape[1] for d, _ in items)
    parts = []
    for raw, item_rate in items:
        data = _resample(raw, item_rate, rate) if item_rate != rate else raw
        if data.shape[1] < channels:
            data = np.repeat(data, channels, axis=1)[:, :channels]
        parts.append(data)
        if float(gap) > 0:
            parts.append(np.zeros((int(float(gap) * rate), channels), dtype="float32"))
    return _write(
        np.concatenate(parts[: -1 if float(gap) > 0 else None]),
        rate,
        output,
        "拼接.wav",
    )


def _resample(data: Any, rate: int, target: int) -> Any:
    from math import gcd

    from scipy.signal import resample_poly

    factor = gcd(rate, target)
    return resample_poly(data, target // factor, rate // factor, axis=0).astype(
        "float32"
    )


@cap("audio.volume")
def volume(
    path: str, gain_db: float = 0, normalize: bool = False, output: str = ""
) -> dict[str, Any]:
    import numpy as np

    data, rate = _read(path)
    if normalize:
        peak = float(np.max(np.abs(data))) or 1.0
        data = data * (0.95 / peak)
    else:
        data = data * (10 ** (float(gain_db) / 20))
    return _write(
        data, rate, output, f"{_stem(path)}_音量{Path(path).suffix or '.wav'}"
    )


@cap("audio.speed")
def speed(path: str, factor: float = 1.25, output: str = "") -> dict[str, Any]:
    """变速（同时变调，像磁带快放）；factor 0.25~4。"""

    value = float(factor)
    if not 0.25 <= value <= 4:
        raise CapError("factor 要在 0.25~4")
    data, rate = _read(path)
    return _write(
        _resample(data, int(rate * value), rate) if value != 1 else data,
        rate,
        output,
        f"{_stem(path)}_x{value:g}.wav",
    )


@cap("audio.convert")
def convert(
    path: str,
    format: str = "wav",  # noqa: A002 - 给模型看的参数名
    sample_rate: int = 0,
    mono: bool = False,
    output: str = "",
) -> dict[str, Any]:
    suffix = f".{str(format).lower().lstrip('.')}"
    if suffix not in FORMATS:
        raise CapError(f"format 只能是 {'/'.join(s[1:] for s in FORMATS)}")
    data, rate = _read(path)
    if mono and data.shape[1] > 1:
        data = data.mean(axis=1, keepdims=True)
    if int(sample_rate) and int(sample_rate) != rate:
        data = _resample(data, rate, int(sample_rate))
        rate = int(sample_rate)
    return _write(data, rate, output, f"{_stem(path)}{suffix}")


@cap("audio.fade")
def fade(
    path: str, fade_in: float = 1, fade_out: float = 1, output: str = ""
) -> dict[str, Any]:
    import numpy as np

    data, rate = _read(path)
    count_in = min(len(data), int(float(fade_in) * rate))
    count_out = min(len(data), int(float(fade_out) * rate))
    if count_in:
        data[:count_in] *= np.linspace(0, 1, count_in, dtype="float32")[:, None]
    if count_out:
        data[-count_out:] *= np.linspace(1, 0, count_out, dtype="float32")[:, None]
    return _write(
        data, rate, output, f"{_stem(path)}_淡入淡出{Path(path).suffix or '.wav'}"
    )


@cap("audio.reverse")
def reverse(path: str, output: str = "") -> dict[str, Any]:
    data, rate = _read(path)
    return _write(data[::-1], rate, output, f"{_stem(path)}_倒放.wav")


@cap("audio.silence_split")
def silence_split(
    path: str, threshold_db: float = -40, min_silence: float = 0.6
) -> dict[str, Any]:
    """按静音切段：找出有声音的片段并各存一个文件（录音分句、去掉空白）。"""

    import numpy as np

    data, rate = _read(path)
    mono = np.abs(data.mean(axis=1))
    window = max(1, int(0.02 * rate))
    energy = np.convolve(mono, np.ones(window) / window, mode="same")
    loud = energy > 10 ** (float(threshold_db) / 20)
    segments = []
    start = None
    quiet = 0
    gap = int(float(min_silence) * rate)
    for index, flag in enumerate(loud):
        if flag:
            if start is None:
                start = index
            quiet = 0
        elif start is not None:
            quiet += 1
            if quiet >= gap:
                segments.append((start, index - quiet))
                start = None
                quiet = 0
    if start is not None:
        segments.append((start, len(loud)))
    files = []
    for number, (first, last) in enumerate(segments[:100], start=1):
        if last - first < rate * 0.1:
            continue
        files.append(
            _write(data[first:last], rate, "", f"{_stem(path)}_段{number:03d}.wav")
            | {"start": round(first / rate, 2)}
        )
    return {"segments": len(files), "files": files[:30]}


@cap("audio.tone")
def tone(
    frequency: float = 440, seconds: float = 1, wave: str = "sine", output: str = ""
) -> dict[str, Any]:
    import numpy as np

    rate = 44100
    length = max(0.05, min(float(seconds), 600))
    t = np.arange(int(rate * length)) / rate
    phase = 2 * np.pi * float(frequency) * t
    if wave == "sine":
        data = np.sin(phase)
    elif wave == "square":
        data = np.sign(np.sin(phase))
    elif wave == "saw":
        data = 2 * (t * float(frequency) % 1) - 1
    elif wave == "noise":
        data = np.random.default_rng().uniform(-1, 1, len(t))
    else:
        raise CapError("wave 只能是 sine/square/saw/noise")
    return _write(
        (0.5 * data).astype("float32")[:, None],
        rate,
        output,
        f"{wave}_{float(frequency):g}Hz.wav",
    )


@cap("audio.melody")
def melody(
    notes: str, bpm: float = 120, instrument: str = "piano", output: str = ""
) -> dict[str, Any]:
    """用 yawnkit.music 把音符串合成音频，如 "C4 D4 E4 C4 | E4 F4 G4:2"，也会顺手出一个 MIDI。"""

    from yawnkit import music

    target = dest(output, "旋律.wav")
    try:
        song = music.Song(bpm=float(bpm)).track(str(notes), instrument=str(instrument))
    except (ValueError, KeyError) as exc:
        raise CapError(
            f"音符写错了：{exc}；乐器可选 {', '.join(music.INSTRUMENTS)}"
        ) from exc
    midi = target.with_suffix(".mid")
    song.save(str(target), midi=str(midi))
    return {"file": ref(target), "midi": ref(midi)}


@cap("audio.waveform")
def waveform(path: str, output: str = "") -> dict[str, Any]:
    import matplotlib as mpl

    mpl.use("Agg")
    import numpy as np
    from matplotlib import pyplot as plt

    data, rate = _read(path)
    mono = data.mean(axis=1)
    step = max(1, len(mono) // 20000)
    fig, ax = plt.subplots(figsize=(10, 3))
    ax.plot(np.arange(0, len(mono), step) / rate, mono[::step], linewidth=0.5)
    ax.set_xlabel("秒")
    ax.set_title(_stem(path))
    target = dest(output, f"{_stem(path)}_波形.png")
    fig.tight_layout()
    fig.savefig(target)
    plt.close(fig)
    return {"file": ref(target)}


@cap("audio.spectrogram")
def spectrogram(path: str, output: str = "") -> dict[str, Any]:
    import matplotlib as mpl

    mpl.use("Agg")
    from matplotlib import pyplot as plt

    data, rate = _read(path)
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.specgram(
        data.mean(axis=1)[: rate * 600], Fs=rate, NFFT=2048, noverlap=1024, cmap="magma"
    )
    ax.set_xlabel("秒")
    ax.set_ylabel("Hz")
    ax.set_title(f"{_stem(path)} 频谱")
    target = dest(output, f"{_stem(path)}_频谱.png")
    fig.tight_layout()
    fig.savefig(target)
    plt.close(fig)
    return {"file": ref(target)}


@cap("audio.loudness")
def loudness(path: str) -> dict[str, Any]:
    """峰值、RMS、粗略 BPM 与主频，判断音量是否过小/削顶。"""

    import numpy as np

    data, rate = _read(path)
    mono = data.mean(axis=1)
    peak = float(np.max(np.abs(mono))) if len(mono) else 0.0
    rms = float(np.sqrt(np.mean(mono**2))) if len(mono) else 0.0
    segment = mono[: rate * 30]
    spectrum = np.abs(np.fft.rfft(segment)) if len(segment) else np.array([0.0])
    freqs = np.fft.rfftfreq(len(segment), 1 / rate) if len(segment) else np.array([0.0])
    dominant = (
        float(freqs[int(np.argmax(spectrum[1:]) + 1)]) if len(spectrum) > 1 else 0.0
    )
    return {
        "peak_db": round(20 * np.log10(peak), 1) if peak else None,
        "rms_db": round(20 * np.log10(rms), 1) if rms else None,
        "clipping": peak >= 0.999,
        "dominant_hz": round(dominant, 1),
    }
