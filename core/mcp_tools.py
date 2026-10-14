import os
import tempfile
import posixpath
from pathlib import Path

# Imports matching project layout
try:
    from core.script_engine import execute_python_script_sync
except ImportError:
    from script_engine import execute_python_script_sync

try:
    from gui.sftp_dialog import get_active_sftp, upload_folder, download_folder
except ImportError:
    from sftp_hops import get_active_sftp, upload_folder, download_folder


def get_tool_definitions():
    """Defines MCP tools for python script execution, SFTP operations, and log comparison."""
    return [
        {
            "name": "execute_python_script",
            "description": "Runs Python automation code in the active session context via execute_python_script_sync.",
            "parameters": {
                "type": "object",
                "properties": {
                    "script_code": {
                        "type": "string",
                        "description": "Python code to run (can use send, wait, sleep, log_on, etc.)"
                    }
                },
                "required": ["script_code"]
            }
        },
        {
            "name": "sftp_upload",
            "description": "Uploads a local file or directory to the remote device using sftp_dialog functions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "local_path": {"type": "string", "description": "Source path on local machine"},
                    "remote_path": {"type": "string", "description": "Destination path on remote host"},
                    "is_directory": {"type": "boolean", "description": "Set True to recursively upload a folder"}
                },
                "required": ["local_path", "remote_path"]
            }
        },
        {
            "name": "sftp_download",
            "description": "Downloads a remote file or directory to the local machine over SFTP.",
            "parameters": {
                "type": "object",
                "properties": {
                    "remote_path": {"type": "string", "description": "Source path on remote host"},
                    "local_path": {"type": "string", "description": "Destination path on local machine"},
                    "is_directory": {"type": "boolean", "description": "Set True to recursively download a folder"}
                },
                "required": ["remote_path", "local_path"]
            }
        },
        {
            "name": "read_reference_and_logs",
            "description": "Reads a local baseline reference file and fetches active terminal plain text buffer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reference_path": {"type": "string", "description": "Path to local reference file"}
                },
                "required": ["reference_path"]
            }
        }
    ]


def handle_tool_call(tool_name: str, arguments: dict, session=None):
    """Executes tools against the active TerminalSession and sftp_dialog utilities."""

    if tool_name == "execute_python_script":
        if not session:
            return {"status": "error", "message": "No active session provided for script execution."}

        script_code = arguments["script_code"]
        
        # Write temporary script file to execute via execute_python_script_sync
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as tmp:
            tmp.write(script_code)
            tmp_path = tmp.name

        try:
            execute_python_script_sync(session, tmp_path)
            return {"status": "success", "message": "Script executed successfully."}
        except Exception as err:
            return {"status": "error", "message": f"Script execution error: {str(err)}"}
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    elif tool_name == "sftp_upload":
        if not session:
            return {"status": "error", "message": "No active SSH session for SFTP."}

        try:
            sftp = get_active_sftp(session)
            local_path = arguments["local_path"]
            remote_path = arguments["remote_path"]
            is_dir = arguments.get("is_directory", False) or os.path.isdir(local_path)

            if is_dir:
                upload_folder(sftp, local_path, remote_path)
            else:
                sftp.put(local_path, remote_path)

            return {"status": "success", "message": f"Uploaded {local_path} -> {remote_path}"}
        except Exception as err:
            return {"status": "error", "message": f"SFTP upload failed: {str(err)}"}

    elif tool_name == "sftp_download":
        if not session:
            return {"status": "error", "message": "No active SSH session for SFTP."}

        try:
            sftp = get_active_sftp(session)
            remote_path = arguments["remote_path"]
            local_path = arguments["local_path"]
            is_dir = arguments.get("is_directory", False)

            if is_dir:
                download_folder(sftp, remote_path, local_path)
            else:
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                sftp.get(remote_path, local_path)

            return {"status": "success", "message": f"Downloaded {remote_path} -> {local_path}"}
        except Exception as err:
            return {"status": "error", "message": f"SFTP download failed: {str(err)}"}

    elif tool_name == "read_reference_and_logs":
        ref_path = arguments["reference_path"]
        ref_content = ""

        if os.path.exists(ref_path):
            with open(ref_path, "r", encoding="utf-8", errors="replace") as f:
                ref_content = f.read()
        else:
            ref_content = f"Error: Baseline reference file '{ref_path}' not found."

        # Fetch live terminal buffer directly from session widget
        terminal_logs = ""
        if session and hasattr(session, "terminal"):
            try:
                # Capture recent buffer (last 10,000 chars) to stay within LLM context
                terminal_logs = session.terminal.toPlainText()[-10000:]
            except Exception as err:
                terminal_logs = f"Error reading terminal text: {str(err)}"

        return {
            "reference_baseline": ref_content,
            "terminal_logs": terminal_logs
        }

    return {"status": "error", "message": f"Unknown tool: {tool_name}"}