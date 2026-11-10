from datetime import timedelta

from src.metrics import compute_metrics, tier_for
from src.utils import parse_iso8601_duration, utc_now


def _v(days_ago, views, likes, comments, dur=600, live=False):
    return {
        "title": "t",
        "published_at": (utc_now() - timedelta(days=days_ago)).isoformat(),
        "views": views,
        "likes": likes,
        "comments": comments,
        "duration_seconds": dur,
        "live": live,
    }


def test_engagement_rate_is_interactions_over_views():
    m = compute_metrics([_v(5, 1000, 40, 10), _v(10, 3000, 100, 50)], subscribers=20000)
    assert m["engagement_rate"] == round(200 / 4000, 5)
    assert m["videos_analysed"] == 2
    assert m["days_since_upload"] == 5


def test_very_new_videos_excluded_from_er():
    m = compute_metrics([_v(1, 100, 90, 9), _v(10, 1000, 30, 0)], subscribers=10000, min_video_age_days=3)
    assert m["engagement_rate"] == 0.03
    assert m["days_since_upload"] == 1  # but still counts for activity


def test_hidden_likes_are_not_guessed():
    m = compute_metrics([_v(5, 1000, None, 10), _v(9, 1000, None, 4)], subscribers=10000)
    assert m["engagement_rate"] is None
    assert m["videos_analysed"] == 0


def test_no_videos():
    m = compute_metrics([], subscribers=None)
    assert m["engagement_rate"] is None and m["last_upload_at"] is None


def test_shorts_share():
    m = compute_metrics([_v(5, 10, 1, 0, dur=40), _v(6, 10, 1, 0, dur=900)], subscribers=1000)
    assert m["shorts_share"] == 0.5


def test_duration_parser():
    assert parse_iso8601_duration("PT4M13S") == 253
    assert parse_iso8601_duration("PT1H2M") == 3720
    assert parse_iso8601_duration("P0D") == 0
    assert parse_iso8601_duration(None) is None


def test_tiers():
    assert tier_for(4000).startswith("Nano")
    assert tier_for(25000).startswith("Micro - mid")
    assert tier_for(None) == "Unknown"
