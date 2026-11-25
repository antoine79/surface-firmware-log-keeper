//! Files: reads that tell a missing file from a failure, and writes that are flushed, read back and compared.
use std::ffi::OsString;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};

use crate::bad;

const O_NOATIME: i32 = 0o1000000;

/// Adds the path to an error, keeping its kind; the system's own words without their error number.
pub fn at(path: &Path) -> impl Fn(io::Error) -> io::Error + '_ {
    move |e| {
        let text = e.to_string();
        let text = e.raw_os_error().and_then(|n| text.strip_suffix(&format!(" (os error {n})"))).unwrap_or(&text);
        io::Error::new(e.kind(), format!("{}: {text}", path.display()))
    }
}

pub fn read_or_none(path: &Path) -> io::Result<Option<Vec<u8>>> {
    match fs::read(path) {
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
        r => r.map(Some).map_err(at(path)),
    }
}

/// Read a file of the EFI partition without updating its access time: that update is a write to the partition.
pub fn read_untouched(path: &Path) -> io::Result<Option<Vec<u8>>> {
    let file = match OpenOptions::new().read(true).custom_flags(O_NOATIME).open(path) {
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(None),
        // O_NOATIME is for the file's owner and for root only
        Err(e) if e.kind() == io::ErrorKind::PermissionDenied => File::open(path),
        r => r,
    };
    let mut data = Vec::new();
    file.and_then(|mut f| f.read_to_end(&mut data)).map_err(at(path))?;
    Ok(Some(data))
}

/// The names in a folder, sorted.
pub fn list(dir: &Path) -> io::Result<Vec<OsString>> {
    let mut names = fs::read_dir(dir).and_then(|d| d.map(|e| e.map(|e| e.file_name())).collect::<io::Result<Vec<_>>>())
        .map_err(at(dir))?;
    names.sort();
    Ok(names)
}

fn fsync_dir(dir: &Path) -> io::Result<()> {
    File::open(dir).and_then(|d| d.sync_all()).map_err(at(dir))
}

/// Create a folder and flush every folder entry made on the way: a purge may depend on it.
pub fn make_dirs(dir: &Path) -> io::Result<()> {
    let dir = std::path::absolute(dir)?;
    let missing: Vec<&Path> = dir.ancestors().take_while(|d| !d.is_dir()).collect();
    for d in missing.into_iter().rev() {
        match fs::create_dir(d) {
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists && d.is_dir() => {}
            r => r.map_err(at(d))?,
        }
        fsync_dir(d.parent().unwrap_or(Path::new("/")))?;
    }
    Ok(())
}

/// Write a file of the archive: `written` goes to a temporary file, which is flushed, read back and compared with
/// `data`, then renamed over the file. On any failure the file that was there stays as it was.
fn write(path: &Path, data: &[u8], written: &[u8]) -> io::Result<()> {
    let mut tmp = path.as_os_str().to_owned();
    tmp.push(".tmp");
    let tmp = PathBuf::from(tmp);
    let attempt = || {
        let mut f = File::create(&tmp)?;
        f.write_all(written)?;
        f.sync_all()?;
        if fs::read(&tmp)? != data {
            return Err(bad("what was read back differs from what was to be written"));
        }
        fs::rename(&tmp, path)
    };
    if let Err(e) = attempt() {
        let _ = fs::remove_file(&tmp);
        return Err(at(path)(e));
    }
    fsync_dir(path.parent().unwrap_or(Path::new(".")))
}

/// Write a file unless it already holds these bytes. True if it was written. `drop_row` is the test fault of SPEC
/// 6: the temporary file loses its last row, so what is read back differs.
pub fn put(path: &Path, data: &[u8], drop_row: bool) -> io::Result<bool> {
    if read_or_none(path)?.as_deref() == Some(data) {
        return Ok(false);
    }
    write(path, data, if drop_row { without_last_row(data) } else { data })?;
    Ok(true)
}

/// The rows of a log but its last one; the column line stays.
fn without_last_row(log: &[u8]) -> &[u8] {
    let body = log.strip_suffix(b"\n").unwrap_or(log);
    body.iter().rposition(|&b| b == b'\n').map_or(log, |i| &log[..=i])
}

/// Archive one raw copy, named after its content. True if it is new; an error of kind `AlreadyExists` if the
/// archive holds other bytes under that name.
pub fn keep_copy(dir: &Path, name: &str, data: &[u8]) -> io::Result<bool> {
    let path = dir.join(name);
    match read_or_none(&path)? {
        None => make_dirs(dir).and_then(|_| write(&path, data, data)).map(|_| true),
        Some(old) if old == data => Ok(false),
        Some(_) => Err(io::Error::new(io::ErrorKind::AlreadyExists, format!("{} holds other bytes", path.display()))),
    }
}

/// One line more in an append-only .tsv file.
pub fn append(path: &Path, fields: &[&str]) -> io::Result<()> {
    make_dirs(path.parent().unwrap_or(Path::new(".")))?;
    let mut f = OpenOptions::new().append(true).create(true).open(path).map_err(at(path))?;
    f.write_all((fields.join("\t") + "\n").as_bytes()).and_then(|_| f.sync_all()).map_err(at(path))
}
