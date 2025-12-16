# AgentCore Runtime Deployment

Deploy a Valyu-powered research agent to AWS Bedrock AgentCore Runtime.

## Architecture

```
User Request
     │
     ▼
┌─────────────────────────────┐
│   AgentCore Runtime         │
│   (Serverless container)    │
│                             │
│   ┌─────────────────────┐   │
│   │   Strands Agent     │   │
│   └──────────┬──────────┘   │
│              │              │
└──────────────┼──────────────┘
               │ MCP + OAuth
               ▼
┌─────────────────────────────┐
│   AgentCore Gateway         │
│   (Centralized tool access) │
└──────────────┬──────────────┘
               │
               ▼
        ┌──────────────┐
        │  Valyu MCP   │
        └──────────────┘
```

## Prerequisites

1. AWS CLI configured (`aws configure`)
2. Python 3.10+
3. Valyu API key from [platform.valyu.ai](https://platform.valyu.ai)

## Complete Setup (Start to Finish)

### Step 1: Install dependencies

```bash
pip install "valyu-agentcore[agentcore]"
```

### Step 2: Set up Gateway with Valyu

First, create a Gateway with Valyu tools:

```bash
cd /path/to/valyu-agentcore/examples/gateway

export VALYU_API_KEY=your-valyu-api-key

python setup_gateway.py
```

This creates `valyu_gateway_config.json` in the gateway folder.

### Step 3: Copy config to runtime folder

```bash
cp valyu_gateway_config.json ../runtime/
cd ../runtime
```

### Step 4: Configure and deploy the agent

```bash
# Configure (creates .bedrock_agentcore.yaml)
agentcore configure --entrypoint agent.py --non-interactive --name valyuagent

# Deploy to AWS (uses CodeBuild, no Docker needed)
agentcore launch

# Test it
agentcore invoke '{"prompt": "What is the current stock price of NVIDIA?"}'
```

## That's it!

The agent is now running in AWS and can answer questions using Valyu search tools.

## Alternative: Environment Variables

Instead of copying the config file, you can set environment variables when deploying:

```bash
agentcore launch \
  --env GATEWAY_URL=https://your-gateway.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp \
  --env COGNITO_CLIENT_ID=your-client-id \
  --env COGNITO_CLIENT_SECRET=your-client-secret \
  --env COGNITO_DOMAIN=your-cognito-domain \
  --env COGNITO_SCOPE=your-gateway-name/invoke
```

## How It Works

1. Agent starts in Runtime container
2. Reads Cognito credentials from config file or environment
3. Gets fresh OAuth token from Cognito on each request
4. Calls Gateway with Bearer token
5. Gateway routes request to Valyu MCP
6. Valyu returns search results
7. Agent uses results to answer the question

## Files

| File | Purpose |
|------|---------|
| `agent.py` | The agent code that runs in Runtime |
| `requirements.txt` | Python dependencies |
| `valyu_gateway_config.json` | Gateway credentials (you copy this from gateway folder) |

## Troubleshooting

**"No such file: valyu_gateway_config.json"**
- You need to copy the config from the gateway folder
- Run: `cp ../gateway/valyu_gateway_config.json .`

**"GATEWAY_URL not found"**
- Either copy the config file OR set environment variables
- See "Alternative: Environment Variables" above

**Token/auth errors**
- Make sure you ran `setup_gateway.py` first
- Check that the config file has valid credentials

**"Agent not found"**
- Run `agentcore configure` before `agentcore launch`

## Cleanup

To delete the deployed agent:

```bash
# List agents
aws bedrock-agentcore-control list-agent-runtimes --region us-east-1

# Delete agent
aws bedrock-agentcore-control delete-agent-runtime \
  --agent-runtime-id YOUR_AGENT_ID \
  --region us-east-1
```

To delete the Gateway (from the gateway folder):

```bash
cd ../gateway
python setup_gateway.py cleanup
```
