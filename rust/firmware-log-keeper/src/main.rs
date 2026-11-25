//! firmware-log-keeper: one keeper run (SPEC section 7), in four steps.
#![forbid(unsafe_code)]
use firmware_logs::archive_reader::Archive;
use firmware_logs::output::Output;
use firmware_logs::settings::Config;
use firmware_logs::{step1_collect, step2_plan, step3_write, step4_purge};

fn main() {
    let mut out = Output::default();
    if std::env::args_os().len() > 1 {
        out.say("usage: firmware-log-keeper");
        std::process::exit(2);
    }
    match Config::from_env() {
        Ok(cfg) => keep(&cfg, &mut out),
        Err(e) => out.error("keeper", e), // a wrong setting: nothing read or written
    }
    std::process::exit(i32::from(out.failed));
}

fn keep(cfg: &Config, out: &mut Output) {
    let staged = step1_collect::staging(cfg, out);
    let archive = Archive::open(cfg, out).map_err(|e| out.error("keeper", e)).ok();
    let files = step1_collect::file_logs(cfg, out);
    let records = step1_collect::records(cfg, out);
    let Some(archive) = archive else { return }; // nothing is merged into an archive that cannot be read
    let plan = step2_plan::plan(&archive, &staged, &files, &records, out);
    let (saved, checked) = step3_write::apply(cfg, &plan, out);
    step4_purge::purge_staging(cfg, &staged, &plan, &saved, out);
    step4_purge::purge_records(cfg, &records, &checked, out);
}
