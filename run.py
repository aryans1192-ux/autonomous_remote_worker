import sys
import time
import logging
import subprocess
import uvicorn
from db.database import setup

logging.basicConfig(
    level=logging.DEBUG if "--debug" in sys.argv else logging.INFO,
    format="%(levelname)s [%(name)s] %(message)s"
)

if __name__ == "__main__":
    print("[Startup] Loading CSV into DuckDB...")
    setup()

    print("[MCP] Starting MCP server on port 8001...")
    mcp_proc = subprocess.Popen(
        [sys.executable, "mcp_server/server.py", "--transport", "streamable-http"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(3)
    print("[MCP] MCP server ready.")

    print("[Startup] Starting API server at http://localhost:8000")
    print("[Startup] Start the frontend separately: streamlit run frontend/app.py")
    try:
        uvicorn.run("api.app:app", host="0.0.0.0", port=8000, reload=False)
    finally:
        mcp_proc.terminate()
