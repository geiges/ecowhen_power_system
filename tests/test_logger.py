"""The Logger: state.json becomes the time series, and nothing reads it back.

The split, the dedup and the daily integral are the three things that can be quietly
wrong for weeks — a wrong column lands in the wrong file, a plateau loses its end, an
energy total is off by the sample spacing — so each gets pinned directly.
"""
import csv
from datetime import datetime, timedelta

import pytest
import pytz

from ecowhen_power_system import aggregation, config_default, discovery, logger
from ecowhen_power_system.logger_main import LoggerProcess

TZ = pytz.timezone(config_default.tz)


def at(hh, mm, ss=0, day=7):
    return TZ.localize(datetime(2026, 7, day, hh, mm, ss))


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


@pytest.fixture
def system_config(fake_bus):
    config, _ = discovery.discover(fake_bus, config_default)
    return config


# ---------------------------------------------------------------------------
# The split — which number belongs in which file
# ---------------------------------------------------------------------------

def test_raw_readings_and_estimates_land_in_different_files(system_config):
    """A12 made this load-bearing: `system/battery_voltage` is what the bus said,
    `battery/voltage` is what nothing measures. They must not share a file.
    """
    state = {
        "timestamp": "2026-07-07T00:00:01+02:00",
        "time": "00:00:01",
        "running_since": "2026-07-06T00:29:00+02:00",
        "system/battery_voltage": 26.38,
        "mppt150/power_yield": 0.0,
        "state": "010104",
        "battery/voltage": 26.2333,
        "battery/current": -0.15,
        "SOC_Kf": 0.5988,
        "wallbox/power_w": 12.0,
        "soc_above_threshold_since": None,
        "minutes_at_full_soc": 0.0,
        "system/power_yield": 0.0,
    }

    rows = logger.split_state(state, system_config)

    assert rows["log"] == {
        "system/battery_voltage": 26.38, "mppt150/power_yield": 0.0, "state": "010104",
    }
    assert rows["sim"] == {
        "battery/voltage": 26.2333, "battery/current": -0.15, "SOC_Kf": 0.5988,
    }
    assert rows["aux"] == {"wallbox/power_w": 12.0}


def test_a_new_estimator_output_gets_a_column_without_editing_a_list(system_config):
    """sim is the residue, not an enumeration — that is how A12's three new keys
    reached the CSV without anyone maintaining a column list."""
    state = {"timestamp": "2026-07-07T00:00:01+02:00", "brand_new_estimate": 1.0}
    assert logger.split_state(state, system_config)["sim"] == {"brand_new_estimate": 1.0}


def test_control_plumbing_never_becomes_a_column(system_config):
    state = {
        "timestamp": "2026-07-07T00:00:01+02:00",
        "running_since": "x", "time": "00:00:01",
        "soc_above_threshold_since": "x", "minutes_at_full_soc": 3.0,
        "system/power_yield": 1.0, "system/total_yield": 2.0,
    }
    rows = logger.split_state(state, system_config)
    assert rows == {"log": {}, "sim": {}, "aux": {}}


# ---------------------------------------------------------------------------
# Rotation, headers, dedup
# ---------------------------------------------------------------------------

@pytest.fixture
def csv_logger(tmp_path):
    return logger.CsvLogger(tmp_path / "log_{date_str}.csv")


def test_writes_a_header_once_then_appends(csv_logger, tmp_path):
    csv_logger.log_step(at(0, 0, 1), {"a": 1})
    csv_logger.log_step(at(0, 0, 6), {"a": 2})

    path = tmp_path / "log_26-07-07.csv"
    assert path.read_text().splitlines()[0] == "time,a"
    assert len(read_csv(path)) == 2


def test_a_new_day_starts_a_new_file(csv_logger, tmp_path):
    csv_logger.log_step(at(23, 59, 55), {"a": 1})
    csv_logger.log_step(at(0, 0, 5, day=8), {"a": 1})

    assert read_csv(tmp_path / "log_26-07-07.csv") == [{"time": "23:59:55", "a": "1"}]
    assert read_csv(tmp_path / "log_26-07-08.csv") == [{"time": "00:00:05", "a": "1"}]


def test_a_restart_appends_rather_than_truncating(tmp_path):
    first = logger.CsvLogger(tmp_path / "log_{date_str}.csv")
    first.log_step(at(0, 0, 1), {"a": 1})

    logger.CsvLogger(tmp_path / "log_{date_str}.csv").log_step(at(0, 0, 6), {"a": 2})

    path = tmp_path / "log_26-07-07.csv"
    assert path.read_text().count("time,a") == 1      # one header, not two
    assert len(read_csv(path)) == 2


