import os
import json
import re
import importlib.util
from typing import Any, Dict, List
from langchain_community.document_loaders import TextLoader
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import FAISS
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, END
from pydantic import BaseModel
from typing import TypedDict
import streamlit as st

# === Custom Exception for System Not Found ===
class SystemNotFoundError(Exception):
    pass

# === Load Configuration ===
CONFIG_PATH = "config.json"
if not os.path.exists(CONFIG_PATH):
    raise FileNotFoundError(f"Configuration file not found at {CONFIG_PATH}")
with open(CONFIG_PATH, 'r') as f:
    config = json.load(f)
PROBLEM_SPACE = config.get("problem_space", "storage_system")
PROBLEM_SPACE_DIR = f"problem_spaces/{PROBLEM_SPACE}"
DATA_MODEL_PATH = f"{PROBLEM_SPACE_DIR}/data_model.json"
TOOLS_CONFIG_PATH = f"{PROBLEM_SPACE_DIR}/tools.json"
RAG_PATH = f"{PROBLEM_SPACE_DIR}/rag.txt"
ANALYZE_PROMPT_PATH = f"{PROBLEM_SPACE_DIR}/analyze_prompt.txt"
FORMAT_PROMPT_PATH = f"{PROBLEM_SPACE_DIR}/format_prompt.txt"

# Validate problem space files
for path in [DATA_MODEL_PATH, TOOLS_CONFIG_PATH, RAG_PATH, ANALYZE_PROMPT_PATH, FORMAT_PROMPT_PATH]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Required file not found at {path}")

# Load data model
try:
    with open(DATA_MODEL_PATH, 'r') as f:
        DATA_MODEL = json.load(f)
except json.JSONDecodeError as e:
    print(f"Error: Invalid JSON in {DATA_MODEL_PATH}: {e}")
    DATA_MODEL = {}
except Exception as e:
    print(f"Error loading {DATA_MODEL_PATH}: {e}")
    DATA_MODEL = {}

# Extract fault analysis structure
fault_analysis_structure = DATA_MODEL.get("fault_analysis_structure", "")
if not fault_analysis_structure:
    print(f"Warning: 'fault_analysis_structure' missing in {DATA_MODEL_PATH}")
    fault_analysis_structure = """{
        "fault_type": "No fault",
        "details": {}
    }"""

# Load tools configuration
try:
    with open(TOOLS_CONFIG_PATH, 'r') as f:
        TOOLS_CONFIG = json.load(f)
except json.JSONDecodeError as e:
    print(f"Error: Invalid JSON in {TOOLS_CONFIG_PATH}: {e}")
    TOOLS_CONFIG = []
except Exception as e:
    print(f"Error loading {TOOLS_CONFIG_PATH}: {e}")
    TOOLS_CONFIG = []

# Initialize tools
TOOLS = {}
for tool in TOOLS_CONFIG:
    tool_path = f"{PROBLEM_SPACE_DIR}/tools/{tool.get('file', '')}"
    function_name = tool.get('function', 'run')  # Default to 'run' if 'function' missing
    tool_name = tool.get('name', 'unknown_tool')
    
    if not os.path.exists(tool_path):
        print(f"Error: Tool file not found at {tool_path} for tool {tool_name}")
        continue
    
    try:
        spec = importlib.util.spec_from_file_location(tool_name, tool_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not hasattr(module, function_name):
            print(f"Error: Tool {tool_name} missing '{function_name}' function in {tool_path}")
            continue
        TOOLS[tool_name] = {
            'run': getattr(module, function_name),
            'parameters': tool.get('parameters', []),
            'required': tool.get('required', [])
        }
    except Exception as e:
        print(f"Error loading tool {tool_name} from {tool_path}: {e}")

# === CONFIG ===
GROQ_API_KEY = ""
GROQ_MODEL = "llama-3.3-70b-versatile"

# === Initialize LLM ===
llm = ChatOpenAI(
    model=GROQ_MODEL,
    openai_api_base="https://api.groq.com/openai/v1",
    openai_api_key=GROQ_API_KEY,
    temperature=0
)

# === Load and Chunk RAG Document ===
print("🔍 Loading and splitting RAG document...")
loader = TextLoader(RAG_PATH)
docs = loader.load()
if not docs:
    raise ValueError("No content loaded from the RAG file")
splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=100)
chunks = splitter.split_documents(docs)

