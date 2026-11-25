//! What the firmware left: this boot's memory log, timings and versions go to staging; the file logs and the
//! error records get their raw copies in the archive. One failing source never stops the others.
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use crate::firmware_memory_log::{self, MemLine};
use crate::{output::Output, settings::Config};
use crate::firmware_error_record::{self, Record};
use crate::safe_files::{append, at, keep_copy, list, make_dirs, put, read_or_none, read_untouched};
use crate::firmware_log_file::{self, FileLog};
use crate::merge_logs::begins;
use crate::{is_hex, latin1, sha16, squeeze, firmware_timings_and_versions};

const LOCATOR: &str = "AdvLoggerLocator-4d60cfb5-f481-4a98-9c81-bff86460c43e";
const RECORD_GUID: &str = "-414e6bdd-e47b-47cc-b244-bb61020cf516";

/// A memory dump of a staging folder.
pub struct Dump {
    pub raw: Vec<u8>,
    pub hash: String,
    pub lines: Vec<MemLine>,
    /// bytes the decoder could not turn into lines
    pub undecoded: usize,
}

/// A staging folder: what was read in one Linux boot. An empty file counts as none.
pub struct Staged {
    pub path: PathBuf,
    pub boot: String,
    pub dump: Option<Dump>,
    pub fpdt: Option<Vec<u8>>,
    pub esrt: Option<Vec<u8>>,
}

impl Staged {
    /// Nothing readable in it.
    pub fn is_empty(&self) -> bool {
        self.dump.is_none() && self.fpdt.is_none() && self.esrt.is_none()
    }
}

/// A file log of the EFI partition, archived.
pub struct SeenFile {
    pub hash: String,
    pub slot: String,
    pub log: FileLog,
}

/// An error-record variable whose archive copy holds its bytes.
pub struct SeenRecord {
    pub var: String,
    pub record: Record,
}

/// `HwErrRec<4 hex digits>-414e6bdd-e47b-47cc-b244-bb61020cf516`, the name of an error-record variable.
pub fn is_record_var(name: &str) -> bool {
    let digits = name.strip_prefix("HwErrRec").and_then(|s| s.strip_suffix(RECORD_GUID));
    digits.is_some_and(|d| d.len() == 4 && d.bytes().all(|b| b.is_ascii_hexdigit()))
}

/// The staging folders, `<UTC time>_<boot id>`, in name order, with their boot ids.
fn staging_folders(root: &Path) -> io::Result<Vec<(PathBuf, String)>> {
    let names = match list(root) {
        Err(e) if e.kind() == io::ErrorKind::NotFound => Vec::new(),
        r => r?,
    };
    Ok(names.into_iter().filter_map(|n| n.into_string().ok()).filter_map(|name| {
        let (prefix, boot) = name.rsplit_once('_')?;
        let ok = crate::digits(prefix.as_bytes(), b"dddd-dd-ddTddddddZ").is_some() && is_hex(boot, 32);
        let path = root.join(&name);
        (ok && path.is_dir()).then(|| (path, boot.to_string()))
    }).collect())
}

/// Stage this boot's sources, then read every staging folder; one that cannot be read stays as it is.
pub fn staging(cfg: &Config, out: &mut Output) -> Vec<Staged> {
    let mut folders = match staging_folders(&cfg.memory_root) {
        Ok(folders) => folders,
        Err(e) => {
            out.error("memory", e);
            return Vec::new();
        }
    };
    let this_boot = folders.iter().find(|(_, boot)| *boot == cfg.boot_id).map(|(path, _)| path.clone());
    let folder = this_boot.clone().unwrap_or_else(|| cfg.memory_root.join(format!("{}_{}", cfg.now.utc_stamp(), cfg.boot_id)));
    stage(cfg, &folder, out);
    if this_boot.is_none() && folder.is_dir() {
        folders.push((folder, cfg.boot_id.clone()));
        folders.sort();
    }
    folders.into_iter().filter_map(|(path, boot)| {
        read_staged(path.clone(), boot).map_err(|e| out.error("memory", format!("{} kept: {e}", path.display()))).ok()
    }).collect()
}

