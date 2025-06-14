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
    function_name = tool.get('function', 'run')
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

# === Initialize RAG Once ===
def initialize_rag():
    """Load and index RAG document once at startup."""
    if "retriever" not in st.session_state:
        print("🔍 Loading and splitting RAG document...")
        loader = TextLoader(RAG_PATH)
        docs = loader.load()
        if not docs:
            raise ValueError("No content loaded from the RAG file")
        splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=100)
        chunks = splitter.split_documents(docs)

        print("📡 Embedding and indexing...")
        embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")
        vectorstore = FAISS.from_documents(chunks, embeddings)
        st.session_state.retriever = vectorstore.as_retriever(search_type="similarity", search_kwargs={"k": 7})

# Run RAG initialization
initialize_rag()

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

    # Initialize state
    state["port"] = port
    state["system_name"] = f"System_{port}"
    state["system_data"] = {}
    state["system_metrics"] = {}
    state["context"] = ""
    state["rag_context"] = ""
    state["skip_analysis"] = False

    # Check for system data directory
    data_dir = f"data_instance_{port}"
    if not os.path.exists(data_dir):
        print(f"Error: Data directory {data_dir} not found")
        state["fault_analysis"] = {"error": "System not found", "port": port}
        state["skip_analysis"] = True
        return state

    # Load system data
    context_parts = []
    system_data = {}
    system_metrics = {}

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
            state["system_name"] = system_data.get("name", f"System_{port}")
            context_parts.append(f"System Information:\n{json.dumps(system_data, indent=2)}")
        except Exception as e:
            context_parts.append(f"⚠️ Error loading system.json: {str(e)}")

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
    state["system_data"] = system_data
    state["system_metrics"] = system_metrics
    return state


# === Agent 2: Fault Analysis Agent ===
def analyze_fault(state: AgentState) -> AgentState:
    """Analyze the fault using RAG logic and system data."""
    try:
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
            print("\n=== Loaded Analyze Prompt ===")
            print(analyze_prompt_content)
        except Exception as e:
            print(f"Error loading {ANALYZE_PROMPT_PATH}: {e}")
            analyze_prompt_content = "Analyze the fault and return a JSON object based on the provided data and structure."

        # Retrieve relevant RAG chunks
        relevant_docs = st.session_state.retriever.invoke(query)
        context_with_rca = "\n".join([doc.page_content for doc in relevant_docs])
        print("\n=== Retrieved RAG Context ===")
        print(context_with_rca)
        
        # Construct system message
        system_message = analyze_prompt_content.replace("{PROBLEM_SPACE}", PROBLEM_SPACE) + f"\nRAG Logic:\n{context_with_rca}"
        print("\n=== Constructed System Message ===")
        print(system_message)

        # Construct messages list
        messages = [
            SystemMessage(content=system_message),
            HumanMessage(content=(
                f"Query: {query}\n\n"
                f"Extracted system data:\n{formatted_input}\n\n"
                f"Expected JSON structure:\n{fault_analysis_structure}"
            ))
        ]
        print("\n=== Constructed Messages ===")
        print(json.dumps([{"role": m.type, "content": m.content} for m in messages], indent=2))

        # Invoke the LLM
        print("\n=== Invoking LLM for Fault Analysis ===")
        response = llm.invoke(messages)
        print(f"Raw LLM Response: {response.content}")
        
        # Parse JSON response
        fault_analysis = None
        raw_response = response.content.strip()
        print("\n=== Processing Raw Response ===")
        print(f"Raw response before cleanup: {raw_response}")
        
        if raw_response.startswith('```json'):
            raw_response = raw_response[7:]
            print("Removed ```json prefix")
        if raw_response.endswith('```'):
            raw_response = raw_response[:-3]
            print("Removed ``` suffix")
        raw_response = raw_response.strip()
        print(f"Raw response after cleanup: {raw_response}")
        
        try:
            print("\n=== Attempting JSON Parse ===")
            fault_analysis = json.loads(raw_response)
            print("Successfully parsed JSON")
            print(f"Parsed JSON: {json.dumps(fault_analysis, indent=2)}")
            
            # Validate required fields
            print("\n=== Validating Required Fields ===")
            if "tool_call" not in fault_analysis:
                raise ValueError("Missing 'tool_call' field in response")
            print("✓ Found tool_call field")
            
            if "tool_name" not in fault_analysis["tool_call"]:
                raise ValueError("Missing 'tool_name' in tool_call")
            print("✓ Found tool_name in tool_call")
            
            if "parameters" not in fault_analysis["tool_call"]:
                raise ValueError("Missing 'parameters' in tool_call")
            print("✓ Found parameters in tool_call")
            
            if "fault_analysis" not in fault_analysis["tool_call"]["parameters"]:
                raise ValueError("Missing 'fault_analysis' in parameters")
            print("✓ Found fault_analysis in parameters")
            
            if "system_data" not in fault_analysis["tool_call"]["parameters"]:
                raise ValueError("Missing 'system_data' in parameters")
            print("✓ Found system_data in parameters")
                
        except json.JSONDecodeError as e:
            print(f"\n❌ JSON Parse Error: {e}")
            print(f"Raw response: {raw_response}")
            fault_analysis = {
                "error": "Invalid JSON response",
                "raw_response": raw_response,
                "parse_error": str(e)
            }
        except ValueError as e:
            print(f"\n❌ Validation Error: {str(e)}")
            fault_analysis = {
                "error": str(e),
                "raw_response": raw_response
            }

        print("\n=== Final Fault Analysis ===")
        print(json.dumps(fault_analysis, indent=2))
        
        state["fault_analysis"] = fault_analysis
        state["rag_context"] = context_with_rca
        return state
        
    except Exception as e:
        print(f"\n❌ Unexpected Error in analyze_fault: {str(e)}")
        import traceback
        print(f"Traceback: {traceback.format_exc()}")
        state["fault_analysis"] = {
            "error": f"Unexpected error: {str(e)}",
            "traceback": traceback.format_exc()
        }
        return state