def test_identical_samples_collapse_but_the_plateau_keeps_its_end(csv_logger, tmp_path):
    """Holding back the last unchanged row is the subtle half of the dedup: without
    it a steady value records when it started and never when it ended, and a reader
    cannot tell a plateau from a dead logger.
    """
    csv_logger.log_step(at(0, 0, 1), {"a": 1})       # written
    csv_logger.log_step(at(0, 0, 6), {"a": 1})       # held
    csv_logger.log_step(at(0, 0, 11), {"a": 1})      # held, replacing the previous
    csv_logger.log_step(at(0, 0, 16), {"a": 2})      # flushes the held row, then writes

    assert read_csv(tmp_path / "log_26-07-07.csv") == [
        {"time": "00:00:01", "a": "1"},
        {"time": "00:00:11", "a": "1"},
        {"time": "00:00:16", "a": "2"},
    ]


def test_dedup_off_writes_every_sample(tmp_path):
    """The predecessor's condition was `skip_no_changes and changed`, so turning
    dedup *off* wrote nothing at all. Nobody noticed because it was never turned off.
    """
    quiet = logger.CsvLogger(tmp_path / "log_{date_str}.csv", skip_no_changes=False)
    for second in (1, 6, 11):
        quiet.log_step(at(0, 0, second), {"a": 1})

    assert len(read_csv(tmp_path / "log_26-07-07.csv")) == 3


def test_a_component_coming_online_grows_the_columns(csv_logger, tmp_path):
    csv_logger.log_step(at(0, 0, 1), {"a": 1})
    csv_logger.log_step(at(0, 0, 6), {"a": 2, "b": 9})

    rows = read_csv(tmp_path / "log_26-07-07.csv")
    assert set(rows[0]) == {"time", "a", "b"}
    assert rows[0] == {"time": "00:00:01", "a": "1", "b": ""}   # old row kept, padded
    assert rows[1] == {"time": "00:00:06", "a": "2", "b": "9"}


# ---------------------------------------------------------------------------
# Daily aggregation
# ---------------------------------------------------------------------------

def _write_log_csv(path, rows, fieldnames):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_energy_is_time_weighted_not_row_counted(tmp_path):
    """The Logger deduplicates, so samples are not evenly spaced. Counting rows would
    weight a quiet hour the same as a busy one.
    """
    _write_log_csv(
        tmp_path / "log_26-07-07.csv",
        [
            {"time": "00:00:00", "multiplus/AC_power_output": 0},
            {"time": "01:00:00", "multiplus/AC_power_output": 1000},   # 1 h at 1000 W
            {"time": "01:00:10", "multiplus/AC_power_output": 1000},   # 10 s at 1000 W
        ],
        ["time", "multiplus/AC_power_output"],
    )

    row = aggregation.day_aggregates(
        tmp_path / "log_26-07-07.csv", tmp_path / "aux_26-07-07.csv",
        "26-07-07", config_default,
    )

    assert row["AC_power_output"] == pytest.approx(1.0 + 1000 * 10 / 3600 / 1000, abs=1e-4)


def test_dc_load_power_is_derived_from_current_and_voltage(tmp_path):
    """It is the one power the DC load output never reports directly."""
    _write_log_csv(
        tmp_path / "log_26-07-07.csv",
        [
            {"time": "00:00:00", "mppt100/DC_load_current": 0, "mppt100/DC_0_voltage": 26.0},
            {"time": "01:00:00", "mppt100/DC_load_current": 2.0, "mppt100/DC_0_voltage": 26.0},
        ],
        ["time", "mppt100/DC_load_current", "mppt100/DC_0_voltage"],
    )

    row = aggregation.day_aggregates(
        tmp_path / "log_26-07-07.csv", tmp_path / "aux_26-07-07.csv",
        "26-07-07", config_default,
    )

    assert row["DC_load_power"] == pytest.approx(52.0 / 1000, abs=1e-4)   # 52 W for 1 h


def test_solar_total_is_the_meter_difference_plus_the_ac_inverter(tmp_path):
    _write_log_csv(
        tmp_path / "log_26-07-07.csv",
        [
            {"time": "00:00:00", "mppt150/total_yield": 500.0, "mppt100/total_yield": 100.0},
            {"time": "23:59:00", "mppt150/total_yield": 504.0, "mppt100/total_yield": 101.5},
        ],
        ["time", "mppt150/total_yield", "mppt100/total_yield"],
    )
    _write_log_csv(
        tmp_path / "aux_26-07-07.csv",
        [
            {"time": "00:00:00", "ac_inverter/power_w": 0},
            {"time": "01:00:00", "ac_inverter/power_w": 500},      # 0.5 kWh
        ],
        ["time", "ac_inverter/power_w"],
    )

    row = aggregation.day_aggregates(
        tmp_path / "log_26-07-07.csv", tmp_path / "aux_26-07-07.csv",
        "26-07-07", config_default,
    )

    assert row["solar_total"] == pytest.approx(6.0, abs=1e-3)      # 4.0 + 1.5 + 0.5