/// Copy this boot's memory log, timings and versions to its staging folder, where they stay until merged and
/// checked.
fn stage(cfg: &Config, folder: &Path, out: &mut Output) {
    let keep = |out: &mut Output, step: &str, name: &str, data: io::Result<Option<Vec<u8>>>| {
        let written = data.and_then(|data| match data {
            Some(data) => make_dirs(folder).and_then(|_| put(&folder.join(name), &data, false)).map(drop),
            None => Ok(()),
        });
        if let Err(e) = written {
            out.error(step, e);
        }
    };
    let memory = memory(cfg, out).and_then(|d| match d {
        Some(d) if replaces(folder, &d, out)? => Ok(Some(d)),
        _ => Ok(None),
    });
    keep(out, "memory", "advlog.bin", memory);
    for (step, name, root, data) in [("fpdt", "fpdt-boot.txt", &cfg.fpdt, firmware_timings_and_versions::fpdt(&cfg.fpdt)),
                                     ("esrt", "esrt.txt", &cfg.esrt, firmware_timings_and_versions::esrt(&cfg.esrt))] {
        if let Ok(None) = data {
            out.skipped(step, format!("nothing in {}", root.display()));
        }
        keep(out, step, name, data);
    }
}

/// This boot's memory log, found through the locator variable, read from /dev/mem or else from the log
/// variables; None when the source is missing.
fn memory(cfg: &Config, out: &mut Output) -> io::Result<Option<Vec<u8>>> {
    let Ok(locator) = fs::read(cfg.efivars.join(LOCATOR)) else {
        out.skipped("memory", "no AdvLoggerLocator variable");
        return Ok(None);
    };
    // no /dev/mem, or refused (lockdown): try the firmware's log variables; missing both ways, the source is missing
    match firmware_memory_log::read_live(&cfg.devmem, &locator) {
        Err(e) if e.kind() != io::ErrorKind::InvalidData => match firmware_memory_log::read_variables(&cfg.efivars, &locator) {
            Err(v) if v.kind() != io::ErrorKind::InvalidData => {
                out.skipped("memory", format!("{e}; {v}"));
                Ok(None)
            }
            r => r.map(Some),
        },
        r => r.map(Some),
    }
}

/// May this read replace the dump an earlier keeper run of this boot left in staging? Its lines are in no log yet,
/// so only if the read begins with every one of them (3.1).
fn replaces(folder: &Path, data: &[u8], out: &mut Output) -> io::Result<bool> {
    let Some(old) = read_or_none(&folder.join("advlog.bin"))? else { return Ok(true) };
    let (new, staged) = (firmware_memory_log::decode(data)?.0, firmware_memory_log::decode(&old)?.0);
    if begins(&new, &staged) {
        return Ok(true);
    }
    if !begins(&staged, &new) {
        // a read that is the beginning of the staged dump adds nothing; any other is an error
        out.error("memory", format!("{} kept: the new read lacks lines of the staged dump", folder.display()));
    }
    Ok(false)
}

fn read_staged(path: PathBuf, boot: String) -> io::Result<Staged> {
    let read = |name: &str| read_or_none(&path.join(name)).map(|d| d.filter(|d| !d.is_empty()));
    let dump = read_or_none(&path.join("advlog.bin"))?.map(|raw| -> io::Result<Dump> {
        let (lines, undecoded) = firmware_memory_log::decode(&raw).map_err(at(&path.join("advlog.bin")))?;
        Ok(Dump { hash: sha16(&raw), raw, lines, undecoded })
    }).transpose()?;
    Ok(Staged { fpdt: read("fpdt-boot.txt")?, esrt: read("esrt.txt")?, path, boot, dump })
}

