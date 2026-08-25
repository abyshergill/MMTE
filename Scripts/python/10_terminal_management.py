# Terminal management helpers.

print("Current terminal:", terminal_name)

new_name = terminal_new("Verification Terminal")
if new_name:
    print("Created:", new_name)
    send("echo verification terminal")

if terminal_select("Terminal 1"):
    print("Returned to Terminal 1")

terminal_rename("Verification Terminal", "Verification Results")
terminal_close("Verification Results")
