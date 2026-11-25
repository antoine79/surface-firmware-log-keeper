//! Step 3 of a keeper run: write the run folders and unattached/firmware.log the plan built and checked.
use std::collections::{BTreeMap, BTreeSet};

use crate::output::Output;
use crate::safe_files::{make_dirs, put};
use crate::settings::Config;
use crate::step2_plan::{Plan, Save};

/// Write the plan. Returns the run folders written and checked, and the records whose row is in a log written and
/// checked.
pub fn apply<'p>(cfg: &Config, plan: &'p Plan, out: &mut Output) -> (BTreeMap<&'p str, &'p Save>, BTreeSet<&'p str>) {
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