/// Archive every file log of the EFI partition, which is only ever read. One per content, in slot order.
pub fn file_logs(cfg: &Config, out: &mut Output) -> Vec<SeenFile> {
    let Some(dir) = cfg.uefilogs.as_ref().filter(|d| d.is_dir()) else {
        out.skipped("bootefi", "no UefiLogs folder");
        return Vec::new();
    };
    // each run of NUL, space, tab, CR and LF is one space, none at the ends
    let index = match read_untouched(&dir.join("UEFI_Index.txt")) {
        Ok(raw) => squeeze(&latin1(&raw.unwrap_or_default()), &['\0', ' ', '\t', '\r', '\n']),
        Err(e) => {
            out.error("bootefi", e); // the file logs are archived all the same
            String::new()
        }
    };
    let index = if index.is_empty() { "-" } else { &index };
    let mut seen: Vec<SeenFile> = Vec::new();
    for slot in (1..=9).map(|n| format!("UEFI_Log{n}.txt")) {
        match file_log(cfg, dir, &slot, index, out) {
            Ok(Some(f)) if !seen.iter().any(|s| s.hash == f.hash) => seen.push(f),
            Ok(_) => {}
            Err(e) => out.error(&slot, e),
        }
    }
    seen
}

fn file_log(cfg: &Config, dir: &Path, slot: &str, index: &str, out: &mut Output) -> io::Result<Option<SeenFile>> {
    let Some(data) = read_untouched(&dir.join(slot))? else { return Ok(None) };
    let content = firmware_log_file::content(&data);
    if content.is_empty() {
        out.skipped(slot, "no content");
        return Ok(None);
    }
    let hash = sha16(content);
    if keep_copy(&cfg.bootefi_root, &format!("{hash}.txt"), content)? {
        out.say(format!("archived file log {hash} from {slot}"));
    }
    let log = FileLog::parse(content);
    let end = log.end.as_ref();
    append(&cfg.bootefi_root.join("seen.tsv"), &[&cfg.now.utc_stamp(), &cfg.boot_id, slot, &hash, &content.len().to_string(),
           &end.map_or(0, |e| e.trailing).to_string(), if end.is_some() { "yes" } else { "no" },
           end.and_then(|e| e.written.as_deref()).unwrap_or("-"), index])?;
    Ok(Some(SeenFile { hash, slot: slot.to_string(), log }))
}

/// One line more in hwerrrec/seen.tsv.
pub fn seen_record(cfg: &Config, action: &str, var: &str, hash: &str, reason: &str) -> io::Result<()> {
    append(&cfg.merged_root.join("hwerrrec").join("seen.tsv"), &[&cfg.now.utc_stamp(), &cfg.boot_id, action, var, hash, reason])
}

/// Archive every error record raw, by content. Returns those whose archive copy holds their bytes.
pub fn records(cfg: &Config, out: &mut Output) -> Vec<SeenRecord> {
    let names = match list(&cfg.efivars) {
        Ok(names) => names,
        Err(e) => {
            out.skipped("errrec", e);
            return Vec::new();
        }
    };
    names.into_iter().filter_map(|n| n.into_string().ok()).filter(|n| is_record_var(n)).filter_map(|var| {
        match record(cfg, &var, out) {
            Ok(r) => r.map(|record| SeenRecord { var, record }),
            Err(e) => {
                out.error("errrec", format!("{var}: {e}"));
                None
            }
        }
    }).collect()
}

fn record(cfg: &Config, var: &str, out: &mut Output) -> io::Result<Option<Record>> {
    let path = cfg.efivars.join(var);
    let raw = fs::read(&path).map_err(at(&path))?;
    let hash = sha16(&raw);
    match keep_copy(&cfg.merged_root.join("hwerrrec"), &format!("{hash}.bin"), &raw) {
        Ok(true) => {
            let seen = seen_record(cfg, "archived", var, &hash, "-");
            out.say(format!("archived error record {hash} from {var}"));
            if let Err(e) = seen {
                out.error("errrec", format!("{var}: seen.tsv: {e}"));
            }
        }
        Ok(false) => {}
        Err(e) if e.kind() == io::ErrorKind::AlreadyExists => {
            let _ = seen_record(cfg, "kept", var, &hash, "archive-mismatch"); // the error line says it all
            out.error("errrec", format!("{var} kept: {e}"));
            return Ok(None);
        }
        Err(e) => return Err(e),
    }
    Ok(Some(firmware_error_record::decode(&raw, &hash)))
}
