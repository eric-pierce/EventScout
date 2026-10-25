from eventscout.config import Config
from eventscout.llm.base import LLM, LLMError, Usage, usage_delta


def make_llm(cfg: Config) -> LLM:
    if cfg.llm.provider == "ollama":
        from eventscout.llm.ollama import OllamaLLM

        return OllamaLLM(cfg.llm.ollama)
    from eventscout.llm.claude import ClaudeLLM

    return ClaudeLLM(cfg.llm.claude, cfg.home)


def llm_model_name(cfg: Config) -> str:
    """The model the configured provider uses, for run stats."""
    return cfg.llm.ollama.model if cfg.llm.provider == "ollama" else cfg.llm.claude.model


__all__ = ["LLM", "LLMError", "Usage", "llm_model_name", "make_llm", "usage_delta"]
