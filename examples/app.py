"""
Valyu AgentCore - Streamlit Demo

Run with: streamlit run examples/app.py

Modes:
- Local: Direct Valyu API calls (requires VALYU_API_KEY)
- Gateway: Local agent + AgentCore Gateway (requires gateway config)
- Runtime: AgentCore Runtime serverless (requires deployed runtime)

For gateway/runtime mode, run from the gateway directory:
    cd examples/gateway && streamlit run ../app.py
"""

import os
import json
import subprocess
import streamlit as st
from queue import Queue
from threading import Thread
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

st.set_page_config(
    page_title="Valyu AgentCore",
    page_icon="🔍",
    layout="wide",
)

# Check for gateway config
GATEWAY_CONFIG = None
for path in [
    "valyu_gateway_config.json",
    "gateway/valyu_gateway_config.json",
    "runtime/valyu_gateway_config.json",
    "../gateway/valyu_gateway_config.json",
    "../runtime/valyu_gateway_config.json",
    "examples/gateway/valyu_gateway_config.json",
    "examples/runtime/valyu_gateway_config.json",
]:
    if os.path.exists(path):
        GATEWAY_CONFIG = path
        break


def check_runtime_available(debug=False):
    """Check if AgentCore Runtime is deployed and available."""
    # Look for runtime directory with agentcore config
    runtime_dirs = [
        ".",  # Current directory if running from runtime/
        "runtime",
        "../runtime",
        "examples/runtime",
        "/Users/harveyyorke/dev/valyu-agentcore/examples/runtime",
    ]

    runtime_dir = None
    for d in runtime_dirs:
        # Check for either config format
        if os.path.exists(os.path.join(d, ".bedrock_agentcore")) or \
           os.path.exists(os.path.join(d, ".bedrock_agentcore.yaml")) or \
           os.path.exists(os.path.join(d, ".agentcore")):
            runtime_dir = d
            break

    if not runtime_dir:
        if debug:
            return {"error": "No agentcore config found in runtime directories", "searched": runtime_dirs}
        return False

    try:
        result = subprocess.run(
            ["agentcore", "status"],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=runtime_dir  # Run from runtime directory
        )
        output = result.stdout
        output_upper = output.upper()

        if debug:
            return {
                "returncode": result.returncode,
                "cwd": runtime_dir,
                "stdout": output[:500],
                "stderr": result.stderr[:200] if result.stderr else "",
            }

        # Check for various indicators that runtime is ready
        if result.returncode == 0 and (
            "(READY)" in output_upper or
            "READY -" in output_upper or
            "READY TO INVOKE" in output_upper or
            "ENDPOINT:" in output_upper
        ):
            return True
        return False
    except subprocess.TimeoutExpired:
        return {"error": "timeout"} if debug else False
    except FileNotFoundError:
        return {"error": "agentcore not found"} if debug else False
    except Exception as e:
        return {"error": str(e)} if debug else False


def get_runtime_config():
    """Get runtime ARN and region from agentcore config."""
    runtime_dirs = [
        ".",  # Current directory if running from runtime/
        "runtime",
        "../runtime",
        "examples/runtime",
        "/Users/harveyyorke/dev/valyu-agentcore/examples/runtime",
    ]

    for d in runtime_dirs:
        yaml_path = os.path.join(d, ".bedrock_agentcore.yaml")
        if os.path.exists(yaml_path):
            try:
                import yaml
                with open(yaml_path, 'r') as f:
                    config = yaml.safe_load(f)

                # Get default agent name
                default_agent = config.get("default_agent")
                if default_agent and "agents" in config:
                    agent_config = config["agents"].get(default_agent, {})
                    bedrock_config = agent_config.get("bedrock_agentcore", {})
                    aws_config = agent_config.get("aws", {})

                    return {
                        "runtime_dir": d,
                        "agent_arn": bedrock_config.get("agent_arn"),
                        "agent_id": bedrock_config.get("agent_id"),
                        "region": aws_config.get("region", "us-east-1"),
                    }
            except Exception:
                pass

    return None


