//! Built-in script templates shown in Help > Sample Scripts & Templates.

pub const TMPL_SSH_LOGIN: &str = r#"; Tera Term macro: SSH auto-login
; Replace the CHANGE_ME values before use.
connect '192.168.1.10 /ssh /user=CHANGE_ME /passwd=CHANGE_ME'
wait '$'
sendln 'uname -a'
pause 1
sendln 'exit'
end
"#;

pub const TMPL_PY_AUTO: &str = r#"# Python terminal automation
print("Starting automation on", terminal_name)
send("uname -a")
wait("$", timeout=15)
sleep(1)
send("uptime")
wait("$", timeout=15)
print("Done")
"#;

pub const TMPL_SERIAL_AT: &str = r#"# Serial AT command tester: query a modem
for command in ["AT", "ATI", "AT+CSQ", "AT+CGMI"]:
    send(command)
    if not wait("OK", timeout=5):
        print("No OK for", command)
    sleep(0.5)
"#;

pub const TMPL_TTL_BATCH: &str = r#"; Tera Term macro: batch of commands with pauses
sendln 'df -h'
pause 2
sendln 'free -m'
mpause 1500
sendln 'ip addr'
pause 1
end
"#;

pub const SAMPLES: &[(&str, &str, &str, &str)] = &[
    ("SSH Auto-Login Macro (.ttl)", "Standard Tera Term TTL macro to auto-enter credentials.", TMPL_SSH_LOGIN, ".ttl"),
    ("Python Terminal Automation (.py)", "Send commands, pause, and log output sequentially.", TMPL_PY_AUTO, ".py"),
    ("Serial AT Command Tester (.py)", "Automated routine to query hardware modems.", TMPL_SERIAL_AT, ".py"),
    ("Batch Command Delay (.ttl)", "Pass multiple lines with timed execution pauses.", TMPL_TTL_BATCH, ".ttl"),
];
