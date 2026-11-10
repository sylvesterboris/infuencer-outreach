"""Offline test doubles for the YouTube API and the LLM.

The channels below are SYNTHETIC test fixtures ("Test ..." names, example.test emails).
They exist only to exercise the code paths; they are never exported as a real dataset.
"""
from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any

from src.utils import utc_now


def _iso(days_ago: int) -> str:
    return (utc_now() - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_channel(
    cid: str,
    title: str,
    subs: int | None,
    description: str = "",
    titles: list[str] | None = None,
    last_upload_days: int = 5,
    views: int = 10_000,
    likes: int | None = 400,
    comments: int = 50,
    duration: str = "PT9M30S",
    country: str | None = "IN",
    topics: list[str] | None = None,
    made_for_kids: bool = False,
) -> dict[str, Any]:
    titles = titles or [f"Video {i}" for i in range(8)]
    videos = []
    for i, t in enumerate(titles):
        stats = {"viewCount": str(views), "commentCount": str(comments)}
        if likes is not None:
            stats["likeCount"] = str(likes)
        videos.append(
            {
                "id": f"{cid}_v{i}",
                "snippet": {"title": t, "publishedAt": _iso(last_upload_days + i * 6), "tags": [], "description": ""},
                "statistics": stats,
                "contentDetails": {"duration": duration},
            }
        )
    channel = {
        "id": cid,
        "snippet": {"title": title, "description": description, "customUrl": f"@{cid.lower()}",
                    "publishedAt": _iso(1500), **({"country": country} if country else {})},
        "statistics": {
            "subscriberCount": str(subs or 0),
            "hiddenSubscriberCount": subs is None,
            "viewCount": "1000000",
            "videoCount": str(len(titles)),
        },
        "brandingSettings": {"channel": {"keywords": ""}},
        "topicDetails": {"topicCategories": [f"https://en.wikipedia.org/wiki/{t}" for t in (topics or [])]},
        "contentDetails": {"relatedPlaylists": {"uploads": f"UU{cid}"}},
        "status": {"madeForKids": made_for_kids},
    }
    return {"channel": channel, "videos": videos}


TECH_TITLES = [
    "Best budget smartphone under 20000 - full review",
    "Python tutorial for beginners: lists and loops",
    "I tried 5 AI tools for students",
    "Laptop buying guide 2026",
    "ChatGPT tutorial: prompts that actually work",
    "Coding roadmap for beginners explained",
    "Earbuds review after one month",
    "How to learn web development fast",
]


def fixture_channels() -> list[dict[str, Any]]:
    return [
        make_channel("CHA", "Test Tech Ankit", 25_000, topics=["Technology"],
                     description="Tech reviews and coding tutorials. Business enquiries: collab@ankit.example.test\n"
                                 "Instagram: https://www.instagram.com/test.ankit/",
                     titles=TECH_TITLES),
        make_channel("CHB", "Test Mega Tech", 500_000, topics=["Technology"], titles=TECH_TITLES,
                     description="business: mega@megatech.example.test"),
        make_channel("CHC", "Test Hidden Subs", None, topics=["Technology"], titles=TECH_TITLES),
        make_channel("CHD", "Test Kitchen Stories", 30_000, topics=["Food"],
                     titles=["Paneer butter masala recipe", "Easy dal tadka", "Street food tour", "Chocolate cake",
                             "Biryani at home", "Mango lassi", "Aloo paratha", "Masala chai"],
                     description="Cooking every week. Contact: kitchen@kitchen.example.test"),
        make_channel("CHE", "Test Code With Riya", 8_000, topics=["Technology"], titles=TECH_TITLES,
                     description="Coding tutorials for beginners. Website: https://riya.example.test"),
        make_channel("CHF", "Test Sleepy Gadgets", 40_000, topics=["Technology"], titles=TECH_TITLES,
                     last_upload_days=120, description="gadgets [at] sleepy [dot] example [dot] test"),
        make_channel("CHG", "Test No Likes Tech", 60_000, topics=["Technology"], titles=TECH_TITLES, likes=None),
        make_channel("CHH", "Test Betting Tech", 20_000, topics=["Technology"],
                     titles=[t + " | betting app bonus" for t in TECH_TITLES]),
        # Same agency inbox as CHA -> must be de-duplicated at send time
        make_channel("CHI", "Test Ankit Shorts", 12_000, topics=["Technology"], titles=TECH_TITLES,
                     duration="PT45S", description="For brand deals email collab@ankit.example.test"),
    ]


class FakeYouTube:
    def __init__(self, fixtures: list[dict[str, Any]] | None = None):
        self.fixtures = {f["channel"]["id"]: f for f in (fixtures or fixture_channels())}
        self.units_used = 0
        self.search_calls: list[str] = []

    def search(self, query: str, **_: Any):
        self.search_calls.append(query)
        self.units_used += 100
        items = [
            {"id": {"videoId": f["videos"][0]["id"]},
             "snippet": {"channelId": cid, "channelTitle": f["channel"]["snippet"]["title"]}}
            for cid, f in self.fixtures.items()
        ]
        return items, None

    def get_channels(self, ids: list[str]):
        self.units_used += 1
        return {cid: self.fixtures[cid]["channel"] for cid in ids if cid in self.fixtures}

    def get_upload_video_ids(self, playlist_id: str, limit: int = 12):
        self.units_used += 1
        cid = playlist_id[2:]
        return [v["id"] for v in self.fixtures[cid]["videos"][:limit]]

    def get_videos(self, ids: list[str]):
        self.units_used += 1
        cid = ids[0].split("_v")[0]
        return [v for v in self.fixtures[cid]["videos"] if v["id"] in ids]


class FakeLLM:
    """Deterministic stand-in for GroqClient. `bad_first` makes the first message draft
    too short so the retry/feedback loop is exercised."""

    model = "fake-llm"

    def __init__(self, bad_first: bool = False):
        self.bad_first = bad_first
        self.calls = 0
        self.message_calls = 0

    def complete_json(self, system: str, user: str, temperature: float | None = None) -> dict[str, Any]:
        self.calls += 1
        if "content_themes" in system:
            titles = re.findall(r"^- (.+?) \(", user, flags=re.M)
            return {
                "content_themes": ["budget smartphone reviews", "Python for beginners", "AI tools for students"],
                "content_tone": "friendly beginner explainers",
                "sub_niche": "Programming & Web Dev",
                "inferred_audience": "college students learning to code",
                "standout_recent_video": titles[0] if titles else "",
            }
        self.message_calls += 1
        data = json.loads(user.split("Creator data (JSON):\n", 1)[1].split("\n\nYour previous draft", 1)[0])
        name = data["channel_name"]
        title = data["recent_videos"][0]["title"]
        if self.bad_first and self.message_calls == 1:
            return {"email_subject": "Hi", "email_body": f"Hi {name}, collab?", "dm": "hey"}
        body = (
            f"Hi {name}, I watched your recent video \"{title}\" and liked how plainly you explain things "
            f"for people who are just starting out. We are {data['brand']['name']}, and we build learning tools "
            f"for school students and teachers. We would love to explore {data['collaboration_angle']['what_we_propose']}. "
            f"It feels like a natural fit for the way you teach. Would you be open to a quick call next week?"
        )
        dm = f"Hey {name}, really enjoyed \"{title}\". Open to a quick chat about a collab with our learning tools?"
        return {"email_subject": f"{name} x {data['brand']['name']} collab idea", "email_body": body, "dm": dm}
