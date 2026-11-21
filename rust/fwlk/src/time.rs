//! Dates and times of the years 1 to 9999, to the millisecond (proleptic Gregorian calendar, as the firmware clock
//! and Python's datetime count them), and the one format of the UTC time of a keeper run.

/// Milliseconds since 0001-01-01T00:00:00.000.
#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Debug)]
pub struct Time(i64);

const DAY: i64 = 86_400_000;
const MAX_MS: i64 = 3_652_059 * DAY - 1; // 9999-12-31T23:59:59.999

/// Days from 1970-01-01 (H. Hinnant's algorithm).
fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let doy = (153 * (m + if m > 2 { -3 } else { 9 }) + 2) / 5 + d - 1;
    era * 146097 + yoe * 365 + yoe / 4 - yoe / 100 + doy - 719468
}

fn civil_from_days(z: i64) -> (i64, i64, i64) {
    let z = z + 719468;
    let era = z.div_euclid(146097);
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (yoe + era * 400 + if m <= 2 { 1 } else { 0 }, m, d)
}

fn epoch() -> i64 {
    days_from_civil(1, 1, 1)
}

impl Time {
    /// The time of these fields, if they are a date and time of the years 1 to 9999.
    pub fn new(y: i64, mo: i64, d: i64, h: i64, mi: i64, s: i64, ms: i64) -> Option<Time> {
        if !(1..=9999).contains(&y) || !(1..=12).contains(&mo) {
            return None;
        }
        let leap = y % 4 == 0 && (y % 100 != 0 || y % 400 == 0);
        let days = [31, if leap { 29 } else { 28 }, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][mo as usize - 1];
        let fits = (1..=days).contains(&d) && (0..24).contains(&h) && (0..60).contains(&mi) && (0..60).contains(&s)
            && (0..1000).contains(&ms);
        fits.then(|| Time((days_from_civil(y, mo, d) - epoch()) * DAY + ((h * 60 + mi) * 60 + s) * 1000 + ms))
    }

    /// This time plus `ms` milliseconds; None outside the years 1 to 9999.
    pub fn add_ms(self, ms: i128) -> Option<Time> {
        let n = self.0 as i128 + ms;
        (0..=MAX_MS as i128).contains(&n).then_some(Time(n as i64))
    }

    /// Milliseconds from `other` to this time.
    pub fn since(self, other: Time) -> i128 {
        self.0 as i128 - other.0 as i128
    }

    pub fn whole_second(self) -> Time {
        Time(self.0 - self.0 % 1000)
    }

    /// (year, month, day, hour, minute, second, millisecond)
    pub fn fields(self) -> [i64; 7] {
        let (y, m, d) = civil_from_days(self.0.div_euclid(DAY) + epoch());
        let r = self.0.rem_euclid(DAY);
        [y, m, d, r / 3_600_000, r / 60_000 % 60, r / 1000 % 60, r % 1000]
    }

    pub fn now() -> Time {
        let unix = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_millis());
        Time((days_from_civil(1970, 1, 1) - epoch()) * DAY + unix as i64)
    }

    /// The UTC time of a keeper run as written everywhere: `YYYY-MM-DDTHHMMSSZ`.
    pub fn utc_stamp(self) -> String {
        let [y, mo, d, h, mi, s, _] = self.fields();
        format!("{y:04}-{mo:02}-{d:02}T{h:02}{mi:02}{s:02}Z")
    }

    /// `FWLK_NOW`: exactly `YYYY-MM-DDTHH:MM:SSZ`, ASCII digits, a real date and time.
    pub fn parse_utc(text: &str) -> Option<Time> {
        let (b, shape) = (text.as_bytes(), b"dddd-dd-ddTdd:dd:ddZ");
        let fits = b.len() == shape.len()
            && b.iter().zip(shape).all(|(c, s)| if *s == b'd' { c.is_ascii_digit() } else { c == s });
        let n = |a: usize, z: usize| text[a..z].parse().unwrap_or(-1);
        fits.then(|| Time::new(n(0, 4), n(5, 7), n(8, 10), n(11, 13), n(14, 16), n(17, 19), 0)).flatten()
    }
}

#[cfg(test)]
mod tests {
    use super::Time;

    #[test]
    fn dates() {
        let t = Time::new(2026, 2, 28, 23, 59, 59, 999).unwrap();
        assert_eq!(t.fields(), [2026, 2, 28, 23, 59, 59, 999]);
        assert_eq!(t.add_ms(1).unwrap().fields(), [2026, 3, 1, 0, 0, 0, 0]);
        assert_eq!(Time::new(1, 1, 1, 0, 0, 0, 0).unwrap().add_ms(-1), None);
        assert_eq!(Time::new(9999, 12, 31, 23, 59, 59, 999).unwrap().add_ms(1), None);
        assert_eq!(Time::new(2025, 2, 29, 0, 0, 0, 0), None);
        assert_eq!(Time::parse_utc("0999-02-03T00:00:00Z").unwrap().utc_stamp(), "0999-02-03T000000Z");
        for bad in ["2026-2-03T00:00:00Z", "2026-02-03t00:00:00Z", "2026-02-03T00:00:60Z", "2026-02-03T00:00:00Zx"] {
            assert_eq!(Time::parse_utc(bad), None, "{bad}");
        }
    }
}
