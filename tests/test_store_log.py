"""The observer's log must survive a dead stdout: when the desktop app that
started it goes away, Windows raises EINVAL on the next print."""
import builtins
import os

from aquila import store


def test_log_keeps_going_when_stdout_is_gone(tmp_path, monkeypatch):
    st = store.Store(str(tmp_path))
    calls = []

    def broken_print(*a, **k):
        calls.append(a)
        raise OSError(22, "Invalid argument")
    monkeypatch.setattr(builtins, "print", broken_print)
    st.log("first")
    st.log("second")
    assert len(calls) == 1                      # stops trying after the first failure
    text = (tmp_path / "survey.log").read_text()
    assert "first" in text and "second" in text


BAD = b"\xff\xfe\x81 torn,\x8d\x90\n"          # not text in any encoding the node uses


def test_block_list_is_trimmed_past_a_damaged_row(tmp_path):
    st = store.Store(str(tmp_path))
    block = {"utc": "20261112T000000Z", "local": "2026-11-12 05:30:00", "thumb": "t.png",
             "center_hz": 1420.0e6}
    st.append_block(block, 1, 0, 12.0)
    with open(st.blocks, "ab") as f:
        f.write(BAD)
        f.write(b"20261112T000001Z,x,t.png,0,0,0.0,1420000000\n" * 6000)
    st.append_block(block, 2, 0, 13.0)          # over the size limit: this one trims
    rows = open(st.blocks, "rb").read().splitlines()
    assert rows[0].startswith(b"utc,local,thumb")
    assert len(rows) == 2001
    assert rows[-1].split(b",")[3] == b"2"


def test_hit_table_is_trimmed_and_counted_past_a_damaged_row(tmp_path):
    st = store.Store(str(tmp_path))
    with open(st.hits, "wb") as f:
        f.write(b"utc,freq_hz,snr\n" + BAD + b"20261112T000000Z,1420000000.0,15.0\n" * 500)
    assert st.count_hits() == 501
    adv = {"footprint_cap_gb": 10, "free_floor_gb": 0, "keep_waterfalls": 5,
           "keep_snippets": 5, "csv_cap_mb": 0.001, "csv_keep_rows": 100}
    st.enforce_caps(adv)
    rows = open(st.hits, "rb").read().splitlines()
    assert rows[0] == b"utc,freq_hz,snr" and len(rows) == 101
    assert st.count_hits() == 100


def test_a_row_cut_short_does_not_swallow_the_next_one(tmp_path):
    st = store.Store(str(tmp_path))
    block = {"utc": "20261112T000000Z", "local": "2026-11-12 05:30:00", "thumb": "t.png",
             "center_hz": 1420.0e6, "n_chan": 1024, "chan_bw": 1.0}
    hit = {"chan": 512, "drift_hz_s": 0.1, "snr": 30.0}
    st.append_hits([hit], block, st.hits)
    st.append_block(block, 1, 0, 30.0)
    for p in (st.hits, st.blocks):
        with open(p, "ab") as f:
            f.write(b"20261112T0000")            # the power went here
    st.append_hits([hit], block, st.hits)
    st.append_block(block, 1, 0, 30.0)
    for p in (st.hits, st.blocks):
        rows = open(p, "rb").read().splitlines()
        assert len(rows) == 4 and rows[2] == b"20261112T0000"
        assert rows[3] == rows[1]
    size = os.path.getsize(st.hits)
    store.end_row(st.hits)                       # a whole table is left alone
    store.end_row(str(tmp_path / "none.csv"))    # and a missing one is not made
    assert os.path.getsize(st.hits) == size and not (tmp_path / "none.csv").exists()
