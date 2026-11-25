//! The keeper's output of SPEC 5: one line per thing done, skipped or failed.
use std::fmt::Display;
use std::io::{self, Write};

use crate::{squeeze, SPACE};

/// Standard output only (the journal): what was done, what was skipped, what failed.
#[derive(Default)]
pub struct Output {
    pub failed: bool,
}

impl Output {
    pub fn say(&self, text: impl Display) {
        let _ = writeln!(io::stdout().lock(), "{}", squeeze(&text.to_string(), &SPACE)); // output nobody reads is dropped
    }

    pub fn skipped(&self, source: &str, reason: impl Display) {
        self.say(format!("skipped {source}: {reason}"));
    }

    pub fn error(&mut self, step: &str, what: impl Display) {
        self.failed = true;
        self.say(format!("error: {step}: {what}"));
    }
}
