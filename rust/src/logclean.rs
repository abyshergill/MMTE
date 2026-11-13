//! Turns raw terminal output into readable log text (port of core/log_clean.py).
//!
//! Colour codes, cursor moves, erase-line commands, window titles and hyperlinks are removed by
//! replaying the stream onto a single line buffer. State is kept between calls so an escape
//! sequence split across two reads is still handled.

#[derive(Clone, Copy, PartialEq, Debug)]
enum State {
    Text,
    Esc,
    Csi,
    Osc,
    OscEsc,
    Skip1,
}

pub struct LogCleaner {
    line: Vec<char>,
    col: usize,
    state: State,
    params: String,
}

impl Default for LogCleaner {
    fn default() -> Self {
        Self::new()
    }
}

impl LogCleaner {
    pub fn new() -> Self {
        Self { line: Vec::new(), col: 0, state: State::Text, params: String::new() }
    }

    /// Returns the completed lines (each ending in `\n`) found in `data`.
    pub fn feed(&mut self, data: &str) -> String {
        let mut out = String::new();
        for ch in data.chars() {
            match self.state {
                State::Text => match ch {
                    '\x1b' => self.state = State::Esc,
                    '\n' => {
                        let s: String = self.line.iter().collect();
                        out.push_str(s.trim_end());
                        out.push('\n');
                        self.line.clear();
                        self.col = 0;
                    }
                    '\r' => self.col = 0,
                    '\x08' | '\x7f' => self.col = self.col.saturating_sub(1),
                    '\t' => self.put(ch),
                    c if c >= ' ' => self.put(c),
                    _ => {}
                },
                State::Esc => match ch {
                    '[' => {
                        self.state = State::Csi;
                        self.params.clear();
                    }
                    ']' => self.state = State::Osc,
                    '(' | ')' | '*' | '+' | '#' | '%' => self.state = State::Skip1,
                    _ => self.state = State::Text,
                },
                State::Skip1 => self.state = State::Text,
                State::Csi => {
                    if ('\x40'..='\x7e').contains(&ch) {
                        let params = std::mem::take(&mut self.params);
                        self.csi(ch, &params);
                        self.state = State::Text;
                    } else {
                        self.params.push(ch);
                    }
                }
                State::Osc => match ch {
                    '\x07' => self.state = State::Text,
                    '\x1b' => self.state = State::OscEsc,
                    _ => {}
                },
                State::OscEsc => self.state = State::Text,
            }
        }
        out
    }

    /// Returns whatever is left on the unfinished line.
    pub fn flush(&mut self) -> String {
        let s: String = self.line.iter().collect();
        self.line.clear();
        self.col = 0;
        let t = s.trim_end();
        if t.is_empty() {
            String::new()
        } else {
            format!("{t}\n")
        }
    }

    fn put(&mut self, ch: char) {
        if self.col < self.line.len() {
            self.line[self.col] = ch;
        } else {
            while self.line.len() < self.col {
                self.line.push(' ');
            }
            self.line.push(ch);
        }
        self.col += 1;
    }

    fn csi(&mut self, fin: char, params: &str) {
        let first = params.trim_start_matches(['?', '>']).split(';').next().unwrap_or("");
        let n: Option<usize> = first.parse().ok();
        match fin {
            'D' => self.col = self.col.saturating_sub(n.unwrap_or(1)),
            'C' => self.col += n.unwrap_or(1),
            'G' => self.col = n.unwrap_or(1).saturating_sub(1),
            'K' => match n.unwrap_or(0) {
                0 => self.line.truncate(self.col),
                2 => {
                    self.line.clear();
                    self.col = 0;
                }
                1 => {
                    for i in 0..self.col.min(self.line.len()) {
                        self.line[i] = ' ';
                    }
                }
                _ => {}
            },
            'J' => {
                if n.unwrap_or(0) == 0 {
                    self.line.truncate(self.col);
                }
            }
            _ => {}
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strips_garbage() {
        let raw = "\x1b[m\n\x1b[1;32muart:~$ \x1b[m\x1b[8D\x1b[J[00:00:00.000,000] \x1b[0m<inf> i3c: init\x1b[0m\r\n\
                   \x1b7\x1b[r\x1b[999;999H\x1b[6n\x1b8root@a:~# \r\n\
                   \x1b[0;32m*\x1b[0m x - PSU\r\nLoaded: (\x1b]8;;file://a/x\x07/usr/x\x1b]8;;\x07 ); \x1b[0;32menabled\x1b[0m\r\n";
        let out = LogCleaner::new().feed(raw);
        assert_eq!(out, "\n[00:00:00.000,000] <inf> i3c: init\nroot@a:~#\n* x - PSU\nLoaded: (/usr/x ); enabled\n");
    }

    #[test]
    fn split_sequence() {
        let mut c = LogCleaner::new();
        let a = c.feed("ab\x1b[3");
        let b = c.feed("1mcd\n");
        assert_eq!(format!("{a}{b}"), "abcd\n");
    }
}
