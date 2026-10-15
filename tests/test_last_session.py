from config.paths import last_session_file
from storage.last_session import SAVED_AT_KEY, load_last_session, save_last_session


def test_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert load_last_session() is None
    save_last_session({"Model": "gemini-3.8-live", "Problems during last session": "B:OUTPUT_STALLED (warning)"})
    loaded = load_last_session()
    assert loaded["Model"] == "gemini-3.8-live"
    assert loaded["Problems during last session"] == "B:OUTPUT_STALLED (warning)"
    assert SAVED_AT_KEY in loaded
    assert not last_session_file().with_suffix(".tmp").exists()


def test_damaged_file_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = last_session_file()
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert load_last_session() is None
    path.write_text("[1, 2]", encoding="utf-8")
    assert load_last_session() is None
