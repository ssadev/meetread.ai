from __future__ import annotations

import logging
import queue
import shutil
import subprocess
import sys
import threading
import time
import wave
from array import array
from datetime import datetime
from pathlib import Path
from typing import Any

from config import SETTINGS, Settings
from storage import MeetingStorage


LOGGER = logging.getLogger(__name__)
# Treat samples below roughly -54 dBFS in 16-bit PCM as silence/noise floor.
MIN_AUDIO_PEAK = 64


class AudioRecorder:
    def __init__(
        self,
        sink_name: str = "MeetBot",
        settings: Settings = SETTINGS,
        device: str | None = None,
    ):
        self.sink_name = sink_name
        self.settings = settings
        self.device = device if device is not None else settings.audio_input_device or f"{sink_name}.monitor"
        self.pulse_source = f"{sink_name}.monitor"
        self._status = {
            "bytes_recorded": 0,
            "chunk_count": 0,
            "errors": [],
            "silent_chunks": 0,
            "started_at": None,
            "finished_at": None,
            "audio_file": None,
            "mp3_file": None,
            "device": self.device,
            "pulse_source": self.pulse_source,
        }
        self._duration_seconds = 0

    def start(self, output_dir: str | Path, stop_event: threading.Event) -> None:
        output_dir = Path(output_dir)
        chunks_dir = output_dir / "audio_chunks"
        chunks_dir.mkdir(parents=True, exist_ok=True)
        self._status["started_at"] = datetime.now().isoformat()
        started = time.monotonic()
        LOGGER.info(
            "Audio recorder starting: backend=%s device=%s pulse_source=%s samplerate=%s channels=%s chunk_seconds=%s",
            self.settings.audio_backend,
            self.device,
            self.pulse_source,
            self.settings.audio_samplerate,
            self.settings.audio_channels,
            self.settings.audio_chunk_seconds,
        )
        try:
            self._record_chunks(chunks_dir, stop_event)
            audio_path = self.concatenate_chunks(output_dir)
            self._status["audio_file"] = audio_path.name if audio_path else None
            if audio_path and self.settings.save_mp3:
                mp3_path = self.convert_to_mp3(audio_path)
                self._status["mp3_file"] = mp3_path.name if mp3_path else None
            if self.settings.delete_chunks_after_concat and audio_path:
                MeetingStorage(output_dir.parent).remove_chunks_dir(output_dir)
        except Exception as exc:
            LOGGER.exception("Audio recorder failed")
            self._status["errors"].append(str(exc))
        finally:
            self._duration_seconds = int(time.monotonic() - started)
            self._status["finished_at"] = datetime.now().isoformat()
            LOGGER.info(
                "Audio recorder finished: duration_seconds=%s chunks=%s bytes=%s audio_file=%s mp3_file=%s errors=%s",
                self._duration_seconds,
                self._status["chunk_count"],
                self._status["bytes_recorded"],
                self._status["audio_file"],
                self._status["mp3_file"],
                len(self._status["errors"]),
            )

    def _record_chunks(self, chunks_dir: Path, stop_event: threading.Event) -> None:
        import sounddevice as sd

        frames_per_second = self.settings.audio_samplerate
        channels = self.settings.audio_channels
        dtype = "int16"
        audio_queue: queue.Queue[bytes] = queue.Queue()

        def callback(indata, frames, time_info, status):  # noqa: ANN001
            if status:
                LOGGER.warning("sounddevice status: %s", status)
            audio_queue.put(bytes(indata))

        # PulseAudio source names are not PortAudio device names. In Docker/Linux, pactl sets the
        # per-meeting monitor as PulseAudio's default source, then sounddevice opens the "pulse"
        # PortAudio device. On other hosts this can still be a concrete loopback device name.
        try:
            stream = sd.RawInputStream(
                samplerate=frames_per_second,
                channels=channels,
                dtype=dtype,
                device=self.device,
                callback=callback,
            )
        except Exception:
            LOGGER.error("Available sounddevice devices:\n%s", sd.query_devices())
            raise

        with stream:
            chunk_index = 1
            while not stop_event.is_set():
                chunk_path = chunks_dir / f"chunk_{chunk_index:03d}.wav"
                self._write_chunk(chunk_path, audio_queue, stop_event)
                if self._chunk_has_audio(chunk_path):
                    self._status["chunk_count"] += 1
                    self._status["bytes_recorded"] += chunk_path.stat().st_size
                    LOGGER.info(
                        "Audio chunk recorded: chunk=%s bytes=%s",
                        chunk_path.name,
                        chunk_path.stat().st_size,
                    )
                    chunk_index += 1
                else:
                    self._status["silent_chunks"] += 1
                    LOGGER.warning("Discarding silent audio chunk: chunk=%s", chunk_path.name)
                    chunk_path.unlink(missing_ok=True)

    def _write_chunk(self, chunk_path: Path, audio_queue: queue.Queue[bytes], stop_event: threading.Event) -> None:
        deadline = time.monotonic() + self.settings.audio_chunk_seconds
        with wave.open(str(chunk_path), "wb") as wav:
            wav.setnchannels(self.settings.audio_channels)
            wav.setsampwidth(2)
            wav.setframerate(self.settings.audio_samplerate)
            while time.monotonic() < deadline and not stop_event.is_set():
                try:
                    wav.writeframes(audio_queue.get(timeout=1.0))
                except queue.Empty:
                    continue
            while not audio_queue.empty():
                wav.writeframes(audio_queue.get_nowait())

    def _chunk_has_audio(self, chunk_path: Path) -> bool:
        if not chunk_path.exists():
            return False
        try:
            with wave.open(str(chunk_path), "rb") as wav:
                frames = wav.readframes(wav.getnframes())
                if not frames:
                    return False
                return _max_sample_peak(frames, wav.getsampwidth()) >= MIN_AUDIO_PEAK
        except wave.Error:
            return False

    def concatenate_chunks(self, output_dir: str | Path) -> Path | None:
        output_dir = Path(output_dir)
        chunks = sorted((output_dir / "audio_chunks").glob("chunk_*.wav"))
        if not chunks:
            return None
        chunk_list = output_dir / "chunk_list.txt"
        chunk_list.write_text("".join(f"file '{chunk.resolve()}'\n" for chunk in chunks), encoding="utf-8")
        output = output_dir / "audio_raw.wav"
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg not found; cannot concatenate audio chunks")
        command = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(chunk_list), "-c", "copy", str(output)]
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        chunk_list.unlink(missing_ok=True)
        LOGGER.info("Audio chunks concatenated: chunks=%s output=%s bytes=%s", len(chunks), output.name, output.stat().st_size)
        return output

    def convert_to_mp3(self, wav_path: str | Path) -> Path | None:
        wav_path = Path(wav_path)
        output = wav_path.with_suffix(".mp3")
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            LOGGER.warning("ffmpeg not found; skipping MP3 conversion")
            return None
        command = [ffmpeg, "-y", "-i", str(wav_path), "-codec:a", "libmp3lame", "-qscale:a", "2", str(output)]
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        LOGGER.info("Audio converted to MP3: output=%s bytes=%s", output.name, output.stat().st_size)
        return output

    def get_duration_seconds(self) -> int:
        return self._duration_seconds

    def record_error(self, message: str) -> None:
        self._status["errors"].append(message)

    def get_status(self) -> dict[str, Any]:
        return dict(self._status)


def _max_sample_peak(frames: bytes, sample_width: int) -> int:
    if sample_width == 1:
        return max((abs(sample - 128) << 8 for sample in frames), default=0)
    if sample_width not in {2, 3, 4} or len(frames) < sample_width:
        return 0
    samples = array("h")
    if sample_width == 2:
        samples.frombytes(frames[: len(frames) - (len(frames) % sample_width)])
        if sys.byteorder != "little":
            samples.byteswap()
        return max((abs(sample) for sample in samples), default=0)
    peak = 0
    shift = 8 if sample_width == 3 else 16
    frame_count = len(frames) // sample_width
    for index in range(frame_count):
        start = index * sample_width
        sample = int.from_bytes(frames[start : start + sample_width], "little", signed=True)
        peak = max(peak, abs(sample) >> shift)
    return peak
