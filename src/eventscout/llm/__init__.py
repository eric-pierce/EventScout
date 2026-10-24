from eventscout.config import Config
from eventscout.llm.base import LLM, LLMError, Usage


def make_llm(cfg: Config) -> LLM:
    if cfg.llm.provider == "ollama":
        from eventscout.llm.ollama import OllamaLLM

        return OllamaLLM(cfg.llm.ollama)
    from eventscout.llm.claude import ClaudeLLM

    return ClaudeLLM(cfg.llm.claude, cfg.home)


__all__ = ["LLM", "LLMError", "Usage", "make_llm"]
