"""
Valyu + AWS Bedrock AgentCore Gateway Integration

Connect to Valyu search tools through AgentCore Gateway for:
- Centralized API key management (store Valyu key in AWS, not in code)
- Audit logging of all tool calls
- Enterprise auth via Cognito/OAuth
- Unified MCP gateway for multiple tools

Quick Start (Add to Existing Gateway):
    from valyu_agentcore.gateway import add_valyu_target

    # Add Valyu tools to your existing gateway
    add_valyu_target(gateway_id="your-gateway-id")

Quick Start (Create New Gateway):
    from valyu_agentcore.gateway import setup_valyu_gateway, GatewayAgent

    # 1. Set up gateway (one-time)
    config = setup_valyu_gateway(valyu_api_key="your-key")

    # 2. Use the agent
    with GatewayAgent.from_config() as agent:
        response = agent("Search for NVIDIA SEC filings")

For direct tool usage without Gateway:
    from valyu_agentcore import webSearch, secSearch
"""

import json
import os
from typing import Optional
from dataclasses import dataclass, field
from urllib.parse import quote


# =============================================================================
# Constants
# =============================================================================

VALYU_MCP_URL = "https://mcp.valyu.ai/mcp"


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class GatewayConfig:
    """Configuration for AgentCore Gateway connection."""
    gateway_id: str
    gateway_url: str
    target_id: str
    region: str = "us-east-1"
    cognito_client_id: Optional[str] = None
    cognito_client_secret: Optional[str] = None
    cognito_user_pool_id: Optional[str] = None
    cognito_domain: Optional[str] = None
    cognito_scope: Optional[str] = None

    def save(self, path: str = "valyu_gateway_config.json"):
        """Save config to JSON file."""
        with open(path, "w") as f:
            json.dump(self.__dict__, f, indent=2)
        print(f"Config saved to {path}")

    @classmethod
    def load(cls, path: str = "valyu_gateway_config.json") -> "GatewayConfig":
        """Load config from JSON file."""
        with open(path, "r") as f:
            data = json.load(f)
        return cls(**data)


# =============================================================================
# Gateway Setup Functions
# =============================================================================

def setup_valyu_gateway(
    valyu_api_key: Optional[str] = None,
    gateway_name: str = "valyu-search-gateway",
    target_name: str = "valyu-mcp",
    region: str = "us-east-1",
    save_config: bool = True,
    config_path: str = "valyu_gateway_config.json",
) -> GatewayConfig:
    """
    Set up AgentCore Gateway with Valyu MCP server as a target.

    This creates:
    1. A Cognito user pool for OAuth authentication
    2. An AgentCore Gateway with MCP protocol
    3. A Valyu MCP server target with API key auth

    Args:
        valyu_api_key: Your Valyu API key (or uses VALYU_API_KEY env var)
        gateway_name: Name for the gateway
        target_name: Name for the Valyu target
        region: AWS region
        save_config: Whether to save config to file
        config_path: Path to save config

    Returns:
        GatewayConfig with all connection details

    Example:
        config = setup_valyu_gateway(valyu_api_key="your-key")
        print(f"Gateway URL: {config.gateway_url}")
    """
    try:
        from bedrock_agentcore_starter_toolkit.operations.gateway.client import GatewayClient
    except ImportError:
        raise ImportError(
            "AgentCore starter toolkit required. Install with:\n"
            "pip install valyu-agentcore[agentcore]"
        )

    import boto3

    # Get API key
    api_key = valyu_api_key or os.environ.get("VALYU_API_KEY")
    if not api_key:
        raise ValueError(
            "Valyu API key required. Provide valyu_api_key or set VALYU_API_KEY env var.\n"
            "Get your key at: https://platform.valyu.ai"
        )

    print(f"Setting up AgentCore Gateway in {region}...")

    # Initialize clients
    toolkit_client = GatewayClient(region_name=region)
    boto_client = boto3.client("bedrock-agentcore-control", region_name=region)

    # Step 1: Create Cognito OAuth authorizer
    print("1. Creating Cognito OAuth authorizer...")
    cognito_response = toolkit_client.create_oauth_authorizer_with_cognito(gateway_name)
    print("   Done")

    # Step 2: Create Gateway
    print("2. Creating AgentCore Gateway...")
    gateway = toolkit_client.create_mcp_gateway(
        name=gateway_name,
        authorizer_config=cognito_response["authorizer_config"],
        enable_semantic_search=True,
    )
    gateway_id = gateway["gatewayId"]
    gateway_url = gateway["gatewayUrl"]
    print(f"   Gateway URL: {gateway_url}")

    # Fix IAM permissions
    toolkit_client.fix_iam_permissions(gateway)
    print("   Waiting for IAM propagation (30s)...")
    import time
    time.sleep(30)

    # Step 3: Create Valyu MCP target
    # The Valyu MCP URL includes the API key as a query parameter
    print("3. Adding Valyu MCP server as target...")

    valyu_mcp_endpoint = f"{VALYU_MCP_URL}?valyuApiKey={quote(api_key)}"

    # Create the MCP server target
    target_response = boto_client.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name=target_name,
        description="Valyu search tools - web, finance, SEC, patents, papers, bio, economics",
        targetConfiguration={
            "mcp": {
                "mcpServer": {
                    "endpoint": valyu_mcp_endpoint
                }
            }
        },
    )
    target_id = target_response["targetId"]
    print(f"   Target ID: {target_id}")

    # Wait for target to be ready
    print("   Waiting for target synchronization...")
    import time
    for _ in range(12):  # Wait up to 2 minutes
        time.sleep(10)
        target_status = boto_client.get_gateway_target(
            gatewayIdentifier=gateway_id,
            targetId=target_id,
        )
        status = target_status.get("status", "UNKNOWN")
        if status == "READY":
            print("   Target ready!")
            break
        elif status == "FAILED":
            raise RuntimeError(f"Target creation failed: {target_status}")
        print(f"   Status: {status}...")

    # Build config - extract client_info from cognito_response
    client_info = cognito_response.get("client_info", {})
    config = GatewayConfig(
        gateway_id=gateway_id,
        gateway_url=gateway_url,
        target_id=target_id,
        region=region,
        cognito_client_id=client_info.get("client_id"),
        cognito_client_secret=client_info.get("client_secret"),
        cognito_user_pool_id=client_info.get("user_pool_id"),
        cognito_domain=client_info.get("domain_prefix"),
        cognito_scope=client_info.get("scope"),
    )

    if save_config:
        config.save(config_path)

    print("\nSetup complete!")
    print(f"Gateway URL: {gateway_url}")
    print(f"Config saved to: {config_path}")

    return config


