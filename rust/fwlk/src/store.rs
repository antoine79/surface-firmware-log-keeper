//! The archive of SPEC section 2: atomic writes, the raw copies, the run folders, the seen.tsv files, retention.
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::ffi::OsString;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Component, Path, PathBuf};
use std::rc::Rc;

use sha2::{Digest, Sha256};

use crate::advlog::Line;
use crate::cli::Config;
use crate::cper::{self, Record};
use crate::filelog::FileLog;
use crate::merge::{self, Row};
use crate::{number, Error, Res};

/// Key prefix of a run known by its file log only.
pub const FILE_ONLY: &str = "fileonly-";
/// End of the name a run folder takes while it is being removed.
const REMOVING: &str = ".removing";
const O_NOATIME: i32 = 0o1000000;

/// `([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{6})_([0-9a-f]{32}|fileonly-[0-9a-f]{16})`: the run key of a run folder's name.
pub fn folder_key(name: &str) -> Option<&str> {
    let shape = b"dddd-dd-ddTdddddd";
    let label = name.as_bytes().get(..shape.len())?;
    if !label.iter().zip(shape).all(|(c, s)| if *s == b'd' { c.is_ascii_digit() } else { c == s }) {
        return None;
    }
    let key = name[shape.len()..].strip_prefix('_')?;
    let hex = |s: &str, n| s.len() == n && s.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b));
    (hex(key, 32) || key.strip_prefix(FILE_ONLY).is_some_and(|h| hex(h, 16))).then_some(key)
}

pub fn sha16(data: &[u8]) -> String {
    Sha256::digest(data).iter().take(8).map(|b| format!("{b:02x}")).collect()
}

/// `path + suffix`
pub fn with_suffix(path: &Path, suffix: &str) -> PathBuf {
    let mut s: OsString = path.as_os_str().to_owned();
    s.push(suffix);
    s.into()
}

/// The lines of a text file, each ended by LF; empty lines are skipped (SPEC 2).
pub fn lines(data: &[u8]) -> Res<Vec<&str>> {
    let text = std::str::from_utf8(data).map_err(|e| Error::value(format!("not UTF-8: {e}")))?;
    Ok(text.split('\n').filter(|l| !l.is_empty()).collect())
}

fn read_all(mut f: File, path: &Path) -> Res<Vec<u8>> {
    if f.metadata().is_ok_and(|m| m.is_dir()) {
        return Err(Error::io(&std::io::Error::from_raw_os_error(21), path));
    }
    let mut data = Vec::new();
    f.read_to_end(&mut data).map_err(|e| Error::io(&e, path))?;
    Ok(data)
}

pub fn read(path: &Path) -> Res<Vec<u8>> {
    read_all(File::open(path).map_err(|e| Error::io(&e, path))?, path)
}

pub fn read_or_none(path: &Path) -> Res<Option<Vec<u8>>> {
    match read(path) {
        Err(e) if e.is_not_found() => Ok(None),
        r => r.map(Some),
    }
}

/// Read a file of the EFI partition without updating its access time: that update is a write to the partition.
/// None if the file does not exist.
pub fn read_untouched(path: &Path) -> Res<Option<Vec<u8>>> {
    let f = match OpenOptions::new().read(true).custom_flags(O_NOATIME).open(path) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        // O_NOATIME is for the file's owner and for root only
        Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => File::open(path).map_err(|e| Error::io(&e, path))?,
        r => r.map_err(|e| Error::io(&e, path))?,
    };
    read_all(f, path).map(Some)
}

pub fn fsync_dir(path: &Path) -> Res<()> {
    let f = File::open(path).map_err(|e| Error::io(&e, path))?;
    f.sync_all().map_err(|e| Error::io(&e, path))
}

/// Absolute, and `.`, `..` and repeated slashes resolved as text (Python's `os.path.abspath`).
pub fn abspath(path: &Path) -> PathBuf {
    let full = if path.is_absolute() { path.to_path_buf() } else {
        std::env::current_dir().unwrap_or_default().join(path)
    };
    let mut out = PathBuf::from("/");
    for c in full.components() {
        match c {
            Component::ParentDir => {
                out.pop();
            }
            Component::Normal(n) => out.push(n),
            _ => {}
        }
    }
    out
}

