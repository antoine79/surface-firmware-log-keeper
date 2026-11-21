//! The keeper's one command (SPEC 7): a keeper run.
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::os::unix::ffi::OsStrExt;
use std::path::Path;
use std::process::{Command, Stdio};

use crate::config::{Config, Output};
use crate::disk::{keep_copy, list, make_dirs, put};
use crate::gather::{self, is_record_var, seen_record, SeenRecord, Staged};
use crate::archive::Archive;
use crate::plan::{self, Plan, Save};

pub fn main() -> i32 {
    let mut out = Output::default();
    if std::env::args_os().len() > 1 {
        out.say("usage: firmware-log-keeper");
        return 2;
    }
    match Config::from_env() {
        Ok(cfg) => keep(&cfg, &mut out),
        Err(e) => out.error("keeper", e), // a wrong setting: nothing read or written
    }
    i32::from(out.failed)
}

fn keep(cfg: &Config, out: &mut Output) {
    let staged = gather::staging(cfg, out);
    let archive = Archive::open(cfg, out).map_err(|e| out.error("keeper", e)).ok();
    let files = gather::file_logs(cfg, out);
    let records = gather::records(cfg, out);
    let Some(archive) = archive else { return }; // nothing is merged into an archive that cannot be read
    let plan = plan::plan(&archive, &staged, &files, &records, out);
    let (saved, checked) = apply(cfg, &plan, out);
    purge_staging(cfg, &staged, &plan, &saved, out);
    purge_records(cfg, &records, &checked, out);
}

/// Write the plan. Returns the run folders written and checked, and the records whose row is in a log written and
/// checked.
fn apply<'p>(cfg: &Config, plan: &'p Plan, out: &mut Output) -> (BTreeMap<&'p str, &'p Save>, BTreeSet<&'p str>) {
    let mut checked = BTreeSet::new();
    if let Some(u) = &plan.unattached {
        let dir = cfg.merged_root.join("unattached");
        let written = u.log.as_ref().map_or(Ok(false), |log| {
            make_dirs(&dir).and_then(|_| put(&dir.join("firmware.log"), log, cfg.drop_row))
        });
        match written {
            Ok(_) => checked.extend(u.records.iter().map(String::as_str)),
            Err(e) => out.error("errrec", format!("unattached/firmware.log: {e}")),
        }
    }
    let mut saved = BTreeMap::new();
    for (key, save) in &plan.runs {
        let written = match save {
            Ok(s) => write_folder(cfg, s).map(|changed| (s, changed)),
            Err(e) => Err(crate::bad(e)),
        };
        match written {
            Ok((s, changed)) => {
                if changed {
                    out.say(format!("merged run {key} into {} (memory {} lines, bootefi {} lines, errrec {})", s.name,
                                    s.mem_lines, s.file_lines, s.records.len()));
                }
                checked.extend(s.records.iter().map(String::as_str));
                saved.insert(key.as_str(), s);
            }
            Err(e) => out.error("merge", format!("{key}: {e}")),
        }
    }
    (saved, checked)
}

/// Write the files of a run folder that changed. True if anything was made or written.
fn write_folder(cfg: &Config, save: &Save) -> std::io::Result<bool> {
    let dir = cfg.merged_root.join(&save.name);
    make_dirs(&dir)?;
    let mut changed = save.new;
    for (name, data) in &save.files {
        changed |= put(&dir.join(name), data, cfg.drop_row && *name == "firmware.log")?;
    }
    Ok(changed)
}

/// Remove each staging folder whose memory log, timings and versions are all in a run folder written and checked.
fn purge_staging(cfg: &Config, staged: &[Staged], plan: &Plan, saved: &BTreeMap<&str, &Save>, out: &mut Output) {
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
fn purge_records(cfg: &Config, records: &[SeenRecord], checked: &BTreeSet<&str>, out: &mut Output) {
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
    fs::remove_file(&path).map_err(crate::disk::at(&path))
}
