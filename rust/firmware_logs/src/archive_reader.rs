//! The archive as it is at the start of the keeper run (SPEC 2): its run folders and unattached/firmware.log,
//! and the raw copies, read on demand and never written here. The report reads the archive with the same functions.
use std::collections::BTreeMap;
use std::io;
use std::path::{Path, PathBuf};

use crate::{output::Output, settings::Config};
use crate::firmware_error_record::{self, Record};
use crate::safe_files::{at, list, read_or_none};
use crate::firmware_log_file::FileLog;
use crate::log_format::{parse_log, Listing, Source};
use crate::{bad, digits, is_hex, sha16};

pub const FILE_ONLY: &str = "fileonly-";

/// The run key of a run folder's name, `<YYYY-MM-DDTHHMMSS>_<boot id or fileonly-hash>`.
pub fn folder_key(name: &str) -> Option<&str> {
    let (label, key) = name.split_at_checked(17)?;
    let key = key.strip_prefix('_')?;
    let fits = digits(label.as_bytes(), b"dddd-dd-ddTdddddd").is_some()
        && (is_hex(key, 32) || key.strip_prefix(FILE_ONLY).is_some_and(|h| is_hex(h, 16)));
    fits.then_some(key)
}

/// A run folder of the archive.
pub struct Folder {
    pub name: String,
    pub listing: io::Result<Listing>,
    pub has_log: bool,
}

/// The archive as it is at the start of the keeper run, read on demand.
pub struct Archive<'c> {
    pub cfg: &'c Config,
    /// run key -> folder
    pub folders: BTreeMap<String, Folder>,
    /// the records of unattached/firmware.log by hash; None if it cannot be read
    pub unattached: Option<BTreeMap<String, Record>>,
}

/// The run folders of a merged root, by run key. A root that does not exist has none.
pub fn run_folders(root: &Path) -> io::Result<BTreeMap<String, Folder>> {
    let names = match list(root) {
        Err(e) if e.kind() == io::ErrorKind::NotFound => Vec::new(),
        r => r?,
    };
    let mut folders = BTreeMap::new();
    for name in names.into_iter().filter_map(|n| n.into_string().ok()) {
        let dir = root.join(&name);
        let Some(key) = folder_key(&name).filter(|_| dir.is_dir()) else { continue };
        let listing = read_or_none(&dir.join("sources.tsv")).and_then(|d| Listing::parse(&d.unwrap_or_default()));
        folders.insert(key.to_string(), Folder { has_log: dir.join("firmware.log").is_file(), name, listing });
    }
    Ok(folders)
}

/// The firmware.log of the run folder `name`, if it has one.
pub fn run_log(root: &Path, name: &str) -> io::Result<Option<Vec<u8>>> {
    read_or_none(&root.join(name).join("firmware.log"))
}

impl<'c> Archive<'c> {
    pub fn open(cfg: &'c Config, out: &mut Output) -> io::Result<Archive<'c>> {
        let folders = run_folders(&cfg.merged_root)?;
        let unattached = read_unattached(&cfg.merged_root)
            .map_err(|e| out.error("errrec", format!("unattached/firmware.log: {e}"))).ok();
        Ok(Archive { cfg, folders, unattached })
    }

    pub fn log(&self, name: &str) -> io::Result<Option<Vec<u8>>> {
        run_log(&self.cfg.merged_root, name)
    }

    /// An archived raw copy, checked against the hash it is named after.
    fn raw(&self, path: PathBuf, hash: &str) -> io::Result<Vec<u8>> {
        let data = std::fs::read(&path).map_err(at(&path))?;
        if sha16(&data) != hash {
            return Err(bad(format!("{} is not the content it is named after", path.display())));
        }
        Ok(data)
    }

    pub fn file(&self, hash: &str) -> io::Result<FileLog> {
        Ok(FileLog::parse(&self.raw(self.cfg.bootefi_root.join(format!("{hash}.txt")), hash)?))
    }

    pub fn record(&self, hash: &str) -> io::Result<Record> {
        Ok(firmware_error_record::decode(&self.raw(self.cfg.merged_root.join("hwerrrec").join(format!("{hash}.bin")), hash)?, hash))
    }

    /// The keys of the run folders whose sources.tsv lists what `lists` looks for.
    pub fn owners(&self, lists: impl Fn(&Listing) -> bool) -> Vec<String> {
        self.folders.iter().filter(|(_, f)| f.listing.as_ref().is_ok_and(&lists)).map(|(k, _)| k.clone()).collect()
    }
}

/// The records whose rows unattached/firmware.log holds, by hash; none if it does not exist.
pub fn read_unattached(root: &Path) -> io::Result<BTreeMap<String, Record>> {
    let rows = match read_or_none(&root.join("unattached").join("firmware.log"))? {
        Some(log) if !log.is_empty() => parse_log(&log)?,
        _ => Vec::new(),
    };
    rows.into_iter().map(|r| match r.source {
        Source::Errrec { id, hash } => Ok((hash.clone(), Record { hash, time: r.time, id, text: r.text })),
        _ => Err(bad("a row that is not an error record's")),
    }).collect()
}
