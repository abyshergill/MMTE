//! GUI-free parts of the text editor: file decoding/encoding, language detection, a small
//! syntax lexer, and find / replace helpers.

use fancy_regex::Regex;

// ---------------------------------------------------------------------------------------
// Decoding / encoding
// ---------------------------------------------------------------------------------------

#[derive(Clone, Debug, PartialEq)]
pub struct Decoded {
    pub text: String, // LF line endings
    pub encoding: String,
    pub bom: bool,
    pub eol: &'static str,
}

pub fn decode_bytes(raw: &[u8]) -> Result<Decoded, String> {
    let head = &raw[..raw.len().min(8192)];
    let utf16_bom = raw.starts_with(&[0xFF, 0xFE]) || raw.starts_with(&[0xFE, 0xFF]);
    if head.contains(&0) && !utf16_bom {
        return Err("This looks like a binary file and cannot be edited as text.".into());
    }
    let (text, encoding, bom) = if raw.starts_with(&[0xEF, 0xBB, 0xBF]) {
        (String::from_utf8_lossy(&raw[3..]).into_owned(), "utf-8", true)
    } else if utf16_bom {
        let le = raw.starts_with(&[0xFF, 0xFE]);
        let units: Vec<u16> = raw[2..].chunks(2).filter(|c| c.len() == 2).map(|c| if le { u16::from_le_bytes([c[0], c[1]]) } else { u16::from_be_bytes([c[0], c[1]]) }).collect();
        (String::from_utf16_lossy(&units), "utf-16", true)
    } else {
        match String::from_utf8(raw.to_vec()) {
            Ok(t) => (t, "utf-8", false),
            Err(_) => (raw.iter().map(|&b| b as char).collect(), "latin-1", false),
        }
    };
    let crlf = text.matches("\r\n").count();
    let lf = text.matches('\n').count() - crlf;
    let eol = if crlf > lf { "\r\n" } else { "\n" };
    let text = text.replace("\r\n", "\n").replace('\r', "\n");
    Ok(Decoded { text, encoding: encoding.into(), bom, eol })
}

pub fn encode_text(text: &str, encoding: &str, bom: bool, eol: &str) -> Vec<u8> {
    let text = if eol != "\n" { text.replace('\n', eol) } else { text.to_string() };
    match encoding {
        "utf-16" => {
            let mut out = vec![0xFF, 0xFE];
            for u in text.encode_utf16() {
                out.extend_from_slice(&u.to_le_bytes());
            }
            out
        }
        "latin-1" => text.chars().map(|c| if (c as u32) < 256 { c as u8 } else { b'?' }).collect(),
        _ => {
            let mut out = Vec::new();
            if bom {
                out.extend_from_slice(&[0xEF, 0xBB, 0xBF]);
            }
            out.extend_from_slice(text.as_bytes());
            out
        }
    }
}

// ---------------------------------------------------------------------------------------
// Languages
// ---------------------------------------------------------------------------------------

pub const LANGUAGES: &[(&str, &str)] = &[
    ("Plain Text", ""),
    ("Python", "python"),
    ("Bash / Shell", "bash"),
    ("Tera Term Macro", "teraterm"),
    ("C", "c"),
    ("C++", "cpp"),
    ("Rust", "rust"),
    ("Go", "go"),
    ("Java", "java"),
    ("JavaScript", "javascript"),
    ("TypeScript", "typescript"),
    ("JSON", "json"),
    ("YAML", "yaml"),
    ("TOML", "toml"),
    ("INI / Config", "ini"),
    ("XML / HTML", "html"),
    ("Markdown", "markdown"),
    ("SQL", "sql"),
    ("Log", "log"),
];

pub fn language_label(alias: &str) -> &'static str {
    LANGUAGES.iter().find(|(_, a)| *a == alias).map(|(l, _)| *l).unwrap_or("Plain Text")
}

pub fn comment_prefix(alias: &str) -> &'static str {
    match alias {
        "python" | "bash" | "yaml" | "toml" | "perl" | "docker" | "make" | "powershell" | "log" | "" => "#",
        "ini" | "teraterm" => ";",
        "lua" | "sql" => "--",
        "batch" => "REM ",
        _ => "//",
    }
}

