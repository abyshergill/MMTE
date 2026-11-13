# Open a second terminal, run a command in each, then return to the first.

first = terminal_name
terminal_new("second")
terminal_select("second")
send("echo hello from the second terminal")
sleep(1)
terminal_select(str(first))
send("echo hello from the first terminal")
print(terminal_list())
