"""
AI-powered vehicle diagnosis module.

Components:
- LLMClient: ZhipuAI streaming wrapper + prompt assembly
- AIEngine: Orchestration (collector → buffer → DTCs → LLM → SSE)
"""

from .llm_client import LLMClient
from .ai_engine import AIEngine

__all__ = ['LLMClient', 'AIEngine']