# === Embeddings and Vector Store ===
print("📡 Embedding and indexing...")
embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")
vectorstore = FAISS.from_documents(chunks, embeddings)
retriever = vectorstore.as_retriever(search_type="similarity", search_kwargs={"k": 7})

# === State Definition for LangGraph ===
class AgentState(TypedDict):
    query: str
    port: int
    system_name: str
    context: str
    fault_analysis: Dict[str, Any]
    formatted_report: str
    system_data: Dict[str, Any]
    system_metrics: Dict[str, Any]
    rag_context: str

# === Agent 1: Data Extraction Agent ===
def extract_relevant_data(state: AgentState) -> AgentState:
    """Extract relevant files and context for the given port and query."""
    query = state["query"]
    port_match = re.search(r'(?:system|port)\s+(\d+)', query.lower())
    port = int(port_match.group(1)) if port_match else 5000

    # Check if data directory exists
    data_dir = f"data_instance_{port}"
    if not os.path.exists(data_dir):
        raise SystemNotFoundError(f"System not found for port {port}")

    # Load system data
    system_data = {}
    system_metrics = {}
    context_parts = []

    # System info
    system_file = f"{data_dir}/system.json"
    if os.path.exists(system_file):
        try:
            with open(system_file, 'r') as f:
                system_data_raw = json.load(f)
            if isinstance(system_data_raw, list) and len(system_data_raw) > 0:
                system_data = system_data_raw[0]
            else:
                system_data = system_data_raw
            system_name = system_data.get("name", f"System_{port}")
            context_parts.append(f"System Information:\n{json.dumps(system_data, indent=2)}")
        except Exception as e:
            context_parts.append(f"⚠️ Error loading system.json: {str(e)}")
            system_name = f"System_{port}"
    else:
        system_name = f"System_{port}"

    # Latest metrics
    metrics_file = f"{data_dir}/system_metrics.json"
    if os.path.exists(metrics_file):
        try:
            with open(metrics_file, 'r') as f:
                metrics_data = json.load(f)
            if metrics_data and isinstance(metrics_data, list) and len(metrics_data) > 0:
                system_metrics = metrics_data[-1]
                context_parts.append(f"Latest Metrics:\n{json.dumps(system_metrics, indent=2)}")
            else:
                context_parts.append(f"⚠️ Warning: system_metrics.json is empty or invalid")
        except Exception as e:
            context_parts.append(f"⚠️ Error loading system_metrics.json: {str(e)}")
    else:
        context_parts.append(f"⚠️ Warning: system_metrics.json not found")

    # Volumes info
    volumes_file = f"{data_dir}/volume.json"
    if os.path.exists(volumes_file):
        try:
            with open(volumes_file, 'r') as f:
                volumes_data = json.load(f)
            context_parts.append(f"Volumes Information:\n{json.dumps(volumes_data, indent=2)}")
        except Exception as e:
            context_parts.append(f"⚠️ Error loading volume.json: {str(e)}")

    # IO metrics
    io_metrics_file = f"{data_dir}/io_metrics.json"
    if os.path.exists(io_metrics_file):
        try:
            with open(io_metrics_file, 'r') as f:
                io_metrics_data = json.load(f)
            context_parts.append(f"IO Metrics:\n{json.dumps(io_metrics_data, indent=2)}")
        except Exception as e:
            context_parts.append(f"⚠️ Error loading io_metrics.json: {str(e)}")

    # Replication metrics
    replication_file = f"{data_dir}/replication_metrics.json"
    if os.path.exists(replication_file):
        try:
            with open(replication_file, 'r') as f:
                replication_data = json.load(f)
            context_parts.append(f"Replication Metrics:\n{json.dumps(replication_data, indent=2)}")
        except Exception as e:
            context_parts.append(f"⚠️ Error loading replication_metrics.json: {str(e)}")

    # Snapshots info
    snapshots_file = f"{data_dir}/snapshots.json"
    if os.path.exists(snapshots_file):
        try:
            with open(snapshots_file, 'r') as f:
                snapshots_data = json.load(f)
            context_parts.append(f"Snapshots Information:\n{json.dumps(snapshots_data, indent=2)}")
        except Exception as e:
            context_parts.append(f"⚠️ Error loading snapshots.json: {str(e)}")

    # Logs
    logs_file = f"{data_dir}/logs_{port}.txt"
    if os.path.exists(logs_file):
        try:
            with open(logs_file, 'r') as f:
                logs_content = f.read()[:1000]
            context_parts.append(f"System Logs:\n{logs_content}")
        except Exception as e:
            context_parts.append(f"⚠️ Error loading logs_{port}.txt: {str(e)}")

    state["context"] = "\n\n".join(context_parts)
    state["port"] = port
    state["system_name"] = system_name
    state["system_data"] = system_data
    state["system_metrics"] = system_metrics
    state["rag_context"] = ""
    return state

