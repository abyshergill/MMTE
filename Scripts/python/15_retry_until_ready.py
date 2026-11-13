# Keep checking a service until it is active, with a limit on the number of tries.

SERVICE = "ssh"
TRIES = 10

for attempt in range(1, TRIES + 1):
    send(f"systemctl is-active {SERVICE}")
    if wait("\nactive", timeout=5):
        alert(f"{SERVICE} is active after {attempt} check(s).")
        break
    sleep(3)
else:
    alert(f"{SERVICE} did not become active after {TRIES} checks.")
