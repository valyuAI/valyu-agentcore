#!/usr/bin/env python3
"""Generate Valyu AgentCore Architecture Diagram"""

from diagrams import Diagram, Cluster, Edge
from diagrams.aws.network import APIGateway
from diagrams.aws.security import Cognito
from diagrams.aws.ml import Bedrock
from diagrams.programming.language import Python
from diagrams.generic.device import Mobile
from diagrams.onprem.compute import Server
from diagrams.onprem.network import Internet

with Diagram("Valyu AgentCore Architecture", 
             filename="valyu-agentcore-architecture", 
             direction="LR", 
             show=False,
             graph_attr={"splines": "ortho"}):  # Use orthogonal edges for cleaner routing
    
    # User/Developer
    developer = Mobile("Developer")
    
    # Local Development Flow
    with Cluster("Local Development"):
        local_agent = Python("Local Agent\n(Strands)")
        valyu_tools = Python("Valyu Tools\n(webSearch, secSearch)")
    
    # Production Flow - AgentCore Runtime
    with Cluster("AWS Bedrock AgentCore"):
        with Cluster("AgentCore Runtime"):
            runtime_agent = Server("Agent\n(Deployed)")
        
        with Cluster("AgentCore Gateway"):
            gateway = APIGateway("MCP Gateway")
            cognito = Cognito("Cognito\nOAuth 2.0")
            
        with Cluster("Gateway Target"):
            mcp_target = Internet("Valyu MCP Target\nmcp.valyu.ai")
    
    # External Services
    valyu_api = Bedrock("Valyu Search API\nplatform.valyu.ai")
    
    # Local Development Flow
    developer >> Edge(label="1. Develop & Test") >> local_agent
    local_agent >> Edge(label="Direct API calls") >> valyu_tools
    valyu_tools >> Edge(label="HTTPS + API Key") >> valyu_api
    
    # Production Deployment Flow
    developer >> Edge(label="2. Deploy\nagentcore launch", style="dashed") >> runtime_agent
    developer >> Edge(label="3. Configure\nGateway", style="dashed") >> gateway
    
    # Runtime to Gateway Flow (forward path)
    runtime_agent >> Edge(label="4. Request token") >> cognito
    cognito >> Edge(label="5. Access token\n(JWT)", color="green") >> runtime_agent
    runtime_agent >> Edge(label="6. Invoke tools\n(Bearer token)", color="blue") >> gateway
    gateway >> Edge(label="7. Forward\nrequest", color="blue") >> mcp_target
    mcp_target >> Edge(label="8. Search\nquery", color="purple") >> valyu_api
    
    # Return path (reverse order)
    valyu_api >> Edge(label="9. Search\nresults", color="orange") >> mcp_target
    mcp_target >> Edge(label="10. MCP\nresponse", color="orange") >> gateway
    gateway >> Edge(label="11. Tool\nresults", color="orange") >> runtime_agent

print("Diagram generated successfully!")
