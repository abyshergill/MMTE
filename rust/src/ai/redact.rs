//! Mask credentials in text before it leaves the machine (port of core/redact.py).

use fancy_regex::Regex;
use once_cell::sync::Lazy;

pub const MASK: &str = "[REDACTED]";

static PATTERNS: Lazy<Vec<(Regex, String)>> = Lazy::new(|| {
    let r = |p: &str, rep: &str| (Regex::new(p).unwrap_or_else(|e| panic!("{p}: {e}")), rep.to_string());
    vec![
        r(r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", MASK),
        r(r"(?i)\b(authorization\s*[:=]\s*(?:bearer|basic|token)\s+)[A-Za-z0-9._~+/=-]{8,}", "${1}[REDACTED]"),
        r(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{16,}", "${1}[REDACTED]"),
        r(r"\bsk-[A-Za-z0-9_-]{16,}\b", MASK),
        r(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b", MASK),
        r(r"\bAKIA[0-9A-Z]{16}\b", MASK),
        r(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b", MASK),
        r(r"\bAIza[0-9A-Za-z_-]{30,}\b", MASK),
        r(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b", MASK),
        r(r#"(?i)\b((?:pass(?:word|wd|phrase)?|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key)\s*[:=]\s*)(?!\[REDACTED\])("[^"\r\n]*"|'[^'\r\n]*'|[^\s,;"']+)"#, "${1}[REDACTED]"),
    ]
});

pub fn redact_secrets(text: &str) -> String {
    let mut out = text.to_string();
    for (re, rep) in PATTERNS.iter() {
        out = re.replace_all(&out, rep.as_str()).into_owned();
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn masks() {
        assert!(!redact_secrets("key sk-abcdefghijklmnopqrstu end").contains("sk-abc"));
        assert_eq!(redact_secrets("password: hunter2"), "password: [REDACTED]");
        assert!(redact_secrets("Authorization: Bearer abcdefgh12345678").contains("[REDACTED]"));
        assert_eq!(redact_secrets("plain text"), "plain text");
    }
}