# === Agent 3: Tool Agent ===
def tool_agent(state: AgentState) -> AgentState:
    """Invoke the tool to calculate contributions."""
    fault_analysis = state["fault_analysis"]
    system_data = state["system_data"]
    
    if fault_analysis.get("error"):
        print(f"Error: Skipping tool invocation due to invalid fault analysis: {fault_analysis}")
        return state

    tool_call = fault_analysis.get("tool_call", {})
    tool_name = tool_call.get("tool_name")
    parameters = tool_call.get("parameters", {})

    if not tool_name or not parameters:
        print(f"Error: Missing tool_call information in fault analysis")
        fault_analysis["error"] = "Missing tool_call information"
        state["fault_analysis"] = fault_analysis
        return state

    if tool_name not in TOOLS:
        print(f"Error: Tool {tool_name} not configured")
        fault_analysis["error"] = f"Tool {tool_name} not found"
        state["fault_analysis"] = fault_analysis
        return state

    tool = TOOLS[tool_name]
    
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

    if not rag_context:
        print("Warning: No RAG context in state, retrieving chunks")
        relevant_docs = st.session_state.retriever.invoke(query)
        rag_context = "\n".join([doc.page_content for doc in relevant_docs])

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

    system_message = format_prompt_content.format(
        PROBLEM_SPACE=PROBLEM_SPACE,
        system_name=system_name,
        port=port,
        rag_context=rag_context
    )

    messages = [
        SystemMessage(content=system_message),
        HumanMessage(content=f"JSON Analysis:\n{json.dumps(fault_analysis, indent=2)}")
    ]

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
        page_title=f"RCA Chatbot ({PROBLEM_SPACE})",
        page_icon="🔍",
        layout="wide"
    )
    st.title(f"🔍 RCA Chatbot ({PROBLEM_SPACE})")
    st.markdown("""
    This chatbot helps analyze system faults and provides detailed RCA (Root Cause Analysis) reports.
    Enter your query about system performance.
    """)

    debug_mode = st.sidebar.checkbox("Debug Mode", value=False)

    if "messages" not in st.session_state:
        st.session_state.messages = [
            {
                "role": "assistant",
                "content": (
                    "Welcome to the RCA Chatbot! Here are some example queries you can try:\n"
                    "- Why is system 5000 experiencing high latency?\n"
                    "- Why is volume1 in system 5000 experiencing high latency?\n"
                    "- Give me a detailed fault report for system 5000\n\n"
                    
                    "Enter your query below to begin."
                )
            }
        ]

    # Render old messages with proper markdown formatting
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            # Ensure markdown is rendered correctly
            st.markdown(message["content"], unsafe_allow_html=False)

    if query := st.chat_input("Enter your query (e.g., 'Why is system 5000 experiencing high latency?')"):
        st.session_state.messages.append({"role": "user", "content": query})
        with st.chat_message("user"):
            st.markdown(query)

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
            
            debug_placeholder = st.empty()
            
            with st.spinner(f"Analyzing system..."):
                import io
                from contextlib import redirect_stdout
                
                if debug_mode:
                    f = io.StringIO()
                    with redirect_stdout(f):
                        result = app.invoke(state)
                    debug_output = f.getvalue()
                    debug_placeholder.text_area("Debug Output", debug_output, height=400)
                else:
                    result = app.invoke(state)
                
                formatted_report = result["formatted_report"]
                
                if "error" in result["fault_analysis"]:
                    error_msg = f"❌ Error during analysis: {result['fault_analysis']['error']}"
                    if debug_mode and "traceback" in result["fault_analysis"]:
                        error_msg += f"\n\nTraceback:\n{result['fault_analysis']['traceback']}"
                    if debug_mode and "raw_response" in result["fault_analysis"]:
                        error_msg += f"\n\nRaw Response:\n{result['fault_analysis']['raw_response']}"
                    st.session_state.messages.append({"role": "assistant", "content": error_msg})
                    with st.chat_message("assistant"):
                        st.markdown(error_msg, unsafe_allow_html=False)
                else:
                    # Store the formatted report wrapped in a markdown code block
                    output = f"```text\n{formatted_report}\n```"
                    st.session_state.messages.append({"role": "assistant", "content": output})
                    with st.chat_message("assistant"):
                        st.markdown(output, unsafe_allow_html=False)
                        
        except Exception as e:
            import traceback
            error_message = f"❌ Unexpected error: {str(e)}\n\nTraceback:\n{traceback.format_exc()}"
            st.session_state.messages.append({"role": "assistant", "content": error_message})
            with st.chat_message("assistant"):
                st.markdown(error_message, unsafe_allow_html=False)


if __name__ == "__main__":
    main()