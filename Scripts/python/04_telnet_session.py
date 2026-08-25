# Telnet connection example.

connect_telnet("192.168.1.50", port=23)
wait("login:")
send("admin")
wait("Password:")
send("CHANGE_ME")
wait("#")
send("show version")
