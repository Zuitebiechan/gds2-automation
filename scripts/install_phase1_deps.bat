@echo off
REM Phase 1 Dependencies Installation Script
REM Run this script to install all required dependencies

echo Installing Phase 1 dependencies...
echo.

echo [1/3] Installing core LangGraph and LangChain...
pip install langgraph>=0.2.0 langchain>=0.3.0 langchain-community>=0.3.0 langchain-openai>=0.2.0 langchain-google-genai>=0.0.5

echo.
echo [2/3] Installing vector DB and embeddings...
pip install lancedb>=0.5.0 sentence-transformers>=2.2.0

echo.
echo [3/3] Installing image processing (if not already installed)...
pip install pillow>=10.0.0

echo.
echo Installation complete!
echo.
echo Next steps:
echo 1. Copy .env.example to .env
echo 2. Add your API keys to .env
echo 3. Run: python scripts/test_imports.py
echo.
pause
