import requests
from core.mcp_tools import get_tool_definitions, handle_tool_call

class TestAgent:
    def __init__(self, endpoint=None, model=None, api_key=None):
        self.endpoint = endpoint or "http://localhost:11434/api/chat"
        self.model = model or "llama3.1"
        self.api_key = api_key or ""

    def run_test_suite(self, prompt_text, reference_file=None, session=None):
        system_instruction = (
            "You are an automated network and hardware test engineer inside Modern Multi Terminal. "
            "Use the provided tools to run terminal scripts, execute SFTP actions, and check live logs against baseline files."
        )
        
        full_user_prompt = prompt_text
        if reference_file:
            full_user_prompt += f"\n[Baseline Reference File Path]: {reference_file}"

        messages = [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": full_user_prompt}
        ]

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": messages,
            "tools": get_tool_definitions(),
            "stream": False
        }

        try:
            res = requests.post(self.endpoint, json=payload, headers=headers, timeout=60).json()
        except Exception as e:
            return f"<b>Connection Error:</b> Unable to connect to LLM at <code>{self.endpoint}</code>.<br>Details: {str(e)}"

        message = res.get("message", {})

        # Tool Call execution loop
        if "tool_calls" in message:
            messages.append(message)
            for tool_call in message["tool_calls"]:
                tool_name = tool_call["function"]["name"]
                tool_args = tool_call["function"]["arguments"]

                tool_output = handle_tool_call(tool_name, tool_args, session=session)

                messages.append({
                    "role": "tool",
                    "content": str(tool_output)
                })

            # Send back tool results to generate final test report
            try:
                final_res = requests.post(
                    self.endpoint, 
                    json={"model": self.model, "messages": messages, "stream": False}, 
                    headers=headers, 
                    timeout=60
                ).json()
                return final_res.get("message", {}).get("content", "Test executed.")
            except Exception as e:
                return f"Error receiving final report: {str(e)}"

        return message.get("content", "No output generated.")