//! Regex-based syntax highlighting for plain terminal output (port of core/terminal_highlight.py).
//! Only default-coloured cells are touched, so real ANSI colours always win.

use fancy_regex::Regex;
use once_cell::sync::Lazy;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Tag {
    PromptUser,
    PromptPath,
    Error,
    Warn,
    Ok,
    Ip,
    Mac,
    Url,
    Path,
    Iface,
    Time,
    Number,
    Meta,
    Str,
}

/// (r, g, b, bold, underline)
pub fn format(tag: Tag) -> ([u8; 3], bool, bool) {
    match tag {
        Tag::PromptUser => ([0x7e, 0xe7, 0x87], true, false),
        Tag::PromptPath => ([0x79, 0xc0, 0xff], true, false),
        Tag::Error => ([0xff, 0x7b, 0x72], true, false),
        Tag::Warn => ([0xe3, 0xb3, 0x41], true, false),
        Tag::Ok => ([0x56, 0xd3, 0x64], false, false),
        Tag::Ip => ([0x39, 0xc5, 0xcf], false, false),
        Tag::Mac => ([0xd2, 0xa8, 0xff], false, false),
        Tag::Url => ([0x58, 0xa6, 0xff], false, true),
        Tag::Path => ([0xa5, 0xd6, 0xff], false, false),
        Tag::Iface => ([0xd2, 0xa8, 0xff], false, false),
        Tag::Time => ([0x8b, 0x94, 0x9e], false, false),
        Tag::Number => ([0xff, 0xa6, 0x57], false, false),
        Tag::Meta => ([0x7c, 0x8c, 0xff], false, false),
        Tag::Str => ([0xa5, 0xd6, 0xa7], false, false),
    }
}

enum Rule {
    One(Tag),
    Groups(Vec<Tag>),
}

static RULES: Lazy<Vec<(Regex, Rule)>> = Lazy::new(|| {
    let r = |p: &str, rule: Rule| (Regex::new(p).unwrap_or_else(|e| panic!("bad pattern {p}: {e}")), rule);
    vec![
        r(r"^\s*\[(?:Active|Connection failed|Running|Disconnected|New terminal|Upload|Uploading|Python|TTL|Playlist|Macro|Editor|Logging|No active connection|Send error)[^\]]*\]", Rule::One(Tag::Meta)),
        r(r"^(\[?[\w.\-]+@[\w.\-]+)((?::|\s)[^\s$#>\]]*)?(\]?\s?[$#>%])(?=\s|$)", Rule::Groups(vec![Tag::PromptUser, Tag::PromptPath, Tag::PromptUser])),
        r(r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s'\x22<>]+", Rule::One(Tag::Url)),
        r(r"\b(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?(?::\d{1,5})?\b", Rule::One(Tag::Ip)),
        r(r"\b(?:\d{4}-\d{2}-\d{2}[T ])?\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\b", Rule::One(Tag::Time)),
        r(r"\b\d{4}-\d{2}-\d{2}\b", Rule::One(Tag::Time)),
        r(r"(?i)\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b", Rule::One(Tag::Mac)),
        r(r"(?i)(?<![\w:])(?=[0-9a-f:]*::|(?:[0-9a-f]{1,4}:){7})[0-9a-f]{0,4}(?::[0-9a-f]{0,4}){2,7}(?:/\d{1,3})?(?![\w:])", Rule::One(Tag::Ip)),
        r(r"(?i)\b(?:errors?|fail(?:ed|ure|s)?|fatal|denied|refused|unreachable|timed?\s?out|critical|panic|invalid|not\s+found|no\s+such\s+file(?:\s+or\s+directory)?|abort(?:ed)?|exception|traceback|segmentation\s+fault|cannot|unable|down|disconnected|offline|err)\b", Rule::One(Tag::Error)),
        r(r"(?i)\b(?:warn(?:ing|ings)?|deprecated|retry(?:ing)?|degraded|notice)\b", Rule::One(Tag::Warn)),
        r(r"(?i)\b(?:ok|success(?:ful(?:ly)?)?|succeeded|passed|up|connected|active|enabled|done|complete(?:d)?|established|running|online|ready|listen(?:ing)?)\b", Rule::One(Tag::Ok)),
        r(r"\b(?:eth|ens|enp|eno|wlan|wlp|lo|br|bond|docker|veth|tun|tap|vlan|Vlan|Gi|Fa|Te|Twe|Hu|GigabitEthernet|FastEthernet|TenGigabitEthernet|Ethernet|Port-channel|Loopback|Tunnel|mgmt)\d(?:[\w/.:\-]*[\w/])?", Rule::One(Tag::Iface)),
        r(r#"(?:"[^"\n]{0,200}"|'[^'\n]{0,200}')"#, Rule::One(Tag::Str)),
        r(r"(?<![\w.\-])(?:~|\.{1,2})?/[\w.\-+@/]*[\w\-+@/]", Rule::One(Tag::Path)),
        r(r"(?<![\w.])[-+]?\d+(?:\.\d+)?(?:%|[KMGTP]i?B?|ms|us|ns|s|bps|[KMG]bps)?(?![\w.])", Rule::One(Tag::Number)),
    ]
});

/// Non-overlapping `(start_col, end_col, tag)` spans (in characters) for one rendered row.
pub fn highlight_spans(line: &str) -> Vec<(usize, usize, Tag)> {
    if line.trim().is_empty() {
        return Vec::new();
    }
    // byte offset -> char column
    let mut byte_to_col = vec![0usize; line.len() + 1];
    let mut ncols = 0;
    for (col, (b, c)) in line.char_indices().enumerate() {
        for k in 0..c.len_utf8() {
            byte_to_col[b + k] = col;
        }
        ncols = col + 1;
    }
    byte_to_col[line.len()] = ncols;
    let mut taken = vec![false; ncols];
    let mut spans = Vec::new();
    let claim = |s: usize, e: usize, tag: Tag, taken: &mut Vec<bool>, spans: &mut Vec<(usize, usize, Tag)>| {
        let (s, e) = (byte_to_col[s], byte_to_col[e]);
        if s >= e || taken[s..e].iter().any(|t| *t) {
            return;
        }
        for t in &mut taken[s..e] {
            *t = true;
        }
        spans.push((s, e, tag));
    };
    for (re, rule) in RULES.iter() {
        for caps in re.captures_iter(line).flatten() {
            match rule {
                Rule::One(tag) => {
                    if let Some(m) = caps.get(0) {
                        claim(m.start(), m.end(), *tag, &mut taken, &mut spans);
                    }
                }
                Rule::Groups(tags) => {
                    for (i, tag) in tags.iter().enumerate() {
                        if let Some(m) = caps.get(i + 1) {
                            if !m.as_str().is_empty() {
                                claim(m.start(), m.end(), *tag, &mut taken, &mut spans);
                            }
                        }
                    }
                }
            }
        }
    }
    spans.sort_by_key(|s| (s.0, s.1));
    spans
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn finds_tokens() {
        let spans = highlight_spans("root@a:~# ping 10.0.0.1 failed https://x.io/a");
        let tags: Vec<Tag> = spans.iter().map(|s| s.2).collect();
        assert!(tags.contains(&Tag::PromptUser));
        assert!(tags.contains(&Tag::Ip));
        assert!(tags.contains(&Tag::Error));
        assert!(tags.contains(&Tag::Url));
    }

    #[test]
    fn empty_line() {
        assert!(highlight_spans("   ").is_empty());
    }

    #[test]
    fn all_patterns_compile() {
        assert!(RULES.len() >= 15);
    }
}
