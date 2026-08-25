# Stop one specific log file when your application has multiple log targets.

log_on("logs/session-a.log")
send("show version")
log_off("logs/session-a.log")
