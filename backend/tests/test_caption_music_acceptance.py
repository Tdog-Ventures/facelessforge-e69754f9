"""Caption-sync + music-ducking acceptance (RED first).

Proves the defects before the fix:
- captions are generated from pre-fit audio, then atempo compresses it;
- the mixer uses fixed attenuation, not sidechain ducking.
"""
import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

ffmpeg = shutil.which("ffmpeg") or "ffmpeg"


def _run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _tone(dest: Path, seconds: float, freq: float = 440.0) -> Path:
    _run([ffmpeg, "-y", "-f", "lavfi", "-i",
          f"sine=frequency={freq}:sample_rate=48000:duration={seconds}",
          "-ar", "48000", "-ac", "2", str(dest)])
    return dest


def test_fitted_narration_has_exact_sample_budget(tmp_path: Path):
    from backend.app import audio_mix
    src = _tone(tmp_path / "src.wav", 31.89551)
    fitted = asyncio.run(audio_mix.fit_narration_to_budget(
        src, budget_seconds=31.5, sample_rate=48000, work_dir=tmp_path))
    import wave
    with wave.open(str(fitted), "rb") as w:
        assert (w.getframerate(), w.getnchannels()) == (48000, 2)
        assert w.getnframes() == round(31.5 * 48000)


def test_over_tolerance_drift_blocks(tmp_path: Path):
    from backend.app import audio_mix
    src = _tone(tmp_path / "src.wav", 40.0)
    with pytest.raises(RuntimeError):
        asyncio.run(audio_mix.fit_narration_to_budget(
            src, budget_seconds=31.5, sample_rate=48000, work_dir=tmp_path))


def test_mux_uses_no_second_atempo_when_fitted():
    from backend.app import audio_mix
    filt = audio_mix.narration_mux_filter(fitted=True, tempo_ratio=1.012556)
    assert "atempo" not in filt


def test_ducking_attenuates_music_under_narration(tmp_path: Path):
    from backend.app import audio_mix
    # Speech-like pulses over a music bed: the COMPRESSED STEM must dip.
    # (Stem-level is the honest measurement — the final mix is dominated
    # by narration by design, so mix-level deltas cannot isolate music.)
    speech = tmp_path / "speech.wav"
    _run([ffmpeg, "-y", "-f", "lavfi", "-i", "sine=frequency=880:duration=4",
          "-af", "volume=4dB", "-ar", "48000", "-ac", "2", str(speech)])
    music = _tone(tmp_path / "music.wav", 6.0, freq=330.0)
    stem = asyncio.run(audio_mix.duck_music_stem(
        music=music, sidechain=speech, speech_offset=2.0, work_dir=tmp_path))
    wet_duck = audio_mix.segment_loudness(stem, 2.5, 5.5)
    wet_intro = audio_mix.segment_loudness(stem, 0.2, 1.8)
    assert wet_intro - wet_duck >= 8.0, \
        f"stem must fall >=8dB (intro={wet_intro:.1f}, duck={wet_duck:.1f})"


def test_mix_recovers_and_keeps_format(tmp_path: Path):
    from backend.app import audio_mix
    speech = tmp_path / "speech.wav"
    _run([ffmpeg, "-y", "-f", "lavfi", "-i", "sine=frequency=880:duration=2",
          "-ar", "48000", "-ac", "2", str(speech)])
    music = _tone(tmp_path / "music.wav", 6.0, freq=330.0)
    out = asyncio.run(audio_mix.mix_with_ducking(
        narration=speech, music=music, intro_seconds=1.0, final_seconds=6.0,
        music_gain_db=-14.0, work_dir=tmp_path))
    intro_loud = audio_mix.segment_loudness(out, 0.1, 0.9)
    assert intro_loud > -40.0, "music must be audible during intro"
    tail_loud = audio_mix.segment_loudness(out, 3.2, 3.9)
    assert tail_loud > -40.0, "music must recover after narration"
    info = audio_mix.probe_audio(out)
    assert info["sample_rate"] == 48000 and info["channels"] == 2
    assert abs(info["duration"] - 6.0) < 0.05
    assert info["peak_db"] < -1.0, "no clipping allowed"


def test_caption_timeline_locks(tmp_path: Path):
    from backend.app import subtitles
    # 31.5s fitted timeline: words evenly spread, last word near boundary.
    n, total = 60, 31.5
    words = [{"word": f"w{i}", "start": i * total / n, "end": (i + 1) * total / n}
             for i in range(n)]
    words[-1]["word"] = "Fin."
    srt_path = tmp_path / "caps.srt"
    subtitles.write_srt_from_words(words, srt_path, intro_offset_seconds=2.5)
    text = srt_path.read_text()
    assert "Fin." in text  # last word present
    import re
    stamps = re.findall(r"(\d+):(\d+):([\d,]+) -->", text)
    def secs(h, m, s):
        return int(h) * 3600 + int(m) * 60 + float(s.replace(",", "."))
    starts = [secs(*s) for s in stamps]
    assert min(starts) >= 2.5  # intro offset applied
    assert max(starts) <= 2.5 + total  # nothing past narration boundary
    # No double offset: first cue starts near intro, not 2x intro.
    assert min(starts) < 2.5 + total / n + 0.6


def test_short_long_punctuated_cues(tmp_path: Path):
    from backend.app import subtitles
    words = [
        {"word": "Hi!", "start": 0.0, "end": 0.4},
        {"word": "well...", "start": 0.4, "end": 1.2},
        {"word": "a-" + "very-" * 10 + "long-word", "start": 1.2, "end": 3.0},
    ]
    ass = subtitles.build_ass_karaoke(words, play_res_x=1080, play_res_y=1920)
    assert "Hi!" in ass and "well..." in ass
    assert "PlayResY: 1920" in ass