# === Agent 2: Fault Analysis Agent ===
def analyze_fault(state: AgentState) -> AgentState:
    """Analyze the fault using RAG logic and system data."""
    query = state["query"]
    context = state["context"]
    system_data = state["system_data"]
    system_metrics = state["system_metrics"]
    
    print("\n=== Analyze Fault Inputs ===")
    print(f"Query: {query}")
    print(f"System Data: {json.dumps(system_data, indent=2)}")
    print(f"System Metrics: {json.dumps(system_metrics, indent=2)}")

    # Flatten context for analysis
    def flatten_json(obj, prefix=""):
        lines = []
        if isinstance(obj, dict):
            for k, v in sorted(obj.items()):
                full_key = f"{prefix}.{k}" if prefix else k
                if isinstance(v, (dict, list)):
                    lines.extend(flatten_json(v, prefix=full_key))
                else:
                    lines.append(f"{full_key} = {v}")
        elif isinstance(obj, list):
            for idx, item in enumerate(obj):
                full_key = f"{prefix}[{idx}]"
                if isinstance(item, (dict, list)):
                    lines.extend(flatten_json(item, prefix=full_key))
                else:
                    lines.append(f"{full_key} = {item}")
        elif isinstance(obj, str):
            sections = obj.split("\n\n")
            for section in sections:
                if section.strip():
                    section_lines = section.strip().split('\n')
                    section_title = section_lines[0].strip()
                    section_content = section.strip()
                    lines.append(f"{prefix}.{section_title} = {section_content}")
        return lines

    flattened = flatten_json(context) if context else []
    formatted_input = "\n".join(flattened)

    # Load analyze prompt
    try:
        with open(ANALYZE_PROMPT_PATH, 'r') as f:
            analyze_prompt_content = f.read().strip()
        if not analyze_prompt_content:
            raise ValueError(f"Empty prompt file: {ANALYZE_PROMPT_PATH}")
    except Exception as e:
        print(f"Error loading {ANALYZE_PROMPT_PATH}: {e}")
        analyze_prompt_content = "Analyze the fault and return a JSON object based on the provided data and structure."

    # Retrieve relevant RAG chunks
    relevant_docs = retriever.invoke(query)
    context_with_rca = "\n".join([doc.page_content for doc in relevant_docs])
    
    # Construct system message
    system_message = analyze_prompt_content.format(PROBLEM_SPACE=PROBLEM_SPACE) + f"\nRAG Logic:\n{context_with_rca}"

    # Construct messages list
    messages = [
        SystemMessage(content=system_message),
        HumanMessage(content=(
            f"Query: {query}\n\n"
            f"Extracted system data:\n{formatted_input}\n\n"
            f"Expected JSON structure:\n{fault_analysis_structure}"
        ))
    ]

    # Invoke the LLM
    print("\n=== Invoking LLM for Fault Analysis ===")
    response = llm.invoke(messages)
    print(f"Raw LLM Response: {response.content}")
    
    # Parse JSON response
    fault_analysis = None
    raw_response = response.content.strip()
    json_str = raw_response
    if raw_response.startswith('```json') and raw_response.endswith('```'):
        json_match = re.search(r'```json\n([\s\S]*?)\n```', raw_response)
        if json_match:
            json_str = json_match.group(1)
    
    try:
        fault_analysis = json.loads(json_str)
    except json.JSONDecodeError as e:
        print(f"Error: Failed to parse JSON: {e}")
        fault_analysis = {"error": "Invalid analysis output", "raw_result": raw_response}

    state["fault_analysis"] = fault_analysis
    state["rag_context"] = context_with_rca
    return state

