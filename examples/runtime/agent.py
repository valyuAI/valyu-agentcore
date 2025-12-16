"""
Valyu Research Agent - AgentCore Runtime Deployment

This agent deploys to AWS Bedrock AgentCore Runtime and accesses Valyu tools
through AgentCore Gateway:

    User → AgentCore Runtime → AgentCore Gateway → Valyu MCP

Setup:
    1. Run setup_gateway.py in examples/gateway/ first
    2. Copy valyu_gateway_config.json to this folder
    3. agentcore configure --entrypoint agent.py --non-interactive --name valyuagent
    4. agentcore launch
    5. agentcore invoke '{"prompt": "What is NVIDIA stock price?"}'

See README.md for complete instructions.
"""

import os
import json
import httpx
from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()


def get_gateway_config():
    """Load gateway configuration from file or environment variables."""
    # Try config file first (check both naming conventions)
    config_path = os.environ.get("GATEWAY_CONFIG_PATH")
    if not config_path:
        if os.path.exists("gateway_config.json"):
            config_path = "gateway_config.json"
        elif os.path.exists("valyu_gateway_config.json"):
            config_path = "valyu_gateway_config.json"

    if config_path and os.path.exists(config_path):
        with open(config_path, "r") as f:
            config = json.load(f)

        # Handle both config formats:
        # Format 1: From setup_valyu_gateway() - flat structure with cognito_* keys
        # Format 2: From agentcore toolkit - nested client_info structure
        if "cognito_client_id" in config:
            # Format 1: valyu_gateway_config.json from setup_valyu_gateway()
            return {
                "gateway_url": config["gateway_url"],
                "client_id": config["cognito_client_id"],
                "client_secret": config["cognito_client_secret"],
                "domain": config["cognito_domain"],
                "scope": config.get("cognito_scope"),
                "region": config.get("region", "us-east-1"),
            }
        else:
            # Format 2: gateway_config.json from agentcore toolkit
            return {
                "gateway_url": config["gateway_url"],
                "client_id": config["client_info"]["client_id"],
                "client_secret": config["client_info"]["client_secret"],
                "domain": config["client_info"]["domain_prefix"],
                "scope": config["client_info"].get("scope"),
                "region": config.get("region", "us-east-1"),
            }

    # Fall back to environment variables
    return {
        "gateway_url": os.environ["GATEWAY_URL"],
        "client_id": os.environ["COGNITO_CLIENT_ID"],
        "client_secret": os.environ["COGNITO_CLIENT_SECRET"],
        "domain": os.environ["COGNITO_DOMAIN"],
        "scope": os.environ.get("COGNITO_SCOPE"),
        "region": os.environ.get("AWS_REGION", "us-east-1"),
    }


def get_access_token(config: dict) -> str:
    """Get OAuth access token from Cognito."""
    token_url = f"https://{config['domain']}.auth.{config['region']}.amazoncognito.com/oauth2/token"

    response = httpx.post(
        token_url,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "client_credentials",
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
            "scope": config["scope"] or "",
        },
    )
    response.raise_for_status()
    return response.json()["access_token"]


def create_agent():
    """Create agent connected to Gateway with fresh OAuth token."""
    from datetime import datetime
    from strands import Agent
    from strands.models import BedrockModel
    from strands.tools.mcp.mcp_client import MCPClient
    from mcp.client.streamable_http import streamablehttp_client

    # Get configuration and fresh access token
    config = get_gateway_config()
    access_token = get_access_token(config)

    # Get current date for system prompt
    current_date = datetime.now().strftime("%B %d, %Y")

    def create_transport():
        return streamablehttp_client(
            config["gateway_url"],
            headers={"Authorization": f"Bearer {access_token}"}
        )

    mcp_client = MCPClient(create_transport)
    mcp_client.__enter__()

    tools = mcp_client.list_tools_sync()

    agent = Agent(
        model=BedrockModel(
            model_id="us.anthropic.claude-sonnet-4-20250514-v1:0",
            region_name=config["region"],
        ),
        tools=tools,
        system_prompt=f"""You are a professional research analyst with access to Valyu search tools.

CRITICAL DATE CONTEXT:
- Today's date: {current_date}
- Current year: {datetime.now().year}

SEARCH GUIDELINES:
When searching for "latest", "recent", "new", or "current" information, ALWAYS include the year {datetime.now().year} in your search query to get current results.
- Correct: "AWS Nova models December 2025"
- Incorrect: "latest AWS models" (will return outdated results)

AVAILABLE TOOLS:
- valyu_search: Web search (include year for recent events)
- valyu_academic_search: Academic papers from arXiv, PubMed
- valyu_financial_search: Stock prices, earnings, market data
- valyu_sec_search: SEC filings (10-K, 10-Q, 8-K)
- valyu_patents: Patent search
- valyu_contents: Extract content from URLs

RESPONSE GUIDELINES:
- Be professional and concise
- Do NOT use emojis in your responses
- Use clean markdown formatting with proper headers
- Structure responses with clear sections
- ALWAYS add inline citations immediately after facts: "fact ([source](url))"
- Example: "Tesla stock rose 15% ([title](https://...))"
- Never state facts from search results without an inline citation""",
    )

    return agent, mcp_client


@app.entrypoint
def invoke(payload: dict) -> str:
    """Process user input and return a response."""
    prompt = payload.get("prompt", "Hello, how can I help with research?")

    agent, mcp_client = create_agent()
    try:
        response = agent(prompt)
        return str(response)
    finally:
        mcp_client.__exit__(None, None, None)


@app.entrypoint
async def stream(payload: dict):
    """Stream agent responses for real-time output."""
    prompt = payload.get("prompt", "Hello, how can I help with research?")

    agent, mcp_client = create_agent()
    try:
        async for event in agent.stream_async(prompt):
            yield event
    finally:
        mcp_client.__exit__(None, None, None)


if __name__ == "__main__":
    app.run()