def cleanup_valyu_gateway(
    config_path: str = "valyu_gateway_config.json",
    region: Optional[str] = None,
):
    """
    Clean up Gateway resources created by setup_valyu_gateway.

    Args:
        config_path: Path to config file
        region: AWS region (uses config if not provided)
    """
    try:
        from bedrock_agentcore_starter_toolkit.operations.gateway.client import GatewayClient
    except ImportError:
        raise ImportError(
            "AgentCore starter toolkit required. Install with:\n"
            "pip install valyu-agentcore[agentcore]"
        )

    import boto3

    config = GatewayConfig.load(config_path)
    region = region or config.region

    print(f"Cleaning up Gateway resources in {region}...")

    toolkit_client = GatewayClient(region_name=region)
    boto_client = boto3.client("bedrock-agentcore-control", region_name=region)

    # Delete target
    print(f"Deleting target {config.target_id}...")
    try:
        boto_client.delete_gateway_target(
            gatewayIdentifier=config.gateway_id,
            targetId=config.target_id,
        )
    except Exception as e:
        print(f"  Warning: {e}")

    # Delete gateway
    print(f"Deleting gateway {config.gateway_id}...")
    try:
        boto_client.delete_gateway(gatewayIdentifier=config.gateway_id)
    except Exception as e:
        print(f"  Warning: {e}")

    # Delete Cognito resources
    if config.cognito_user_pool_id:
        print("Deleting Cognito resources...")
        try:
            cognito_client = boto3.client("cognito-idp", region_name=region)
            cognito_client.delete_user_pool(UserPoolId=config.cognito_user_pool_id)
        except Exception as e:
            print(f"  Warning: {e}")

    print("Cleanup complete!")


# =============================================================================
# Add to Existing Gateway
# =============================================================================

