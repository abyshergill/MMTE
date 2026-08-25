# SSH connection example.
# Credentials should come from a safer future secrets mechanism, not source code.

connect_ssh(
    host="192.168.1.50",
    username="admin",
    password="CHANGE_ME",
    port=22,
)

wait("$")
send("show version")
wait(1.0)
send("show system uptime")
print("SSH commands completed")
