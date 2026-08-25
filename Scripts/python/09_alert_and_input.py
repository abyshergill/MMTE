# alert() displays a Yes/No confirmation dialog.
# It returns True when the user chooses Yes and stops the script otherwise.

alert("The next command will change device configuration.")

hostname = input("Enter the target hostname:")
if not hostname:
    print("No hostname entered; stopping")
    raise RuntimeError("User cancelled hostname input")

print("Continuing with target:", hostname)
send("show running-config")
