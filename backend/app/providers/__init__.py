from ..config import Settings
from .mock import MockIntelligenceProvider
from .openai_compatible import OpenAICompatibleProvider


def create_intelligence_provider(settings: Settings):
    fallback = MockIntelligenceProvider()
    if settings.provider_mode == "production" and settings.llm_configured:
        return OpenAICompatibleProvider(settings, fallback=fallback)
    return fallback

__all__ = ["MockIntelligenceProvider", "OpenAICompatibleProvider", "create_intelligence_provider"]
