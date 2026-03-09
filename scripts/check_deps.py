#!/usr/bin/env python
"""
Quick dependency check - verifies if required packages are installed.
"""

import sys

def check_dependencies():
    """Check if all required dependencies are installed."""
    required = {
        'langgraph': 'LangGraph state machine',
        'langchain': 'LangChain core',
        'langchain_community': 'LangChain community',
        'langchain_openai': 'LangChain OpenAI adapter',
        'lancedb': 'LanceDB vector database',
        'sentence_transformers': 'Sentence Transformers embeddings',
    }
    
    print("Checking dependencies...\n")
    
    missing = []
    installed = []
    
    for package, description in required.items():
        try:
            __import__(package)
            print(f"[OK] {package:25s} - {description}")
            installed.append(package)
        except ImportError:
            print(f"[MISSING] {package:25s} - {description}")
            missing.append(package)
    
    print("\n" + "="*60)
    print(f"Installed: {len(installed)}/{len(required)}")
    
    if missing:
        print(f"\nMissing packages: {', '.join(missing)}")
        print("\nTo install missing packages, run:")
        print(f"  pip install {' '.join(missing)}")
        return 1
    else:
        print("\n[SUCCESS] All dependencies are installed!")
        return 0

if __name__ == "__main__":
    sys.exit(check_dependencies())