def add_valyu_target(
    gateway_id: str,
    valyu_api_key: Optional[str] = None,
    region: str = "us-east-1",
    target_name: str = "valyu-search",
) -> dict:
    """
    Add Valyu MCP server as a target to an existing AgentCore Gateway.

    This is the recommended approach for enterprises that already have
    an AgentCore Gateway. Just add Valyu as a target to get access to
    all Valyu search tools.

    Args:
        gateway_id: Your existing gateway ID (e.g., "my-gateway-abc123")
        valyu_api_key: Your Valyu API key (or uses VALYU_API_KEY env var)
        region: AWS region where your gateway is deployed
        target_name: Name for the Valyu target (default: "valyu-search")

    Returns:
        dict with target_id, gateway_id, and status

    Example:
        from valyu_agentcore.gateway import add_valyu_target

        result = add_valyu_target(
            gateway_id="my-existing-gateway",
            valyu_api_key="your-key",
        )
        print(f"Added target: {result['target_id']}")
    """
    import boto3
    import time

    # Get API key
    api_key = valyu_api_key or os.environ.get("VALYU_API_KEY")
    if not api_key:
        raise ValueError(
            "Valyu API key required. Provide valyu_api_key or set VALYU_API_KEY env var.\n"
            "Get your key at: https://platform.valyu.ai"
        )

    client = boto3.client("bedrock-agentcore-control", region_name=region)

    # Build the MCP endpoint URL with API key
    valyu_mcp_endpoint = f"{VALYU_MCP_URL}?valyuApiKey={quote(api_key)}"

    print(f"Adding Valyu target to gateway: {gateway_id}")

    # Create the target
    response = client.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name=target_name,
        description="Valyu search tools - web, finance, SEC filings, patents, academic papers, company research",
        targetConfiguration={
            "mcp": {
                "mcpServer": {
                    "endpoint": valyu_mcp_endpoint
                }
            }
        },
    )

    target_id = response["targetId"]
    print(f"Target created: {target_id}")

    # Wait for target to sync
    print("Waiting for target to sync...")
    for _ in range(12):  # Up to 2 minutes
        time.sleep(10)
        status_response = client.get_gateway_target(
            gatewayIdentifier=gateway_id,
            targetId=target_id,
        )
        status = status_response.get("status", "UNKNOWN")

        if status == "READY":
            print("Target ready!")
            break
        elif status == "FAILED":
            error = status_response.get("statusReasons", [])
            raise RuntimeError(f"Target sync failed: {error}")
        print(f"  Status: {status}...")

    print(f"\nValyu tools now available through your gateway!")

    return {
        "target_id": target_id,
        "gateway_id": gateway_id,
        "region": region,
        "status": "READY",
    }


# =============================================================================
# Gateway Agent
# =============================================================================

def get_access_token(config: GatewayConfig) -> str:
    """Get OAuth access token from Cognito."""
    import httpx

    if not all([config.cognito_client_id, config.cognito_client_secret, config.cognito_domain]):
        raise ValueError("Cognito credentials not found in config")

    token_url = f"https://{config.cognito_domain}.auth.{config.region}.amazoncognito.com/oauth2/token"

    # Use the scope from config, or default to gateway invoke scope
    scope = config.cognito_scope or f"{config.gateway_id.split('-')[0]}-search-gateway/invoke"

    response = httpx.post(
        token_url,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "client_credentials",
            "client_id": config.cognito_client_id,
            "client_secret": config.cognito_client_secret,
            "scope": scope,
        },
    )
    response.raise_for_status()

    return response.json()["access_token"]


class GatewayAgent:
    """
    Agent connected to Valyu tools through AgentCore Gateway.

    Handles MCP client lifecycle and authentication automatically.

    Example:
        # Using saved config
        with GatewayAgent.from_config() as agent:
            response = agent("Search for NVIDIA SEC filings")
            print(response)

        # Manual setup
        with GatewayAgent(
            gateway_url="https://xxx.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
            access_token="eyJ...",
        ) as agent:
            response = agent("Research Tesla financials")
    """

    def __init__(
        self,
        gateway_url: str,
        access_token: str,
        model_id: str = "us.anthropic.claude-sonnet-4-20250514-v1:0",
        region: str = "us-east-1",
        system_prompt: Optional[str] = None,
    ):
        self.gateway_url = gateway_url
        self.access_token = access_token
        self.model_id = model_id
        self.region = region
        self.system_prompt = system_prompt or self._default_system_prompt()
        self._mcp_client = None
        self._agent = None

    @staticmethod
    def _default_system_prompt() -> str:
        return """You are a research assistant with access to Valyu search tools via AgentCore Gateway.

Available tools:
- valyu_search: Web search for current information, news, articles
- valyu_academic_search: Academic papers from arXiv, PubMed, journals
- valyu_financial_search: Stock prices, earnings, market data
- valyu_sec_search: SEC filings (10-K, 10-Q, 8-K) with section search
- valyu_patents: USPTO patent search
- valyu_bio_search: Biomedical data - PubMed, clinical trials, FDA drug labels, bioRxiv, medRxiv
- valyu_company_research: Company information, profiles, financials
- valyu_contents: Extract full content from any URL

Always cite sources with markdown links."""

    @classmethod
    def from_config(
        cls,
        config_path: str = "valyu_gateway_config.json",
        model_id: str = "us.anthropic.claude-sonnet-4-20250514-v1:0",
        system_prompt: Optional[str] = None,
    ) -> "GatewayAgent":
        """
        Create GatewayAgent from saved config file.

        Args:
            config_path: Path to config JSON file
            model_id: Bedrock model ID
            system_prompt: Custom system prompt

        Returns:
            GatewayAgent instance (use with 'with' statement)
        """
        config = GatewayConfig.load(config_path)
        access_token = get_access_token(config)

        return cls(
            gateway_url=config.gateway_url,
            access_token=access_token,
            model_id=model_id,
            region=config.region,
            system_prompt=system_prompt,
        )

    def __enter__(self):
        try:
            from strands import Agent
            from strands.models import BedrockModel
            from strands.tools.mcp.mcp_client import MCPClient
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError:
            raise ImportError(
                "Strands and MCP packages required. Install with:\n"
                "pip install valyu-agentcore[agentcore]"
            )

        def create_transport():
            return streamablehttp_client(
                self.gateway_url,
                headers={"Authorization": f"Bearer {self.access_token}"}
            )

        self._mcp_client = MCPClient(create_transport)
        self._mcp_client.__enter__()

        # Get all tools from Gateway
        tools = self._mcp_client.list_tools_sync()

        self._agent = Agent(
            model=BedrockModel(
                model_id=self.model_id,
                region_name=self.region,
                streaming=True,
            ),
            tools=tools,
            system_prompt=self.system_prompt,
        )

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._mcp_client:
            self._mcp_client.__exit__(exc_type, exc_val, exc_tb)

    def __call__(self, prompt: str):
        """Send a prompt to the agent."""
        if self._agent is None:
            raise RuntimeError("Agent not initialized. Use 'with' statement.")
        return self._agent(prompt)

    def list_tools(self) -> list:
        """List available tools from Gateway."""
        if self._mcp_client is None:
            raise RuntimeError("Not connected. Use 'with' statement.")
        return self._mcp_client.list_tools_sync()


