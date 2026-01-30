import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from src.workflows import ReadDataDisplayAgentWorkflow
print("Import OK")
w = ReadDataDisplayAgentWorkflow()
print(f"Workflow: {w.name}")
print(f"Description: {w.description}")
