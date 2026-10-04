"""Run with the project's Python. An offline MCP tools/resources/prompts example."""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("myclaude-learning")


@mcp.tool()
def add(a: float, b: float) -> float:
    """Add two numbers without accessing files or network."""
    return a + b


@mcp.resource("learning://agent-loop")
def agent_loop() -> str:
    return "Agent loop: model -> tool call -> permission check -> tool result -> model."


@mcp.prompt()
def explain_agent(topic: str = "tool calling") -> str:
    return f"Explain {topic} with a concrete Agent loop example."


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    mcp.settings.host = "127.0.0.1"
    mcp.settings.port = args.port
    mcp.run(transport=args.transport)