def invoke_runtime_streaming(prompt: str, queue: Queue):
    """Invoke AgentCore Runtime with streaming response using boto3."""
    try:
        import boto3

        config = get_runtime_config()
        if not config or not config.get("agent_arn"):
            # Fallback to CLI if no ARN
            queue.put(("done", invoke_runtime_cli(prompt)))
            return

        from botocore.config import Config
        boto_config = Config(
            read_timeout=600,  # 10 minutes for long tool executions with big results
            connect_timeout=60,
            retries={'max_attempts': 3, 'mode': 'adaptive'}
        )
        client = boto3.client('bedrock-agentcore', region_name=config["region"], config=boto_config)
        payload = json.dumps({"prompt": prompt}).encode()

        response = client.invoke_agent_runtime(
            agentRuntimeArn=config["agent_arn"],
            contentType="application/json",
            accept="application/json",
            payload=payload
        )

        # Process streaming response from StreamingBody
        # Response format is JSON lines with events like:
        # {"event": {"contentBlockDelta": {"delta": {"text": "..."}}}}
        full_response = ""
        stream_body = response.get('response')
        tool_in_progress = False

        if stream_body:
            # Read streaming body line by line
            # Response is in SSE format: "data: {json}"
            for line in stream_body.iter_lines():
                if not line:
                    continue
                try:
                    # Decode bytes to string
                    line_str = line.decode('utf-8') if isinstance(line, bytes) else line

                    # Strip SSE "data: " prefix
                    if line_str.startswith('data: '):
                        line_str = line_str[6:]  # Remove "data: " prefix

                    event_data = json.loads(line_str)

                    # Skip if not a dict (some events are just strings)
                    if not isinstance(event_data, dict):
                        continue

                    # Handle text delta events
                    if 'event' in event_data:
                        event = event_data['event']
                        if 'contentBlockDelta' in event:
                            delta = event['contentBlockDelta'].get('delta', {})
                            text = delta.get('text', '')
                            if text:
                                # Clear tool status when text arrives after tool use
                                if tool_in_progress:
                                    queue.put(("tool_done", ""))
                                    tool_in_progress = False
                                full_response += text
                                queue.put(("text", text))
                        elif 'contentBlockStart' in event:
                            # Check for tool use start
                            start = event['contentBlockStart'].get('start', {})
                            tool_use = start.get('toolUse', {})
                            tool_name = tool_use.get('name', '')
                            if tool_name:
                                tool_in_progress = True
                                clean_name = tool_name.split("___")[-1] if "___" in tool_name else tool_name
                                queue.put(("tool", f"Using tool: {clean_name}"))
                            elif full_response and not full_response.endswith('\n'):
                                # New text block starting after previous content - add newline
                                full_response += "\n\n"
                                queue.put(("text", "\n\n"))
                        elif 'messageStart' in event:
                            # New message starting - add separation if we have content
                            if full_response and not full_response.endswith('\n'):
                                full_response += "\n\n"
                                queue.put(("text", "\n\n"))
                        elif 'contentBlockStop' in event:
                            pass  # Block finished
                        elif 'messageStop' in event:
                            # Clear tool status at end
                            if tool_in_progress:
                                queue.put(("tool_done", ""))
                                tool_in_progress = False

                    # Also handle strands callback format (for completeness)
                    if 'current_tool_use' in event_data:
                        tool_name = event_data['current_tool_use'].get('name', '')
                        if tool_name:
                            clean_name = tool_name.split("___")[-1] if "___" in tool_name else tool_name
                            queue.put(("tool", f"Using tool: {clean_name}"))

                    # Handle tool results to extract sources
                    if 'message' in event_data:
                        msg = event_data['message']
                        if 'content' in msg:
                            for block in msg['content']:
                                if 'text' in block:
                                    # Use final text if we didn't capture it streaming
                                    if not full_response:
                                        full_response = block['text']
                                # Extract sources from tool results
                                if 'toolResult' in block:
                                    tool_result = block['toolResult']
                                    if tool_result.get('status') == 'success':
                                        for content in tool_result.get('content', []):
                                            if 'text' in content:
                                                try:
                                                    # Parse the Valyu response to get sources
                                                    text = content['text']
                                                    # Find JSON in the response
                                                    json_start = text.find('{')
                                                    if json_start >= 0:
                                                        valyu_data = json.loads(text[json_start:])
                                                        sources = []
                                                        for result in valyu_data.get('results', [])[:10]:
                                                            url = result.get('url', result.get('source', ''))
                                                            title = result.get('title', url)[:60]
                                                            if url:
                                                                sources.append({"title": title, "url": url})
                                                        if sources:
                                                            queue.put(("sources", sources))
                                                except:
                                                    pass

                except json.JSONDecodeError:
                    # Skip malformed lines
                    continue

        queue.put(("done", full_response))

    except ImportError:
        queue.put(("done", invoke_runtime_cli(prompt)))
    except Exception as e:
        # If we got partial response, use it; otherwise try CLI fallback
        if full_response and len(full_response) > 100:
            # We got substantial content before failure - use it
            queue.put(("done", full_response + "\n\n---\n*Response may be incomplete due to connection issue.*"))
        else:
            # Try CLI fallback
            queue.put(("tool", "Reconnecting..."))
            try:
                result = invoke_runtime_cli(prompt)
                queue.put(("done", result))
            except Exception as cli_error:
                queue.put(("done", f"Error: Connection failed. Please try again."))


def run_runtime_no_tools(prompt: str, queue: Queue):
    """Run runtime query without tools for comparison."""
    # Add instruction to not use tools
    no_tools_prompt = f"""IMPORTANT: Do not use any tools. Answer this question using only your training knowledge.
If you don't have current information, say so.

Question: {prompt}"""
    invoke_runtime_streaming(no_tools_prompt, queue)


