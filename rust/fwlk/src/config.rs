//! The settings of SPEC 6, and the keeper's output of SPEC 5.
use std::fmt::Display;
use std::io::{self, Write};
use std::path::{Component, Path, PathBuf};

use crate::time::Time;
use crate::{bad, is_hex, squeeze, SPACE};

pub struct Config {
    pub memory_root: PathBuf,
    pub bootefi_root: PathBuf,
    pub merged_root: PathBuf,
    pub efivars: PathBuf,
    pub uefilogs: Option<PathBuf>,
    pub devmem: PathBuf,
    pub fpdt: PathBuf,
    pub esrt: PathBuf,
    pub boot_id: String,
    /// the UTC time of this keeper run
    pub now: Time,
    /// FWLK_FAULT=drop-row, for tests only
    pub drop_row: bool,
}

/// A setting: unset or empty is None; a value must be UTF-8 text.
fn var(name: &str) -> io::Result<Option<String>> {
    match std::env::var_os(name) {
        Some(v) if !v.is_empty() => v.into_string().map(Some).map_err(|_| bad(format!("{name} is not UTF-8 text"))),
        _ => Ok(None),
    }
}

fn path(name: &str, default: &str) -> io::Result<PathBuf> {
    Ok(var(name)?.unwrap_or_else(|| default.into()).into())
}

impl Config {
    pub fn from_env() -> io::Result<Config> {
        let uefilogs = match var("FWLK_UEFILOGS")? {
            Some(p) => Some(p.into()),
            None => ["/boot/efi/UefiLogs", "/efi/UefiLogs", "/boot/UefiLogs"].into_iter().map(PathBuf::from)
                .find(|d| d.is_dir()),
        };
        let raw = match var("FWLK_BOOT_ID")? {
            Some(id) => id,
            None => std::fs::read_to_string("/proc/sys/kernel/random/boot_id")?,
        };
        // it names folders that later keeper runs must find again
        let boot_id = raw.trim_matches(SPACE).replace('-', "").to_ascii_lowercase();
        if !is_hex(&boot_id, 32) {
            return Err(bad(format!("boot id {:?} is not 32 hex digits", raw.trim_matches(SPACE))));
        }
        let now = match var("FWLK_NOW")? {
            Some(t) => Time::parse_utc(&t).ok_or_else(|| bad(format!("FWLK_NOW {t:?} is not YYYY-MM-DDTHH:MM:SSZ")))?,
            None => Time::now(),
        };
        let cfg = Config {
            memory_root: path("FWLK_MEMORY_ROOT", "/var/log/firmware_memory")?,
            bootefi_root: path("FWLK_BOOTEFI_ROOT", "/var/log/firmware_bootefilogs")?,
            merged_root: path("FWLK_MERGED_ROOT", "/var/log/firmware")?,
            efivars: path("FWLK_EFIVARS", "/sys/firmware/efi/efivars")?,
            uefilogs,
            devmem: path("FWLK_DEVMEM", "/dev/mem")?,
            fpdt: path("FWLK_SYSFS_FPDT", "/sys/firmware/acpi/fpdt/boot")?,
            esrt: path("FWLK_SYSFS_ESRT", "/sys/firmware/efi/esrt/entries")?,
            boot_id,
            now,
            drop_row: var("FWLK_FAULT")?.as_deref() == Some("drop-row"),
        };
        cfg.check_roots()?;
        Ok(cfg)
    }

    /// Three separate roots, none touching the firmware's folders: staging folders are removed once merged, which
    /// must never reach the archive, and nothing is ever written on the EFI partition or in efivars.
    fn check_roots(&self) -> io::Result<()> {
        let roots = [&self.memory_root, &self.bootefi_root, &self.merged_root];
        for (i, a) in roots.iter().enumerate() {
            for b in roots[i + 1..].iter().copied().chain(&self.uefilogs).chain([&self.efivars]) {
                let (x, y) = (resolved(a), resolved(b));
                if x.starts_with(&y) || y.starts_with(&x) {
                    return Err(bad(format!("{} and {} are the same folder, or one is inside the other", a.display(),
                                           b.display())));
                }
            }
        }
        Ok(())
    }
}

/// Absolute, with `..` resolved as text: symbolic links are not followed (SPEC 6).
fn resolved(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for c in std::path::absolute(path).unwrap_or_else(|_| path.into()).components() {
        if c == Component::ParentDir {
            out.pop();
        } else {
            out.push(c);
        }
    }
    out
}

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
