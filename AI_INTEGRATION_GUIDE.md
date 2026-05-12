# AI Integration Guidelines - FreshFizz ERP

This document outlines the standards, architecture, and best practices for integrating AI capabilities within the FreshFizz Manufacturing ERP system.

## 1. Overview
The AI Assistant is designed to provide real-time insights, automate routine tasks, and facilitate data-driven decision-making across all ERP modules (Inventory, Sales, Production, Finance, etc.).

## 2. Architecture
The AI integration follows a tool-calling architecture:
- **LLM Provider**: [Groq](https://groq.com/) using the `llama-3.3-70b-versatile` model.
- **Backend**: Django (via the `ai_assistant` app).
- **Communication**: REST API endpoints for chat and assistant interactions.
- **Execution**: The LLM identifies required tools, the backend executes them against the database, and the results are returned to the LLM to generate the final response.

## 3. Security & Best Practices

### 3.1 API Key Management
- **NEVER** hardcode API keys in the source code.
- Use environment variables (e.g., `GROQ_API_KEY`) and access them via `django.conf.settings` or `os.environ`.
- Ensure `.env` files are in `.gitignore`.

### 3.2 Role-Based Access Control (RBAC)
- All AI tools **MUST** respect the permissions of the logged-in user.
- Before executing a tool, verify if the `request.user` has the required permissions for the underlying data.
- Example: `get_finance_overview` should only be executable if the user has the `finance` or `admin` role.

### 3.3 Data Privacy
- Avoid sending Personally Identifiable Information (PII) to the LLM unless strictly necessary for the task.
- Anonymize data where possible before passing it to the tool response.

## 4. Tool Development (Function Calling)

### 4.1 Naming Conventions
- Function names should be descriptive and use snake_case (e.g., `list_pending_sales_orders`).
- Tool names in the `TOOLS_DEFINITION` must exactly match the keys in `TOOL_MAP`.

### 4.2 Tool Definition (`TOOLS_DEFINITION`)
- Provide clear, concise descriptions for the LLM.
- Define parameters accurately using JSON Schema.
- Keep the number of tools manageable.
- Note: The `parameters` in the description **DO NOT** need to include the `user` object, as that is injected by the backend during execution.

### 4.3 Implementation (`tools.py`)
- Tools should take a `user` (Django User object) as their first argument to facilitate RBAC.
- Example: `def get_inventory_summary(user, item_name=None): ...`
- Tools should return data in a structured format, preferably JSON strings.
- Always include error handling within tool functions.

## 5. Conversation Design & UX

### 5.1 User Confirmation & Heuristics
- For any action that modifies data (e.g., adjusting stock, creating orders, deleting records), the AI **MUST** first:
    1. Explain what it intends to do.
    2. Explain why it is doing it.
    3. Explicitly ask for user confirmation.
- **Frontend Heuristics**: The `AIContext` in the frontend uses keyword-based heuristics (e.g., "should i", "confirm", "proceed") to detect when the AI is asking for permission and automatically displays "Confirm Action" and "Cancel" buttons.
- **Structured Actions**: Future integrations should aim to return structured JSON from the backend (e.g., an `actions` field in the response) rather than relying on text parsing.

### 5.2 Voice Integration & TTS/STT
- **STT**: The system uses the browser's `SpeechRecognition` API for voice input.
- **TTS**: The system uses `SpeechSynthesisUtterance` to read back assistant responses when voice mode is enabled.
- **Conciseness**: AI responses should be structured with bullet points and bold text for visual clarity, but remain conversational for audio playback.

### 5.3 Error Handling
- If the LLM fails or a tool errors out, provide a user-friendly message.
- The frontend displays a toast notification (`AI Assistant is currently unavailable`) if the API call fails or times out.

## 6. Model Parameters
- **Model**: `llama-3.3-70b-versatile`
- **Temperature**: Default (typically 0.7 for balance between creativity and accuracy).
- **Max Tokens**: 4096 (standard for long analytic responses).
- **Stop Sequences**: None (let the model decide completion).

## 7. How to Add a New AI Capability

1. **Define the Logic**: Create a new function in `ai_assistant/tools.py` that interacts with the relevant Django models.
2. **Register the Tool**: Add the function to the `TOOL_MAP` dictionary.
3. **Describe for the LLM**: Add the tool signature to `TOOLS_DEFINITION`.
4. **Update System Prompt**: If the new capability requires specific behavior, update the `system_prompt` in `ai_assistant/views.py`.
5. **Frontend Support**: If the new capability requires a specific UI action (like navigating to a new page or opening a modal), update `executeChatAction` in `AIContext.tsx`.
6. **Test**: Use the AI chat interface to verify that the LLM correctly identifies and calls the new tool.

## 8. Performance & Optimization
- **Backend Latency**: Minimize database queries within tools. Use `.only()` or `.values()` to fetch only necessary fields.
- **Token Usage**: Monitor the size of tool outputs. If a tool returns too much data (e.g., thousands of rows), truncate or summarize it before returning to the LLM.
- **Cold Starts**: Groq-based inference is generally fast, but be prepared for occasional latency peaks.