def invoke_runtime_cli(prompt: str) -> str:
    """Invoke AgentCore Runtime with CLI (non-streaming fallback)."""
    runtime_dirs = [
        ".",  # Current directory if running from runtime/
        "runtime",
        "../runtime",
        "examples/runtime",
        "/Users/harveyyorke/dev/valyu-agentcore/examples/runtime",
    ]

    runtime_dir = None
    for d in runtime_dirs:
        if os.path.exists(os.path.join(d, ".bedrock_agentcore")) or \
           os.path.exists(os.path.join(d, ".bedrock_agentcore.yaml")) or \
           os.path.exists(os.path.join(d, ".agentcore")):
            runtime_dir = d
            break

    if not runtime_dir:
        return "Error: No agentcore config found"

    try:
        payload = json.dumps({"prompt": prompt})
        result = subprocess.run(
            ["agentcore", "invoke", payload],
            capture_output=True,
            text=True,
            timeout=300,  # 5 minutes for complex queries
            cwd=runtime_dir
        )
        if result.returncode == 0:
            return result.stdout.strip()
        else:
            return f"Runtime error: {result.stderr}"
    except subprocess.TimeoutExpired:
        return "Error: Runtime invocation timed out"
    except FileNotFoundError:
        return "Error: agentcore CLI not found"
    except Exception as e:
        return f"Error: {str(e)}"


def invoke_runtime(prompt: str) -> str:
    """Invoke AgentCore Runtime with a prompt (non-streaming wrapper)."""
    return invoke_runtime_cli(prompt)


def run_runtime_query(prompt: str, queue: Queue):
    """Run runtime query in a thread with streaming support."""
    # Try streaming first, fallback to CLI
    invoke_runtime_streaming(prompt, queue)


# Runtime availability is checked dynamically now


def get_system_prompt_base():
    """Get the base system prompt with date and guidelines."""
    from datetime import datetime
    current_date = datetime.now().strftime("%B %d, %Y")
    current_year = datetime.now().year

    return f"""CRITICAL DATE CONTEXT:
- Today's date: {current_date}
- Current year: {current_year}

SEARCH GUIDELINES:
When searching for "latest", "recent", "new", or "current" information, ALWAYS include the year {current_year} in your search query.
- Correct: "AWS announcements December 2025"
- Incorrect: "latest AWS announcements"

RESPONSE GUIDELINES:
- Be professional and concise
- Do NOT use emojis in your responses
- Use clean markdown formatting with proper headers
- ALWAYS add inline citations immediately after facts: "fact ([source](url))"
- Never state facts from search results without an inline citation"""


def get_local_agents():
    """Get local agent configurations."""
    from valyu_agentcore import (
        webSearch,
        financeSearch,
        secSearch,
        paperSearch,
        patentSearch,
        bioSearch,
        economicsSearch,
    )

    base = get_system_prompt_base()

    return {
        "🌐 Web Search": {
            "tools": [webSearch],
            "system_prompt": f"""You are a research assistant with access to web search.

{base}

Use web search to find current information, news, and articles.""",
            "temp": 0.3,
            "placeholder": "Search the web for anything...",
        },
        "📈 Finance": {
            "tools": [financeSearch],
            "system_prompt": f"""You are a financial analyst with access to real-time market data, stock prices, and earnings information.

{base}

Provide accurate financial data with inline citations.""",
            "temp": 0.2,
            "placeholder": "e.g., What is NVIDIA's current stock price?",
        },
        "📄 SEC Filings": {
            "tools": [secSearch],
            "system_prompt": f"""You are an SEC filings analyst. Search 10-K, 10-Q, and 8-K filings.

{base}

Cite the specific filing (company, form type, date) for every fact.""",
            "temp": 0.2,
            "placeholder": "e.g., What are Apple's main risk factors?",
        },
        "📚 Academic Papers": {
            "tools": [paperSearch],
            "system_prompt": f"""You are a research assistant specializing in academic literature from arXiv, PubMed, and journals.

{base}

Cite papers with titles, authors, and links.""",
            "temp": 0.2,
            "placeholder": "e.g., Recent advances in transformer architectures",
        },
        "💡 Patents": {
            "tools": [patentSearch],
            "system_prompt": f"""You are a patent analyst. Search USPTO patents to find relevant intellectual property.

{base}

Cite patent numbers and assignees.""",
            "temp": 0.2,
            "placeholder": "e.g., Apple's patents on face recognition",
        },
        "🧬 Biomedical": {
            "tools": [bioSearch],
            "system_prompt": f"""You are a biomedical research assistant. Search clinical trials, FDA data, and medical literature.

{base}

Cite clinical trial IDs, FDA sources, or paper references.""",
            "temp": 0.2,
            "placeholder": "e.g., Clinical trials for Alzheimer's treatments",
        },
        "📊 Economics": {
            "tools": [economicsSearch],
            "system_prompt": f"""You are an economics analyst. Search BLS, FRED, and World Bank data for economic indicators.

{base}

Cite the data source (BLS, FRED, World Bank) for every statistic.""",
            "temp": 0.2,
            "placeholder": "e.g., US unemployment rate trends",
        },
        "💼 Financial Analyst": {
            "tools": [secSearch, financeSearch, webSearch],
            "system_prompt": f"""You are a senior financial analyst specializing in equity research and investment analysis.

{base}

Your task is to provide comprehensive financial analysis by:
1. Gathering data from SEC filings, market data, and current news
2. Analyzing financial metrics, competitive position, and market trends
3. Providing balanced, data-driven insights

Structure your analysis with clear sections.""",
            "temp": 0.3,
            "placeholder": "e.g., Analyze NVIDIA's competitive position in AI chips",
        },
        "🔬 Research Assistant": {
            "tools": [paperSearch, patentSearch, webSearch],
            "system_prompt": f"""You are a research assistant specializing in technical and scientific research.

{base}

Your task is to:
1. Search academic literature for relevant papers
2. Check patent databases for related IP
3. Find current developments and news

Synthesize findings into a comprehensive research summary.""",
            "temp": 0.2,
            "placeholder": "e.g., State of the art in quantum error correction",
        },
        "🔎 Due Diligence": {
            "tools": [secSearch, financeSearch, webSearch, patentSearch],
            "system_prompt": f"""You are a due diligence analyst for M&A and investment evaluation.

{base}

Conduct comprehensive due diligence covering:
1. Company Overview - Business model, leadership, history
2. Financial Analysis - Revenue, profitability, cash position
3. Market Position - Market size, competitors, advantages
4. Risk Assessment - Regulatory, legal, key person risks
5. Intellectual Property - Patents, key technologies
6. Recent Developments - News, strategic moves, red flags

Be thorough and objective.""",
            "temp": 0.2,
            "placeholder": "e.g., Conduct due diligence on Stripe",
        },
    }


