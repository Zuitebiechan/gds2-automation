"""
LLM Factory for multi-provider support.

Supports easy switching between ZhipuAI, Gemini, and OpenAI.
"""

from typing import Literal, Optional
import json
import os
import logging

logger = logging.getLogger(__name__)

LLMProvider = Literal["zhipuai", "gemini", "openai"]


class LLMFactory:
    """
    Factory for creating LLM instances.
    
    Supports multiple providers with unified interface.
    """
    
    @staticmethod
    def create(
        provider: LLMProvider,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        **kwargs
    ):
        """
        Create LLM instance.
        
        Args:
            provider: "zhipuai" | "gemini" | "openai"
            model: Model name (provider-specific)
            api_key: API key (if not in env)
            **kwargs: Additional parameters (temperature, max_tokens, etc.)
        
        Returns:
            LangChain ChatModel instance
        
        Examples:
            >>> llm = LLMFactory.create("zhipuai", model="glm-4")
            >>> llm = LLMFactory.create("gemini", model="gemini-2.0-flash-exp")
        """
        if provider == "zhipuai":
            return LLMFactory._create_zhipuai(model, api_key, **kwargs)
        
        elif provider == "gemini":
            return LLMFactory._create_gemini(model, api_key, **kwargs)
        
        elif provider == "openai":
            return LLMFactory._create_openai(model, api_key, **kwargs)
        
        else:
            raise ValueError(f"Unknown provider: {provider}")
    
    @staticmethod
    def _load_zhipuai_key_from_config() -> str | None:
        """
        Load ZhipuAI API key from %APPDATA%/VCI_Proxy/config.json.
        Per project constraint: API keys stored in config.json, never in source code.
        """
        try:
            appdata = os.getenv("APPDATA", "")
            if not appdata:
                return None
            config_path = os.path.join(appdata, "VCI_Proxy", "config.json")
            if not os.path.exists(config_path):
                return None
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            key = config.get("zhipu_api_key") or config.get("zhipuai_api_key") or config.get("ZHIPUAI_API_KEY")
            if key:
                logger.debug("Loaded ZhipuAI API key from config.json")
            return key
        except Exception as e:
            logger.debug(f"Failed to read config.json: {e}")
            return None

    @staticmethod
    def _create_zhipuai(model: Optional[str], api_key: Optional[str], **kwargs):
        """
        Create ZhipuAI LLM using OpenAI-compatible adapter.

        Key resolution order:
        1. Explicit api_key parameter
        2. ZHIPUAI_API_KEY environment variable
        3. %APPDATA%/VCI_Proxy/config.json

        Model defaults to glm-4.7 with thinking disabled per project constraints.
        """
        from langchain_openai import ChatOpenAI

        api_key = (
            api_key
            or os.getenv("ZHIPUAI_API_KEY")
            or LLMFactory._load_zhipuai_key_from_config()
        )
        if not api_key:
            raise ValueError(
                "ZhipuAI API key not found. Set ZHIPUAI_API_KEY env var "
                "or add 'zhipuai_api_key' to %APPDATA%/VCI_Proxy/config.json"
            )

        # Default model: glm-4.7 per CLAUDE.md constraint
        model = model or "glm-4.7"

        # Build extra_body for ZhipuAI-specific parameters
        extra_body = kwargs.pop("extra_body", {})
        # Disable thinking per constraint: all tokens go to content output
        if "thinking" not in extra_body:
            extra_body["thinking"] = {"type": "disabled"}

        return ChatOpenAI(
            model=model,
            openai_api_key=api_key,
            openai_api_base="https://open.bigmodel.cn/api/paas/v4/",
            temperature=kwargs.get("temperature", 0.3),
            max_tokens=kwargs.get("max_tokens", 2048),
            timeout=kwargs.get("timeout", 60),
            extra_body=extra_body if extra_body else None,
        )
    
    @staticmethod
    def _create_gemini(model: Optional[str], api_key: Optional[str], **kwargs):
        """Create Google Gemini LLM."""
        from langchain_google_genai import ChatGoogleGenerativeAI
        
        api_key = api_key or os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY not found in environment")
        
        return ChatGoogleGenerativeAI(
            model=model or "gemini-2.0-flash-exp",
            google_api_key=api_key,
            temperature=kwargs.get("temperature", 0.3),
            max_output_tokens=kwargs.get("max_tokens", 2048),
            timeout=kwargs.get("timeout", 60)
        )
    
    @staticmethod
    def _create_openai(model: Optional[str], api_key: Optional[str], **kwargs):
        """Create OpenAI LLM."""
        from langchain_openai import ChatOpenAI
        
        api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY not found in environment")
        
        return ChatOpenAI(
            model=model or "gpt-4",
            api_key=api_key,
            temperature=kwargs.get("temperature", 0.3),
            max_tokens=kwargs.get("max_tokens", 2048),
            timeout=kwargs.get("timeout", 60)
        )


def create_llm(
    provider: Optional[str] = None,
    model: Optional[str] = None,
    **kwargs
):
    """
    Convenience function to create LLM.
    
    Reads provider from environment if not specified.
    
    Args:
        provider: LLM provider (defaults to LLM_PROVIDER env var)
        model: Model name (defaults to LLM_MODEL env var)
        **kwargs: Additional parameters
    
    Returns:
        LangChain ChatModel instance
    
    Examples:
        >>> # Using environment variables
        >>> llm = create_llm()
        
        >>> # Explicit provider
        >>> llm = create_llm(provider="gemini", model="gemini-2.0-flash-exp")
    """
    provider = provider or os.getenv("LLM_PROVIDER", "zhipuai")
    model = model or os.getenv("LLM_MODEL")
    
    return LLMFactory.create(provider, model, **kwargs)
