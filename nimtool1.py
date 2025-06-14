import os
import json
import re
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
from volume_contribution_calculator import calculate_volume_contribution

# === CONFIG ===
TEXT_PATH = "rca2.txt"
GROQ_API_KEY = ""
GROQ_MODEL = "llama-3.3-70b-versatile"

# === Initialize LLM ===
llm = ChatOpenAI(
    model=GROQ_MODEL,
    openai_api_base="https://api.groq.com/openai/v1",
    openai_api_key=GROQ_API_KEY,
    temperature=0
)

# === Load and Chunk RCA Document ===
print("\n\n🔍 Loading and splitting RCA document...")
if not os.path.exists(TEXT_PATH):
    raise FileNotFoundError(f"Text file not found at {TEXT_PATH}")
loader = TextLoader(TEXT_PATH)
docs = loader.load()
if not docs:
    raise ValueError("No content loaded from the text file")
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
    system_metrics: Dict[str, Any]  # New field for metrics

# === Agent 1: Data Extraction Agent ===
def extract_relevant_data(state: AgentState) -> AgentState:
    """Extract relevant files and context for the given port and query."""
    query = state["query"]
    port_match = re.search(r'(?:system|port)\s+(\d+)', query.lower())
    port = int(port_match.group(1)) if port_match else 5000

    # Load system data
    data_dir = f"data/data_instance_{port}"
    system_data = {}
    system_metrics = {}
    if not os.path.exists(data_dir):
        state["context"] = f"⚠️ Warning: Data directory {data_dir} not found"
        state["port"] = port
        state["system_name"] = f"System_{port}"
        state["system_data"] = system_data
        state["system_metrics"] = system_metrics
        return state

    context_parts = []
    
    # System info
    system_file = f"{data_dir}/system.json"
    if os.path.exists(system_file):
        with open(system_file, 'r') as f:
            system_data_raw = json.load(f)
        # Handle list structure
        if isinstance(system_data_raw, list) and len(system_data_raw) > 0:
            system_data = system_data_raw[0]
            system_name = system_data.get("name", f"System_{port}")
            context_parts.append(f"System Information:\n{json.dumps(system_data, indent=2)}")
        else:
            system_data = system_data_raw
            system_name = system_data.get("name", f"System_{port}")
            context_parts.append(f"System Information:\n{json.dumps(system_data, indent=2)}")

    # Latest metrics
    metrics_file = f"{data_dir}/system_metrics.json"
    if os.path.exists(metrics_file):
        with open(metrics_file, 'r') as f:
            metrics_data = json.load(f)
        if metrics_data and isinstance(metrics_data, list) and len(metrics_data) > 0:
            system_metrics = metrics_data[-1]  # Store latest metrics
            context_parts.append(f"Latest Metrics:\n{json.dumps(system_metrics, indent=2)}")

    # Volumes info
    volumes_file = f"{data_dir}/volume.json"
    if os.path.exists(volumes_file):
        with open(volumes_file, 'r') as f:
            volumes_data = json.load(f)
        context_parts.append(f"Volumes Information:\n{json.dumps(volumes_data, indent=2)}")

    # IO metrics
    io_metrics_file = f"{data_dir}/io_metrics.json"
    if os.path.exists(io_metrics_file):
        with open(io_metrics_file, 'r') as f:
            io_metrics_data = json.load(f)
        context_parts.append(f"IO Metrics:\n{json.dumps(io_metrics_data, indent=2)}")

    # Replication metrics
    replication_file = f"{data_dir}/replication_metrics.json"
    if os.path.exists(replication_file):
        with open(replication_file, 'r') as f:
            replication_data = json.load(f)
        context_parts.append(f"Replication Metrics:\n{json.dumps(replication_data, indent=2)}")

    # Snapshots info
    snapshots_file = f"{data_dir}/snapshots.json"
    if os.path.exists(snapshots_file):
        with open(snapshots_file, 'r') as f:
            snapshots_data = json.load(f)
        context_parts.append(f"Snapshots Information:\n{json.dumps(snapshots_data, indent=2)}")

    # Logs
    logs_file = f"{data_dir}/logs_{port}.txt"
    if os.path.exists(logs_file):
        with open(logs_file, 'r') as f:
            logs_content = f.read()[:1000]
        context_parts.append(f"System Logs:\n{logs_content}")

    state["context"] = "\n\n".join(context_parts)
    state["port"] = port
    state["system_name"] = system_name
    state["system_data"] = system_data
    state["system_metrics"] = system_metrics
    return state