/// Create a folder and flush every folder entry made on the way: a purge may depend on it.
pub fn make_dirs(path: &Path) -> Res<()> {
    let path = abspath(path);
    if !path.is_dir() {
        let parent = path.parent().unwrap_or(Path::new("/"));
        make_dirs(parent)?;
        if let Err(e) = fs::create_dir(&path) {
            if !(e.kind() == std::io::ErrorKind::AlreadyExists && path.is_dir()) {
                return Err(Error::io(&e, &path));
            }
        }
        fsync_dir(parent)?;
    }
    Ok(())
}

pub type Check<'a> = &'a dyn Fn(&[u8]) -> Res<()>;

/// Write an archive file: temporary file, fsync, read back and compare, then rename.
///
/// check: called with the bytes read back; if it fails, the file that was there stays as it was.
pub fn write(path: &Path, data: &[u8], check: Option<Check>) -> Res<()> {
    let tmp = with_suffix(path, ".tmp");
    let attempt = || -> Res<()> {
        let mut f = File::create(&tmp).map_err(|e| Error::io(&e, &tmp))?;
        f.write_all(data).and_then(|_| f.flush()).and_then(|_| f.sync_all()).map_err(|e| Error::io(&e, &tmp))?;
        drop(f);
        let back = read(&tmp)?;
        if back != data {
            return Err(Error::os(format!("{} differs from what was written", path.display())));
        }
        if let Some(check) = check {
            check(&back)?;
        }
        fs::rename(&tmp, path).map_err(|e| Error::io(&e, path))
    };
    if let Err(e) = attempt() {
        if tmp.exists() {
            let _ = fs::remove_file(&tmp);
        }
        return Err(e);
    }
    fsync_dir(path.parent().unwrap_or(Path::new("")))
}

/// One line more in an append-only .tsv file.
pub fn append(path: &Path, fields: &[&str]) -> Res<()> {
    make_dirs(path.parent().unwrap_or(Path::new("")))?;
    let mut f = OpenOptions::new().append(true).create(true).open(path).map_err(|e| Error::io(&e, path))?;
    let line = fields.join("\t") + "\n";
    f.write_all(line.as_bytes()).and_then(|_| f.flush()).and_then(|_| f.sync_all()).map_err(|e| Error::io(&e, path))
}

/// The names of a folder's entries, sorted by their bytes.
pub fn list(path: &Path) -> Res<Vec<OsString>> {
    let mut names = Vec::new();
    for e in fs::read_dir(path).map_err(|e| Error::io(&e, path))? {
        names.push(e.map_err(|e| Error::io(&e, path))?.file_name());
    }
    names.sort();
    Ok(names)
}

/// The names of a folder's entries that are UTF-8 (the only ones that can be run folders), sorted.
fn list_text(path: &Path) -> Res<Vec<String>> {
    Ok(list(path)?.into_iter().filter_map(|n| n.into_string().ok()).collect())
}

/// Total size of the files of a tree, never through a symbolic link to a folder (SPEC 6). A folder that cannot be
/// listed counts as empty; a file that cannot be sized is an error.
fn tree_size(path: &Path) -> Res<u128> {
    let Ok(entries) = fs::read_dir(path) else { return Ok(0) };
    let mut total = 0;
    for p in entries.flatten().map(|e| e.path()) {
        if !p.is_dir() {
            total += u128::from(fs::metadata(&p).map_err(|e| Error::io(&e, &p))?.len());
        } else if !p.is_symlink() {
            total += tree_size(&p)?;
        }
    }
    Ok(total)
}

/// A file log, an error record, or nothing, when keep_copy archives one.
pub enum Kept {
    New,
    Same,
    Other,
}

pub struct Store<'c> {
    pub cfg: &'c Config,
    pub root: PathBuf,
    pub bootefi: PathBuf,
    pub records_dir: PathBuf,
    pub unattached_log: PathBuf,
    pub runs_tsv: PathBuf,
    files: HashMap<String, Rc<FileLog>>,
    records: HashMap<String, Record>,
    /// run key -> folder name
    pub folders: BTreeMap<String, String>,
    /// content hash -> keys of the runs listing it
    pub file_owners: HashMap<String, BTreeSet<String>>,
    pub record_owners: HashMap<String, BTreeSet<String>>,
    /// keys of the run folders without a log: its write was stopped, or failed its check
    pub unfinished: Vec<String>,
}