class StreamingCallbackHandler:
    """Callback handler that streams text to a queue for Streamlit."""

    def __init__(self, queue: Queue):
        self.queue = queue
        self.current_text = ""

    def __call__(self, **kwargs):
        """Handle streaming events from Strands agent."""
        if "data" in kwargs:
            text = kwargs["data"]
            if isinstance(text, str):
                self.current_text += text
                self.queue.put(("text", text))

        if "current_tool_use" in kwargs:
            tool = kwargs["current_tool_use"]
            if isinstance(tool, dict):
                tool_name = tool.get("name", "unknown")
                self.queue.put(("tool", f"Using tool: {tool_name}"))


def create_local_agent(config, callback_handler=None):
    """Create a local agent with Valyu tools."""
    from strands import Agent
    from strands.models import BedrockModel

    return Agent(
        model=BedrockModel(
            model_id="us.anthropic.claude-sonnet-4-20250514-v1:0",
            temperature=config["temp"],
        ),
        tools=[tool() for tool in config["tools"]],
        system_prompt=config["system_prompt"],
        callback_handler=callback_handler,
    )


def create_vanilla_agent(callback_handler=None):
    """Create a vanilla agent without tools."""
    from strands import Agent
    from strands.models import BedrockModel

    return Agent(
        model=BedrockModel(
            model_id="us.anthropic.claude-sonnet-4-20250514-v1:0",
            temperature=0.3,
        ),
        tools=[],
        system_prompt="You are a helpful assistant. Answer questions to the best of your ability based on your training data. If you don't have current information, acknowledge this limitation.",
        callback_handler=callback_handler,
    )


def create_gateway_agent(config_path, callback_handler=None):
    """Create a gateway agent from config."""
    from valyu_agentcore.gateway import GatewayConfig, get_access_token

    try:
        from strands import Agent
        from strands.models import BedrockModel
        from strands.tools.mcp.mcp_client import MCPClient
        from mcp.client.streamable_http import streamablehttp_client
    except ImportError:
        raise ImportError("Install with: pip install valyu-agentcore[agentcore]")

    config = GatewayConfig.load(config_path)
    access_token = get_access_token(config)

    class GatewayAgentWrapper:
        def __init__(self):
            self.config = config
            self.access_token = access_token
            self.callback_handler = callback_handler
            self._mcp_client = None
            self._agent = None
            self._tools = []

        def __enter__(self):
            def create_transport():
                return streamablehttp_client(
                    self.config.gateway_url,
                    headers={"Authorization": f"Bearer {self.access_token}"}
                )

            self._mcp_client = MCPClient(create_transport)
            self._mcp_client.__enter__()

            tools = self._mcp_client.list_tools_sync()
            self._tools = tools

            # Build dynamic system prompt from available tools
            tool_descriptions = []
            for tool in tools:
                name = getattr(tool, 'name', getattr(tool, 'tool_name', str(tool)))
                desc = getattr(tool, 'description', '')
                # Clean up tool name (remove prefixes like "valyu-mcp___")
                clean_name = name.split("___")[-1] if "___" in name else name
                if clean_name.startswith("valyu_") or clean_name.startswith("x_amz"):
                    short_desc = desc[:100] + "..." if len(desc) > 100 else desc
                    tool_descriptions.append(f"- {clean_name}: {short_desc}")

            tools_list = "\n".join(tool_descriptions) if tool_descriptions else "Tools loaded from gateway"

            system_prompt = f"""You are a research assistant with access to Valyu search tools via Gateway.

Available tools:
{tools_list}

Always cite sources with markdown links."""

            self._agent = Agent(
                model=BedrockModel(
                    model_id="us.anthropic.claude-sonnet-4-20250514-v1:0",
                    region_name=self.config.region,
                ),
                tools=tools,
                system_prompt=system_prompt,
                callback_handler=self.callback_handler,
            )
            return self

        def __exit__(self, *args):
            if self._mcp_client:
                self._mcp_client.__exit__(*args)

        def __call__(self, prompt):
            return self._agent(prompt)

        def get_tools(self):
            """Return list of available tools."""
            return self._tools

    return GatewayAgentWrapper()


