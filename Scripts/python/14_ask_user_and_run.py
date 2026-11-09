# Ask for a value, then use it in a command.

host = input("Host to ping")
count = input("How many packets? (default 3)") or "3"
send(f"ping -c {count} {host}")
wait("packets transmitted", timeout=30)