# =============================================================================
# CloudFormation Template Generation
# =============================================================================

def generate_cloudformation_template(
    valyu_api_key_secret_arn: str,
    gateway_name: str = "ValyuSearchGateway",
    output_path: Optional[str] = None,
) -> str:
    """
    Generate a CloudFormation template for Valyu + AgentCore Gateway.

    Args:
        valyu_api_key_secret_arn: ARN of Secrets Manager secret containing Valyu API key
        gateway_name: Name for the gateway
        output_path: Optional path to save template

    Returns:
        CloudFormation template as YAML string
    """
    template = f"""AWSTemplateFormatVersion: '2010-09-09'
Description: Valyu Search Tools + AgentCore Gateway Integration

Parameters:
  GatewayName:
    Type: String
    Default: {gateway_name}
    Description: Name for the AgentCore Gateway

  ValyuApiKeySecretArn:
    Type: String
    Default: {valyu_api_key_secret_arn}
    Description: ARN of Secrets Manager secret containing Valyu API key

Resources:
  # IAM Role for Gateway
  GatewayRole:
    Type: AWS::IAM::Role
    Properties:
      RoleName: !Sub '${{GatewayName}}-role'
      AssumeRolePolicyDocument:
        Version: '2012-10-17'
        Statement:
          - Effect: Allow
            Principal:
              Service: bedrock-agentcore.amazonaws.com
            Action: sts:AssumeRole
      Policies:
        - PolicyName: GatewayPolicy
          PolicyDocument:
            Version: '2012-10-17'
            Statement:
              - Effect: Allow
                Action:
                  - secretsmanager:GetSecretValue
                Resource: !Ref ValyuApiKeySecretArn

  # AgentCore Gateway
  ValyuGateway:
    Type: AWS::BedrockAgentCore::Gateway
    Properties:
      Name: !Ref GatewayName
      Description: Gateway for Valyu search tools
      AuthorizerType: AWS_IAM
      ProtocolType: MCP
      RoleArn: !GetAtt GatewayRole.Arn
      ProtocolConfiguration:
        mcp:
          supportedVersions:
            - '2025-03-26'
          searchType: SEMANTIC

Outputs:
  GatewayId:
    Description: Gateway ID
    Value: !Ref ValyuGateway

  GatewayUrl:
    Description: Gateway MCP endpoint URL
    Value: !GetAtt ValyuGateway.GatewayUrl

  GatewayArn:
    Description: Gateway ARN
    Value: !GetAtt ValyuGateway.GatewayArn

# Note: After deploying this stack, add the Valyu MCP target manually:
#
# aws bedrock-agentcore-control create-gateway-target \\
#   --gateway-identifier <GatewayId> \\
#   --name valyu-search \\
#   --target-configuration '{{
#     "mcp": {{
#       "mcpServer": {{
#         "endpoint": "https://mcp.valyu.ai/mcp?valyuApiKey=YOUR_KEY"
#       }}
#     }}
#   }}'
"""

    if output_path:
        with open(output_path, "w") as f:
            f.write(template)
        print(f"Template saved to {output_path}")

    return template
