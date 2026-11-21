//! Dates and times of the years 1 to 9999, to the millisecond (proleptic Gregorian calendar, as the firmware clock
//! counts them). The standard library has no calendar: this is the one conversion the keeper needs.
use std::fmt;

/// Milliseconds since 0001-01-01T00:00:00.000.
#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Debug)]
pub struct Time(i64);

const DAY: i64 = 86_400_000;
const LAST: i64 = 3_652_059 * DAY - 1; // 9999-12-31T23:59:59.999

/// Days from 0001-01-01 (H. Hinnant's algorithm, moved to that epoch).
fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let (era, yoe) = (y.div_euclid(400), y.rem_euclid(400));
    let doy = (153 * ((m + 9) % 12) + 2) / 5 + d - 1;
    era * 146_097 + yoe * 365 + yoe / 4 - yoe / 100 + doy - 306
}

fn civil_from_days(z: i64) -> (i64, i64, i64) {
    let z = z + 306;
    let (era, doe) = (z.div_euclid(146_097), z.rem_euclid(146_097));
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let (d, m) = (doy - (153 * mp + 2) / 5 + 1, (mp + 2) % 12 + 1);
    (yoe + era * 400 + i64::from(m <= 2), m, d)
}

impl Time {
    /// The time of [year, month, day, hour, minute, second] and milliseconds, if it is one of the years 1 to 9999.
    pub fn new([y, mo, d, h, mi, s]: [i64; 6], ms: i64) -> Option<Time> {
        // a day that does not exist, such as February 30, comes back as another one
        let date = (1..=9999).contains(&y) && (1..=12).contains(&mo) && (1..=31).contains(&d)
            && civil_from_days(days_from_civil(y, mo, d)) == (y, mo, d);
        let clock = (0..24).contains(&h) && (0..60).contains(&mi) && (0..60).contains(&s) && (0..1000).contains(&ms);
        (date && clock).then(|| Time(days_from_civil(y, mo, d) * DAY + ((h * 60 + mi) * 60 + s) * 1000 + ms))
    }

    /// This time plus `ms` milliseconds; None outside the years 1 to 9999.
    pub fn add_ms(self, ms: i64) -> Option<Time> {
        self.0.checked_add(ms).filter(|n| (0..=LAST).contains(n)).map(Time)
    }

    /// Milliseconds from `other` to this time.
    pub fn since(self, other: Time) -> i64 {
        self.0 - other.0
    }

    pub fn whole_second(self) -> Time {
        Time(self.0 - self.0 % 1000)
    }

    /// (year, month, day, hour, minute, second, millisecond)
    fn fields(self) -> [i64; 7] {
        let (y, m, d) = civil_from_days(self.0.div_euclid(DAY));
        let r = self.0.rem_euclid(DAY);
        [y, m, d, r / 3_600_000, r / 60_000 % 60, r / 1000 % 60, r % 1000]
    }

    pub fn now() -> Time {
        let unix = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_millis());
        Time(days_from_civil(1970, 1, 1) * DAY + i64::try_from(unix).unwrap_or(0))
    }

    /// The UTC time of a keeper run as written everywhere: `YYYY-MM-DDTHHMMSSZ`.
    pub fn utc_stamp(self) -> String {
        let [y, mo, d, h, mi, s, _] = self.fields();
        format!("{y:04}-{mo:02}-{d:02}T{h:02}{mi:02}{s:02}Z")
    }

    /// The time label of a run folder's name: `YYYY-MM-DDTHHMMSS`.
    pub fn label(self) -> String {
        self.utc_stamp()[..17].to_string()
    }

    /// `FWLK_NOW`: exactly `YYYY-MM-DDTHH:MM:SSZ`, a real date and time.
    pub fn parse_utc(text: &str) -> Option<Time> {
        Time::new(crate::digits(text.as_bytes(), b"dddd-dd-ddTdd:dd:ddZ")?.try_into().ok()?, 0)
    }

    /// The time column of a firmware.log: `YYYY-MM-DDTHH:MM:SS.mmm`.
    pub fn parse_row(text: &str) -> Option<Time> {
        let fields = crate::digits(text.as_bytes(), b"dddd-dd-ddTdd:dd:dd.ddd")?;
        let [y, mo, d, h, mi, s, ms] = fields[..] else { return None };
        Time::new([y, mo, d, h, mi, s], ms)
    }
}

/// `YYYY-MM-DDTHH:MM:SS.mmm`, the time column of a firmware.log.
impl fmt::Display for Time {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        let [y, mo, d, h, mi, s, ms] = self.fields();
        write!(f, "{y:04}-{mo:02}-{d:02}T{h:02}:{mi:02}:{s:02}.{ms:03}")
    }
}

#[cfg(test)]
mod tests {
    use super::Time;

    #[test]
    fn dates() {
        let t = Time::new([2026, 2, 28, 23, 59, 59], 999).unwrap();
        assert_eq!(t.to_string(), "2026-02-28T23:59:59.999");
        assert_eq!(t.add_ms(1).unwrap().to_string(), "2026-03-01T00:00:00.000");
        assert_eq!(Time::parse_row("2024-02-29T12:00:00.001"), Time::new([2024, 2, 29, 12, 0, 0], 1));
        assert_eq!(Time::new([1, 1, 1, 0, 0, 0], 0).unwrap().add_ms(-1), None);
        assert_eq!(Time::new([9999, 12, 31, 23, 59, 59], 999).unwrap().add_ms(1), None);
        assert_eq!(Time::new([2025, 2, 29, 0, 0, 0], 0), None);
        assert_eq!(Time::new([2026, 4, 31, 0, 0, 0], 0), None);
        assert_eq!(Time::new([2026, 13, 1, 0, 0, 0], 0), None);
        assert_eq!(Time::parse_utc("1970-01-01T00:00:00Z").unwrap().since(Time::new([1, 1, 1, 0, 0, 0], 0).unwrap()),
                   719_162 * super::DAY);
        assert_eq!(Time::parse_utc("0999-02-03T00:00:00Z").unwrap().utc_stamp(), "0999-02-03T000000Z");
        for bad in ["2026-2-03T00:00:00Z", "2026-02-03t00:00:00Z", "2026-02-03T00:00:60Z", "2026-02-03T00:00:00Zx"] {
            assert_eq!(Time::parse_utc(bad), None, "{bad}");
        }
    }
}
