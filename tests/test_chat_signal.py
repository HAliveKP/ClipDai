"""Unit tests for clipdai.chat_signal using synthetic data (no network, no API keys)."""
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from clipdai.chat_signal import chat_score, fetch_chat_messages, messages_per_second, normalize, smooth


def test_messages_per_second_bins_messages_by_second():
    messages = [(0.2, "hi"), (0.9, "hello"), (1.5, "hey"), (2.0, "yo")]
    series = messages_per_second(messages, 5.0)
    assert series.tolist() == [2.0, 1.0, 1.0, 0.0, 0.0]


def test_messages_per_second_clamps_to_duration():
    messages = [(-1.0, "early"), (3.0, "mid"), (5.0, "at_duration_edge"), (9.0, "late")]
    series = messages_per_second(messages, 5.0)
    assert series.tolist() == [0.0, 0.0, 0.0, 1.0, 0.0]


def test_messages_per_second_empty_duration():
    series = messages_per_second([], 0.0)
    assert series.tolist() == [0.0]


def test_smooth_averages_over_approximately_five_seconds():
    # one message per second for 10 seconds -> smoothed interior is ~1.0
    series = messages_per_second([(float(t), "m") for t in range(10)], 10.0)
    smoothed = smooth(series, window_seconds=5.0)
    assert abs(float(smoothed[4]) - 1.0) < 1e-6
    assert 0.0 <= float(smoothed[0]) <= 1.0
    assert smoothed.shape == series.shape


def test_normalize_maps_min_to_zero_and_max_to_one():
    series = messages_per_second([(0.0, "a"), (1.0, "b"), (1.0, "c")], 2.0)  # [1, 2]
    norm = normalize(series)
    assert abs(float(norm[0]) - 0.0) < 1e-6
    assert abs(float(norm[1]) - 1.0) < 1e-6


def test_normalize_flat_series_is_zeros():
    series = messages_per_second([(0.0, "a"), (1.0, "b")], 2.0)  # [1, 1]
    assert (normalize(series) == 0.0).all()


def test_chat_score_averages_over_range():
    series = messages_per_second([(0.0, "a"), (1.0, "b"), (1.0, "c")], 2.0)   # [1, 2]
    norm = normalize(series)                                        # [0, 1]
    assert abs(chat_score(0.0, 2.0, norm) - 0.5) < 1e-6
    assert abs(chat_score(1.0, 2.0, norm) - 1.0) < 1e-6
    assert chat_score(0.0, 0.0, norm) == 0.0


def test_chat_score_missing_series_returns_zero():
    assert chat_score(0.0, 10.0) == 0.0
    assert chat_score(0.0, 10.0, None) == 0.0


def test_fetch_chat_messages_download_error_falls_back_to_empty():
    """A failed chat download (unsupported platform, network error, ...) must not crash scoring."""
    from chat_downloader import ChatDownloader
    with mock.patch.object(ChatDownloader, "get_chat", side_effect=RuntimeError("no chat here")):
        messages = fetch_chat_messages("https://www.twitch.tv/videos/123456789")
    assert messages == []


def test_fetch_chat_messages_uses_disk_cache(tmp_path):
    """The same URL is downloaded once; re-runs read from disk."""
    from chat_downloader import ChatDownloader
    page = [
        {"time_in_seconds": 1.0, "message": "hello"},
        {"time_in_seconds": 2.5, "message": "world"},
    ]
    with mock.patch.object(ChatDownloader, "get_chat", return_value=iter(page)) as get_chat:
        first = fetch_chat_messages("https://www.youtube.com/watch?v=abcdefghijk", cache_dir=tmp_path)
        second = fetch_chat_messages("https://www.youtube.com/watch?v=abcdefghijk", cache_dir=tmp_path)
    assert first == [(1.0, "hello"), (2.5, "world")]
    assert second == first
    get_chat.assert_called_once()  # second call served from disk cache


if __name__ == "__main__":
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            if name == "test_fetch_chat_messages_uses_disk_cache":
                fn(tmp)
            else:
                fn()
            print(f"PASS {name}")
    print("all chat_signal tests passed")
