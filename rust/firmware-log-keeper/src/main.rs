//! firmware-log-keeper: one keeper run (SPEC section 7).
#![forbid(unsafe_code)]

fn main() {
    std::process::exit(fwlk::cli::main(std::env::args_os().skip(1).collect()));
}