def run_agent_with_streaming(agent, prompt, queue):
    """Run agent in a thread and stream results to queue."""
    try:
        response = agent(prompt)
        queue.put(("done", str(response)))
    except Exception as e:
        queue.put(("error", str(e)))


def stream_to_placeholder(queue, placeholder, status_placeholder=None):
    """Stream queue messages to a Streamlit placeholder."""
    full_response = ""
    while True:
        try:
            msg_type, msg_data = queue.get(timeout=0.05)
            if msg_type == "text":
                full_response += msg_data
                placeholder.markdown(full_response + "▌")
            elif msg_type == "tool" and status_placeholder:
                status_placeholder.caption(f"🔧 {msg_data}")
            elif msg_type == "done":
                full_response = msg_data
                return full_response
            elif msg_type == "error":
                return f"Error: {msg_data}"
        except:
            return full_response


def main():
    st.title("🔍 Valyu AgentCore")
    st.caption("AI agents powered by Valyu's data APIs")

    # Check runtime availability (cached in session state, with refresh option)
    if "runtime_available" not in st.session_state:
        st.session_state.runtime_available = check_runtime_available()

    # Build available modes
    available_modes = ["🏠 Local"]
    if GATEWAY_CONFIG:
        available_modes.append("☁️ Gateway")
    if st.session_state.runtime_available:
        available_modes.append("🚀 Runtime")

    # Mode selection
    mode = "local"
    if len(available_modes) > 1:
        selected = st.sidebar.radio(
            "Mode",
            available_modes,
            help="Local: Direct API. Gateway: Local agent + AWS Gateway. Runtime: Serverless AWS.",
        )
        if "Gateway" in selected:
            mode = "gateway"
        elif "Runtime" in selected:
            mode = "runtime"
        else:
            mode = "local"

    # Check requirements and show status
    if mode == "local":
        if not os.environ.get("VALYU_API_KEY"):
            st.error("⚠️ Set VALYU_API_KEY environment variable")
            st.code("export VALYU_API_KEY=your-api-key", language="bash")
            st.stop()
        st.sidebar.caption("Direct Valyu API calls")
    elif mode == "gateway":
        st.sidebar.success("✓ Gateway config found")
        st.sidebar.caption("Local agent → AWS Gateway → Valyu")
    elif mode == "runtime":
        st.sidebar.success("✓ Runtime deployed")
        st.sidebar.caption("AWS Runtime (serverless) → Gateway → Valyu")

    # View mode
    view_mode = st.sidebar.radio(
        "View",
        ["💬 Chat", "⚡ Compare"],
        help="Compare shows side-by-side: with Valyu vs without",
    )

    # Sidebar agent selection
    if mode == "local":
        with st.sidebar:
            st.header("Select Agent")
            agents = get_local_agents()

            single_tools = ["🌐 Web Search", "📈 Finance", "📄 SEC Filings", "📚 Academic Papers", "💡 Patents", "🧬 Biomedical", "📊 Economics"]
            multi_tools = ["💼 Financial Analyst", "🔬 Research Assistant", "🔎 Due Diligence"]

            st.subheader("Single Tools", divider="gray")
            agent_name = st.radio(
                "Single:",
                single_tools,
                label_visibility="collapsed",
                key="single_tools"
            )

            st.subheader("Multi-Tool Agents", divider="gray")
            multi_choice = st.radio(
                "Multi:",
                multi_tools,
                label_visibility="collapsed",
                index=None,
                key="multi_tools"
            )

            # Use multi-tool if selected, otherwise use single tool
            if multi_choice:
                agent_name = multi_choice

            st.divider()
            st.caption(f"**Tools:** {', '.join([t.__name__ for t in agents[agent_name]['tools']])}")
            placeholder = agents[agent_name]["placeholder"]
    elif mode == "gateway":
        with st.sidebar:
            st.header("Gateway Mode")

            # Show available tools from gateway
            if "gateway_tools" in st.session_state:
                tools = st.session_state.gateway_tools
                st.success(f"✓ {len(tools)} tools available")
                with st.expander("View Tools"):
                    for tool in tools:
                        name = getattr(tool, 'name', str(tool))
                        clean_name = name.split("___")[-1] if "___" in name else name
                        if clean_name.startswith("valyu_"):
                            st.caption(f"• {clean_name}")
            else:
                st.info("Tools will load on first query")

            agent_name = "gateway"
            placeholder = "Ask anything - all tools available..."
    else:  # runtime mode
        with st.sidebar:
            st.header("Runtime Mode")
            st.info("🚀 Serverless execution on AWS")
            st.caption("Agent runs in AgentCore Runtime with all Valyu tools via Gateway")

            # Show available tools (known tools from Gateway MCP)
            runtime_tools = [
                ("search", "Web"),
                ("financial_search", "Stocks & Market"),
                ("sec_search", "SEC Filings"),
                ("academic_search", "Papers"),
                ("patent_search", "Patents"),
                ("bio_search", "Biomedical"),
                ("economics_search", "Economics"),
                ("contents", "URL Extract"),
            ]
            st.success(f"✓ {len(runtime_tools)} Valyu tools")
            with st.expander("View Tools"):
                for tool_name, tool_desc in runtime_tools:
                    st.markdown(f"• **{tool_name}** - {tool_desc}")

            agent_name = "runtime"
            placeholder = "Ask anything - runs on AWS Runtime..."

    # Runtime status check / refresh button
    with st.sidebar:
        st.divider()
        col1, col2 = st.columns([2, 1])
        with col1:
            if st.session_state.runtime_available:
                st.caption("🟢 Runtime: Ready")
            else:
                st.caption("⚪ Runtime: Not detected")
        with col2:
            if st.button("🔄", help="Refresh runtime status"):
                debug_info = check_runtime_available(debug=True)
                st.session_state.runtime_available = check_runtime_available()
                st.session_state.runtime_debug = debug_info
                st.rerun()

        # Show debug info if available
        if "runtime_debug" in st.session_state:
            with st.expander("Debug Info"):
                st.json(st.session_state.runtime_debug)

    # Clear chat button
    if st.sidebar.button("Clear Chat", use_container_width=True):
        st.session_state.messages = []
        st.session_state.compare_results = []
        if "gateway_ctx" in st.session_state:
            try:
                st.session_state.gateway_ctx.__exit__(None, None, None)
            except:
                pass
            del st.session_state["gateway_ctx"]
        if "gateway_tools" in st.session_state:
            del st.session_state["gateway_tools"]
        st.rerun()

    # Initialize state
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "compare_results" not in st.session_state:
        st.session_state.compare_results = []
    if "current_agent" not in st.session_state:
        st.session_state.current_agent = None

    # Clear chat if agent changed
    if st.session_state.current_agent != (mode, agent_name, view_mode):
        st.session_state.messages = []
        st.session_state.compare_results = []
        st.session_state.current_agent = (mode, agent_name, view_mode)
        if "gateway_ctx" in st.session_state:
            try:
                st.session_state.gateway_ctx.__exit__(None, None, None)
            except:
                pass
            del st.session_state["gateway_ctx"]

    # =========================================================================
    # COMPARE VIEW
    # =========================================================================
    if view_mode == "⚡ Compare":
        # Custom CSS for visual separation
        st.markdown("""
        <style>
        .search-card {
            background: linear-gradient(135deg, #1a472a 0%, #2d5a3d 100%);
            border: 1px solid #3d7a4d;
            border-radius: 12px;
            padding: 20px;
            margin: 10px 0;
        }
        .no-search-card {
            background: linear-gradient(135deg, #4a1a1a 0%, #5a2d2d 100%);
            border: 1px solid #7a3d3d;
            border-radius: 12px;
            padding: 20px;
            margin: 10px 0;
        }
        .query-box {
            background: #262730;
            border-left: 4px solid #ff6b6b;
            padding: 15px;
            margin: 20px 0;
            border-radius: 0 8px 8px 0;
        }
        </style>
        """, unsafe_allow_html=True)

        st.subheader("Side-by-Side: Valyu Search vs No Search")
        st.caption("See the difference real-time data makes")

        # Display previous comparisons
        labels_with = {
            "local": "🔍 **Local + Valyu**",
            "gateway": "☁️ **Gateway + Valyu**",
            "runtime": "🚀 **Runtime + Valyu**",
        }
        labels_without = {
            "local": "🚫 **Local (No Tools)**",
            "gateway": "🚫 **Local (No Tools)**",
            "runtime": "🚫 **Local (No Tools)**",
        }
        for result in st.session_state.compare_results:
            st.markdown(f'<div class="query-box"><strong>Query:</strong> {result["query"]}</div>', unsafe_allow_html=True)

            col1, spacer, col2 = st.columns([10, 1, 10])

            result_mode = result.get("mode", "local")
            with col1:
                st.success(labels_with.get(result_mode, "🔍 **With Valyu**"))
                with st.container(border=True):
                    st.markdown(result["with_search"])

            with col2:
                st.error(labels_without.get(result_mode, "🚫 **No Tools**"))
                with st.container(border=True):
                    st.markdown(result["without_search"])

            st.markdown("---")

        # Input for new comparison
        if prompt := st.chat_input(placeholder):
            st.markdown(f'<div class="query-box"><strong>Query:</strong> {prompt}</div>', unsafe_allow_html=True)

            col1, spacer, col2 = st.columns([10, 1, 10])

            # Create queues for both
            queue_with = Queue()
            queue_without = Queue()

            callback_with = StreamingCallbackHandler(queue_with)
            callback_without = StreamingCallbackHandler(queue_without)

            # Create agents and start threads based on mode
            if mode == "local":
                agents = get_local_agents()
                agent_with = create_local_agent(agents[agent_name], callback_handler=callback_with)
                thread_with = Thread(target=run_agent_with_streaming, args=(agent_with, prompt, queue_with))
                # Local vanilla agent for comparison
                agent_without = create_vanilla_agent(callback_handler=callback_without)
                thread_without = Thread(target=run_agent_with_streaming, args=(agent_without, prompt, queue_without))
            elif mode == "gateway":
                if "gateway_ctx" not in st.session_state:
                    gateway_agent = create_gateway_agent(GATEWAY_CONFIG, callback_handler=callback_with)
                    st.session_state.gateway_ctx = gateway_agent.__enter__()
                    st.session_state.gateway_tools = st.session_state.gateway_ctx.get_tools()
                agent_with = st.session_state.gateway_ctx
                thread_with = Thread(target=run_agent_with_streaming, args=(agent_with, prompt, queue_with))
                # Local vanilla agent for comparison
                agent_without = create_vanilla_agent(callback_handler=callback_without)
                thread_without = Thread(target=run_agent_with_streaming, args=(agent_without, prompt, queue_without))
            else:  # runtime mode
                thread_with = Thread(target=run_runtime_query, args=(prompt, queue_with))
                # Local model without tools for comparison (cleaner than telling runtime not to use tools)
                agent_without = create_vanilla_agent(callback_handler=callback_without)
                thread_without = Thread(target=run_agent_with_streaming, args=(agent_without, prompt, queue_without))

            thread_with.start()
            thread_without.start()

            # Stream both in parallel
            mode_labels_with = {
                "local": "🔍 **Local + Valyu**",
                "gateway": "☁️ **Gateway + Valyu**",
                "runtime": "🚀 **Runtime + Valyu**",
            }
            mode_labels_without = {
                "local": "🚫 **Local (No Tools)**",
                "gateway": "🚫 **Local (No Tools)**",
                "runtime": "🚫 **Local (No Tools)**",
            }
            with col1:
                st.success(mode_labels_with.get(mode, "🔍 **With Valyu Search**"))
                with st.container(border=True):
                    placeholder_with = st.empty()
                    status_with = st.empty()

            with col2:
                st.error(mode_labels_without.get(mode, "🚫 **Without Search**"))
                with st.container(border=True):
                    placeholder_without = st.empty()

            response_with = ""
            response_without = ""
            tools_used = []
            sources_found = []

            # Poll both queues
            while thread_with.is_alive() or thread_without.is_alive():
                # Check with-search queue
                try:
                    msg_type, msg_data = queue_with.get_nowait()
                    if msg_type == "text":
                        response_with += msg_data
                        placeholder_with.markdown(response_with + "▌")
                    elif msg_type == "tool":
                        # Track tool and show inline
                        tool_name = msg_data.replace("Using tool: ", "")
                        if tool_name not in tools_used:
                            tools_used.append(tool_name)
                        placeholder_with.markdown(response_with + f"\n\n🔧 *{msg_data}...*▌")
                    elif msg_type == "tool_done":
                        placeholder_with.markdown(response_with + "▌")
                    elif msg_type == "sources":
                        # Track sources
                        sources_found.extend(msg_data)
                        source_names = [s['title'] if isinstance(s, dict) else s for s in msg_data[:3]]
                        sources_text = "\n\n📚 **Sources:** " + " • ".join(source_names)
                        placeholder_with.markdown(response_with + sources_text + "▌")
                    elif msg_type == "done":
                        # Don't replace, just mark done
                        if not response_with:
                            response_with = msg_data
                    elif msg_type == "error":
                        response_with = f"Error: {msg_data}"
                except:
                    pass

                # Check without-search queue
                try:
                    msg_type, msg_data = queue_without.get_nowait()
                    if msg_type == "text":
                        response_without += msg_data
                        placeholder_without.markdown(response_without + "▌")
                    elif msg_type == "done":
                        response_without = msg_data
                    elif msg_type == "error":
                        response_without = f"Error: {msg_data}"
                except:
                    pass

                import time
                time.sleep(0.05)

            # Drain remaining queue items (don't overwrite accumulated response)
            while True:
                try:
                    msg_type, msg_data = queue_with.get_nowait()
                    if msg_type == "text":
                        response_with += msg_data
                    elif msg_type == "tool":
                        tool_name = msg_data.replace("Using tool: ", "")
                        if tool_name not in tools_used:
                            tools_used.append(tool_name)
                    elif msg_type == "sources":
                        sources_found.extend(msg_data)
                    elif msg_type == "done" and not response_with:
                        response_with = msg_data
                except:
                    break

            while True:
                try:
                    msg_type, msg_data = queue_without.get_nowait()
                    if msg_type == "text":
                        response_without += msg_data
                    elif msg_type == "done" and not response_without:
                        response_without = msg_data
                except:
                    break

            thread_with.join(timeout=1)
            thread_without.join(timeout=1)

            # Build final response with footer
            final_with = response_with
            if tools_used:
                final_with += f"\n\n---\n🔧 **Tools used:** {', '.join(tools_used)}"

            status_with.empty()
            placeholder_with.markdown(final_with)

            # Add expandable sources section
            if sources_found:
                with col1:
                    with st.expander(f"📚 View Sources ({len(sources_found)})"):
                        for source in sources_found:
                            if isinstance(source, dict):
                                st.markdown(f"**[{source['title']}]({source['url']})**")
                                st.caption(f"{source['url']}")
                            else:
                                st.markdown(f"• {source}")

            placeholder_without.markdown(response_without)

            # Save result
            st.session_state.compare_results.append({
                "query": prompt,
                "mode": mode,
                "with_search": final_with,
                "without_search": response_without,
            })

    # =========================================================================
    # CHAT VIEW
    # =========================================================================
    else:
        # Display chat history
        for message in st.session_state.messages:
            with st.chat_message(message["role"]):
                st.markdown(message["content"])

        # Chat input
        if prompt := st.chat_input(placeholder):
            st.session_state.messages.append({"role": "user", "content": prompt})
            with st.chat_message("user"):
                st.markdown(prompt)

            with st.chat_message("assistant"):
                response_placeholder = st.empty()
                status_placeholder = st.empty()

                stream_queue = Queue()
                callback = StreamingCallbackHandler(stream_queue)

                try:
                    if mode == "local":
                        agents = get_local_agents()
                        agent = create_local_agent(agents[agent_name], callback_handler=callback)
                        thread = Thread(target=run_agent_with_streaming, args=(agent, prompt, stream_queue))
                        thread.start()
                    elif mode == "gateway":
                        if "gateway_ctx" not in st.session_state:
                            gateway_agent = create_gateway_agent(GATEWAY_CONFIG, callback_handler=callback)
                            st.session_state.gateway_ctx = gateway_agent.__enter__()
                            # Store tools for sidebar display
                            st.session_state.gateway_tools = st.session_state.gateway_ctx.get_tools()
                        agent = st.session_state.gateway_ctx
                        thread = Thread(target=run_agent_with_streaming, args=(agent, prompt, stream_queue))
                        thread.start()
                    else:  # runtime mode
                        status_placeholder.caption("🚀 Invoking AgentCore Runtime...")
                        thread = Thread(target=run_runtime_query, args=(prompt, stream_queue))
                        thread.start()

                    # Stream results (or wait for runtime)
                    full_response = ""
                    tools_used = []
                    sources_found = []
                    while True:
                        try:
                            msg_type, msg_data = stream_queue.get(timeout=0.1)
                            if msg_type == "text":
                                full_response += msg_data
                                response_placeholder.markdown(full_response + "▌")
                            elif msg_type == "tool":
                                # Track and show tool status inline
                                tool_name = msg_data.replace("Using tool: ", "")
                                if tool_name not in tools_used:
                                    tools_used.append(tool_name)
                                response_placeholder.markdown(full_response + f"\n\n🔧 *{msg_data}...*▌")
                            elif msg_type == "tool_done":
                                response_placeholder.markdown(full_response + "▌")
                            elif msg_type == "sources":
                                sources_found.extend(msg_data)
                                source_names = [s['title'] if isinstance(s, dict) else s for s in msg_data[:3]]
                                sources_text = "\n\n📚 **Sources:** " + " • ".join(source_names)
                                response_placeholder.markdown(full_response + sources_text + "▌")
                            elif msg_type == "done":
                                if not full_response:
                                    full_response = msg_data
                                break
                            elif msg_type == "error":
                                full_response = f"Error: {msg_data}"
                                break
                        except:
                            if not thread.is_alive():
                                break

                    thread.join(timeout=1)

                    # Build final response with footer
                    response_text = full_response
                    if tools_used:
                        response_text += f"\n\n---\n🔧 **Tools used:** {', '.join(tools_used)}"

                except Exception as e:
                    response_text = f"Error: {str(e)}"
                    sources_found = []

                finally:
                    # Always clear status and show final response
                    status_placeholder.empty()
                    response_placeholder.markdown(response_text if response_text else full_response)

                # Add expandable sources section
                if sources_found:
                    with st.expander(f"📚 View Sources ({len(sources_found)})"):
                        for source in sources_found:
                            if isinstance(source, dict):
                                st.markdown(f"**[{source['title']}]({source['url']})**")
                                st.caption(f"{source['url']}")
                            else:
                                st.markdown(f"• {source}")

                # Save response (without sources expander content for history)
                st.session_state.messages.append({"role": "assistant", "content": response_text})


if __name__ == "__main__":
    main()
