# Run a list of commands and stop if one of them never returns to the prompt.

COMMANDS = ["uname -a", "uptime", "df -h", "free -m", "ip -br addr"]
PROMPT = "$"

for number, command in enumerate(COMMANDS, start=1):
    send(command)
    if not wait(PROMPT, timeout=20):
        alert(f"Command {number} ({command}) did not finish in 20 s.")
        break
    sleep(0.5)
