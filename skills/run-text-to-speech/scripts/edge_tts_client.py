"""Edge streaming adapter using actual word boundaries and existing TTS outputs."""
import asyncio
from pathlib import Path
import subprocess
import tempfile
import time

from utils.scripts.ffmpeg_plan import find_ffmpeg
from utils.scripts.tts_backend import validate_backend_parameters


def normalize_boundary(chunk: dict) -> dict:
    # Edge timestamps are 100 ns ticks; project timestamps are integer milliseconds.
    return {"text": chunk["text"], "start_ms": round(chunk["offset"] / 10000),
            "end_ms": round((chunk["offset"] + chunk["duration"]) / 10000)}


class EdgeTTSClient:
    def __init__(self, config: dict, error_types=(ValueError, RuntimeError, RuntimeError)):
        self.config = config
        self.error_types = error_types

    async def _stream(self, text: str, voice: str, params: dict) -> tuple[bytes, list]:
        import edge_tts
        api = self.config["api"]
        communicate = edge_tts.Communicate(
            text, voice, rate=f'{int(params["speech_rate"]):+d}%',
            volume=f'{int(params["loudness_rate"]):+d}%', boundary="WordBoundary",
            connect_timeout=int(api["timeout_seconds"]),
            receive_timeout=int(api["timeout_seconds"]))
        audio = bytearray()
        items = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio.extend(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                items.append(normalize_boundary(chunk))
        if not audio or not items:
            raise RuntimeError("Edge 未返回音频或词级时间戳")
        return bytes(audio), [{"items": items}]

    def synthesize(self, text: str, speaker_id: str, resource_id: str, params: dict):
        TTSConfigurationError, TTSRetryableError, TTSVerificationError = self.error_types
        try:
            validate_backend_parameters("edge-tts", params, resource_id)
        except ValueError as exc:
            raise TTSConfigurationError(str(exc)) from exc
        api = self.config["api"]
        if type(api.get("max_retries")) is not int or api["max_retries"] < 1:
            raise TTSConfigurationError("api.max_retries 必须为正整数")
        for attempt in range(1, int(api["max_retries"]) + 1):
            try:
                audio, events = asyncio.run(self._stream(text, speaker_id, params))
                break
            except (ValueError, TypeError) as exc:
                raise TTSConfigurationError("Edge 调用参数无效") from exc
            except Exception as exc:
                if attempt == int(api["max_retries"]):
                    raise TTSRetryableError(f"Edge 合成失败：{type(exc).__name__}") from exc
                time.sleep(float(api["retry_backoff_seconds"]) * attempt)
        if params["format"] == "mp3" and params["sample_rate"] == 24000:
            return audio, events
        with tempfile.TemporaryDirectory(dir=self.config["_staging_dir"]) as directory:
            source = Path(directory) / "source.mp3"
            target = Path(directory) / "converted.audio"
            source.write_bytes(audio)
            codecs = {"wav": ("pcm_s16le", "wav"), "pcm": ("pcm_s16le", "s16le"),
                      "ogg_opus": ("libopus", "ogg"), "mp3": ("libmp3lame", "mp3")}
            codec, container = codecs[params["format"]]
            result = subprocess.run([str(find_ffmpeg()), "-v", "error", "-i", str(source),
                                     "-ar", str(params["sample_rate"]), "-ac", "1", "-c:a", codec,
                                     "-f", container, str(target)], capture_output=True)
            if result.returncode:
                raise TTSVerificationError("Edge 音频格式转换失败")
            return target.read_bytes(), events
