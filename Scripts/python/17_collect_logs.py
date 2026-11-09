# Log a set of commands to a file, one section per command.

log_on("logs/collect_logs.log")
for title, command in [("Kernel", "dmesg | tail -n 30"), ("Disk", "df -h"), ("Memory", "free -m")]:
    send(f"echo '=== {title} ==='")
    send(command)
    wait(1.5)
log_off()
