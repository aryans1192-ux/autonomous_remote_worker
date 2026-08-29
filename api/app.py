import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from langchain_mcp_adapters.client import MultiServerMCPClient

from agent.graph import build_graph
from api.routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[Agent] Building LangGraph agent...")
    mcp_tools = []
    try:
        client = MultiServerMCPClient({
            "aeps-rca": {
                "url": "http://127.0.0.1:8001/mcp",
                "transport": "streamable_http",
            }
        })
        mcp_tools = await asyncio.wait_for(client.get_tools(), timeout=10.0)
        print(f"[MCP] Loaded {len(mcp_tools)} tool(s) from MCP server")
    except asyncio.TimeoutError:
        print("[MCP] Timeout after 10s. Using direct tools.")
    except Exception as e:
        print(f"[MCP] Failed ({type(e).__name__}: {e}). Using direct tools.")

    app.state.graph = build_graph(mcp_tools=mcp_tools)
    print("[Agent] Ready.")
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Eko AePS RCA Worker API",
        description="Transaction-Failure Root Cause Analysis Worker for Eko AePS cash-out operations",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    return app


app = create_app()