# === Agent 3: Tool Agent ===
def tool_agent(state: AgentState) -> AgentState:
    """Invoke the tool to calculate contributions."""
    fault_analysis = state["fault_analysis"]
    system_data = state["system_data"]
    
    if fault_analysis.get("error"):
        print(f"Error: Skipping tool invocation due to invalid fault analysis: {fault_analysis}")
        return state

    tool_name = "volume_contribution_calculator"
    if tool_name not in TOOLS:
        print(f"Error: Tool {tool_name} not configured")
        fault_analysis["error"] = f"Tool {tool_name} not found"
        state["fault_analysis"] = fault_analysis
        return state

    tool = TOOLS[tool_name]
    parameters = {
        "fault_analysis": fault_analysis,
        "system_data": system_data
    }
    
    missing_params = [p for p in tool['required'] if p not in parameters]
    if missing_params:
        print(f"Error: Missing required parameters for {tool_name}: {missing_params}")
        fault_analysis["error"] = f"Missing parameters: {missing_params}"
        state["fault_analysis"] = fault_analysis
        return state

    try:
        fault_analysis = tool['run'](**parameters)
        print("\n=== Fault Analysis with Tool Contributions ===")
        print(json.dumps(fault_analysis, indent=2))
    except Exception as e:
        print(f"Error in tool execution: {e}")
        fault_analysis["error"] = f"Tool execution failed: {str(e)}"

    state["fault_analysis"] = fault_analysis
    return state

# === Agent 4: Response Formatting Agent ===
def format_response(state: AgentState) -> AgentState:
    """Format the fault analysis into a human-readable report using RAG context."""
    fault_analysis = state["fault_analysis"]
    system_name = state["system_name"]
    port = state["port"]
    query = state["query"]
    rag_context = state.get("rag_context", "")

    # Fallback retrieval if rag_context is empty
    if not rag_context:
        print("Warning: No RAG context in state, retrieving chunks")
        relevant_docs = retriever.invoke(query)
        rag_context = "\n".join([doc.page_content for doc in relevant_docs])

    # Load format prompt
    try:
        with open(FORMAT_PROMPT_PATH, 'r') as f:
            format_prompt_content = f.read().strip()
        if not format_prompt_content:
            raise ValueError(f"Empty prompt file: {FORMAT_PROMPT_PATH}")
    except Exception as e:
        print(f"Error loading {FORMAT_PROMPT_PATH}: {e}")
        format_prompt_content = (
            "Format the JSON fault analysis into a concise, human-readable report for system {system_name} (Port: {port}). "
            "Include fault type, key details, and next actions."
        )

    # Construct system message
    system_message = format_prompt_content.format(
        PROBLEM_SPACE=PROBLEM_SPACE,
        system_name=system_name,
        port=port,
        rag_context=rag_context
    )

    # Construct messages list
    messages = [
        SystemMessage(content=system_message),
        HumanMessage(content=f"JSON Analysis:\n{json.dumps(fault_analysis, indent=2)}")
    ]

    # Invoke the LLM
    print("\n=== Invoking LLM for Formatting ===")
    response = llm.invoke(messages)
    formatted_report = response.content
    print(f"Formatted Report:\n{formatted_report}")
    state["formatted_report"] = formatted_report
    return state