impl<'c> Store<'c> {
    pub fn new(cfg: &'c Config) -> Res<Store<'c>> {
        let root = cfg.merged_root.clone();
        let mut s = Store {
            cfg, bootefi: cfg.bootefi_root.clone(), records_dir: root.join("hwerrrec"),
            unattached_log: root.join("unattached").join("firmware.log"), runs_tsv: root.join("runs.tsv"), root,
            files: HashMap::new(), records: HashMap::new(), folders: BTreeMap::new(), file_owners: HashMap::new(),
            record_owners: HashMap::new(), unfinished: Vec::new(),
        };
        for name in s.folder_names()? {
            let key = folder_key(&name).unwrap_or_default().to_string();
            s.folders.insert(key.clone(), name.clone());
            if !s.root.join(&name).join("firmware.log").is_file() && !s.unfinished.contains(&key) {
                s.unfinished.push(key.clone());
            }
            for (kind, h) in s.listed(&name)? {
                let owners = match kind.as_str() {
                    "bootefi" => &mut s.file_owners,
                    "errrec" => &mut s.record_owners,
                    _ => continue,
                };
                owners.entry(h).or_default().insert(key.clone());
            }
        }
        Ok(s)
    }

    pub fn folder_names(&self) -> Res<Vec<String>> {
        let names = match list_text(&self.root) {
            Err(e) if e.is_not_found() => return Ok(Vec::new()),
            r => r?,
        };
        Ok(names.into_iter().filter(|n| folder_key(n).is_some() && self.root.join(n).is_dir()).collect())
    }

    /// (kind, hash) of every line of a run folder's sources.tsv.
    pub fn listed(&self, name: &str) -> Res<Vec<(String, String)>> {
        let data = read_or_none(&self.root.join(name).join("sources.tsv"))?.unwrap_or_default();
        Ok(lines(&data)?.iter().filter(|l| l.contains('\t')).map(|l| {
            let mut f = l.split('\t');
            (f.next().unwrap_or("").to_string(), f.next().unwrap_or("").to_string())
        }).collect())
    }

    // ----- raw copies, by content hash

    /// Archive one raw copy: new, the same bytes were there, or other bytes are.
    pub fn keep_copy(&self, folder: &Path, name: &str, data: &[u8]) -> Res<Kept> {
        let path = folder.join(name);
        Ok(match read_or_none(&path)? {
            None => {
                make_dirs(folder)?;
                write(&path, data, None)?;
                Kept::New
            }
            Some(old) if old == data => Kept::Same,
            Some(_) => Kept::Other,
        })
    }

    pub fn keep_file(&self, h: &str, content: &[u8]) -> Res<Kept> {
        self.keep_copy(&self.bootefi, &format!("{h}.txt"), content)
    }

    pub fn keep_record(&self, h: &str, raw: &[u8]) -> Res<Kept> {
        self.keep_copy(&self.records_dir, &format!("{h}.bin"), raw)
    }

    fn load(path: &Path, h: &str) -> Res<Vec<u8>> {
        let data = read(path)?;
        if sha16(&data) != h {
            return Err(Error::value(format!("{} is not the content it is named after", path.display())));
        }
        Ok(data)
    }

    /// The archived file log of that hash, parsed.
    pub fn file(&mut self, h: &str) -> Res<Rc<FileLog>> {
        if !self.files.contains_key(h) {
            let f = FileLog::new(&Self::load(&self.bootefi.join(format!("{h}.txt")), h)?);
            self.files.insert(h.to_string(), Rc::new(f));
        }
        Ok(self.files[h].clone())
    }

    /// (timestamp, record id, message) of the archived error record of that hash.
    pub fn record(&mut self, h: &str) -> Res<Record> {
        if !self.records.contains_key(h) {
            let r = cper::decode(&Self::load(&self.records_dir.join(format!("{h}.bin")), h)?, h);
            self.records.insert(h.to_string(), r);
        }
        Ok(self.records[h].clone())
    }

    // ----- logs

    /// Write a firmware.log if it changed. True if it was written.
    ///
    /// check: the check of 3.5, run on the candidate read back before it replaces the log that was there.
    pub fn write_log(&self, path: &Path, rows: &[Row], check: Check) -> Res<bool> {
        let mut data = merge::log_bytes(rows);
        if read_or_none(path)?.as_deref() == Some(&data[..]) {
            check(&data)?;
            return Ok(false);
        }
        if self.cfg.fault.as_deref() == Some("drop-row") && !rows.is_empty() {
            data = merge::log_bytes(&rows[..rows.len() - 1]); // test only: the candidate loses its last row
        }
        write(path, &data, Some(check))?;
        Ok(true)
    }

    /// {hash: (time, record id, message)} of the rows of unattached/firmware.log.
    pub fn load_unattached(&self) -> Res<BTreeMap<String, Record>> {
        let mut out = BTreeMap::new();
        if let Some(data) = read_or_none(&self.unattached_log)?.filter(|d| !d.is_empty()) {
            for r in merge::parse(&data)? {
                out.insert(r.hash, (r.time, r.number, r.message));
            }
        }
        Ok(out)
    }

    pub fn save_unattached(&self, records: &BTreeMap<String, Record>) -> Res<()> {
        make_dirs(self.unattached_log.parent().unwrap_or(Path::new("")))?;
        self.write_log(&self.unattached_log, &merge::unattached(records.iter()),
                       &|data| merge::verify(data, None, None, records, false))?;
        Ok(())
    }

    pub fn seen_file(&self, fields: &[&str]) -> Res<()> {
        append(&self.bootefi.join("seen.tsv"), fields)
    }

    /// One line more in hwerrrec/seen.tsv. Returns the error if it could not be written: the caller reports it,
    /// unless the failure that led to a `kept` line is already reported.
    pub fn seen_record(&self, action: &str, variable: &str, h: &str, reason: &str) -> Option<Error> {
        append(&self.records_dir.join("seen.tsv"), &[&self.cfg.utc, &self.cfg.boot_id, action, variable, h, reason]).err()
    }

    // ----- removal of a run folder (SPEC 4)

    /// Remove a run folder in two steps, so that a keeper run stopped in between never leaves half a run
    /// folder under a run folder's name: renamed first, then deleted.
    pub fn remove_run(&self, name: &str) -> Res<()> {
        let (from, gone) = (self.root.join(name), self.root.join(format!("{name}{REMOVING}")));
        fs::rename(&from, &gone).map_err(|e| Error::io(&e, &from))?;
        fsync_dir(&self.root)?;
        crate::purge::remove_folder(&gone)
    }

    /// Paths of the run folders an earlier keeper run renamed and could not finish removing.
    pub fn leftovers(&self) -> Res<Vec<PathBuf>> {
        let names = match list_text(&self.root) {
            Err(e) if e.is_not_found() => return Ok(Vec::new()),
            r => r?,
        };
        Ok(names.iter().filter(|n| n.strip_suffix(REMOVING).is_some_and(|k| folder_key(k).is_some()))
            .map(|n| self.root.join(n)).collect())
    }

    // ----- retention (SPEC 6)

    /// Remove run folders above the limits, in the order the keeper made them (runs.tsv; a folder it does not
    /// list counts as made first), never the last one, never one of the keys in `protected`; then the raw file
    /// logs they alone listed.
    pub fn prune(&self, protected: &HashSet<String>) -> Res<()> {
        let mut made: HashMap<String, i64> = HashMap::new();
        let runs = read_or_none(&self.runs_tsv)?.unwrap_or_default();
        for line in lines(&runs)? {
            let n = made.len() as i64;
            made.entry(line.rsplit('\t').next().unwrap_or("").to_string()).or_insert(n);
        }
        let mut names = self.folder_names()?;
        let key = |n: &str| folder_key(n).unwrap_or_default().to_string();
        names.sort_by_key(|n| (made.get(&key(n)).copied().unwrap_or(-1), n.clone()));
        let mut sizes: BTreeMap<String, u128> = BTreeMap::new();
        for n in &names {
            sizes.insert(n.clone(), tree_size(&self.root.join(n))?);
        }
        let mut dropped = BTreeSet::new();
        for name in names.iter().take(names.len().saturating_sub(1)) {
            let total: u128 = sizes.values().sum();
            if sizes.len() as u128 <= self.cfg.keep_runs as u128 && total <= self.cfg.keep_bytes as u128 {
                break;
            }
            if protected.contains(&key(name)) {
                continue;
            }
            dropped.extend(self.listed(name)?.into_iter().filter(|(k, _)| k == "bootefi").map(|(_, h)| h));
            self.remove_run(name)?;
            sizes.remove(name);
        }
        if !dropped.is_empty() {
            for n in sizes.keys() {
                for (kind, h) in self.listed(n)? {
                    if kind == "bootefi" {
                        dropped.remove(&h);
                    }
                }
            }
            for h in dropped {
                let p = self.bootefi.join(format!("{h}.txt"));
                match fs::remove_file(&p) {
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                    r => r.map_err(|e| Error::io(&e, &p))?,
                }
            }
        }
        Ok(())
    }
}

/// One firmware run of the archive: its memory log's lines, the hashes of its file logs and error records.
pub struct Run {
    pub key: String,
    pub name: Option<String>,
    pub mem: Option<Vec<Line>>,
    pub mem_hash: Option<String>,
    pub fpdt: Option<Vec<u8>>,
    pub esrt: Option<Vec<u8>>,
    /// what stops a save; lost_memory ends when the memory log is read again
    pub problem: Option<String>,
    pub lost_memory: Option<String>,
    /// hash -> slot it was first seen in
    pub files: BTreeMap<String, String>,
    pub records: BTreeSet<String>,
    pub verified: bool,
}

/// What Run::render gives: the rows and the sources they were made from.
pub struct Rendered {
    pub out: merge::Rendered,
    pub files: BTreeMap<String, Rc<FileLog>>,
    pub records: BTreeMap<String, Record>,
}

impl Run {
    pub fn new(store: &Store, key: &str) -> Run {
        let mut r = Run {
            key: key.to_string(), name: store.folders.get(&key.to_string()).cloned(), mem: None, mem_hash: None,
            fpdt: None, esrt: None, problem: None, lost_memory: None, files: BTreeMap::new(),
            records: BTreeSet::new(), verified: false,
        };
        let Some(name) = r.name.clone() else { return r };
        let folder = store.root.join(&name);
        let mut memory = None;
        let read_sources = |r: &mut Run, memory: &mut Option<(String, i64)>| -> Res<()> {
            let data = read_or_none(&folder.join("sources.tsv"))?.unwrap_or_default();
            for line in lines(&data)? {
                let f: Vec<&str> = line.split('\t').collect();
                if f.len() != 5 {
                    return Err(Error::value(format!("line with {} fields", f.len())));
                }
                match f[0] {
                    "memory" => *memory = Some((f[1].to_string(), number(f[2])?)),
                    "bootefi" => {
                        r.files.insert(f[1].to_string(), f[3].to_string());
                    }
                    "errrec" => {
                        r.records.insert(f[1].to_string());
                    }
                    _ => {}
                }
            }
            Ok(())
        };
        if let Err(e) = read_sources(&mut r, &mut memory) {
            r.problem = Some(format!("{name}/sources.tsv: {e}"));
        }
        if let Some((h, count)) = memory {
            // the memory log's lines exist nowhere else than in the log itself
            r.mem_hash = Some(h);
            let lines = || -> Res<Vec<Line>> {
                // no log at all (its first write did not pass the check) holds no line
                let log = read_or_none(&folder.join("firmware.log"))?.filter(|d| !d.is_empty())
                    .unwrap_or_else(|| merge::COLUMNS.as_bytes().to_vec());
                Ok(merge::memory_lines(&merge::parse(&log)?))
            };
            match lines() {
                Ok(mem) => {
                    if mem.len() as i64 != count {
                        r.lost_memory = Some(format!("{name}/firmware.log: {} of its {count} memory lines", mem.len()));
                    }
                    r.mem = Some(mem);
                }
                Err(e) => r.lost_memory = Some(format!("{name}/firmware.log: {e}")),
            }
        }
        r
    }

