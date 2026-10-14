send("./auto_load_user.sh")

# Wait for the Broadcom Command Monitor to initialize
wait("BCM.0>")

# Configure base VLANs and MAC addresses
send("vlan remove 1 pbm=xe")
wait("BCM.0>")
send("pvlan set xe2 4093")
wait("BCM.0>")
send("pvlan set xe3 4094")
wait("BCM.0>")
send("vlan create 4093 pbm=xe2,xe3 ubm=xe2,xe3")
wait("BCM.0>")
send("l2 add Port=xe3 MACaddress=00:00:00:00:00:02 Vlanid=4093")
wait("BCM.0>")
send("vlan create 4094 pbm=xe3,xe2 ubm=xe3,xe2")
wait("BCM.0>")
send("l2 add Port=xe2 MACaddress=00:00:00:00:00:02 Vlanid=4094")
wait("BCM.0>")
send("pvlan set xe3 4095")
wait("BCM.0>")

# Clear counters and execute sequence timing
send("clear c xe2,xe3")
wait("BCM.0>")
send("sleep 2")
wait("BCM.0>")
send("pvlan set xe3 4094")
wait("BCM.0>")
send("sleep 1")
wait("BCM.0>")

# Generate traffic
send("tx 64000 pbm=xe2 vlan=4093 length=295 SM=00:00:00:00:00:03 DM=00:00:00:00:00:02")
wait("Packet generate, length=295")
wait("BCM.0>")
send("sleep 2")
wait("BCM.0>")
send("pvlan set xe3 4095")
wait("BCM.0>")
send("sleep 3")
wait("BCM.0>")

# Verify counters
send("show c same changed xe2")
wait("BCM.0>")
send("show c same changed xe3")
wait("BCM.0>")

# Exit the BCM shell and close telnet gracefully
send("exit")
wait("left promiscuous mode")
wait("root@DS6000:")

# Send the standard telnet escape character (Ctrl+])
send("\x1D", newline=False)
wait("telnet>")
send("q")
wait("Connection closed.")