# === Agent 2: Fault Analysis Agent ===
def analyze_fault(state: AgentState) -> AgentState:
    """Analyze the fault using rca2.txt logic and system data."""
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
            for k, v in obj.items():
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
                    section_lines = section.split('\n')
                    section_title = section_lines[0].strip()
                    section_content = section
                    lines.append(f"{prefix}.{section_title} = {section_content}")
        return lines

    flattened = flatten_json(context) if context else []
    formatted_input = "\n".join(flattened)

    # JSON structure for fault analysis
    json_structure = """{
        "fault_type": "High latency due to high saturation" or "High latency due to high capacity" or "High latency due to replication link issues" or "No fault",
        "details": {
            "latency": <latency value from system_metrics>,
            "capacity_percentage": <capacity percentage inferred from system_metrics and volumes>,
            "saturation": <saturation percentage from system_metrics>,
            "volume_capacity": <total volume capacity from volume data>,
            "snapshot_capacity": <total snapshot capacity from snapshot data>,
            "maximum_capacity": <maximum capacity from system_data>,
            "maximum_throughput": <maximum throughput from system_data>,
            "volume_details": [
                {
                    "volume_id": <volume id>,
                    "name": <volume name>,
                    "capacity_percentage": <capacity percentage inferred from volume size and max_capacity>,
                    "size": <size>,
                    "snapshot_count": <snapshot count>,
                    "throughput": <throughput from volume or io_metrics>,
                    "workload_size": <workload size from volume or io_metrics>
                }
            ],
            "replication_issues": [
                {
                    "volume_id": <volume id>,
                    "volume_name": <volume name>,
                    "target_id": <target system id>,
                    "target_system_name": <target system name>,
                    "latency": <latency value>,
                    "timestamp": <timestamp>
                }
            ]
        }
    }"""

    # Construct analysis prompt
    analysis_prompt = ChatPromptTemplate.from_messages([
        SystemMessage(content=(
            "You are an RCA assistant. Based on the structured system data, system metrics, and RCA logic from rca2.txt, diagnose the root cause of the issue described in the query. "
            "Steps:\n"
            "1. Apply the fault diagnosis rules from rca2.txt to determine the fault type.\n"
            "2. Use system_metrics to infer latency, saturation, and capacity_percentage where available.\n"
            "3. Return only the highest causing fault.\n"
            "4. Include relevant details such as latency, capacity, saturation, volume, snapshot, and replication information.\n"
            "5. If replication metrics are available, check for replication impairment issues as per rca2.txt.\n"
            "6. Ensure the response is based solely on rca2.txt logic, system_data, and system_metrics, without assuming hardcoded thresholds.\n"
            "7. Latency < 3ms indicates no fault.\n"
            "8. Return a valid JSON object with the structure provided, using numeric values for all fields (e.g., compute percentages directly).\n"
            "9. Do not include Python expressions (e.g., (5 / 20) * 100) or additional text in the JSON; compute the values explicitly.\n"
            "10. Perform proper reasoning and analysis based on the provided data and rca2.txt logic before concluding the fault type; do not make assumptions.\n"
            
        )),
        HumanMessage(content=(
            f"Query: {query}\n\n"
            f"Extracted system data:\n{formatted_input}\n\n"
            f"Expected JSON structure:\n{json_structure}"
        ))
    ])

    # Retrieve relevant RCA chunks
    relevant_docs = retriever.invoke(query)
    context_with_rca = "\n".join([doc.page_content for doc in relevant_docs])
    
    # Update the system message with RCA context
    system_message = analysis_prompt.messages[0].content + f"\nRCA Logic from rca2.txt:\n{context_with_rca}"
    
    # Create the final messages list
    messages = [
        SystemMessage(content=system_message),
        HumanMessage(content=analysis_prompt.messages[1].content)
    ]
    
    # Invoke the LLM with the messages
    print("\n=== Invoking LLM for Fault Analysis ===")
    response = llm.invoke(messages)
    print(f"Raw LLM Response: {response.content}")
    
    # Try to parse the JSON, handling mixed text+JSON response
    fault_analysis = None
    raw_response = response.content
    try:
        fault_analysis = json.loads(raw_response)
    except json.JSONDecodeError:
        print("Error: Failed to parse LLM response as JSON, attempting to extract JSON from raw_result")
        json_match = re.search(r'```json\n([\s\S]*?)\n```', raw_response)
        if json_match:
            json_str = json_match.group(1)
            # Preprocess JSON to replace invalid expressions
            json_str = re.sub(r'\((\d+)\s*/\s*(\d+)\)\s*\*\s*100', lambda m: str(int(m.group(1)) / int(m.group(2)) * 100), json_str)
            try:
                fault_analysis = json.loads(json_str)
            except json.JSONDecodeError as e:
                print(f"Error: Failed to parse extracted JSON: {e}")
                fault_analysis = {"error": "Invalid analysis output", "raw_result": raw_response}
        else:
            fault_analysis = {"error": "Invalid analysis output", "raw_result": raw_response}

    # Calculate volume contributions
    if not fault_analysis.get("error"):
        if not system_data.get("max_capacity"):
            print(f"Warning: max_capacity not found in system_data for port {state['port']}")
        try:
            fault_analysis = calculate_volume_contribution(fault_analysis, system_data)
            print("\n=== Fault Analysis with Volume Contributions ===")
            print(json.dumps(fault_analysis, indent=2))
        except Exception as e:
            print(f"Error in volume contribution calculation: {e}")
            fault_analysis["error"] = f"Volume contribution calculation failed: {str(e)}"
    else:
        print(f"Error: Skipping volume contribution calculation due to invalid fault analysis: {fault_analysis}")

    state["fault_analysis"] = fault_analysis
    return state