    /// Take a memory dump of this boot, unless the memory log already held has lines the dump lacks: those
    /// lines exist nowhere else. True if every line of the dump is in the run's memory log afterwards.
    pub fn offer_memory(&mut self, lines: &[Line], h: &str) -> bool {
        let take = self.mem_hash.as_deref() == Some(h) || self.mem.is_none() && self.lost_memory.is_none()
            || self.mem.as_ref().is_some_and(|m| merge::begins(lines, m));
        if take {
            (self.mem, self.mem_hash, self.lost_memory) = (Some(lines.to_vec()), Some(h.to_string()), None);
            return true;
        }
        self.mem.as_ref().is_some_and(|m| merge::begins(m, lines))
    }

    pub fn empty(&self) -> bool {
        !(self.name.is_some() || self.mem.is_some() || !self.files.is_empty()
          || self.fpdt.as_ref().is_some_and(|f| !f.is_empty()) || self.esrt.as_ref().is_some_and(|e| !e.is_empty()))
    }

    /// The rows in order and their sources, from the sources as they are now.
    pub fn render(&self, store: &mut Store) -> Res<Rendered> {
        if let Some(p) = self.problem.as_ref().or(self.lost_memory.as_ref()) {
            return Err(Error::value(p.clone()));
        }
        let mut files = BTreeMap::new();
        for h in self.files.keys() {
            files.insert(h.clone(), store.file(h)?);
        }
        let mut records = BTreeMap::new();
        for h in &self.records {
            records.insert(h.clone(), store.record(h)?);
        }
        let out = merge::render(self.mem.as_deref(), &files, &records)?;
        Ok(Rendered { out, files, records })
    }

