# A practical diagnostic sequence.
# Adapt prompts and commands to your device.

log_on("logs/device_diagnostic.log")

print("Diagnostic started on", terminal_name)
send("show version")
if not wait("#", timeout=30):
    log_off()
    raise RuntimeError("No device prompt after show version")

send("show interfaces")
wait(2.0)
send("show system uptime")
wait(2.0)

alert("Review the diagnostic output before continuing.")
send("show running-config")
wait(3.0)

log_off()
print("Diagnostic completed")
