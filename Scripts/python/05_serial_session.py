# Serial connection example.
# Use the port name shown by your operating system.

connect_serial(
    port="COM6",
    baudrate=115200,
    bytesize=8,
    parity="N",
    stopbits=1.0,
)

wait(1.0)
send("\r")
send("show version")
