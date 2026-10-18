# ============================================================
# COMPONENT: C11a / Cloud Entrypoint - Hugging Face Space App
# PURPOSE: Space entrypoint serving FastAPI backend and interactive frontend on port 7860.
# INPUT: HTTP requests from Hugging Face Space proxy.
# OUTPUT: Full aviation safety investigation dashboard and REST API.
# CALLED BY: Hugging Face Space runner      CALLS: api.main
# IF IT BREAKS, CHECK: PORT environment variable, database initialization, static file mounts.
# ============================================================

import os
import uvicorn
from api.main import app, init_db_if_missing

# Ensure replay-only mode and default paths in cloud deployment
os.environ.setdefault("ALLOW_LIVE", "false")
os.environ.setdefault("DATA_DIR", "data")

# Initialize database if missing on startup
init_db_if_missing()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    uvicorn.run(app, host="0.0.0.0", port=port)
