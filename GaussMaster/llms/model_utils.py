"""Utilities for resolving model/provider runtime settings."""
import os


def resolve_model_backend(model_params: dict) -> str:
    """Resolve registry backend name from model configuration."""
    backend = model_params.get("api_type") or model_params.get("provider")
    if not backend:
        raise ValueError("Missing 'api_type' or 'provider' in model configuration.")
    backend = str(backend).strip()
    # 注册表里真实类名是 OpenAICompatible；下列值与之等价（协议相同，仅厂商/url 不同）：
    # - OpenAICompatible：与 Pangu、Baichuan 一样写「实现类名」
    # - DeepSeek：与 Baichuan 一样写「厂商/品牌名」，映射到同一实现
    # - openai_compatible：历史配置
    if backend == "OpenAICompatible":
        return "OpenAICompatible"
    if backend.lower() in ("openai_compatible", "deepseek"):
        return "OpenAICompatible"
    return backend


def resolve_remote_model_name(model_name: str, model_params: dict) -> str:
    """Resolve upstream model id used in request payload."""
    return (
        model_params.get("model")
        or model_params.get("remote_model")
        or model_params.get("model_name")
        or model_name
    )


def resolve_api_key(model_params: dict):
    """Resolve api key with model-specific env priority."""
    env_name = model_params.get("api_key_env")
    if env_name:
        env_key = os.environ.get(env_name)
        if env_key:
            return env_key
    return (
        os.environ.get("LLM_API_KEY")
        or os.environ.get("SILICONFLOW_API_KEY")
        or model_params.get("api_key")
    )
