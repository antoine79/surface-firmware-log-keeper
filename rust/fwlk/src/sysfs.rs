//! Boot timings and firmware versions, copied from sysfs as text (SPEC 1 and 2). None when there is nothing.
use std::fs;
use std::io;
use std::path::Path;

use crate::disk::{at, list};
use crate::SPACE;

fn value(path: &Path) -> io::Result<String> {
    let data = fs::read(path).map_err(at(path))?;
    Ok(String::from_utf8_lossy(&data).trim_matches(SPACE).to_string())
}

/// The sorted names of the entries of a folder that pass `keep`; None if the folder is missing or has none.
fn entries(root: &Path, keep: fn(&Path) -> bool) -> io::Result<Option<Vec<String>>> {
    let names = match list(root) {
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
        r => r?,
    };
    let names: Vec<String> = names.into_iter().filter(|n| keep(&root.join(n)))
        .map(|n| n.to_string_lossy().into_owned()).collect();
    Ok(Some(names).filter(|n| !n.is_empty()))
}

/// One `<file name>=<value>` line per file, sorted by name.
pub fn fpdt(root: &Path) -> io::Result<Option<Vec<u8>>> {
    let Some(names) = entries(root, Path::is_file)? else { return Ok(None) };
    let lines: io::Result<Vec<String>> = names.iter().map(|n| Ok(format!("{n}={}\n", value(&root.join(n))?))).collect();
    Ok(Some(lines?.concat().into_bytes()))
}

/// One line per entry, sorted: `<entry> fw_class=… fw_version=… last_attempt_version=… last_attempt_status=…`.
pub fn esrt(root: &Path) -> io::Result<Option<Vec<u8>>> {
    let Some(entries) = entries(root, Path::is_dir)? else { return Ok(None) };
    let line = |e: &String| -> io::Result<String> {
        let values = ["fw_class", "fw_version", "last_attempt_version", "last_attempt_status"].into_iter()
            .map(|k| Ok(format!(" {k}={}", value(&root.join(e).join(k))?))).collect::<io::Result<String>>()?;
        Ok(format!("{e}{values}\n"))
    };
    Ok(Some(entries.iter().map(line).collect::<io::Result<String>>()?.into_bytes()))
}