pub fn detect_language(name: &str, text: &str) -> &'static str {
    let base = name.rsplit(['/', '\\']).next().unwrap_or("").to_lowercase();
    let ext = base.rsplit_once('.').map(|(_, e)| e.to_string()).unwrap_or_default();
    match base.as_str() {
        ".bashrc" | ".profile" | ".zshrc" | ".env" => return "bash",
        "dockerfile" | "containerfile" => return "docker",
        "makefile" | "gnumakefile" => return "make",
        _ => {}
    }
    let by_ext = match ext.as_str() {
        "py" | "pyw" => "python",
        "sh" | "bash" | "zsh" | "ksh" => "bash",
        "ttl" => "teraterm",
        "c" | "h" => "c",
        "cpp" | "cc" | "cxx" | "hpp" | "hh" => "cpp",
        "rs" => "rust",
        "go" => "go",
        "java" => "java",
        "js" | "mjs" | "jsx" => "javascript",
        "ts" | "tsx" => "typescript",
        "json" => "json",
        "yaml" | "yml" => "yaml",
        "toml" => "toml",
        "ini" | "conf" | "cfg" | "service" => "ini",
        "xml" | "html" | "htm" | "svg" => "html",
        "md" | "markdown" => "markdown",
        "sql" => "sql",
        "log" => "log",
        _ => "",
    };
    if !by_ext.is_empty() {
        return by_ext;
    }
    let first = text.lines().next().unwrap_or("");
    if first.starts_with("#!") {
        if first.contains("python") {
            return "python";
        }
        if ["bash", "/sh", "zsh", "ksh"].iter().any(|s| first.contains(s)) {
            return "bash";
        }
    }
    ""
}

// ---------------------------------------------------------------------------------------
// Lexer
// ---------------------------------------------------------------------------------------

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Class {
    Plain,
    Comment,
    Str,
    Number,
    Keyword,
    Type,
    Tag,
    Heading,
    Error,
    Warn,
}

struct Spec {
    line_comments: &'static [&'static str],
    block: Option<(&'static str, &'static str)>,
    quotes: &'static [char],
    triple: bool,
    keywords: &'static [&'static str],
    types: &'static [&'static str],
    ci: bool,
}