def test_one_corrupt_row_does_not_cost_the_whole_day(tmp_path):
    """A power cut mid-write leaves a row of NUL bytes — the predecessor's real logs
    have them, and its aggregator threw the day away over one bad sample.
    """
    path = tmp_path / "log_26-07-07.csv"
    _write_log_csv(
        path,
        [{"time": "00:00:00", "multiplus/AC_power_output": 0},
         {"time": "\x00\x00\x00", "multiplus/AC_power_output": 0},
         {"time": "01:00:00", "multiplus/AC_power_output": 1000}],
        ["time", "multiplus/AC_power_output"],
    )

    row = aggregation.day_aggregates(path, tmp_path / "aux_26-07-07.csv",
                                     "26-07-07", config_default)

    # The gap is integrated across, so the hour is not lost with the sample.
    assert row["AC_power_output"] == pytest.approx(1.0, abs=1e-4)


def test_a_day_with_no_readings_leaves_the_extremes_empty(tmp_path):
    """The predecessor wrote -inf / inf, which is not a temperature."""
    _write_log_csv(tmp_path / "log_26-07-07.csv", [], ["time"])

    row = aggregation.day_aggregates(
        tmp_path / "log_26-07-07.csv", tmp_path / "aux_26-07-07.csv",
        "26-07-07", config_default,
    )

    assert row["max_system/battery_temperature"] is None
    assert row["min_system/battery_voltage"] is None


def test_each_completed_day_is_aggregated_exactly_once(tmp_path):
    for day in ("05", "06", "07"):
        _write_log_csv(
            tmp_path / f"log_26-07-{day}.csv",
            [{"time": "00:00:00", "system/battery_voltage": 26.0},
             {"time": "12:00:00", "system/battery_voltage": 27.0}],
            ["time", "system/battery_voltage"],
        )

    agg = aggregation.DailyAggregator(tmp_path, config_default)

    assert agg.update(at(12, 0)) == ["26-07-05", "26-07-06"]   # today is excluded
    assert agg.update(at(12, 0)) == []                          # no duplicates
    assert agg.update(at(12, 0, day=8)) == ["26-07-07"]         # yesterday, now closed

    rows = read_csv(tmp_path / "aggregations" / "daily.csv")
    assert [r["date"] for r in rows] == ["26-07-05", "26-07-06", "26-07-07"]


def test_a_restarted_aggregator_does_not_rewrite_history(tmp_path):
    _write_log_csv(tmp_path / "log_26-07-05.csv",
                   [{"time": "00:00:00", "system/battery_voltage": 26.0}],
                   ["time", "system/battery_voltage"])
    aggregation.DailyAggregator(tmp_path, config_default).update(at(12, 0))

    aggregation.DailyAggregator(tmp_path, config_default).update(at(12, 0))

    assert len(read_csv(tmp_path / "aggregations" / "daily.csv")) == 1


# ---------------------------------------------------------------------------
# The process
# ---------------------------------------------------------------------------

@pytest.fixture
def process(tmp_path, fake_bus, system_config):
    config_path = tmp_path / "system_configuration.yaml"
    discovery.save_system_configuration(system_config, config_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return LoggerProcess(
        data_dir=data_dir,
        runtime_state_path=tmp_path / "state.json",
        system_config_path=config_path,
    ), tmp_path / "state.json", data_dir


def _publish(path, timestamp, **values):
    import json
    state = {"timestamp": timestamp, "system/battery_voltage": 26.38,
             "SOC_Kf": 0.6, "wallbox/power_w": 0.0}
    state.update(values)
    path.write_text(json.dumps(state))


def test_one_poll_becomes_one_row_in_each_file(process):
    proc, state_path, data_dir = process
    _publish(state_path, "2026-07-07T00:00:01+02:00")

    assert proc.step(at(0, 0, 1)) is True

    assert read_csv(data_dir / "log_26-07-07.csv")[0]["system/battery_voltage"] == "26.38"
    assert read_csv(data_dir / "sim_26-07-07.csv")[0]["SOC_Kf"] == "0.6"
    assert read_csv(data_dir / "aux_26-07-07.csv")[0]["wallbox/power_w"] == "0.0"


def test_the_same_poll_is_never_logged_twice(process):
    """The Logger polls a file, so it sees each state several times."""
    proc, state_path, data_dir = process
    _publish(state_path, "2026-07-07T00:00:01+02:00")

    assert proc.step(at(0, 0, 1)) is True
    assert proc.step(at(0, 0, 2)) is False
    assert proc.step(at(0, 0, 3)) is False

    assert len(read_csv(data_dir / "log_26-07-07.csv")) == 1


def test_a_dead_power_system_is_said_out_loud_once(process, capsys):
    proc, state_path, _ = process
    _publish(state_path, "2026-07-07T00:00:01+02:00")
    proc.step(at(0, 0, 1))

    proc.step(at(0, 0, 1) + timedelta(minutes=5))
    proc.step(at(0, 0, 1) + timedelta(minutes=6))

    assert capsys.readouterr().out.count("is the Power System running?") == 1


def test_a_missing_state_file_is_not_fatal(process):
    proc, _, _ = process
    assert proc.step(at(0, 0, 1)) is False
