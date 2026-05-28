from __future__ import annotations

import wave
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import audio_recorder as module
from audio_recorder import AudioRecorder


def _write_wav(path: Path, sample: int = 1000) -> None:
    frame = int(sample).to_bytes(2, "little", signed=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(44100)
        wav.writeframes(frame * 200)


class AudioRecorderTests(unittest.TestCase):
    def test_concatenate_chunks_invokes_ffmpeg(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            chunks = tmp_path / "audio_chunks"
            chunks.mkdir()
            _write_wav(chunks / "chunk_001.wav")
            calls = []

            def fake_run(command, check, stdout, stderr):
                calls.append(command)
                (tmp_path / "audio_raw.wav").write_bytes(b"wav")

            with patch.object(module.shutil, "which", lambda name: "/usr/bin/ffmpeg"):
                with patch.object(module.subprocess, "run", fake_run):
                    output = AudioRecorder().concatenate_chunks(tmp_path)

            self.assertEqual(output, tmp_path / "audio_raw.wav")
            self.assertEqual(calls[0][:6], ["/usr/bin/ffmpeg", "-y", "-f", "concat", "-safe", "0"])
            self.assertFalse((tmp_path / "chunk_list.txt").exists())

    def test_empty_wav_chunk_is_not_counted_as_audio(self):
        with TemporaryDirectory() as tmp:
            chunk = Path(tmp) / "empty.wav"
            with wave.open(str(chunk), "wb") as wav:
                wav.setnchannels(2)
                wav.setsampwidth(2)
                wav.setframerate(44100)

            self.assertFalse(AudioRecorder()._chunk_has_audio(chunk))

    def test_silent_wav_chunk_is_not_counted_as_audio(self):
        with TemporaryDirectory() as tmp:
            chunk = Path(tmp) / "chunk.wav"
            _write_wav(chunk, sample=0)

            self.assertFalse(AudioRecorder()._chunk_has_audio(chunk))

    def test_wav_chunk_with_signal_is_counted_as_audio(self):
        with TemporaryDirectory() as tmp:
            chunk = Path(tmp) / "chunk.wav"
            _write_wav(chunk)

            self.assertTrue(AudioRecorder()._chunk_has_audio(chunk))
