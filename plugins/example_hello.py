"""Example plugin. Copy this file, rename it and change it to make your own."""

PLUGIN_NAME = "Hello Example"
PLUGIN_VERSION = "1.0"
PLUGIN_DESCRIPTION = "Adds Plugins > Hello Example > Show terminal info, and a hello() script function."


def register(api):
    def show_info():
        s = api.current_session()
        if s is None:
            api.status("No terminal open")
            return
        api.status(f"{s.name}: {s.connected_label}")

    api.add_menu_action("Show terminal info", show_info)
    api.add_script_function("hello", lambda name="world": f"hello {name}")