# === Agent 3: Response Formatting Agent ===
def format_response(state: AgentState) -> AgentState:
    """Format the fault analysis into a human-readable report."""
    fault_analysis = state["fault_analysis"]
    system_name = state["system_name"]
    port = state["port"]

    formatting_prompt = ChatPromptTemplate.from_messages([
    SystemMessage(content=(
        f"Format the following JSON fault analysis into a concise, human-readable report for system {system_name} (Port: {port}). "
        "Include the fault type, key details (e.g., latency, capacity, saturation), and relevant volume, snapshot, or replication information. "
        "Use bullet points for volumes, snapshots, replication issues, and volume contributions. "
        "Keep it clear, structured, and under 300 words. Avoid raw JSON or code-like formatting. "
        "Only include the highest causing fault and ensure the report is actionable. "

        # Volume contribution rules
        "Use the volume_contributions field from the JSON for consistent volume contribution reporting. "
        "For saturation faults, include saturation_contribution from volume_contributions. "
        "If volume_contributions is empty or missing, indicate that volume contribution data is unavailable and suggest checking system configuration. "

        # Special rules for replication faults
        "For replication issues, report them only if this system is the source (i.e., latency is observed in its replication_metrics file). "
        "Do not treat target system replication latency as a fault. Only show replication issues with latency >= 3 ms. "
        "When reporting replication faults, highlight the primary affected volume with 100% contribution. "

        # Conditional sections
        "If fault_type is 'No fault', omit the 'Bully Volume' section entirely. "
        "Otherwise, include them with appropriate data from volume_contributions. "

        # Report format guide
        "Use this structure:\n"
        f"Fault Report for System {system_name}\n"
        "Fault Type: <type>\n"
        "Key Details: <metrics>\n"
        "Replication Issues: <only if fault_type is replication-related>\n"
        "Volume Information: <details with volume size in GB, workload size in KB if available,  throughput in MB/s as (2000 * workload size) / 1024>\n"
        "Snapshot Information: <only if fault_type is snapshot-related>\n"
        "Volume Contributions:\n"
        "- <volume_name> (<volume_id>): capacity contribution: <contribution_percentage>% "
        "(saturation contribution: <saturation_contribution>% if applicable)\n"
        "[Only if fault_type is not 'No fault']\n"
        "Bully Volume: <volume and contribution from volume_contributions for the applicable fault>\n"
        "Next Actions: <detailed actions to be taken>\n"
    )),
    HumanMessage(content=f"JSON Analysis:\n{json.dumps(fault_analysis, indent=2)}")
])

    messages = [
        SystemMessage(content=formatting_prompt.messages[0].content),
        HumanMessage(content=formatting_prompt.messages[1].content)
    ]
    
    formatted_report = llm.invoke(messages).content
    state["formatted_report"] = formatted_report
    return state

# === LangGraph Workflow ===
workflow = StateGraph(AgentState)

workflow.add_node("extract_data", extract_relevant_data)
workflow.add_node("analyze_fault", analyze_fault)
workflow.add_node("format_response", format_response)

workflow.add_edge("extract_data", "analyze_fault")
workflow.add_edge("analyze_fault", "format_response")
workflow.add_edge("format_response", END)

workflow.set_entry_point("extract_data")

app = workflow.compile()

# === Main Loop ===
def main():
    print("\n✅ LangGraph Agentic Fault Detection System")
    print("Example queries:")
    print("- Why is system 5000 experiencing high latency?")
    print("- Check replication issues in system 5001")
    print("- Analyze snapshot capacity in system 5000")
    print("- Show faults across all systems")
    print("Type 'exit' to quit\n")

    while True:
        query = input("🔎 Enter your query: ").strip()
        if query.lower() in ("exit", "quit"):
            print("👋 Exiting. Take care!")
            break

        try:
            state = {"query": query, "port": 0, "system_name": "", "context": "", "fault_analysis": {}, "formatted_report": "", "system_data": {}, "system_metrics": {}}
            result = app.invoke(state)
            print("\n" + "="*100)
            print(result["formatted_report"])
            print("="*100 + "\n")
        except Exception as e:
            print(f"❌ Error during analysis: {e}")

if __name__ == "__main__":
    main()