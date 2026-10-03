"""Shared, local-only TTS backend preflight; credentials never leave memory."""
from importlib.metadata import version, PackageNotFoundError
import inspect
import re
from pathlib import Path

BACKENDS = {"edge-tts", "volcengine"}
ADAPTER_VERSION = "1"
EDGE_TTS_VERSION = "7.2.8"


def validate_backend_configuration(config: dict) -> None:
    backend = config.get("backend", "volcengine")
    if not isinstance(backend, str) or backend not in BACKENDS:
        raise ValueError("backend 必须为 edge-tts 或 volcengine")
    if config.get("schema_version", 1) >= 2 and "backend" not in config:
        raise ValueError("新版配置缺少 backend；默认配置应为 edge-tts")
    api = config.get("api")
    if not isinstance(api, dict):
        raise ValueError("api 必须为对象")
    if type(api.get("max_retries")) is not int or api["max_retries"] < 1:
        raise ValueError("api.max_retries 必须为正整数")
    if type(api.get("timeout_seconds")) is not int or api["timeout_seconds"] <= 0:
        raise ValueError("api.timeout_seconds 必须为正整数")
    if type(api.get("retry_backoff_seconds")) not in (int, float) or not 0 <= api["retry_backoff_seconds"] < float("inf"):
        raise ValueError("api.retry_backoff_seconds 必须为有限非负数")


def validate_voice_mapping(data: dict, backend: str) -> None:
    """Validate the selected provider's mapping without requiring the other provider."""
    selected = data.get("edge_tts") if backend == "edge-tts" else data
    if not isinstance(selected, dict) or not isinstance(selected.get("voices"), list):
        raise ValueError("音色映射必须包含 voices 列表")
    names, identifiers = set(), set()
    for voice in selected["voices"]:
        if not isinstance(voice, dict) or not isinstance(voice.get("name"), str) or not voice["name"].strip() or not isinstance(voice.get("speaker_id"), str) or not voice["speaker_id"].strip():
            raise ValueError("音色映射缺少 name 或 speaker_id")
        if voice["name"] in names:
            raise ValueError("音色映射名称重复")
        names.add(voice["name"])
        identifiers.add(voice["speaker_id"])
        if backend == "edge-tts":
            if not re.fullmatch(r"[a-z]{2,}-[A-Z]{2,}-.+Neural", str(voice["speaker_id"])):
                raise ValueError("Edge 音色映射中的 speaker_id 无效")
        elif not voice.get("resource_id"):
            raise ValueError("火山音色映射缺少 resource_id")
    if selected.get("default_voice") not in names | identifiers:
        raise ValueError("默认音色不在映射中")


def validate_backend_parameters(backend: str, params: dict, resource_id: str = "") -> None:
    if backend not in BACKENDS:
        raise ValueError("backend 必须为 edge-tts 或 volcengine")
    for key in ("speech_rate", "loudness_rate", "sample_rate"):
        if type(params.get(key)) is not int:
            raise ValueError(f"{key} 必须为整数")
    if not -50 <= params["speech_rate"] <= 100:
        raise ValueError("speech_rate 必须位于 [-50, 100]")
    if params["sample_rate"] <= 0:
        raise ValueError("sample_rate 必须为正整数")
    if params.get("format") not in {"mp3", "wav", "pcm", "ogg_opus"}:
        raise ValueError("不支持的音频格式")
    if backend == "edge-tts":
        if resource_id:
            raise ValueError("edge-tts 不支持 resource-id")
        if params.get("emotion") or params.get("emotion_scale", 4) != 4:
            raise ValueError("edge-tts 不支持 emotion / emotion-scale 参数")
        if params["loudness_rate"] < -100:
            raise ValueError("Edge loudness_rate 不得小于 -100")


def dotenv_credential(root: Path, name: str) -> str:
    """Strictly read the project .env, ignoring process environment variables."""
    try:
        from dotenv import dotenv_values
    except ImportError as exc:
        raise ValueError("当前 Python 环境缺少 python-dotenv") from exc
    try:
        value = (dotenv_values(root / ".env", interpolate=False).get(name) or "").strip()
    except OSError as exc:
        raise ValueError("无法读取项目根目录 .env") from exc
    if not value or "${" in value or value.lower() in {"todo", "your-api-key", "your_api_key"}:
        raise ValueError(f"请在项目根目录 .env 中填写 {name}")
    return value


def check_backend(root: Path, backend: str, *, mock: bool = False) -> dict:
    if backend not in BACKENDS:
        raise ValueError("backend 必须为 edge-tts 或 volcengine")
    if mock:
        return {"backend_version": "mock", "timestamp_granularity": "word"}
    if backend == "volcengine":
        dotenv_credential(root, "VOLCENGINE_API_KEY")
        return {"backend_version": "api-v3", "timestamp_granularity": "word"}
    try:
        import edge_tts
        installed = version("edge-tts")
    except (ImportError, PackageNotFoundError) as exc:
        raise ValueError("当前 Python 环境缺少 edge-tts；请安装 runtime/.venv/requirements.txt") from exc
    try:
        has_boundary = "boundary" in inspect.signature(edge_tts.Communicate).parameters
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("edge-tts 缺少有效的 Communicate / WordBoundary 接口") from exc
    if installed != EDGE_TTS_VERSION or not has_boundary:
        raise ValueError("edge-tts 需要锁定版本 7.2.8 及 WordBoundary 接口")
    return {"backend_version": installed, "timestamp_granularity": "word"}