    /// Write the folder (only the files that changed); the log replaces the one before only if it passes the
    /// check of 3.5. Returns (changed, memory lines, lines of the merged file log, records).
    pub fn save(&mut self, store: &mut Store) -> Res<(bool, usize, usize, usize)> {
        let Rendered { out, files, records } = self.render(store)?;
        let rows = &out.rows;
        let merged = out.merged.as_ref();
        // the label is the first firmware-clock time; without one, the UTC time of the keeper run that made the folder
        let label = match (rows.first().and_then(|r| r.time), &self.name) {
            (Some(t), _) => merge::fmt_time(Some(t))[..19].replace(':', ""),
            (None, Some(n)) => n[..17].to_string(),
            (None, None) => store.cfg.utc[..17].to_string(),
        };
        let name = format!("{label}_{}", self.key);
        let folder = store.root.join(&name);
        let mut changed = self.name.as_deref() != Some(&name);
        match &self.name {
            Some(old) if changed => {
                let from = store.root.join(old);
                fs::rename(&from, &folder).map_err(|e| Error::io(&e, &from))?;
                fsync_dir(&store.root)?;
            }
            Some(_) => {}
            None => {
                // a new run: noted first, so that retention knows the order the folders were made in
                append(&store.runs_tsv, &[&store.cfg.utc, &store.cfg.boot_id, &self.key])?;
                make_dirs(&folder)?;
            }
        }
        self.name = Some(name.clone());
        store.folders.insert(self.key.clone(), name);
        let mut sources = Vec::new();
        if let Some(mem) = &self.mem {
            sources.push(format!("memory\t{}\t{}\t-\t-\n", self.mem_hash.as_deref().unwrap_or(""), mem.len()));
        }
        let mut hashes: Vec<&String> = files.keys().collect();
        hashes.sort_by_key(|h| (Some(*h) != merged, *h));
        for h in hashes {
            let slot = self.files.get(h).map_or("", |s| s.as_str());
            let role = if Some(h) == merged { "merged" } else { "extra" };
            sources.push(format!("bootefi\t{h}\t{}\t{slot}\t{role}\n", files[h].lines.len()));
        }
        sources.extend(records.keys().map(|h| format!("errrec\t{h}\t1\t-\t-\n")));
        // sources.tsv first: if the log write then fails, the next keeper run finds what the log should hold
        let sources = sources.concat().into_bytes();
        for (file_name, data) in [("sources.tsv", Some(&sources)), ("fpdt-boot.txt", self.fpdt.as_ref()),
                                  ("esrt.txt", self.esrt.as_ref())] {
            let path = folder.join(file_name);
            if let Some(data) = data {
                if read_or_none(&path)?.as_ref() != Some(data) {
                    write(&path, data, None)?;
                    changed = true;
                }
            }
        }
        let log = folder.join("firmware.log");
        let old = read_or_none(&log)?;
        let file = merged.map(|h| &*files[h]);
        // 3.5, and whatever sources.tsv says: the log replaced is the only copy of what it holds
        let check = |data: &[u8]| -> Res<()> {
            merge::verify(data, self.mem.as_deref(), file, &records, out.tagged)?;
            match &old {
                Some(old) if old != data => merge::keeps(data, old),
                _ => Ok(()),
            }
        };
        changed |= store.write_log(&log, rows, &check)?;
        self.verified = true;
        Ok((changed, self.mem.as_ref().map_or(0, |m| m.len()), file.map_or(0, |f| f.lines.len()), records.len()))
    }
}
