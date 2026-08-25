# Log paths are relative to this script's directory.
# log_on() enables logging for the current terminal session.

log_on("logs/diagnostic.log")
print("Logging started")
send("show version")
wait(1.0)
print("Device output should now be recorded")
log_off()
print("All logging stopped")