const PY_KW: &[&str] = &["False", "None", "True", "and", "as", "assert", "async", "await", "break", "class", "continue", "def", "del", "elif", "else", "except", "finally", "for", "from", "global", "if", "import", "in", "is", "lambda", "nonlocal", "not", "or", "pass", "raise", "return", "try", "while", "with", "yield", "match", "case"];
const PY_TY: &[&str] = &["int", "str", "float", "bool", "list", "dict", "set", "tuple", "bytes", "print", "len", "range", "open", "self", "send", "wait", "sleep", "alert", "input", "log_on", "log_off", "connect_ssh", "connect_serial", "connect_telnet", "terminal_new", "terminal_close", "terminal_rename", "terminal_list", "terminal_select", "terminal_name", "run_script"];
const C_KW: &[&str] = &["auto", "break", "case", "const", "continue", "default", "do", "else", "enum", "extern", "for", "goto", "if", "inline", "register", "return", "sizeof", "static", "struct", "switch", "typedef", "union", "volatile", "while", "class", "namespace", "template", "typename", "public", "private", "protected", "virtual", "new", "delete", "using", "nullptr", "true", "false", "this", "try", "catch", "throw", "constexpr", "override"];
const C_TY: &[&str] = &["int", "char", "short", "long", "float", "double", "void", "unsigned", "signed", "bool", "size_t", "uint8_t", "uint16_t", "uint32_t", "uint64_t", "int8_t", "int16_t", "int32_t", "int64_t", "string", "vector"];
const RS_KW: &[&str] = &["as", "async", "await", "break", "const", "continue", "crate", "dyn", "else", "enum", "extern", "false", "fn", "for", "if", "impl", "in", "let", "loop", "match", "mod", "move", "mut", "pub", "ref", "return", "self", "Self", "static", "struct", "super", "trait", "true", "type", "unsafe", "use", "where", "while"];
const RS_TY: &[&str] = &["i8", "i16", "i32", "i64", "i128", "isize", "u8", "u16", "u32", "u64", "u128", "usize", "f32", "f64", "bool", "char", "str", "String", "Vec", "Option", "Result", "Some", "None", "Ok", "Err", "Box", "Arc"];
const GO_KW: &[&str] = &["break", "case", "chan", "const", "continue", "default", "defer", "else", "fallthrough", "for", "func", "go", "goto", "if", "import", "interface", "map", "package", "range", "return", "select", "struct", "switch", "type", "var", "true", "false", "nil"];
const JAVA_KW: &[&str] = &["abstract", "assert", "break", "case", "catch", "class", "const", "continue", "default", "do", "else", "enum", "extends", "final", "finally", "for", "if", "implements", "import", "instanceof", "interface", "native", "new", "package", "private", "protected", "public", "return", "static", "super", "switch", "synchronized", "this", "throw", "throws", "try", "void", "volatile", "while", "true", "false", "null"];
const JS_KW: &[&str] = &["async", "await", "break", "case", "catch", "class", "const", "continue", "debugger", "default", "delete", "do", "else", "export", "extends", "finally", "for", "function", "if", "import", "in", "instanceof", "let", "new", "of", "return", "super", "switch", "this", "throw", "try", "typeof", "var", "void", "while", "with", "yield", "true", "false", "null", "undefined", "interface", "type", "enum"];
const SH_KW: &[&str] = &["if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done", "case", "esac", "in", "function", "return", "exit", "break", "continue", "local", "export", "readonly", "source", "alias", "unset", "shift", "trap"];
const SH_TY: &[&str] = &["echo", "cd", "ls", "cat", "grep", "sed", "awk", "sudo", "systemctl", "printf", "test", "read", "eval", "exec", "kill", "chmod", "chown", "mkdir", "rm", "cp", "mv"];
const TTL_KW: &[&str] = &["connect", "wait", "send", "sendln", "pause", "mpause", "end", "if", "then", "endif", "else", "for", "next", "while", "endwhile", "goto", "call", "return", "messagebox", "inputbox", "logopen", "logclose", "disconnect", "closett", "waitln", "sendfile", "timeout"];
const SQL_KW: &[&str] = &["select", "from", "where", "insert", "into", "values", "update", "set", "delete", "create", "table", "drop", "alter", "join", "left", "right", "inner", "outer", "on", "group", "by", "order", "having", "limit", "and", "or", "not", "null", "as", "distinct", "union", "index", "primary", "key", "foreign", "references"];
const JSON_KW: &[&str] = &["true", "false", "null"];
const YAML_KW: &[&str] = &["true", "false", "null", "yes", "no", "on", "off"];
const TOML_KW: &[&str] = &["true", "false"];

