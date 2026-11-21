//! The two purges of SPEC section 4. The callers only come here after the archive copy is written and checked.
use std::fs;
use std::path::Path;
use std::process::{Command, Stdio};

use crate::{Error, Res};

const RECORD_GUID: &str = "-414e6bdd-e47b-47cc-b244-bb61020cf516";

/// `HwErrRec[0-9A-Fa-f]{4}-414e6bdd-e47b-47cc-b244-bb61020cf516`, the name of an error-record variable.
pub fn is_record_var(name: &str) -> bool {
    name.strip_prefix("HwErrRec").and_then(|s| s.get(..4).zip(s.get(4..)))
        .is_some_and(|(n, rest)| n.bytes().all(|b| b.is_ascii_hexdigit()) && rest == RECORD_GUID)
}

/// Delete one error-record variable: remove the immutable attribute with the chattr program, then the file.
///
/// The path is built here and the name checked here, so that no caller can ever have another file deleted.
pub fn delete_variable(efivars: &Path, name: &str) -> Res<()> {
    if !is_record_var(name) {
        return Err(Error::value(format!("{name} is not an error-record variable")));
    }
    let path = efivars.join(name);
    // a failure of chattr itself is ignored: the deletion says whether it mattered
    let _ = Command::new("chattr").arg("-i").arg(&path).stdin(Stdio::null()).stdout(Stdio::null())
        .stderr(Stdio::null()).status();
    fs::remove_file(&path).map_err(|e| Error::io(&e, &path))
}

/// Remove a staging folder or a run folder already renamed for its removal (SPEC 4): its files first, then its
/// subfolders in the order they are listed, then the folder itself. The first failure stops it. A symbolic link
/// is removed, never followed.
pub fn remove_folder(path: &Path) -> Res<()> {
    let io = |e: std::io::Error| Error::io(&e, path);
    if fs::symlink_metadata(path).map_err(io)?.file_type().is_symlink() {
        return Err(Error::os(format!("{} is a symbolic link", path.display())));
    }
    let mut folders = Vec::new();
    for e in fs::read_dir(path).map_err(io)? {
        let e = e.map_err(io)?;
        if e.file_type().map_err(io)?.is_dir() {
            folders.push(e.path());
        } else {
            fs::remove_file(e.path()).map_err(|x| Error::io(&x, &e.path()))?;
        }
    }
    for f in folders {
        remove_folder(&f)?;
    }
    fs::remove_dir(path).map_err(io)
}
