import sys
from pathlib import Path

# The backend modules import each other flatly (`import buckets`), matching how uvicorn
# runs them from inside backend/. Put that directory on the path for tests too.
sys.path.insert(0, str(Path(__file__).resolve().parent / "backend"))
