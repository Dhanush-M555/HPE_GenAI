# Storage System AI Diagnostic Agent

This AI agent helps users diagnose and troubleshoot latency issues in storage systems by analyzing metrics, logs, and configurations.

## Features

- Time-aware analysis: Understands temporal references in user queries (e.g., "yesterday at 1am")
- Tool-based diagnosis: Uses Groq LLM to call tools for fetching relevant data
- RAG integration: Incorporates support documentation into analysis
- Visualization: Displays key metrics in interactive charts
- Conversation history: Maintains context throughout the diagnostic session

## Supported Latency Issue Types

The agent can diagnose three main types of latency faults:

1. **High Capacity Issues**: Detects when storage capacity usage is causing latency
2. **High Saturation Issues**: Identifies when system is overloaded with I/O
3. **Replication Link Issues**: Diagnoses problems with replication connections

## Setup Instructions

### Prerequisites

- Python 3.8+
- Groq API key (for LLM access)

### Installation

1. Clone this repository:
   ```
   git clone <repository-url>
   cd storage-ai-agent
   ```

2. Install required dependencies:
   ```
   pip install -r requirements.txt
   ```

3. Set up environment variables (create a `.env` file):
   ```
   GROQ_API_KEY=your_groq_api_key_here
   ```

4. Ensure support documentation is available:
   - Place your documentation as `support_documentation.pdf` or `support_documentation.txt` in the project root

### Running the Agent

Start the Streamlit app:
```
streamlit run ai_agent.py
```

The web interface will be available at http://localhost:8501

## Usage Instructions

1. Select a storage system from the dropdown menu in the sidebar
2. Type your query in the text area (e.g., "Why was my latency high yesterday at 1am?")
3. Click "Analyze" to initiate the diagnostic process
4. Review the AI's analysis and recommendations
5. Ask follow-up questions to get more specific information

## Metrics Files Reference

The agent analyzes these key data sources:

- **system_metrics.json**: System-wide metrics (capacity, throughput, saturation)
- **io_metrics.json**: I/O performance data by volume and host
- **replication_metrics.json**: Replication performance and status
- **Log files**: System events and status messages

## Development

To extend the agent with new diagnostic capabilities:

1. Add new tool definitions to `ai_tools.py`
2. Update the support documentation with relevant information
3. Enhance visualization in `ai_agent.py` as needed

## License

[MIT License](LICENSE)