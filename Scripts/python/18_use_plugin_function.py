# Uses hello(), which is added by the example plugin in plugins/example_hello.py.
# Plugins > Reload Plugins must have run once (it does at start-up).

send("echo " + hello("plugin"))