fn spec(alias: &str) -> Option<Spec> {
    let s = |lc: &'static [&'static str], block, quotes: &'static [char], triple, kw: &'static [&'static str], ty: &'static [&'static str], ci| Spec { line_comments: lc, block, quotes, triple, keywords: kw, types: ty, ci };
    let cblock = Some(("/*", "*/"));
    Some(match alias {
        "python" => s(&["#"], None, &['"', '\''], true, PY_KW, PY_TY, false),
        "bash" | "docker" | "make" => s(&["#"], None, &['"', '\''], false, SH_KW, SH_TY, false),
        "teraterm" => s(&[";"], None, &['"', '\''], false, TTL_KW, &[], true),
        "c" | "cpp" => s(&["//"], cblock, &['"', '\''], false, C_KW, C_TY, false),
        "rust" => s(&["//"], cblock, &['"'], false, RS_KW, RS_TY, false),
        "go" => s(&["//"], cblock, &['"', '\'', '`'], false, GO_KW, C_TY, false),
        "java" | "csharp" => s(&["//"], cblock, &['"', '\''], false, JAVA_KW, C_TY, false),
        "javascript" | "typescript" => s(&["//"], cblock, &['"', '\'', '`'], false, JS_KW, &[], false),
        "json" => s(&[], None, &['"'], false, JSON_KW, &[], false),
        "yaml" => s(&["#"], None, &['"', '\''], false, YAML_KW, &[], false),
        "toml" => s(&["#"], None, &['"', '\''], false, TOML_KW, &[], false),
        "ini" => s(&[";", "#"], None, &['"'], false, &[], &[], false),
        "sql" => s(&["--"], cblock, &['\''], false, SQL_KW, &[], true),
        "html" => s(&[], Some(("<!--", "-->")), &['"', '\''], false, &[], &[], false),
        _ => return None,
    })
}

fn is_ident(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

/// Returns non-overlapping `(start_char, end_char, class)` spans for the whole text.
pub fn lex(text: &str, alias: &str) -> Vec<(usize, usize, Class)> {
    let chars: Vec<char> = text.chars().collect();
    let n = chars.len();
    let mut out: Vec<(usize, usize, Class)> = Vec::new();
    if alias == "log" {
        return lex_log(&chars);
    }
    if alias == "markdown" {
        return lex_markdown(&chars);
    }
    let Some(sp) = spec(alias) else { return out };
    let starts_with = |i: usize, pat: &str| -> bool {
        let p: Vec<char> = pat.chars().collect();
        i + p.len() <= n && chars[i..i + p.len()].iter().zip(p.iter()).all(|(a, b)| a == b)
    };
    let mut i = 0;
    let mut line_start = true;
    while i < n {
        let c = chars[i];
        if c == '\n' {
            line_start = true;
            i += 1;
            continue;
        }
        let at_line_start = line_start && !c.is_whitespace();
        if !c.is_whitespace() {
            line_start = false;
        }
        // INI sections / YAML keys / HTML tags
        if alias == "ini" && at_line_start && c == '[' {
            let end = chars[i..].iter().position(|&x| x == '\n').map(|p| i + p).unwrap_or(n);
            out.push((i, end, Class::Heading));
            i = end;
            continue;
        }
        if alias == "html" && c == '<' && i + 1 < n && !starts_with(i, "<!--") {
            let mut j = i + 1;
            while j < n && chars[j] != '>' {
                j += 1;
            }
            out.push((i, (j + 1).min(n), Class::Tag));
            i = (j + 1).min(n);
            continue;
        }
        // comments
        if let Some((open, close)) = sp.block {
            if starts_with(i, open) {
                let mut j = i + open.chars().count();
                while j < n && !starts_with(j, close) {
                    j += 1;
                }
                let end = (j + close.chars().count()).min(n);
                out.push((i, end, Class::Comment));
                i = end;
                continue;
            }
        }
        let is_lc = sp.line_comments.iter().any(|lc| {
            if !starts_with(i, lc) {
                return false;
            }
            // `;` in TTL and `#` in INI only count at the start of a statement / line for ini
            true
        });
        if is_lc && !(alias == "bash" && c == '#' && i > 0 && chars[i - 1] == '$') {
            let end = chars[i..].iter().position(|&x| x == '\n').map(|p| i + p).unwrap_or(n);
            out.push((i, end, Class::Comment));
            i = end;
            continue;
        }
        // strings
        if sp.quotes.contains(&c) {
            if sp.triple && starts_with(i, &c.to_string().repeat(3)) {
                let q3 = c.to_string().repeat(3);
                let mut j = i + 3;
                while j < n && !starts_with(j, &q3) {
                    j += 1;
                }
                let end = (j + 3).min(n);
                out.push((i, end, Class::Str));
                i = end;
                continue;
            }
            let mut j = i + 1;
            while j < n && chars[j] != c && (chars[j] != '\n' || c == '`') {
                if chars[j] == '\\' && j + 1 < n {
                    j += 1;
                }
                j += 1;
            }
            let end = if j < n && chars[j] == c { j + 1 } else { j.min(n) };
            // YAML / JSON keys followed by ':' stay strings (fine)
            out.push((i, end, Class::Str));
            i = end;
            continue;
        }
        // numbers
        if c.is_ascii_digit() && (i == 0 || !is_ident(chars[i - 1])) {
            let mut j = i;
            while j < n && (chars[j].is_ascii_alphanumeric() || chars[j] == '.' || chars[j] == '_') {
                j += 1;
            }
            out.push((i, j, Class::Number));
            i = j;
            continue;
        }
        // identifiers
        if c.is_alphabetic() || c == '_' {
            let mut j = i;
            while j < n && is_ident(chars[j]) {
                j += 1;
            }
            let word: String = chars[i..j].iter().collect();
            let w = if sp.ci { word.to_lowercase() } else { word.clone() };
            if sp.keywords.iter().any(|k| if sp.ci { k.to_lowercase() == w } else { *k == w }) {
                out.push((i, j, Class::Keyword));
            } else if sp.types.contains(&w.as_str()) {
                out.push((i, j, Class::Type));
            } else if alias == "yaml" && at_line_start {
                // key: value
                if chars[j..].iter().take_while(|&&x| x != '\n').any(|&x| x == ':') && chars[j] == ':' {
                    out.push((i, j, Class::Type));
                }
            }
            i = j;
            continue;
        }
        i += 1;
    }
    out
}

fn lex_log(chars: &[char]) -> Vec<(usize, usize, Class)> {
    let text: String = chars.iter().collect();
    let mut out = Vec::new();
    let re_err = Regex::new(r"(?i)\b(error|fail(?:ed|ure)?|fatal|critical|panic|exception|denied)\b").unwrap();
    let re_warn = Regex::new(r"(?i)\b(warn(?:ing)?|deprecated)\b").unwrap();
    let re_time = Regex::new(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b|\b\d{2}:\d{2}:\d{2}(?:\.\d+)?\b").unwrap();
    // map byte -> char index
    let mut map = vec![0usize; text.len() + 1];
    for (ci, (b, c)) in text.char_indices().enumerate() {
        for k in 0..c.len_utf8() {
            map[b + k] = ci;
        }
    }
    map[text.len()] = chars.len();
    let mut taken = vec![false; chars.len()];
    for (re, cl) in [(&re_err, Class::Error), (&re_warn, Class::Warn), (&re_time, Class::Number)] {
        for m in re.find_iter(&text).flatten() {
            let (s, e) = (map[m.start()], map[m.end()]);
            if taken[s..e].iter().any(|t| *t) {
                continue;
            }
            for t in &mut taken[s..e] {
                *t = true;
            }
            out.push((s, e, cl));
        }
    }
    out.sort_by_key(|x| x.0);
    out
}

fn lex_markdown(chars: &[char]) -> Vec<(usize, usize, Class)> {
    let mut out = Vec::new();
    let mut i = 0;
    let n = chars.len();
    let mut in_code = false;
    while i < n {
        let end = chars[i..].iter().position(|&x| x == '\n').map(|p| i + p).unwrap_or(n);
        let line: String = chars[i..end].iter().collect();
        if line.trim_start().starts_with("```") {
            in_code = !in_code;
            out.push((i, end, Class::Comment));
        } else if in_code {
            out.push((i, end, Class::Str));
        } else if line.starts_with('#') {
            out.push((i, end, Class::Heading));
        } else if line.trim_start().starts_with(['-', '*', '>']) {
            let off = line.chars().count() - line.trim_start().chars().count();
            out.push((i + off, i + off + 1, Class::Keyword));
        }
        i = end + 1;
    }
    out
}

// ---------------------------------------------------------------------------------------
// Find / replace
// ---------------------------------------------------------------------------------------

pub fn build_pattern(query: &str, case_sensitive: bool, use_regex: bool) -> Result<Option<Regex>, String> {
    if query.is_empty() {
        return Ok(None);
    }
    let body = if use_regex { query.to_string() } else { fancy_regex::escape(query).into_owned() };
    let pat = if case_sensitive { body } else { format!("(?i){body}") };
    Regex::new(&pat).map(Some).map_err(|e| e.to_string())
}

/// All matches as char ranges (start, end).
pub fn find_all(text: &str, re: &Regex) -> Vec<(usize, usize)> {
    let mut map = vec![0usize; text.len() + 1];
    let mut count = 0;
    for (ci, (b, c)) in text.char_indices().enumerate() {
        for k in 0..c.len_utf8() {
            map[b + k] = ci;
        }
        count = ci + 1;
    }
    map[text.len()] = count;
    re.find_iter(text).flatten().filter(|m| m.end() > m.start()).map(|m| (map[m.start()], map[m.end()])).collect()
}

pub fn char_to_byte(text: &str, ci: usize) -> usize {
    text.char_indices().nth(ci).map(|(b, _)| b).unwrap_or(text.len())
}

pub fn replace_all(text: &str, re: &Regex, with: &str, use_regex: bool) -> (String, usize) {
    let mut out = String::new();
    let mut last = 0;
    let mut count = 0;
    for caps in re.captures_iter(text).flatten() {
        let Some(m) = caps.get(0) else { continue };
        if m.end() == m.start() {
            continue;
        }
        out.push_str(&text[last..m.start()]);
        if use_regex {
            let w: Vec<char> = with.chars().collect();
            let mut i = 0;
            while i < w.len() {
                if w[i] == '$' && i + 1 < w.len() {
                    if w[i + 1] == '$' {
                        out.push('$');
                        i += 2;
                        continue;
                    }
                    let (digits, skip) = if w[i + 1] == '{' {
                        let close = w[i + 2..].iter().position(|&c| c == '}');
                        match close {
                            Some(p) => (w[i + 2..i + 2 + p].iter().collect::<String>(), p + 3),
                            None => (String::new(), 0),
                        }
                    } else {
                        let d: String = w[i + 1..].iter().take_while(|c| c.is_ascii_digit()).collect();
                        let l = d.len();
                        (d, l + 1)
                    };
                    if let Ok(n) = digits.parse::<usize>() {
                        if let Some(g) = caps.get(n) {
                            out.push_str(g.as_str());
                        }
                        i += skip;
                        continue;
                    }
                }
                out.push(w[i]);
                i += 1;
            }
        } else {
            out.push_str(with);
        }
        last = m.end();
        count += 1;
    }
    out.push_str(&text[last..]);
    (out, count)
}

/// Line / column (1-based) of a char offset.
pub fn line_col(text: &str, ci: usize) -> (usize, usize) {
    let mut line = 1;
    let mut col = 1;
    for (i, c) in text.chars().enumerate() {
        if i >= ci {
            break;
        }
        if c == '\n' {
            line += 1;
            col = 1;
        } else {
            col += 1;
        }
    }
    (line, col)
}

/// Char offset of the start of 1-based `line`.
pub fn line_start_offset(text: &str, line: usize) -> usize {
    if line <= 1 {
        return 0;
    }
    let mut seen = 1;
    for (i, c) in text.chars().enumerate() {
        if c == '\n' {
            seen += 1;
            if seen == line {
                return i + 1;
            }
        }
    }
    text.chars().count()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decode_roundtrip() {
        let d = decode_bytes(b"a\r\nb\r\n").unwrap();
        assert_eq!(d.text, "a\nb\n");
        assert_eq!(d.eol, "\r\n");
        assert_eq!(encode_text(&d.text, &d.encoding, d.bom, d.eol), b"a\r\nb\r\n");
        let bom = decode_bytes(&[0xEF, 0xBB, 0xBF, b'x']).unwrap();
        assert!(bom.bom);
        assert_eq!(encode_text(&bom.text, "utf-8", true, "\n"), vec![0xEF, 0xBB, 0xBF, b'x']);
        assert!(decode_bytes(&[0, 1, 2]).is_err());
        assert_eq!(decode_bytes(&[0xE9]).unwrap().encoding, "latin-1");
    }

    #[test]
    fn languages() {
        assert_eq!(detect_language("/x/a.py", ""), "python");
        assert_eq!(detect_language("a.ttl", ""), "teraterm");
        assert_eq!(detect_language("noext", "#!/bin/bash\n"), "bash");
        assert_eq!(detect_language("a.txt", ""), "");
        assert_eq!(comment_prefix("ini"), ";");
    }

    #[test]
    fn lexes_python() {
        let spans = lex("def f(x): # hi\n    return \"a\" + 12\n", "python");
        let has = |c: Class| spans.iter().any(|s| s.2 == c);
        assert!(has(Class::Keyword) && has(Class::Comment) && has(Class::Str) && has(Class::Number));
    }

    #[test]
    fn find_replace() {
        let re = build_pattern("a.b", true, false).unwrap().unwrap();
        assert_eq!(find_all("a.b axb", &re), vec![(0, 3)]);
        let re = build_pattern("a(.)b", false, true).unwrap().unwrap();
        let (t, n) = replace_all("AxB ayb", &re, "[$1]", true);
        assert_eq!((t.as_str(), n), ("[x] [y]", 2));
        assert!(build_pattern("(", true, true).is_err());
        assert_eq!(line_col("ab\ncd", 4), (2, 2));
        assert_eq!(line_start_offset("ab\ncd\nef", 3), 6);
    }
}
