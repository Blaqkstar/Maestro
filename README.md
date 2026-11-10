# SWE AI Orchestrated System

An intelligent, multi-agent software engineering assistant that automates complex coding tasks through orchestrated AI agents working in tandem.

## Overview

The SWE AI Orchestrated System is a sophisticated framework that combines multiple specialized AI agents to perform complex software development tasks. It operates as both a command-line interface tool and web-based GUI that can plan, code, and edit files within any project while maintaining strict adherence to project rules and best practices.

## Architecture

### Core Components

1. **Conductor Agent (`agent.py`)** - The central orchestrator that routes requests to appropriate workflows
2. **Planning Agents**
   - `plan_agent.py` - Read-only planning with iterative context gathering
   - `chat_agent.py` - Interactive terminal-based planning session
3. **Code Agents**
   - `codebase_agent.py` - Multi-file editing and code generation agent
4. **Rule System** (`agents_md.py`) - Project-specific rules filtering for different agent roles
5. **GUI Interface** (`gui_server.py`) - Web-based interface with real-time streaming responses

### Agent Roles & Workflows

The system supports multiple orchestrated workflows:

1. **Plan Workflow** - Creates detailed plans before implementation
2. **Code Workflow** - Direct code implementation with safety checks  
3. **Search-Replace Workflow** - Precise file modifications using search-replace blocks
4. **Copy Workflow** - Text and documentation generation

## Key Features

- **Multi-Agent Collaboration**: Multiple AI agents working together on complex tasks
- **Project-Aware Rules**: Adapts behavior based on project-specific rules (AGENTS.md, .cursorrules)
- **Safety Mechanisms**:
  - Backup creation before file modifications
  - Content validation and integrity checks
  - Length and structural safety validations
  - HTML document completeness verification
- **Context Intelligence**: Auto-discovery of files and context gathering during planning
- **Interactive Mode**: Terminal-based chat for iterative development
- **Web Interface**: Real-time streaming responses with diff visualization
- **VS Code Integration**: Seamless integration with LLM Orchestra extension

## Installation & Setup

### Prerequisites

1. Python 3.8 or higher
2. Node.js (for LLM Orchestra extension)
3. Local LLM server running on `localhost:1234`
4. LLM Orchestra VS Code Extension installed

### Quick Start

```bash
# Clone repository and install dependencies
git clone <repository-url>
cd SWE-AI-Orchestrated-System

# Install Python requirements (if any)
pip install -r requirements.txt

# Set up local LLM server at localhost:1234
# Ensure your LLM service supports the Qwen 3B model
```

## System Files Structure

- `agent.py` - Main orchestration point that routes requests to appropriate workflows
- `plan_agent.py` - Read-only planning agent with iterative context gathering  
- `chat_agent.py` - Interactive terminal-based planning session
- `codebase_agent.py` - Multi-file editing and code generation agent
- `agents_md.py` - Project rule filtering system for different agent roles
- `system-rules.md` - Fallback rules applied when no project-specific rules exist
- `gui_server.py` - Web server with real-time streaming responses
- `gui.html` - Web interface for the GUI server

## Workflows & Usage Patterns

### 1. Plan Workflow (`plan-workflow`)
For tasks requiring detailed planning:
```bash
python plan_agent.py . "Add newsletter signup form" --save
```

### 2. Code Workflow (`code-workflow`)
Direct code implementation with safety checks:
```bash
python codebase_agent.py . "Fix the navbar collapse CSS"
```

### 3. Interactive Session (`chat-agent`)
For iterative development and discussion:
```bash
python chat_agent.py .
# Then interact via terminal commands like:
# /read src/main.js - Read specific file  
# /tree - Show project tree
# /save - Save the plan artifact
# /help - Show help
```

### 4. Web GUI Interface (`gui_server.py`)
Launch web interface for visual interaction:
```bash
python gui_server.py
# Access at http://localhost:5000
```

## Safety Mechanisms

The system implements multiple safety checks to prevent harmful modifications:

1. **Backup Creation**: All files are backed up before modification
2. **Length Validation**: Prevents suspiciously short outputs that might be truncated  
3. **HTML Integrity Checks**: Ensures HTML documents remain complete and valid
4. **Content Filtering**: Removes debug prints, secrets, and dead code
5. **File Path Validation**: Prevents escaping repository boundaries

## Configuration & Rules System

The system supports flexible rule configuration through:

1. **AGENTS.md** - Project-specific rules for agents
2. **.cursorrules** - Cursor IDE integration rules  
3. **.clinerules** - Cline AI assistant rules
4. **system-rules.md** - Fallback rules applied globally

### Rule Filtering

Rules are filtered by agent role:
- `conductor` - Routing decisions
- `scout` - File discovery and analysis
- `coder` - Code generation and editing  
- `writer` - Documentation and text creation
- `critic` - Validation and review

## Batch Scripts & Tools

### Windows Batch Files:

- **Start-Agents.bat** - Launch all agents in the system
- **Start-GUI.bat** - Start GUI web server interface
- **Start-Server.bat** - Launch LLM server instance  
- **run-agent.bat** - Run individual agent processes
- **agent-cli.bat** - Command-line interface wrapper

### PowerShell Script:

- **start-orchestra.ps1** - Initialize and start the LLM Orchestra framework

## Integration with LLM Orchestra Extension

This SWE system integrates with VS Code's [LLM Orchestra extension](https://marketplace.visualstudio.com/items?itemName=local.llm-orchestra) to provide:

1. **Webview Interface**: Interactive AI assistant panel
2. **Multi-Agent Visualization**: See reasoning process of each agent  
3. **Code Review Tools**: Diff-based code change review before application
4. **Workspace Integration**: Seamless project file operations within VS Code

## Troubleshooting

### Common Issues:

1. **LLM Server Not Found**: Ensure LLM server is running at `localhost:1234` with Qwen 3B model available
2. **Permission Denied Errors**: Check if all files are readable/writable by the user process  
3. **Command Line Length Limitations**: Large prompts may be handled via stdin instead of command line arguments

### Diagnostic Commands:

```bash
# Check system status
python smoke-test.py

# View current agent configurations
python agent.py --help

# Test individual agents
python plan_agent.py . "Test plan workflow" --quiet
```

## Development

### Creating New Agents

To add a new agent, follow these patterns:
1. Create agent implementation file (`new_agent.py`)
2. Implement appropriate workflow logic  
3. Register in the routing system if needed

### Extending Workflows

Add new workflows by:
1. Defining the orchestration configuration for your new workflow
2. Creating necessary agent components
3. Updating routing decisions based on user prompts

## Contributing

This project welcomes contributions to enhance the multi-agent orchestration capabilities, add new workflow types, or improve safety mechanisms. Please follow these steps:

1. Fork the repository  
2. Create feature branches for each change
3. Submit pull requests with clear descriptions
4. Ensure all tests pass before submission

## License

MIT License - see LICENSE file for details.

## Acknowledgements

This system builds upon and integrates with:

- [LLM Orchestra](https://github.com/your-repo/llm-orchestra) framework for multi-agent collaboration  
- VS Code Extension ecosystem
- Modern AI agent orchestration patterns

For detailed information about each component, please refer to the individual source files.# Maestro
