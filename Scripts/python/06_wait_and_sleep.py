# wait() accepts either a prompt string or a number of seconds.

send("show version")
if not wait("#", timeout=30.0):
    print("Prompt was not detected")
    raise RuntimeError("Device did not return the expected prompt")

# Numeric wait sleeps while still checking the Stop/Pause checkpoint.
wait(2.0)
sleep(0.5)
print("Timing sequence completed")