# === LangGraph Workflow ===
workflow = StateGraph(AgentState)

workflow.add_node("extract_data", extract_relevant_data)
workflow.add_node("analyze_fault", analyze_fault)
workflow.add_node("tool_agent", tool_agent)
workflow.add_node("format_response", format_response)

workflow.add_edge("extract_data", "analyze_fault")
workflow.add_edge("analyze_fault", "tool_agent")
workflow.add_edge("tool_agent", "format_response")
workflow.add_edge("format_response", END)

workflow.set_entry_point("extract_data")

app = workflow.compile()

# === Streamlit UI ===
def main():
    st.set_page_config(
        page_title=f"RCA Chatbot for {PROBLEM_SPACE}",
        page_icon="🔍",
        layout="wide"
    )
    st.title(f"🔍 RCA Chatbot for {PROBLEM_SPACE}")
    st.markdown("""
    This chatbot helps analyze system faults and provides detailed RCA (Root Cause Analysis) reports.
    Enter your query to perform a detailed analysis of system issues.
    """)

    # Initialize session state for chat history
    if "messages" not in st.session_state:
        st.session_state.messages = [
            {
                "role": "assistant",
                "content": (
                    "Welcome to the RCA Chatbot! Here are some example queries you can try:\n"
                    "- Why is system 5000 experiencing high latency?\n"
                    "- Why is volume1 in system 5000 experiencing high latency?\n"
                    "Enter your query below to begin."
                )
            }
        ]

    # Add Clear button
    if st.button("Clear Chat"):
        st.session_state.messages = [
            {
                "role": "assistant",
                "content": (
                    "Welcome to the RCA Chatbot! Here are some example queries you can try:\n"
                    "- Why is system 5000 experiencing high latency?\n"
                    "- Why is volume1 in system 5000 experiencing high latency?\n"
                    "- Enter your query below to begin"
                )
            }
        ]
        st.rerun()

    # Display chat history
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            if message["role"] == "assistant" and message["content"].startswith("```text\n"):
                # Display formatted reports in monospace block
                st.markdown(message["content"])
            else:
                # Display other messages (user queries, welcome message, errors) as plain markdown
                st.markdown(message["content"])

    # Chat input
    if query := st.chat_input("Enter your query (e.g., 'Why is system 5000 experiencing high latency?')"):
        # Add user message to chat history
        st.session_state.messages.append({"role": "user", "content": query})
        with st.chat_message("user"):
            st.markdown(query)

        # Process query
        try:
            state = {
                "query": query,
                "port": 0,
                "system_name": "",
                "context": "",
                "fault_analysis": {},
                "formatted_report": "",
                "system_data": {},
                "system_metrics": {},
                "rag_context": ""
            }
            with st.spinner(f"Analyzing system..."):
                result = app.invoke(state)
                formatted_report = result["formatted_report"]
                # Ensure the report is wrapped in formatted text block
                output = f"```text\n{formatted_report}\n```"
                st.session_state.messages.append({"role": "assistant", "content": output})
                with st.chat_message("assistant"):
                    st.markdown(output)
        except SystemNotFoundError as e:
            error_message = "System not found"
            st.session_state.messages.append({"role": "assistant", "content": error_message})
            with st.chat_message("assistant"):
                st.markdown(error_message)
        except Exception as e:
            error_message = f"❌ Error during analysis: {str(e)}"
            st.session_state.messages.append({"role": "assistant", "content": error_message})
            with st.chat_message("assistant"):
                st.markdown(error_message)

if __name__ == "__main__":
    main()