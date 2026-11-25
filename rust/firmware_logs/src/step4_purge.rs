//! Step 4 of a keeper run: remove the staging folders and delete the firmware's error records whose content is in
//! a log written and checked.
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::os::unix::ffi::OsStrExt;
use std::path::Path;
use std::process::{Command, Stdio};

use crate::output::Output;
use crate::safe_files::{keep_copy, list};
use crate::settings::Config;
use crate::step1_collect::{is_record_var, seen_record, SeenRecord, Staged};
use crate::step2_plan::{Plan, Save};

/// Remove each staging folder whose memory log, timings and versions are all in a run folder written and checked.
pub fn purge_staging(cfg: &Config, staged: &[Staged], plan: &Plan, saved: &BTreeMap<&str, &Save>, out: &mut Output) {
    for s in staged {
        let path = s.path.display();
        if s.is_empty() {
            // a file left under its temporary name by a keeper run that died is never read
            if list(&s.path).is_ok_and(|names| names.iter().all(|n| n.as_bytes().ends_with(b".tmp"))) {
                remove(&s.path, out);
            } else {
                out.error("memory", format!("{path} kept: nothing readable in it"));
            }
            continue;
        }
        let Some(save) = saved.get(s.boot.as_str()) else { continue }; // its merge failed, and said so
        if !plan.held.contains(&s.path) {
            out.error("memory", format!("{path} kept: its run holds other lines, timings or versions"));
            continue;
        }
        // a dump with bytes the decoder could not read is the only copy of those bytes
        if let Some(d) = s.dump.as_ref().filter(|d| d.undecoded > 0) {
            let name = format!("advlog-{}.bin", d.hash);
            if let Err(e) = keep_copy(&cfg.merged_root.join(&save.name), &name, &d.raw) {
                out.error("memory", format!("{path} kept: {e}"));
                continue;
            }
            let (n, folder) = (d.undecoded, &save.name);
            out.error("memory", format!("{path}: {n} bytes not decoded, dump kept as {folder}/{name}"));
        }
        remove(&s.path, out);
    }
}

/// A symbolic link in it is removed, never followed.
fn remove(path: &Path, out: &mut Output) {
    if let Err(e) = fs::remove_dir_all(path) {
        out.error("purge", format!("{}: {e}", path.display()));
    }
}

/// Delete each error record from the firmware whose archive copy holds its bytes and whose row is in a log written
/// and checked.
pub fn purge_records(cfg: &Config, records: &[SeenRecord], checked: &BTreeSet<&str>, out: &mut Output) {
    for SeenRecord { var, record } in records {
        let hash = record.hash.as_str();
        if !checked.contains(hash) {
            let _ = seen_record(cfg, "kept", var, hash, "not-verified");
            out.error("errrec", format!("{var} kept: its row is not checked in a log"));
            continue;
        }
        if let Err(e) = delete_variable(&cfg.efivars, var) {
            let _ = seen_record(cfg, "kept", var, hash, "delete-failed");
            out.error("errrec", format!("{var} kept: {e}"));
            continue;
        }
        let seen = seen_record(cfg, "deleted", var, hash, "-");
        out.say(format!("deleted error record {var}"));
        if let Err(e) = seen {
            out.error("errrec", format!("{var}: seen.tsv: {e}"));
        }
    }
}

/// Delete one error-record variable: remove its immutable attribute with the chattr program, then the file. The
/// name is checked here, so that no caller can ever have another file deleted.
fn delete_variable(efivars: &Path, name: &str) -> std::io::Result<()> {
    if !is_record_var(name) {
        return Err(crate::bad(format!("{name} is not an error-record variable")));
    }
    let path = efivars.join(name);
    // a failure of chattr itself is ignored: the deletion says whether it mattered
    let _ = Command::new("chattr").arg("-i").arg(&path).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null())
        .status();
    fs::remove_file(&path).map_err(crate::safe_files::at(&path))
}
