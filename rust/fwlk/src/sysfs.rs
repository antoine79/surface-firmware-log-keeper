//! Boot timings and firmware versions, copied from sysfs as text (SPEC 1 and 2). None when there is nothing.
use std::path::Path;

use crate::store::read;
use crate::{Error, Res};

fn value(path: &Path) -> Res<String> {
    Ok(String::from_utf8_lossy(&read(path)?).trim_matches([' ', '\t', '\r', '\n']).to_string())
}

/// Sorted names of the entries of a folder that pass `keep`; None if it cannot be listed. A name that is not UTF-8
/// cannot be written in the text: an error.
fn entries(root: &Path, keep: fn(&Path) -> bool) -> Option<Res<Vec<String>>> {
    let mut names = Vec::new();
    for e in std::fs::read_dir(root).ok()?.flatten().filter(|e| keep(&e.path())) {
        match e.file_name().into_string() {
            Ok(n) => names.push(n),
            Err(n) => return Some(Err(Error::value(format!("{n:?} in {} is not UTF-8", root.display())))),
        }
    }
    names.sort();
    Some(Ok(names))
}

/// One '<file name>=<value>' line per file, sorted by name.
pub fn fpdt(root: &Path) -> Res<Option<Vec<u8>>> {
    let Some(names) = entries(root, Path::is_file) else { return Ok(None) };
    let mut out = String::new();
    for n in names? {
        out += &format!("{n}={}\n", value(&root.join(&n))?);
    }
    Ok(Some(out.into_bytes()).filter(|b| !b.is_empty()))
}

/// One line per entry, sorted: '<entry> fw_class=... fw_version=... last_attempt_version=... last_attempt_status=...'.
pub fn esrt(root: &Path) -> Res<Option<Vec<u8>>> {
    let Some(entries) = entries(root, Path::is_dir) else { return Ok(None) };
    let mut out = String::new();
    for e in entries? {
        let values: Vec<String> = ["fw_class", "fw_version", "last_attempt_version", "last_attempt_status"].iter()
            .filter_map(|k| value(&root.join(&e).join(k)).ok().map(|v| format!("{k}={v}"))).collect();
        out += &format!("{e} {}\n", values.join(" "));
    }
    Ok(Some(out.into_bytes()).filter(|b| !b.is_empty()))
}
