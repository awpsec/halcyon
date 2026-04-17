import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import app.api.routes as routes_service
import app.services.background as background_service
import app.services.feed as feed_service
import app.services.sync as sync_service
from app.core.config import Settings
from app.models.base import Base
from app.models.entities import Channel, LibraryRoot, LiveMonitoredChannel, RetentionItem, SelectedFolder, Series, SyncJob, SyncSettings, UserProfile, Video, VideoFile, WatchProgress, YouTubeChannelSnapshot, YouTubeCommentReplySnapshot, YouTubeCommentSnapshot, YouTubeLiveStreamSnapshot, YouTubeMatch, YouTubeVideoSnapshot
from app.services.media import fingerprint_file
from app.services.scanner import scan_selected_folders
from app.services.sync import apply_sync_item, auto_organize_channel_files, choose_playlist_series_title, fetch_channel_about_details, refresh_live_streams, sync_scope, sync_video
from app.services.utils import slugify


def make_session(tmp_path: Path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)()


def test_fetch_channel_about_details_parses_counts_without_api(monkeypatch):
    html = """
    <html>
      <head><meta property="og:image" content="https://yt3.googleusercontent.com/avatar=s176-c-k-c0x00ffffff-no-rj" /></head>
      <body>
        <script>
          var ytInitialData = {
            "contents": {
              "aboutChannelViewModel": {
                "title": {"content": "Asmongold TV"},
                "description": {"content": "Channel description"},
                "subscriberCountText": "4.49M subscribers",
                "viewCountText": "5,135,286,525 views",
                "videoCountText": "6,945 videos",
                "joinedDateText": {"content": "Joined Dec 9, 2019"},
                "canonicalChannelUrl": "http://www.youtube.com/@AsmonTV",
                "links": []
              }
            }
          };
        </script>
      </body>
    </html>
    """

    class DummyResponse:
        def __init__(self, text: str):
            self.text = text
            self.is_error = False

    async def fake_throttled_get(*args, **kwargs):
        return DummyResponse(html)

    monkeypatch.setattr(sync_service, "throttled_get", fake_throttled_get)

    async def run():
        async with httpx.AsyncClient() as client:
            return await fetch_channel_about_details(client, "channel-asmongold", 3, include_art=False)

    result = asyncio.run(run())

    assert result is not None
    assert result["subscriber_count"] == 4_490_000
    assert result["view_count"] == 5_135_286_525
    assert result["video_count"] == 6_945


def test_infer_channel_ids_from_neighbor_titles_rejects_low_signal_overlap(tmp_path: Path):
    with make_session(tmp_path) as db:
        generic_channel = Channel(name="Unknown Channel", slug="unknown-channel")
        politics_channel = Channel(name="HasanAbi", slug="hasanabi")
        db.add_all([generic_channel, politics_channel])
        db.flush()

        matched_video = Video(
            title="Hillary Clinton is so Fucking Stupid",
            slug="hillary-clinton-is-so-fucking-stupid",
            channel_id=politics_channel.id,
            created_at=datetime.utcnow() - timedelta(days=1),
            duration_seconds=1200,
            is_available=True,
        )
        target_video = Video(
            title="This is so fucking stupid...",
            slug="this-is-so-fucking-stupid",
            channel_id=generic_channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=900,
            is_available=True,
        )
        db.add_all([matched_video, target_video])
        db.flush()
        db.add(
            YouTubeMatch(
                video_id=matched_video.id,
                youtube_video_id="hasan1234567",
                youtube_channel_id="channel-hasan",
                status="matched",
                confidence=0.95,
            )
        )
        db.commit()

        assert sync_service.infer_channel_ids_from_neighbor_titles(db, target_video) == []


def test_fetch_youtube_web_candidates_parses_video_renderers(monkeypatch):
    html = """
    <html>
      <body>
        <script>
          var ytInitialData = {
            "contents": {
              "twoColumnSearchResultsRenderer": {
                "primaryContents": {
                  "sectionListRenderer": {
                    "contents": [
                      {
                        "itemSectionRenderer": {
                          "contents": [
                            {
                              "videoRenderer": {
                                "videoId": "abc123def45",
                                "title": {"runs": [{"text": "Can Jynxzi Find 60 Trash Talking Props? (1v60)"}]},
                                "ownerText": {
                                  "runs": [
                                    {
                                      "text": "Jynxzi",
                                      "navigationEndpoint": {
                                        "browseEndpoint": {
                                          "browseId": "channel-jynxzi"
                                        }
                                      }
                                    }
                                  ]
                                },
                                "lengthText": {"simpleText": "36:39"},
                                "thumbnail": {
                                  "thumbnails": [
                                    {"url": "https://i.ytimg.com/vi/abc123def45/hqdefault.jpg"}
                                  ]
                                },
                                "viewCountText": {"simpleText": "14,845 views"}
                              }
                            }
                          ]
                        }
                      }
                    ]
                  }
                }
              }
            }
          };
        </script>
      </body>
    </html>
    """

    class DummyResponse:
        def __init__(self, text: str):
            self.text = text
            self.is_error = False

    async def fake_throttled_get(*args, **kwargs):
        return DummyResponse(html)

    monkeypatch.setattr(sync_service, "throttled_get", fake_throttled_get)

    async def run():
        async with httpx.AsyncClient() as client:
            return await sync_service.fetch_youtube_web_candidates(
                client,
                ["Can Jynxzi Find 60 Trash Talking Props? (1v60)"],
                3,
            )

    results = asyncio.run(run())

    assert len(results) == 1
    assert results[0]["id"] == "abc123def45"
    assert results[0]["snippet"]["title"] == "Can Jynxzi Find 60 Trash Talking Props? (1v60)"
    assert results[0]["snippet"]["channelTitle"] == "Jynxzi"
    assert results[0]["snippet"]["channelId"] == "channel-jynxzi"
    assert results[0]["statistics"]["viewCount"] == 14_845
    assert results[0]["_waytube_duration_seconds"] == 36 * 60 + 39


def test_fetch_google_dork_video_ids_merges_across_queries(monkeypatch):
    html_by_query = {
        'site:youtube.com/watch "Asmongold TV British Navy is a joke now"': """
        <html><body>
          <a href="/url?q=https://www.youtube.com/watch?v=wrong12345a&sa=U"></a>
        </body></html>
        """,
        'site:youtube.com/watch "British Navy is a joke now"': """
        <html><body>
          <a href="/url?q=https://www.youtube.com/watch?v=right12345b&sa=U"></a>
        </body></html>
        """,
    }

    class DummyResponse:
        def __init__(self, text: str):
            self.text = text
            self.is_error = False

    async def fake_throttled_get(*args, **kwargs):
        return DummyResponse(html_by_query[kwargs["params"]["q"]])

    monkeypatch.setattr(sync_service, "throttled_get", fake_throttled_get)

    async def run():
        async with httpx.AsyncClient() as client:
            return await sync_service.fetch_google_dork_video_ids(
                client,
                ["Asmongold TV British Navy is a joke now", "British Navy is a joke now"],
                3,
            )

    results = asyncio.run(run())

    assert results == ["wrong12345a", "right12345b"]


def test_fetch_youtube_web_candidates_merges_across_queries(monkeypatch):
    html_by_query = {
        "Asmongold TV British Navy is a joke now": """
        <html><body><script>
        var ytInitialData = {
          "contents": {
            "twoColumnSearchResultsRenderer": {
              "primaryContents": {
                "sectionListRenderer": {
                  "contents": [
                    {
                      "itemSectionRenderer": {
                        "contents": [
                          {
                            "videoRenderer": {
                              "videoId": "wrong12345a",
                              "title": {"runs": [{"text": "This is absolutely embarrassing.."}]},
                              "ownerText": {"runs": [{"text": "Asmongold TV", "navigationEndpoint": {"browseEndpoint": {"browseId": "channel-asmongold"}}}]},
                              "lengthText": {"simpleText": "15:00"},
                              "thumbnail": {"thumbnails": [{"url": "https://i.ytimg.com/vi/wrong12345a/hqdefault.jpg"}]},
                              "viewCountText": {"simpleText": "12,345 views"}
                            }
                          }
                        ]
                      }
                    }
                  ]
                }
              }
            }
          }
        };
        </script></body></html>
        """,
        "British Navy is a joke now": """
        <html><body><script>
        var ytInitialData = {
          "contents": {
            "twoColumnSearchResultsRenderer": {
              "primaryContents": {
                "sectionListRenderer": {
                  "contents": [
                    {
                      "itemSectionRenderer": {
                        "contents": [
                          {
                            "videoRenderer": {
                              "videoId": "right12345b",
                              "title": {"runs": [{"text": "British Navy is a joke now"}]},
                              "ownerText": {"runs": [{"text": "Asmongold TV", "navigationEndpoint": {"browseEndpoint": {"browseId": "channel-asmongold"}}}]},
                              "lengthText": {"simpleText": "15:03"},
                              "thumbnail": {"thumbnails": [{"url": "https://i.ytimg.com/vi/right12345b/hqdefault.jpg"}]},
                              "viewCountText": {"simpleText": "67,890 views"}
                            }
                          }
                        ]
                      }
                    }
                  ]
                }
              }
            }
          }
        };
        </script></body></html>
        """,
    }

    class DummyResponse:
        def __init__(self, text: str):
            self.text = text
            self.is_error = False

    async def fake_throttled_get(*args, **kwargs):
        return DummyResponse(html_by_query[kwargs["params"]["search_query"]])

    monkeypatch.setattr(sync_service, "throttled_get", fake_throttled_get)

    async def run():
        async with httpx.AsyncClient() as client:
            return await sync_service.fetch_youtube_web_candidates(
                client,
                ["Asmongold TV British Navy is a joke now", "British Navy is a joke now"],
                3,
            )

    results = asyncio.run(run())

    assert [item["id"] for item in results] == ["wrong12345a", "right12345b"]


def test_fetch_watch_page_candidate_keeps_full_timestamp_publish_at(monkeypatch):
    html = """
    <html>
      <head>
        <meta property="og:image" content="https://i.ytimg.com/vi/abc123def45/maxresdefault.jpg" />
        <meta property="og:title" content="Live event title" />
      </head>
      <body>
        <script>
          var ytInitialPlayerResponse = {
            "videoDetails": {
              "title": "Live event title",
              "author": "ESL Counter-Strike",
              "channelId": "channel-esl",
              "shortDescription": "Live description",
              "lengthSeconds": "0",
              "viewCount": "276296",
              "thumbnail": {
                "thumbnails": [
                  {"url": "https://i.ytimg.com/vi/abc123def45/maxresdefault.jpg"}
                ]
              }
            },
            "microformat": {
              "playerMicroformatRenderer": {
                "publishDate": "2026-04-09T04:34:45-07:00"
              }
            }
          };
        </script>
      </body>
    </html>
    """

    class DummyResponse:
        def __init__(self, text: str):
            self.text = text
            self.is_error = False

    async def fake_throttled_get(*args, **kwargs):
        return DummyResponse(html)

    monkeypatch.setattr(sync_service, "throttled_get", fake_throttled_get)

    async def run():
        async with httpx.AsyncClient() as client:
            return await sync_service.fetch_watch_page_candidate(client, "abc123def45", 3)

    result = asyncio.run(run())

    assert result is not None
    assert result["snippet"]["publishedAt"] == "2026-04-09T04:34:45-07:00"


def test_fetch_live_stream_candidates_web_rejects_unverified_watch_page_candidates(monkeypatch):
    class DummyResponse:
        def __init__(self, url: str):
            self.text = ""
            self.is_error = False
            self.url = url

    async def fake_throttled_get(*args, **kwargs):
        return DummyResponse("https://www.youtube.com/watch?v=abc123def45")

    async def fake_fetch_watch_page_candidate(*args, **kwargs):
        return {
            "id": "abc123def45",
            "snippet": {
                "title": "Madagascar Best Moments",
                "channelTitle": "Madagascar Cartoons",
                "channelId": "channel-madagascar",
                "description": None,
                "publishedAt": None,
                "thumbnails": {},
            },
            "statistics": {},
            "_waytube_duration_seconds": None,
            "_waytube_source": "watch-page",
        }

    monkeypatch.setattr(sync_service, "throttled_get", fake_throttled_get)
    monkeypatch.setattr(sync_service, "fetch_watch_page_candidate", fake_fetch_watch_page_candidate)

    async def run():
        async with httpx.AsyncClient() as client:
            return await sync_service.fetch_live_stream_candidates_web(
                client,
                "channel-pgl",
                3,
                channel_name="PGL",
            )

    checked, items = asyncio.run(run())

    assert checked is True
    assert items == []


def test_fetch_live_stream_candidates_web_accepts_matching_channel_title_when_id_is_sparse(monkeypatch):
    class DummyResponse:
        def __init__(self, url: str):
            self.text = ""
            self.is_error = False
            self.url = url

    async def fake_throttled_get(*args, **kwargs):
        return DummyResponse("https://www.youtube.com/watch?v=abc123def45")

    async def fake_fetch_watch_page_candidate(*args, **kwargs):
        return {
            "id": "abc123def45",
            "snippet": {
                "title": "",
                "channelTitle": "PGL",
                "channelId": None,
                "description": None,
                "publishedAt": None,
                "thumbnails": {},
            },
            "statistics": {},
            "_waytube_duration_seconds": None,
            "_waytube_source": "watch-page",
        }

    monkeypatch.setattr(sync_service, "throttled_get", fake_throttled_get)
    monkeypatch.setattr(sync_service, "fetch_watch_page_candidate", fake_fetch_watch_page_candidate)

    async def run():
        async with httpx.AsyncClient() as client:
            return await sync_service.fetch_live_stream_candidates_web(
                client,
                "channel-pgl",
                3,
                channel_name="PGL",
            )

    checked, items = asyncio.run(run())

    assert checked is True
    assert len(items) == 1
    assert items[0]["snippet"]["title"] == "Live stream"
    assert items[0]["snippet"]["channelTitle"] == "PGL"
    assert items[0]["snippet"]["channelId"] == "channel-pgl"
    assert (
        items[0]["snippet"]["thumbnails"]["high"]["url"]
        == "https://i.ytimg.com/vi/abc123def45/hqdefault.jpg"
    )


def test_fetch_live_stream_candidates_web_rejects_variant_channel_title_when_id_is_sparse(monkeypatch):
    class DummyResponse:
        def __init__(self, url: str):
            self.text = ""
            self.is_error = False
            self.url = url

    calls = {"count": 0}

    async def fake_throttled_get(*args, **kwargs):
        del kwargs
        calls["count"] += 1
        if calls["count"] == 1:
            return DummyResponse(args[1])
        return DummyResponse("https://www.youtube.com/watch?v=abc123def45")

    async def fake_fetch_watch_page_candidate(*args, **kwargs):
        return {
            "id": "abc123def45",
            "snippet": {
                "title": "Current stream",
                "channelTitle": "PGL CS2",
                "channelId": None,
                "description": None,
                "publishedAt": None,
                "thumbnails": {},
            },
            "statistics": {},
            "_waytube_duration_seconds": None,
            "_waytube_source": "watch-page",
        }

    monkeypatch.setattr(sync_service, "throttled_get", fake_throttled_get)
    monkeypatch.setattr(sync_service, "fetch_watch_page_candidate", fake_fetch_watch_page_candidate)

    async def run():
        async with httpx.AsyncClient() as client:
            return await sync_service.fetch_live_stream_candidates_web(
                client,
                "channel-pgl",
                3,
                channel_name="PGL",
            )

    checked, items = asyncio.run(run())

    assert checked is True
    assert items == []


def test_fetch_live_stream_candidates_web_preserves_live_renderer_metadata_when_watch_page_is_sparse(monkeypatch):
    html = """
    <html><body><script>
    var ytInitialData = {
      "contents": {
        "twoColumnBrowseResultsRenderer": {
          "tabs": [
            {
              "tabRenderer": {
                "content": {
                  "richGridRenderer": {
                    "contents": [
                      {
                        "richItemRenderer": {
                          "content": {
                            "videoRenderer": {
                              "videoId": "abc123def45",
                              "title": {"runs": [{"text": "PGL Bucharest 2026 - Main Stream"}]},
                              "ownerText": {
                                "runs": [
                                  {
                                    "text": "PGL",
                                    "navigationEndpoint": {
                                      "browseEndpoint": {
                                        "browseId": "channel-pgl"
                                      }
                                    }
                                  }
                                ]
                              },
                              "thumbnailOverlays": [
                                {
                                  "thumbnailOverlayTimeStatusRenderer": {
                                    "style": "LIVE",
                                    "text": {"runs": [{"text": "LIVE"}]}
                                  }
                                }
                              ],
                              "thumbnail": {
                                "thumbnails": [
                                  {"url": "https://i.ytimg.com/vi/abc123def45/hqdefault.jpg"}
                                ]
                              }
                            }
                          }
                        }
                      }
                    ]
                  }
                }
              }
            }
          ]
        }
      }
    };
    </script></body></html>
    """

    class DummyResponse:
        def __init__(self, url: str, text: str):
            self.text = text
            self.is_error = False
            self.url = url

    async def fake_throttled_get(*args, **kwargs):
        del kwargs
        url = args[1]
        return DummyResponse(url, html)

    async def fake_fetch_watch_page_candidate(*args, **kwargs):
        return {
            "id": "abc123def45",
            "snippet": {
                "title": "",
                "channelTitle": None,
                "channelId": None,
                "description": None,
                "publishedAt": None,
                "thumbnails": {},
            },
            "statistics": {},
            "_waytube_duration_seconds": None,
            "_waytube_source": "watch-page",
        }

    monkeypatch.setattr(sync_service, "throttled_get", fake_throttled_get)
    monkeypatch.setattr(sync_service, "fetch_watch_page_candidate", fake_fetch_watch_page_candidate)

    async def run():
        async with httpx.AsyncClient() as client:
            return await sync_service.fetch_live_stream_candidates_web(
                client,
                "channel-pgl",
                3,
                channel_name="PGL",
            )

    checked, items = asyncio.run(run())

    assert checked is True
    assert len(items) == 1
    assert items[0]["snippet"]["title"] == "PGL Bucharest 2026 - Main Stream"
    assert items[0]["snippet"]["channelTitle"] == "PGL"
    assert items[0]["snippet"]["channelId"] == "channel-pgl"


def test_fetch_fallback_candidates_merges_across_query_batches(monkeypatch):
    seen_google_batches: list[list[str]] = []
    seen_web_id_batches: list[list[str]] = []
    seen_web_candidate_batches: list[list[str]] = []

    async def fake_fetch_google_dork_video_ids(client, queries, requests_per_second, status_callback=None):
        del client, requests_per_second, status_callback
        seen_google_batches.append(list(queries))
        if len(seen_google_batches) == 1:
            return ["firstbatch01a"]
        return ["secondbatch1"]

    async def fake_fetch_youtube_web_video_ids(client, queries, requests_per_second, status_callback=None):
        del client, requests_per_second, status_callback
        seen_web_id_batches.append(list(queries))
        return []

    async def fake_fetch_youtube_web_candidates(client, queries, requests_per_second, status_callback=None):
        del client, requests_per_second, status_callback
        seen_web_candidate_batches.append(list(queries))
        return []

    async def fake_fetch_watch_page_candidate(client, youtube_video_id, requests_per_second, status_callback=None):
        del client, requests_per_second, status_callback
        return {
            "id": youtube_video_id,
            "snippet": {
                "title": "Exact title match",
                "channelTitle": "Known Channel",
                "channelId": "channel-known",
                "publishedAt": "2026-04-15T12:00:00Z",
            },
            "statistics": {},
            "_waytube_duration_seconds": 600,
            "_waytube_source": "watch-page",
        }

    monkeypatch.setattr(sync_service, "fetch_google_dork_video_ids", fake_fetch_google_dork_video_ids)
    monkeypatch.setattr(sync_service, "fetch_youtube_web_video_ids", fake_fetch_youtube_web_video_ids)
    monkeypatch.setattr(sync_service, "fetch_youtube_web_candidates", fake_fetch_youtube_web_candidates)
    monkeypatch.setattr(sync_service, "fetch_watch_page_candidate", fake_fetch_watch_page_candidate)

    async def run():
        async with httpx.AsyncClient() as client:
            return await sync_service.fetch_fallback_candidates(
                client,
                ["q1", "q2", "q3", "q4", "q5", "q6"],
                3,
            )

    result = asyncio.run(run())

    assert [item["id"] for item in result] == ["firstbatch01a", "secondbatch1"]
    assert seen_google_batches == [["q1", "q2", "q3", "q4"], ["q5", "q6"]]
    assert seen_web_id_batches == [["q1", "q2", "q3", "q4"], ["q5", "q6"]]
    assert seen_web_candidate_batches == [["q1", "q2", "q3", "q4"], ["q5", "q6"]]


def test_fetch_fallback_candidates_preserves_rich_youtube_web_metadata_for_duplicate_ids(monkeypatch):
    async def fake_fetch_google_dork_video_ids(client, queries, requests_per_second, status_callback=None):
        del client, queries, requests_per_second, status_callback
        return []

    async def fake_fetch_youtube_web_video_ids(client, queries, requests_per_second, status_callback=None):
        del client, queries, requests_per_second, status_callback
        return ["abc123def45"]

    async def fake_fetch_watch_page_candidate(client, youtube_video_id, requests_per_second, status_callback=None):
        del client, youtube_video_id, requests_per_second, status_callback
        return {
            "id": "abc123def45",
            "snippet": {
                "title": "",
                "channelTitle": None,
                "channelId": None,
                "description": None,
                "publishedAt": None,
                "thumbnails": {},
            },
            "statistics": {},
            "_waytube_duration_seconds": 1905,
            "_waytube_source": "watch-page",
        }

    async def fake_fetch_youtube_web_candidates(client, queries, requests_per_second, status_callback=None):
        del client, queries, requests_per_second, status_callback
        return [
            {
                "id": "abc123def45",
                "snippet": {
                    "title": "These body cams are wild",
                    "channelTitle": "Asmongold TV",
                    "channelId": "channel-asmongold",
                    "description": None,
                    "publishedAt": None,
                    "thumbnails": {"high": {"url": "https://i.ytimg.com/vi/abc123def45/hqdefault.jpg"}},
                },
                "statistics": {"viewCount": 1500},
                "_waytube_duration_seconds": 1906,
                "_waytube_source": "youtube-web-search",
            }
        ]

    monkeypatch.setattr(sync_service, "fetch_google_dork_video_ids", fake_fetch_google_dork_video_ids)
    monkeypatch.setattr(sync_service, "fetch_youtube_web_video_ids", fake_fetch_youtube_web_video_ids)
    monkeypatch.setattr(sync_service, "fetch_watch_page_candidate", fake_fetch_watch_page_candidate)
    monkeypatch.setattr(sync_service, "fetch_youtube_web_candidates", fake_fetch_youtube_web_candidates)

    async def run():
        async with httpx.AsyncClient() as client:
            return await sync_service.fetch_fallback_candidates(
                client,
                ["These body cams are wild"],
                3,
            )

    result = asyncio.run(run())

    assert len(result) == 1
    assert result[0]["snippet"]["title"] == "These body cams are wild"
    assert result[0]["snippet"]["channelTitle"] == "Asmongold TV"
    assert result[0]["snippet"]["channelId"] == "channel-asmongold"
    assert result[0]["_waytube_duration_seconds"] == 1905


def test_fetch_fallback_candidates_continues_after_full_first_batch(monkeypatch):
    seen_google_batches: list[list[str]] = []

    async def fake_fetch_google_dork_video_ids(client, queries, requests_per_second, status_callback=None):
        del client, requests_per_second, status_callback
        seen_google_batches.append(list(queries))
        if len(seen_google_batches) == 1:
            return [f"first{index:02d}batch"[:11] for index in range(12)]
        return ["secondbat01"]

    async def fake_fetch_youtube_web_video_ids(client, queries, requests_per_second, status_callback=None):
        del client, queries, requests_per_second, status_callback
        return []

    async def fake_fetch_youtube_web_candidates(client, queries, requests_per_second, status_callback=None):
        del client, queries, requests_per_second, status_callback
        return []

    async def fake_fetch_watch_page_candidate(client, youtube_video_id, requests_per_second, status_callback=None):
        del client, requests_per_second, status_callback
        return {
            "id": youtube_video_id,
            "snippet": {
                "title": f"Candidate {youtube_video_id}",
                "channelTitle": "Known Channel",
                "channelId": "channel-known",
                "publishedAt": "2026-04-15T12:00:00Z",
            },
            "statistics": {},
            "_waytube_duration_seconds": 600,
            "_waytube_source": "watch-page",
        }

    monkeypatch.setattr(sync_service, "fetch_google_dork_video_ids", fake_fetch_google_dork_video_ids)
    monkeypatch.setattr(sync_service, "fetch_youtube_web_video_ids", fake_fetch_youtube_web_video_ids)
    monkeypatch.setattr(sync_service, "fetch_youtube_web_candidates", fake_fetch_youtube_web_candidates)
    monkeypatch.setattr(sync_service, "fetch_watch_page_candidate", fake_fetch_watch_page_candidate)

    async def run():
        async with httpx.AsyncClient() as client:
            return await sync_service.fetch_fallback_candidates(
                client,
                ["q1", "q2", "q3", "q4", "q5", "q6"],
                3,
            )

    result = asyncio.run(run())

    assert [item["id"] for item in result][-1] == "secondbat01"
    assert seen_google_batches == [["q1", "q2", "q3", "q4"], ["q5", "q6"]]


def test_fetch_fallback_candidates_advances_to_next_query_batch_when_first_batch_empty(monkeypatch):
    seen_google_batches: list[list[str]] = []

    async def fake_fetch_google_dork_video_ids(client, queries, requests_per_second, status_callback=None):
        del client, requests_per_second, status_callback
        seen_google_batches.append(list(queries))
        if len(seen_google_batches) == 1:
            return []
        return ["secondbat01"]

    async def fake_fetch_youtube_web_video_ids(client, queries, requests_per_second, status_callback=None):
        del client, requests_per_second, status_callback
        return []

    async def fake_fetch_youtube_web_candidates(client, queries, requests_per_second, status_callback=None):
        del client, queries, requests_per_second, status_callback
        return []

    async def fake_fetch_watch_page_candidate(client, youtube_video_id, requests_per_second, status_callback=None):
        del client, requests_per_second, status_callback
        return {
            "id": youtube_video_id,
            "snippet": {
                "title": "Second batch title",
                "channelTitle": "Known Channel",
                "channelId": "channel-known",
                "publishedAt": "2026-04-15T12:00:00Z",
            },
            "statistics": {},
            "_waytube_duration_seconds": 600,
            "_waytube_source": "watch-page",
        }

    monkeypatch.setattr(sync_service, "fetch_google_dork_video_ids", fake_fetch_google_dork_video_ids)
    monkeypatch.setattr(sync_service, "fetch_youtube_web_video_ids", fake_fetch_youtube_web_video_ids)
    monkeypatch.setattr(sync_service, "fetch_youtube_web_candidates", fake_fetch_youtube_web_candidates)
    monkeypatch.setattr(sync_service, "fetch_watch_page_candidate", fake_fetch_watch_page_candidate)

    async def run():
        async with httpx.AsyncClient() as client:
            return await sync_service.fetch_fallback_candidates(
                client,
                ["q1", "q2", "q3", "q4", "q5", "q6"],
                3,
            )

    result = asyncio.run(run())

    assert [item["id"] for item in result] == ["secondbat01"]
    assert seen_google_batches == [["q1", "q2", "q3", "q4"], ["q5", "q6"]]


def test_fetch_fallback_candidates_merges_watch_page_and_web_candidates(monkeypatch):
    async def fake_fetch_google_dork_video_ids(client, queries, requests_per_second, status_callback=None):
        del client, queries, requests_per_second, status_callback
        return ["watchpage01a"]

    async def fake_fetch_youtube_web_video_ids(client, queries, requests_per_second, status_callback=None):
        del client, queries, requests_per_second, status_callback
        return ["watchpage01a", "watchpage01b"]

    async def fake_fetch_youtube_web_candidates(client, queries, requests_per_second, status_callback=None):
        del client, queries, requests_per_second, status_callback
        return [
            {
                "id": "webcand001a",
                "snippet": {
                    "title": "Web candidate title",
                    "channelTitle": "Known Channel",
                    "channelId": "channel-known",
                    "publishedAt": "2026-04-15T12:00:00Z",
                },
                "statistics": {},
                "_waytube_duration_seconds": 600,
                "_waytube_source": "youtube-web-search",
            }
        ]

    async def fake_fetch_watch_page_candidate(client, youtube_video_id, requests_per_second, status_callback=None):
        del client, requests_per_second, status_callback
        return {
            "id": youtube_video_id,
            "snippet": {
                "title": f"Watch page {youtube_video_id}",
                "channelTitle": "Known Channel",
                "channelId": "channel-known",
                "publishedAt": "2026-04-15T12:00:00Z",
            },
            "statistics": {},
            "_waytube_duration_seconds": 600,
            "_waytube_source": "watch-page",
        }

    monkeypatch.setattr(sync_service, "fetch_google_dork_video_ids", fake_fetch_google_dork_video_ids)
    monkeypatch.setattr(sync_service, "fetch_youtube_web_video_ids", fake_fetch_youtube_web_video_ids)
    monkeypatch.setattr(sync_service, "fetch_youtube_web_candidates", fake_fetch_youtube_web_candidates)
    monkeypatch.setattr(sync_service, "fetch_watch_page_candidate", fake_fetch_watch_page_candidate)

    async def run():
        async with httpx.AsyncClient() as client:
            return await sync_service.fetch_fallback_candidates(
                client,
                ["q1", "q2"],
                3,
            )

    result = asyncio.run(run())

    assert [item["id"] for item in result] == ["watchpage01a", "watchpage01b", "webcand001a"]


def test_resolve_synced_channel_target_ignores_review_rows_when_reusing_channels(tmp_path: Path):
    with make_session(tmp_path) as db:
        review_channel = Channel(name="Shroud", slug="shroud")
        unknown_channel = Channel(name="Unknown Channel", slug="unknown-channel")
        db.add_all([review_channel, unknown_channel])
        db.commit()

        review_video = Video(
            title="Wrong match",
            slug="wrong-match",
            channel_id=review_channel.id,
            duration_seconds=600,
        )
        target_video = Video(
            title="Needs proper channel",
            slug="needs-proper-channel",
            channel_id=unknown_channel.id,
            duration_seconds=600,
        )
        db.add_all([review_video, target_video])
        db.commit()
        db.refresh(review_video)
        db.refresh(target_video)

        db.add(
            YouTubeMatch(
                video_id=review_video.id,
                youtube_video_id="abc123def45",
                youtube_channel_id="channel-asmongold",
                status="review",
                confidence=0.71,
            )
        )
        db.commit()

        resolved = sync_service.resolve_synced_channel_target(
            db,
            target_video,
            "channel-asmongold",
            "Asmongold TV",
        )
        db.flush()

        assert resolved is not None
        assert resolved.slug == "asmongold-tv"
        assert target_video.channel_id == resolved.id


def test_resolve_synced_channel_target_reuses_matching_creator_when_locked_channel_mismatches(tmp_path: Path):
    with make_session(tmp_path) as db:
        frankie_channel = Channel(name="FRANKIEonPCin1080p", slug="frankieonpcin1080p")
        wrong_channel = Channel(name="Bizim Kanal", slug="bizim-kanal")
        db.add_all([frankie_channel, wrong_channel])
        db.commit()
        db.refresh(frankie_channel)
        db.refresh(wrong_channel)

        wrong_video = Video(
            title="Wrong carry-over",
            slug="wrong-carry-over",
            channel_id=wrong_channel.id,
            duration_seconds=600,
        )
        target_video = Video(
            title="LADY BANDITS! - Arma 2: DayZ Mod - Ep.30",
            slug="lady-bandits-locked-channel",
            channel_id=frankie_channel.id,
            duration_seconds=1443,
        )
        db.add_all([wrong_video, target_video])
        db.commit()
        db.refresh(wrong_video)
        db.refresh(target_video)

        db.add(
            YouTubeMatch(
                video_id=wrong_video.id,
                youtube_video_id="bizim12345ab",
                youtube_channel_id="channel-bizim",
                status="matched",
                confidence=0.93,
            )
        )
        db.add(
            YouTubeChannelSnapshot(
                youtube_channel_id="channel-bizim",
                title="Bizim Kanal",
            )
        )
        db.commit()

        resolved = sync_service.resolve_synced_channel_target(
            db,
            target_video,
            "channel-bizim",
            "Bizim Kanal",
        )
        db.flush()

        assert resolved is not None
        assert resolved.id == wrong_channel.id
        assert target_video.channel_id == wrong_channel.id


def test_normalize_channel_assignments_splits_mixed_channel_before_refreshing_snapshot(tmp_path: Path):
    with make_session(tmp_path) as db:
        shroud_channel = Channel(name="shroud", slug="shroud")
        db.add(shroud_channel)
        db.flush()

        shroud_video = Video(
            title="The best pirate game is finally out..",
            slug="best-pirate-game-finally-out",
            channel_id=shroud_channel.id,
            duration_seconds=2 * 3600 + 35 * 60 + 16,
            is_available=True,
        )
        asmongold_video = Video(
            title="This is so f***ing stupid..",
            slug="this-is-so-fing-stupid",
            channel_id=shroud_channel.id,
            duration_seconds=17 * 60 + 52,
            is_available=True,
        )
        db.add_all([shroud_video, asmongold_video])
        db.flush()
        db.add_all(
            [
                YouTubeMatch(
                    video_id=shroud_video.id,
                    youtube_video_id="shroudpirate1",
                    youtube_channel_id="channel-shroud",
                    status="matched",
                    confidence=0.97,
                ),
                YouTubeMatch(
                    video_id=asmongold_video.id,
                    youtube_video_id="asmonstupid1",
                    youtube_channel_id="channel-asmongold",
                    status="matched",
                    confidence=0.97,
                ),
                YouTubeChannelSnapshot(
                    youtube_channel_id="channel-shroud",
                    title="shroud",
                    description="shroud channel",
                ),
                YouTubeChannelSnapshot(
                    youtube_channel_id="channel-asmongold",
                    title="Asmongold TV",
                    description="Asmongold channel",
                ),
            ]
        )
        db.commit()

        sync_service.normalize_channel_assignments(db)

        db.refresh(shroud_video)
        db.refresh(asmongold_video)
        db.refresh(shroud_channel)
        asmongold_channel = db.get(Channel, asmongold_video.channel_id)

        assert shroud_video.channel_id == shroud_channel.id
        assert shroud_channel.name == "shroud"
        assert asmongold_channel is not None
        assert asmongold_channel.id != shroud_channel.id
        assert asmongold_channel.slug == "asmongold-tv"
        assert asmongold_channel.name == "Asmongold TV"
        assert asmongold_channel.description == "Asmongold channel"


def test_apply_sync_item_preserves_existing_snapshot_metadata_when_refresh_is_sparse(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="Unknown Channel", slug="unknown-channel")
        db.add(channel)
        db.commit()
        db.refresh(channel)

        video = Video(
            title="Original title",
            slug="original-title",
            channel_id=channel.id,
            duration_seconds=600,
        )
        db.add(video)
        db.commit()
        db.refresh(video)

        db.add(
            YouTubeMatch(
                video_id=video.id,
                youtube_video_id="abc123def45",
                youtube_channel_id="channel-jre",
                status="matched",
                confidence=0.92,
            )
        )
        db.add(
            YouTubeVideoSnapshot(
                youtube_video_id="abc123def45",
                youtube_channel_id="channel-jre",
                title="Original title",
                description="Original description",
                duration_seconds=600,
                thumbnail_url="https://i.ytimg.com/vi/abc123def45/hqdefault.jpg",
                tags=["podcast"],
                view_count=35000,
                like_count=1200,
            )
        )
        db.add(
            YouTubeChannelSnapshot(
                youtube_channel_id="channel-jre",
                title="PowerfulJRE",
            )
        )
        db.commit()

        async def fake_fetch_return_youtube_dislike_details(*args, **kwargs):
            return None

        async def fake_fetch_channel_about_details(*args, **kwargs):
            return None

        monkeypatch.setattr(sync_service, "fetch_return_youtube_dislike_details", fake_fetch_return_youtube_dislike_details)
        monkeypatch.setattr(sync_service, "fetch_channel_about_details", fake_fetch_channel_about_details)
        monkeypatch.setattr(sync_service, "download_thumbnail", lambda *args, **kwargs: None)

        item = {
            "id": "abc123def45",
            "snippet": {
                "title": "Original title",
                "channelId": None,
                "channelTitle": None,
                "description": None,
                "publishedAt": None,
                "thumbnails": {},
            },
            "statistics": {
                "viewCount": None,
                "likeCount": None,
            },
            "_waytube_duration_seconds": None,
            "_waytube_source": "watch-page",
        }

        async def run():
            async with httpx.AsyncClient() as client:
                return await apply_sync_item(
                    db,
                    video,
                    item,
                    comment_limit=0,
                    requests_per_second=3,
                    client=client,
                    api_key=None,
                )

        result = asyncio.run(run())
        db.refresh(result)
        snapshot = db.scalar(select(YouTubeVideoSnapshot).where(YouTubeVideoSnapshot.youtube_video_id == "abc123def45"))

        assert result.youtube_channel_id == "channel-jre"
        assert snapshot is not None
        assert snapshot.youtube_channel_id == "channel-jre"
        assert snapshot.description == "Original description"
        assert snapshot.thumbnail_url == "https://i.ytimg.com/vi/abc123def45/hqdefault.jpg"
        assert snapshot.tags == ["podcast"]
        assert snapshot.view_count == 35000
        assert snapshot.like_count == 1200


def test_refresh_live_if_due_rechecks_empty_state_quickly(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        settings_row = SyncSettings(
            live_tab_enabled=True,
            last_live_sync_at=datetime.utcnow() - timedelta(seconds=routes_service.LIVE_EMPTY_REFRESH_INTERVAL_SECONDS + 5),
            requests_per_second=3,
        )
        db.add(settings_row)
        db.commit()

        calls: list[bool] = []

        async def fake_refresh_live_streams(*args, **kwargs):
            calls.append(True)
            return []

        monkeypatch.setattr(routes_service, "refresh_live_streams", fake_refresh_live_streams)
        monkeypatch.setattr(routes_service, "_active_youtube_api_key", lambda _db: None)

        asyncio.run(routes_service._refresh_live_if_due(db))

        assert calls == [True]


def test_hydrate_candidate_from_watch_page_preserves_existing_channel_metadata_when_watch_page_is_sparse(monkeypatch):
    async def fake_fetch_watch_page_candidate(*args, **kwargs):
        return {
            "id": "abc123def45",
            "snippet": {
                "title": "Can Jynxzi Find 60 Trash Talking Props? (1v60)",
                "channelTitle": None,
                "channelId": None,
                "description": "Hydrated description",
                "publishedAt": None,
                "thumbnails": {},
            },
            "statistics": {
                "viewCount": None,
                "likeCount": None,
            },
            "_waytube_duration_seconds": None,
            "_waytube_source": "watch-page",
        }

    monkeypatch.setattr(sync_service, "fetch_watch_page_candidate", fake_fetch_watch_page_candidate)

    item = {
        "id": "abc123def45",
        "snippet": {
            "title": "Can Jynxzi Find 60 Trash Talking Props? (1v60)",
            "channelTitle": "Jynxzi",
            "channelId": "channel-jynxzi",
            "description": None,
            "publishedAt": "2026-04-14T12:00:00Z",
            "thumbnails": {"high": {"url": "https://i.ytimg.com/vi/abc123def45/hqdefault.jpg"}},
        },
        "statistics": {
            "viewCount": 14845,
        },
        "_waytube_duration_seconds": 36 * 60 + 39,
        "_waytube_source": "youtube-web-search",
    }

    async def run():
        async with httpx.AsyncClient() as client:
            return await sync_service.hydrate_candidate_from_watch_page(client, item, 3)

    result = asyncio.run(run())

    assert result["snippet"]["channelTitle"] == "Jynxzi"
    assert result["snippet"]["channelId"] == "channel-jynxzi"
    assert result["snippet"]["description"] == "Hydrated description"
    assert result["snippet"]["publishedAt"] == "2026-04-14T12:00:00Z"
    assert result["statistics"]["viewCount"] == 14845
    assert result["_waytube_duration_seconds"] == 36 * 60 + 39


def test_slugify_collapses_apostrophes_without_extra_dash() -> None:
    assert slugify("Moore's Law is Dead") == "moores-law-is-dead"
    assert slugify("Moore’s Law is Dead") == "moores-law-is-dead"


def test_normalize_youtube_api_quota_resets_stale_day(tmp_path: Path) -> None:
    with make_session(tmp_path) as db:
        settings_row = SyncSettings(
            youtube_api_quota_day="2026-04-10",
            youtube_api_quota_used_units=7300,
        )
        db.add(settings_row)
        db.commit()
        db.refresh(settings_row)

        changed = sync_service.normalize_youtube_api_quota(settings_row, now=datetime.fromisoformat("2026-04-11T12:00:00+00:00"))

        assert changed is True
        assert settings_row.youtube_api_quota_day == sync_service.current_youtube_quota_day(datetime.fromisoformat("2026-04-11T12:00:00+00:00"))
        assert settings_row.youtube_api_quota_used_units == 0


def test_build_youtube_api_quota_summary_clamps_remaining_values(tmp_path: Path) -> None:
    with make_session(tmp_path) as db:
        settings_row = SyncSettings(
            youtube_api_quota_day="2026-04-11",
            youtube_api_quota_used_units=12_500,
        )
        db.add(settings_row)
        db.commit()
        db.refresh(settings_row)

        original_current_day = sync_service.current_youtube_quota_day
        sync_service.current_youtube_quota_day = lambda now=None: "2026-04-11"
        try:
            summary = sync_service.build_youtube_api_quota_summary(settings_row)
        finally:
            sync_service.current_youtube_quota_day = original_current_day

        assert summary["youtube_api_quota_daily_limit"] == 10_000
        assert summary["youtube_api_quota_used_units"] == 10_000
        assert summary["youtube_api_quota_remaining_units"] == 0
        assert summary["youtube_api_quota_remaining_percent"] == 0
        assert summary["youtube_api_quota_estimated"] is True


def test_build_youtube_api_quota_summary_treats_stale_day_as_reset(tmp_path: Path) -> None:
    with make_session(tmp_path) as db:
        settings_row = SyncSettings(
            youtube_api_quota_day="2026-04-15",
            youtube_api_quota_used_units=10_000,
        )
        db.add(settings_row)
        db.commit()
        db.refresh(settings_row)

        original_current_day = sync_service.current_youtube_quota_day
        sync_service.current_youtube_quota_day = lambda now=None: "2026-04-16"
        try:
            summary = sync_service.build_youtube_api_quota_summary(settings_row)
        finally:
            sync_service.current_youtube_quota_day = original_current_day

        assert summary["youtube_api_quota_used_units"] == 0
        assert summary["youtube_api_quota_remaining_units"] == 10_000
        assert summary["youtube_api_quota_remaining_percent"] == 100.0


def test_choose_playlist_series_title_prefers_exact_membership_and_non_generic_playlist(tmp_path: Path) -> None:
    with make_session(tmp_path) as db:
        channel = Channel(name="FRANKIEonPCin1080p", slug="frankieonpcin1080p")
        existing_series = Series(name="Arma 2 DayZ Mod", slug="arma-2-dayz-mod")
        db.add_all([channel, existing_series])
        db.flush()

        video = Video(
            title="BATTLE OF THE BRIDGE! - Arma 2: DayZ Mod - Ep 48",
            slug="battle-of-the-bridge",
            channel_id=channel.id,
            series_id=existing_series.id,
            duration_seconds=1200,
            is_available=True,
        )
        db.add(video)
        db.commit()
        db.refresh(video)

        title, position = choose_playlist_series_title(
            video,
            "bridge12345a",
            [
                {
                    "id": "uploads",
                    "title": "Uploads",
                    "positions": {"bridge12345a": 47},
                },
                {
                    "id": "dayz",
                    "title": "DayZ Mod (FRANKIEonPC)",
                    "positions": {"bridge12345a": 47},
                },
            ],
        )

        assert title == "DayZ Mod (FRANKIEonPC)"
        assert position == 47


def test_sync_video_uses_recent_channel_uploads_when_title_search_misses(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="Asmongold TV", slug="asmongold-tv")
        db.add(channel)
        db.flush()

        matched_path = tmp_path / "library" / "asmongold" / "known.mp4"
        matched_path.parent.mkdir(parents=True, exist_ok=True)
        matched_path.write_bytes(b"known")
        matched_video = Video(
            title="Known upload",
            slug="known-upload",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=901,
            published_at=datetime(2026, 4, 6),
            is_available=True,
        )
        db.add(matched_video)
        db.flush()
        db.add(
            VideoFile(
                video_id=matched_video.id,
                absolute_path=str(matched_path),
                relative_path="asmongold/known.mp4",
                file_size=matched_path.stat().st_size,
                fingerprint="a" * 64,
            )
        )
        db.add(
            YouTubeMatch(
                video_id=matched_video.id,
                youtube_video_id="known123",
                youtube_channel_id="channel-asmongold",
                status="matched",
                confidence=1.0,
                reasons=["known-channel"],
            )
        )

        target_path = tmp_path / "library" / "asmongold" / "stale-title.mp4"
        target_path.write_bytes(b"target")
        target_video = Video(
            title="Old launch title before rename",
            slug="old-launch-title-before-rename",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=900,
            published_at=datetime(2026, 4, 6),
            is_available=True,
        )
        db.add(target_video)
        db.flush()
        db.add(
            VideoFile(
                video_id=target_video.id,
                absolute_path=str(target_path),
                relative_path="asmongold/stale-title.mp4",
                file_size=target_path.stat().st_size,
                fingerprint="b" * 64,
            )
        )
        db.commit()

        async def fake_fetch_search_candidates(*args, **kwargs):
            return []

        async def fake_fetch_recent_channel_upload_candidates(*args, **kwargs):
            return [
                {
                    "id": "renamed123",
                    "snippet": {
                        "title": "Asmongold reacts to a completely different title",
                        "channelTitle": "Asmongold TV",
                        "channelId": "channel-asmongold",
                        "publishedAt": "2026-04-06T12:00:00Z",
                    },
                    "statistics": {},
                    "_waytube_duration_seconds": 900,
                    "_waytube_source": "youtube-api-channel-recent",
                }
            ]

        async def fake_fetch_watch_page_candidate(*args, **kwargs):
            return {
                "id": "renamed123",
                "snippet": {
                    "title": "Old launch title before rename",
                    "channelTitle": "Asmongold TV",
                    "channelId": "channel-asmongold",
                    "publishedAt": "2026-04-06T12:00:00Z",
                },
                "statistics": {},
                "_waytube_duration_seconds": 900,
                "_waytube_source": "watch-page",
            }

        async def fail_fetch_channel_candidates(*args, **kwargs):
            raise AssertionError("sync_video should not hit channel lookup when local channel ids already exist")

        async def fake_apply_sync_item(
            db: Session,
            video: Video,
            item: dict,
            **kwargs,
        ) -> YouTubeMatch:
            match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
            if not match:
                match = YouTubeMatch(video_id=video.id)
                db.add(match)
                db.flush()
            match.youtube_video_id = item["id"]
            match.youtube_channel_id = item["snippet"]["channelId"]
            match.status = kwargs["status"]
            match.confidence = kwargs["confidence"]
            match.reasons = kwargs["reasons"]
            db.commit()
            db.refresh(match)
            return match

        monkeypatch.setattr(sync_service, "fetch_search_candidates", fake_fetch_search_candidates)
        monkeypatch.setattr(sync_service, "fetch_recent_channel_upload_candidates", fake_fetch_recent_channel_upload_candidates)
        monkeypatch.setattr(sync_service, "fetch_watch_page_candidate", fake_fetch_watch_page_candidate)
        monkeypatch.setattr(sync_service, "fetch_channel_candidates", fail_fetch_channel_candidates)
        monkeypatch.setattr(sync_service, "apply_sync_item", fake_apply_sync_item)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    target_video,
                    api_key="test-api-key",
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        assert result.status == "matched"
        assert result.youtube_video_id == "renamed123"
        assert result.youtube_channel_id == "channel-asmongold"
        assert result.confidence is not None and result.confidence >= 0.58
        assert "duration-tight" in (result.reasons or [])


def test_sync_video_rejects_weak_neighbor_title_channel_hints_for_orphans_without_api(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="Asmongold TV", slug="asmongold-tv")
        db.add(channel)
        db.flush()

        matched_path = tmp_path / "library" / "known.mp4"
        matched_path.parent.mkdir(parents=True, exist_ok=True)
        matched_path.write_bytes(b"known")
        matched_video = Video(
            title="Britain is cooked",
            slug="britain-is-cooked",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=903,
            published_at=datetime(2026, 4, 6),
            is_available=True,
        )
        db.add(matched_video)
        db.flush()
        db.add(
            VideoFile(
                video_id=matched_video.id,
                absolute_path=str(matched_path),
                relative_path="known.mp4",
                file_size=matched_path.stat().st_size,
                fingerprint="c" * 64,
            )
        )
        db.add(
            YouTubeMatch(
                video_id=matched_video.id,
                youtube_video_id="knownbrit1",
                youtube_channel_id="channel-asmongold",
                status="matched",
                confidence=1.0,
                reasons=["known-channel"],
            )
        )

        target_path = tmp_path / "library" / "orphan.mp4"
        target_path.write_bytes(b"target")
        target_video = Video(
            title="Britain Navy is a joke now",
            slug="britain-navy-is-a-joke-now",
            created_at=datetime.utcnow(),
            duration_seconds=900,
            published_at=datetime(2026, 4, 6),
            is_available=True,
        )
        db.add(target_video)
        db.flush()
        db.add(
            VideoFile(
                video_id=target_video.id,
                absolute_path=str(target_path),
                relative_path="orphan.mp4",
                file_size=target_path.stat().st_size,
                fingerprint="d" * 64,
            )
        )
        db.commit()

        captured_queries: list[str] = []

        async def fake_fetch_fallback_candidates(client, queries, requests_per_second, status_callback=None):
            captured_queries.extend(queries)
            return [
                {
                    "id": "britnavy123",
                    "snippet": {
                        "title": "This is absolutely embarrassing..",
                        "channelTitle": "Asmongold TV",
                        "channelId": "channel-asmongold",
                        "publishedAt": "2026-04-06T12:00:00Z",
                    },
                    "statistics": {},
                    "_waytube_duration_seconds": 900,
                    "_waytube_source": "watch-page",
                }
            ]

        async def fake_apply_sync_item(
            db: Session,
            video: Video,
            item: dict,
            **kwargs,
        ) -> YouTubeMatch:
            match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
            if not match:
                match = YouTubeMatch(video_id=video.id)
                db.add(match)
                db.flush()
            match.youtube_video_id = item["id"]
            match.youtube_channel_id = item["snippet"]["channelId"]
            match.status = kwargs["status"]
            match.confidence = kwargs["confidence"]
            match.reasons = kwargs["reasons"]
            db.commit()
            db.refresh(match)
            return match

        monkeypatch.setattr(sync_service, "fetch_fallback_candidates", fake_fetch_fallback_candidates)
        monkeypatch.setattr(sync_service, "apply_sync_item", fake_apply_sync_item)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    target_video,
                    api_key=None,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        assert result.status == "unmatched"
        assert result.youtube_channel_id is None
        assert captured_queries[0] == "Britain Navy is a joke now"
        assert all("Asmongold TV" not in query for query in captured_queries)


def test_sync_video_skips_generic_channel_name_in_fallback_queries_without_api(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        generic_channel = Channel(name="Offline library", slug="offline-library", inferred_from_path=True)
        db.add(generic_channel)
        db.flush()

        target_path = tmp_path / "library" / "offline.mp4"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"target")
        target_video = Video(
            title="Can Jynxzi Find 60 Trash Talking Props? (1v60)",
            slug="can-jynxzi-find-60-trash-talking-props",
            channel_id=generic_channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=36 * 60 + 39,
            published_at=datetime(2026, 4, 14),
            is_available=True,
        )
        db.add(target_video)
        db.flush()
        db.add(
            VideoFile(
                video_id=target_video.id,
                absolute_path=str(target_path),
                relative_path="offline.mp4",
                file_size=target_path.stat().st_size,
                fingerprint="g" * 64,
            )
        )
        db.commit()

        captured_queries: list[str] = []

        async def fake_fetch_fallback_candidates(client, queries, requests_per_second, status_callback=None):
            del client, requests_per_second, status_callback
            captured_queries.extend(queries)
            return [
                {
                    "id": "abc123def45",
                    "snippet": {
                        "title": "Can Jynxzi Find 60 Trash Talking Props? (1v60)",
                        "channelTitle": "Jynxzi",
                        "channelId": "channel-jynxzi",
                        "publishedAt": "2026-04-14T12:00:00Z",
                    },
                    "statistics": {},
                    "_waytube_duration_seconds": 36 * 60 + 39,
                    "_waytube_source": "watch-page",
                }
            ]

        async def fake_apply_sync_item(
            db: Session,
            video: Video,
            item: dict,
            **kwargs,
        ) -> YouTubeMatch:
            match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
            if not match:
                match = YouTubeMatch(video_id=video.id)
                db.add(match)
                db.flush()
            match.youtube_video_id = item["id"]
            match.youtube_channel_id = item["snippet"]["channelId"]
            match.status = kwargs["status"]
            match.confidence = kwargs["confidence"]
            match.reasons = kwargs["reasons"]
            db.commit()
            db.refresh(match)
            return match

        monkeypatch.setattr(sync_service, "fetch_fallback_candidates", fake_fetch_fallback_candidates)
        monkeypatch.setattr(sync_service, "apply_sync_item", fake_apply_sync_item)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    target_video,
                    api_key=None,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        assert result.status == "matched"
        assert result.youtube_channel_id == "channel-jynxzi"
        assert captured_queries
        assert captured_queries[0] == "Can Jynxzi Find 60 Trash Talking Props? (1v60)"
        assert all("Offline library" not in query for query in captured_queries)


def test_sync_video_prioritizes_known_local_channel_queries_without_api(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="UFD Tech", slug="ufd-tech")
        db.add(channel)
        db.flush()

        target_path = tmp_path / "library" / "ufd.mp4"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"target")
        target_video = Video(
            title="The Nvidia Warranty Situation is Crazy",
            slug="the-nvidia-warranty-situation-is-crazy",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=19 * 60 + 3,
            published_at=datetime(2026, 4, 14),
            is_available=True,
        )
        db.add(target_video)
        db.flush()
        db.add(
            VideoFile(
                video_id=target_video.id,
                absolute_path=str(target_path),
                relative_path="ufd.mp4",
                file_size=target_path.stat().st_size,
                fingerprint="ufdquery" * 8,
            )
        )
        db.commit()

        captured_queries: list[str] = []

        async def fake_fetch_fallback_candidates(client, queries, requests_per_second, status_callback=None):
            del client, requests_per_second, status_callback
            captured_queries.extend(queries)
            return [
                {
                    "id": "ufd123abc45",
                    "snippet": {
                        "title": "The Nvidia Warranty Situation is Crazy",
                        "channelTitle": "UFD Tech",
                        "channelId": "channel-ufd-tech",
                        "publishedAt": "2026-04-14T12:00:00Z",
                    },
                    "statistics": {},
                    "_waytube_duration_seconds": 19 * 60 + 3,
                    "_waytube_source": "watch-page",
                }
            ]

        async def fake_apply_sync_item(
            db: Session,
            video: Video,
            item: dict,
            **kwargs,
        ) -> YouTubeMatch:
            match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
            if not match:
                match = YouTubeMatch(video_id=video.id)
                db.add(match)
                db.flush()
            match.youtube_video_id = item["id"]
            match.youtube_channel_id = item["snippet"]["channelId"]
            match.status = kwargs["status"]
            match.confidence = kwargs["confidence"]
            match.reasons = kwargs["reasons"]
            db.commit()
            db.refresh(match)
            return match

        monkeypatch.setattr(sync_service, "fetch_fallback_candidates", fake_fetch_fallback_candidates)
        monkeypatch.setattr(sync_service, "apply_sync_item", fake_apply_sync_item)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    target_video,
                    api_key=None,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        assert result.status == "matched"
        assert result.youtube_channel_id == "channel-ufd-tech"
        assert captured_queries
        assert captured_queries[0].startswith("UFD Tech ")
        assert "The Nvidia Warranty Situation is Crazy" in captured_queries[0]


def test_sync_video_matches_ohnepixel_video_without_api(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="ohnePixel", slug="ohnepixel")
        db.add(channel)
        db.flush()

        target_path = tmp_path / "library" / "ohnepixel.mp4"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"target")
        target_video = Video(
            title="CS2 Sticker Capsule Opening Goes Wrong",
            slug="cs2-sticker-capsule-opening-goes-wrong",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=17 * 60 + 52,
            published_at=datetime(2026, 4, 14),
            is_available=True,
        )
        db.add(target_video)
        db.flush()
        db.add(
            VideoFile(
                video_id=target_video.id,
                absolute_path=str(target_path),
                relative_path="ohnepixel.mp4",
                file_size=target_path.stat().st_size,
                fingerprint="ohnepixel" * 8,
            )
        )
        db.commit()

        captured_queries: list[str] = []

        async def fake_fetch_fallback_candidates(client, queries, requests_per_second, status_callback=None):
            del client, requests_per_second, status_callback
            captured_queries.extend(queries)
            return [
                {
                    "id": "ohne123abcd",
                    "snippet": {
                        "title": "CS2 Sticker Capsule Opening Goes Wrong",
                        "channelTitle": "ohnePixel",
                        "channelId": "channel-ohnepixel",
                        "publishedAt": "2026-04-14T12:00:00Z",
                    },
                    "statistics": {},
                    "_waytube_duration_seconds": 17 * 60 + 52,
                    "_waytube_source": "watch-page",
                }
            ]

        async def fake_apply_sync_item(
            db: Session,
            video: Video,
            item: dict,
            **kwargs,
        ) -> YouTubeMatch:
            match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
            if not match:
                match = YouTubeMatch(video_id=video.id)
                db.add(match)
                db.flush()
            match.youtube_video_id = item["id"]
            match.youtube_channel_id = item["snippet"]["channelId"]
            match.status = kwargs["status"]
            match.confidence = kwargs["confidence"]
            match.reasons = kwargs["reasons"]
            db.commit()
            db.refresh(match)
            return match

        monkeypatch.setattr(sync_service, "fetch_fallback_candidates", fake_fetch_fallback_candidates)
        monkeypatch.setattr(sync_service, "apply_sync_item", fake_apply_sync_item)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    target_video,
                    api_key=None,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        assert result.status == "matched"
        assert result.youtube_channel_id == "channel-ohnepixel"
        assert captured_queries
        assert captured_queries[0].startswith("ohnePixel ")


def test_sync_video_matches_ohnepixel_raw_video_without_api(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="ohnePixel Raw", slug="ohnepixel-raw")
        db.add(channel)
        db.flush()

        target_path = tmp_path / "library" / "ohnepixel-raw.mp4"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"target")
        target_video = Video(
            title="CS2 Sticker Capsule Opening Goes Wrong",
            slug="cs2-sticker-capsule-opening-goes-wrong-raw",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=17 * 60 + 52,
            published_at=datetime(2026, 4, 14),
            is_available=True,
        )
        db.add(target_video)
        db.flush()
        db.add(
            VideoFile(
                video_id=target_video.id,
                absolute_path=str(target_path),
                relative_path="ohnepixel-raw.mp4",
                file_size=target_path.stat().st_size,
                fingerprint="ohnepixelraw" * 5 + "abcd",
            )
        )
        db.commit()

        captured_queries: list[str] = []

        async def fake_fetch_fallback_candidates(client, queries, requests_per_second, status_callback=None):
            del client, requests_per_second, status_callback
            captured_queries.extend(queries)
            return [
                {
                    "id": "ohne123abcd",
                    "snippet": {
                        "title": "CS2 Sticker Capsule Opening Goes Wrong",
                        "channelTitle": "ohnePixel",
                        "channelId": "channel-ohnepixel",
                        "publishedAt": "2026-04-14T12:00:00Z",
                    },
                    "statistics": {},
                    "_waytube_duration_seconds": 17 * 60 + 52,
                    "_waytube_source": "watch-page",
                }
            ]

        async def fake_apply_sync_item(
            db: Session,
            video: Video,
            item: dict,
            **kwargs,
        ) -> YouTubeMatch:
            match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
            if not match:
                match = YouTubeMatch(video_id=video.id)
                db.add(match)
                db.flush()
            match.youtube_video_id = item["id"]
            match.youtube_channel_id = item["snippet"]["channelId"]
            match.status = kwargs["status"]
            match.confidence = kwargs["confidence"]
            match.reasons = kwargs["reasons"]
            db.commit()
            db.refresh(match)
            return match

        monkeypatch.setattr(sync_service, "fetch_fallback_candidates", fake_fetch_fallback_candidates)
        monkeypatch.setattr(sync_service, "apply_sync_item", fake_apply_sync_item)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    target_video,
                    api_key=None,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        assert result.status == "matched"
        assert result.youtube_channel_id == "channel-ohnepixel"
        assert captured_queries
        assert captured_queries[0].startswith("ohnePixel Raw ")
        assert any(query.startswith("ohnePixel ") for query in captured_queries[1:])


def test_sync_video_accepts_verified_known_channel_candidate_with_sparse_channel_title(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="LVNDMARK", slug="lvndmark")
        db.add(channel)
        db.flush()

        known_path = tmp_path / "library" / "known.mp4"
        target_path = tmp_path / "library" / "target.mp4"
        known_path.parent.mkdir(parents=True, exist_ok=True)
        known_path.write_bytes(b"known")
        target_path.write_bytes(b"target")

        known_video = Video(
            title="Earlier LVNDMARK upload",
            slug="earlier-lvndmark-upload",
            channel_id=channel.id,
            created_at=datetime.utcnow() - timedelta(days=1),
            duration_seconds=1200,
            is_available=True,
        )
        target_video = Video(
            title="SOLO feels Crazy after the AI buff! Gray Zone Warfare",
            slug="solo-feels-crazy-after-the-ai-buff-gray-zone-warfare",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=20 * 60 + 4,
            published_at=datetime(2026, 4, 16),
            is_available=True,
        )
        db.add_all([known_video, target_video])
        db.flush()
        db.add_all(
            [
                VideoFile(
                    video_id=known_video.id,
                    absolute_path=str(known_path),
                    relative_path="known.mp4",
                    file_size=known_path.stat().st_size,
                    fingerprint="k" * 64,
                ),
                VideoFile(
                    video_id=target_video.id,
                    absolute_path=str(target_path),
                    relative_path="target.mp4",
                    file_size=target_path.stat().st_size,
                    fingerprint="t" * 64,
                ),
            ]
        )
        db.add(
            YouTubeMatch(
                video_id=known_video.id,
                youtube_video_id="lvndmark-known-1",
                youtube_channel_id="channel-lvndmark",
                status="matched",
                confidence=0.98,
            )
        )
        db.add(
            YouTubeChannelSnapshot(
                youtube_channel_id="channel-lvndmark",
                title="LVNDMARK",
            )
        )
        db.commit()

        async def fake_fetch_recent_channel_upload_candidates_web(*args, **kwargs):
            return [
                {
                    "id": "lvndmark-target-1",
                    "snippet": {
                        "title": "SOLO feels Crazy after the AI buff! - Gray Zone Warfare",
                        "channelTitle": "",
                        "channelId": "channel-lvndmark",
                        "publishedAt": "2026-04-16T12:00:00Z",
                    },
                    "statistics": {},
                    "_waytube_duration_seconds": 20 * 60 + 4,
                    "_waytube_source": "watch-page",
                }
            ]

        async def fake_fetch_fallback_candidates(*args, **kwargs):
            return []

        async def fake_apply_sync_item(
            db: Session,
            video: Video,
            item: dict,
            **kwargs,
        ) -> YouTubeMatch:
            match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
            if not match:
                match = YouTubeMatch(video_id=video.id)
                db.add(match)
                db.flush()
            match.youtube_video_id = item["id"]
            match.youtube_channel_id = item["snippet"]["channelId"]
            match.status = kwargs["status"]
            match.confidence = kwargs["confidence"]
            match.reasons = kwargs["reasons"]
            db.commit()
            db.refresh(match)
            return match

        monkeypatch.setattr(sync_service, "fetch_recent_channel_upload_candidates_web", fake_fetch_recent_channel_upload_candidates_web)
        monkeypatch.setattr(sync_service, "fetch_fallback_candidates", fake_fetch_fallback_candidates)
        monkeypatch.setattr(sync_service, "apply_sync_item", fake_apply_sync_item)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    target_video,
                    api_key=None,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        assert result.status == "matched"
        assert result.youtube_video_id == "lvndmark-target-1"
        assert result.youtube_channel_id == "channel-lvndmark"
        assert "duration-tight" in (result.reasons or [])


def test_sync_video_accepts_strong_overlap_candidate_for_locked_local_channel_without_authority(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="UFD Tech", slug="ufd-tech")
        db.add(channel)
        db.flush()

        target_path = tmp_path / "library" / "ufd.mp4"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"target")
        target_video = Video(
            title="The Nvidia Warranty Situation is Crazy",
            slug="the-nvidia-warranty-situation-is-crazy",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=19 * 60 + 3,
            published_at=datetime(2026, 4, 16),
            is_available=True,
        )
        db.add(target_video)
        db.flush()
        db.add(
            VideoFile(
                video_id=target_video.id,
                absolute_path=str(target_path),
                relative_path="ufd.mp4",
                file_size=target_path.stat().st_size,
                fingerprint="ufdtech" * 9 + "ab",
            )
        )
        db.commit()

        async def fake_fetch_fallback_candidates(*args, **kwargs):
            return [
                {
                    "id": "ufd-warranty-1",
                    "snippet": {
                        "title": "Nvidia Warranty Situation Is Crazy",
                        "channelTitle": "",
                        "channelId": "channel-ufd-tech",
                        "publishedAt": "2026-04-16T12:00:00Z",
                    },
                    "statistics": {},
                    "_waytube_duration_seconds": 19 * 60 + 3,
                    "_waytube_source": "watch-page",
                }
            ]

        async def fake_apply_sync_item(
            db: Session,
            video: Video,
            item: dict,
            **kwargs,
        ) -> YouTubeMatch:
            match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
            if not match:
                match = YouTubeMatch(video_id=video.id)
                db.add(match)
                db.flush()
            match.youtube_video_id = item["id"]
            match.youtube_channel_id = item["snippet"]["channelId"]
            match.status = kwargs["status"]
            match.confidence = kwargs["confidence"]
            match.reasons = kwargs["reasons"]
            db.commit()
            db.refresh(match)
            return match

        monkeypatch.setattr(sync_service, "fetch_fallback_candidates", fake_fetch_fallback_candidates)
        monkeypatch.setattr(sync_service, "apply_sync_item", fake_apply_sync_item)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    target_video,
                    api_key=None,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        assert result.status == "matched"
        assert result.youtube_video_id == "ufd-warranty-1"
        assert result.youtube_channel_id == "channel-ufd-tech"


def test_sync_video_accepts_censored_title_match_for_unknown_channel_without_api(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        unknown_channel = Channel(name="Unknown Channel", slug="unknown-channel")
        db.add(unknown_channel)
        db.flush()

        target_path = tmp_path / "library" / "asmongold.mp4"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"target")
        target_video = Video(
            title="This is so f***ing stupid..",
            slug="this-is-so-f-ing-stupid",
            channel_id=unknown_channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=22 * 60 + 43,
            is_available=True,
        )
        db.add(target_video)
        db.flush()
        db.add(
            VideoFile(
                video_id=target_video.id,
                absolute_path=str(target_path),
                relative_path="asmongold.mp4",
                file_size=target_path.stat().st_size,
                fingerprint="asmongold" * 7 + "ab",
            )
        )
        db.commit()

        async def fake_fetch_fallback_candidates(*args, **kwargs):
            return [
                {
                    "id": "asmongold-censored-1",
                    "snippet": {
                        "title": "This is so fucking stupid...",
                        "channelTitle": "Asmongold TV",
                        "channelId": "channel-asmongold",
                    },
                    "statistics": {},
                    "_waytube_duration_seconds": 22 * 60 + 43,
                    "_waytube_source": "watch-page",
                }
            ]

        async def fake_apply_sync_item(
            db: Session,
            video: Video,
            item: dict,
            **kwargs,
        ) -> YouTubeMatch:
            match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
            if not match:
                match = YouTubeMatch(video_id=video.id)
                db.add(match)
                db.flush()
            match.youtube_video_id = item["id"]
            match.youtube_channel_id = item["snippet"]["channelId"]
            match.status = kwargs["status"]
            match.confidence = kwargs["confidence"]
            match.reasons = kwargs["reasons"]
            db.commit()
            db.refresh(match)
            return match

        monkeypatch.setattr(sync_service, "fetch_fallback_candidates", fake_fetch_fallback_candidates)
        monkeypatch.setattr(sync_service, "apply_sync_item", fake_apply_sync_item)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    target_video,
                    api_key=None,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        assert result.status == "matched"
        assert result.youtube_video_id == "asmongold-censored-1"
        assert result.youtube_channel_id == "channel-asmongold"
        assert "duration-tight" in (result.reasons or [])
        assert "title-overlap-high" in (result.reasons or [])


def test_sync_video_accepts_overlap_duration_date_candidate_for_unknown_channel_without_api(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        unknown_channel = Channel(name="Unknown Channel", slug="unknown-channel")
        db.add(unknown_channel)
        db.flush()

        target_path = tmp_path / "library" / "bellum.mp4"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"target")
        target_video = Video(
            title="Playing BELLUM for the first time (New MILSIM)",
            slug="playing-bellum-for-the-first-time",
            channel_id=unknown_channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=18 * 60 + 11,
            published_at=datetime(2026, 4, 16),
            is_available=True,
        )
        db.add(target_video)
        db.flush()
        db.add(
            VideoFile(
                video_id=target_video.id,
                absolute_path=str(target_path),
                relative_path="bellum.mp4",
                file_size=target_path.stat().st_size,
                fingerprint="bellummatch" * 6 + "ab",
            )
        )
        db.commit()

        async def fake_fetch_fallback_candidates(*args, **kwargs):
            return [
                {
                    "id": "bellum-match-1",
                    "snippet": {
                        "title": "Playing BELLUM for the very first time - New MILSIM",
                        "channelTitle": "Controlled Pairs Gaming",
                        "channelId": "channel-bellum",
                        "publishedAt": "2026-04-16T15:00:00Z",
                    },
                    "statistics": {},
                    "_waytube_duration_seconds": 18 * 60 + 11,
                    "_waytube_source": "watch-page",
                }
            ]

        async def fake_apply_sync_item(
            db: Session,
            video: Video,
            item: dict,
            **kwargs,
        ) -> YouTubeMatch:
            match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
            if not match:
                match = YouTubeMatch(video_id=video.id)
                db.add(match)
                db.flush()
            match.youtube_video_id = item["id"]
            match.youtube_channel_id = item["snippet"]["channelId"]
            match.status = kwargs["status"]
            match.confidence = kwargs["confidence"]
            match.reasons = kwargs["reasons"]
            db.commit()
            db.refresh(match)
            return match

        monkeypatch.setattr(sync_service, "fetch_fallback_candidates", fake_fetch_fallback_candidates)
        monkeypatch.setattr(sync_service, "apply_sync_item", fake_apply_sync_item)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    target_video,
                    api_key=None,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        assert result.status == "matched"
        assert result.youtube_video_id == "bellum-match-1"
        assert result.youtube_channel_id == "channel-bellum"
        assert "duration-tight" in (result.reasons or [])
        assert "date" in (result.reasons or [])


def test_sync_video_keeps_searching_later_fallback_batches_after_noisy_first_hit(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="Asmongold TV", slug="asmongold-tv")
        db.add(channel)
        db.flush()

        target_path = tmp_path / "library" / "asmon.mp4"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"target")
        target_video = Video(
            title="British Navy is a joke now",
            slug="british-navy-is-a-joke-now-later-batch",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=15 * 60,
            published_at=datetime(2026, 4, 14),
            is_available=True,
        )
        db.add(target_video)
        db.flush()
        db.add(
            VideoFile(
                video_id=target_video.id,
                absolute_path=str(target_path),
                relative_path="asmon.mp4",
                file_size=target_path.stat().st_size,
                fingerprint="laterbatch" * 6 + "ab",
            )
        )
        db.commit()

        seen_google_batches: list[list[str]] = []

        async def fake_fetch_google_dork_video_ids(client, queries, requests_per_second, status_callback=None):
            del client, requests_per_second, status_callback
            seen_google_batches.append(list(queries))
            if len(seen_google_batches) == 1:
                return ["wronghasan01"]
            return ["rightasmon1"]

        async def fake_fetch_youtube_web_video_ids(*args, **kwargs):
            return []

        async def fake_fetch_youtube_web_candidates(*args, **kwargs):
            return []

        async def fake_fetch_watch_page_candidate(client, youtube_video_id, requests_per_second, status_callback=None):
            del client, requests_per_second, status_callback
            if youtube_video_id == "wronghasan01":
                return {
                    "id": "wronghasan01",
                    "snippet": {
                        "title": "Hillary Clinton is so Fucking Stupid",
                        "channelTitle": "HasanAbi",
                        "channelId": "channel-hasan",
                        "publishedAt": "2026-04-14T12:00:00Z",
                    },
                    "statistics": {},
                    "_waytube_duration_seconds": 59 * 60 + 11,
                    "_waytube_source": "watch-page",
                }
            return {
                "id": "rightasmon1",
                "snippet": {
                    "title": "British Navy is a joke now",
                    "channelTitle": "Asmongold TV",
                    "channelId": "channel-asmongold",
                    "publishedAt": "2026-04-14T12:00:00Z",
                },
                "statistics": {},
                "_waytube_duration_seconds": 15 * 60,
                "_waytube_source": "watch-page",
            }

        monkeypatch.setattr(sync_service, "fetch_google_dork_video_ids", fake_fetch_google_dork_video_ids)
        monkeypatch.setattr(sync_service, "fetch_youtube_web_video_ids", fake_fetch_youtube_web_video_ids)
        monkeypatch.setattr(sync_service, "fetch_youtube_web_candidates", fake_fetch_youtube_web_candidates)
        monkeypatch.setattr(sync_service, "fetch_watch_page_candidate", fake_fetch_watch_page_candidate)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    target_video,
                    api_key=None,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        assert result.status == "matched"
        assert result.youtube_video_id == "rightasmon1"
        assert result.youtube_channel_id == "channel-asmongold"
        assert len(seen_google_batches) == 2


def test_sync_video_ignores_generic_channel_bucket_matches_without_api(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        generic_channel = Channel(name="Unknown Channel", slug="unknown-channel")
        db.add(generic_channel)
        db.flush()

        matched_path = tmp_path / "library" / "matched.mp4"
        matched_path.parent.mkdir(parents=True, exist_ok=True)
        matched_path.write_bytes(b"matched")
        matched_video = Video(
            title="Existing matched upload",
            slug="existing-matched-upload",
            channel_id=generic_channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=900,
            published_at=datetime(2026, 4, 14),
            is_available=True,
        )
        db.add(matched_video)
        db.flush()
        db.add(
            VideoFile(
                video_id=matched_video.id,
                absolute_path=str(matched_path),
                relative_path="matched.mp4",
                file_size=matched_path.stat().st_size,
                fingerprint="h" * 64,
            )
        )
        db.add(
            YouTubeMatch(
                video_id=matched_video.id,
                youtube_video_id="knownasm123",
                youtube_channel_id="channel-asmongold",
                status="matched",
                confidence=1.0,
                reasons=["known-channel"],
            )
        )
        db.add(
            YouTubeChannelSnapshot(
                youtube_channel_id="channel-asmongold",
                title="Asmongold TV",
            )
        )

        target_path = tmp_path / "library" / "target.mp4"
        target_path.write_bytes(b"target")
        target_video = Video(
            title="Joe Rogan Experience #2483 - Spencer Pratt",
            slug="joe-rogan-experience-2483-spencer-pratt",
            channel_id=generic_channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=2 * 3600 + 34 * 60 + 23,
            published_at=datetime(2026, 4, 14),
            is_available=True,
        )
        db.add(target_video)
        db.flush()
        db.add(
            VideoFile(
                video_id=target_video.id,
                absolute_path=str(target_path),
                relative_path="target.mp4",
                file_size=target_path.stat().st_size,
                fingerprint="i" * 64,
            )
        )
        db.commit()

        captured_queries: list[str] = []
        recent_channel_lookups: list[list[str]] = []

        async def fake_fetch_recent_channel_upload_candidates_web(
            client,
            channel_ids,
            requests_per_second,
            status_callback=None,
        ):
            del client, requests_per_second, status_callback
            recent_channel_lookups.append(list(channel_ids))
            return []

        async def fake_fetch_fallback_candidates(client, queries, requests_per_second, status_callback=None):
            del client, requests_per_second, status_callback
            captured_queries.extend(queries)
            return [
                {
                    "id": "jre2483abc1",
                    "snippet": {
                        "title": "Joe Rogan Experience #2483 - Spencer Pratt",
                        "channelTitle": "PowerfulJRE",
                        "channelId": "channel-jre",
                        "publishedAt": "2026-04-14T12:00:00Z",
                    },
                    "statistics": {},
                    "_waytube_duration_seconds": 2 * 3600 + 34 * 60 + 23,
                    "_waytube_source": "watch-page",
                }
            ]

        async def fake_apply_sync_item(
            db: Session,
            video: Video,
            item: dict,
            **kwargs,
        ) -> YouTubeMatch:
            match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
            if not match:
                match = YouTubeMatch(video_id=video.id)
                db.add(match)
                db.flush()
            match.youtube_video_id = item["id"]
            match.youtube_channel_id = item["snippet"]["channelId"]
            match.status = kwargs["status"]
            match.confidence = kwargs["confidence"]
            match.reasons = kwargs["reasons"]
            db.commit()
            db.refresh(match)
            return match

        monkeypatch.setattr(sync_service, "fetch_recent_channel_upload_candidates_web", fake_fetch_recent_channel_upload_candidates_web)
        monkeypatch.setattr(sync_service, "fetch_fallback_candidates", fake_fetch_fallback_candidates)
        monkeypatch.setattr(sync_service, "apply_sync_item", fake_apply_sync_item)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    target_video,
                    api_key=None,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        assert result.status == "matched"
        assert result.youtube_channel_id == "channel-jre"
        assert recent_channel_lookups == []
        assert captured_queries
        assert all("Asmongold TV" not in query for query in captured_queries)


def test_sync_video_rejects_weak_neighbor_channel_hint_matches_for_generic_channel(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        generic_channel = Channel(name="Unknown Channel", slug="unknown-channel")
        db.add(generic_channel)
        db.flush()

        target_path = tmp_path / "library" / "target-review.mp4"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"target-review")
        target_video = Video(
            title="Joe Rogan Experience #2483 - Spencer Pratt",
            slug="joe-rogan-experience-2483-spencer-pratt-review",
            channel_id=generic_channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=2 * 3600 + 34 * 60 + 23,
            published_at=datetime(2026, 4, 14),
            is_available=True,
        )
        db.add(target_video)
        db.flush()
        db.add(
            VideoFile(
                video_id=target_video.id,
                absolute_path=str(target_path),
                relative_path="target-review.mp4",
                file_size=target_path.stat().st_size,
                fingerprint="j" * 64,
            )
        )
        db.add(
            YouTubeChannelSnapshot(
                youtube_channel_id="channel-shroud",
                title="shroud",
            )
        )
        db.commit()

        async def fake_fetch_recent_channel_upload_candidates_web(*args, **kwargs):
            return []

        async def fake_fetch_fallback_candidates(*args, **kwargs):
            return [
                {
                    "id": "wrongshroud001",
                    "snippet": {
                        "title": "Unexpected gameplay archive",
                        "channelTitle": "shroud",
                        "channelId": "channel-shroud",
                        "publishedAt": "2026-04-14T12:00:00Z",
                    },
                    "statistics": {},
                    "_waytube_duration_seconds": 2 * 3600 + 34 * 60 + 23,
                    "_waytube_source": "watch-page",
                }
            ]

        monkeypatch.setattr(sync_service, "infer_channel_ids_from_neighbor_titles", lambda *args, **kwargs: ["channel-shroud"])
        monkeypatch.setattr(sync_service, "fetch_recent_channel_upload_candidates_web", fake_fetch_recent_channel_upload_candidates_web)
        monkeypatch.setattr(sync_service, "fetch_fallback_candidates", fake_fetch_fallback_candidates)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    target_video,
                    api_key=None,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        assert result.youtube_video_id is None
        assert result.status == "unmatched"
        assert result.reasons == []


def test_sync_video_rejects_hint_only_candidate_without_authoritative_channel(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        generic_channel = Channel(name="Unknown Channel", slug="unknown-channel")
        db.add(generic_channel)
        db.flush()

        target_path = tmp_path / "library" / "target-hint-review.mp4"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"target-hint-review")
        target_video = Video(
            title="Best pirate game finally released",
            slug="best-pirate-game-finally-released",
            channel_id=generic_channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=2 * 3600 + 35 * 60 + 16,
            published_at=datetime(2026, 4, 14),
            is_available=True,
        )
        db.add(target_video)
        db.flush()
        db.add(
            VideoFile(
                video_id=target_video.id,
                absolute_path=str(target_path),
                relative_path="target-hint-review.mp4",
                file_size=target_path.stat().st_size,
                fingerprint="h" * 64,
            )
        )
        db.add(
            YouTubeChannelSnapshot(
                youtube_channel_id="channel-shroud",
                title="shroud",
            )
        )
        db.commit()

        async def fake_fetch_recent_channel_upload_candidates_web(*args, **kwargs):
            return []

        async def fake_fetch_fallback_candidates(*args, **kwargs):
            return [
                {
                    "id": "shroudpirate1",
                    "snippet": {
                        "title": "The best pirate game is finally out..",
                        "channelTitle": "shroud",
                        "channelId": "channel-shroud",
                        "publishedAt": "2026-04-14T12:00:00Z",
                    },
                    "statistics": {},
                    "_waytube_duration_seconds": 2 * 3600 + 35 * 60 + 16,
                    "_waytube_source": "watch-page",
                }
            ]

        monkeypatch.setattr(sync_service, "infer_channel_ids_from_neighbor_titles", lambda *args, **kwargs: ["channel-shroud"])
        monkeypatch.setattr(sync_service, "fetch_recent_channel_upload_candidates_web", fake_fetch_recent_channel_upload_candidates_web)
        monkeypatch.setattr(sync_service, "fetch_fallback_candidates", fake_fetch_fallback_candidates)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    target_video,
                    api_key=None,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        assert result.youtube_video_id is None
        assert result.status == "unmatched"
        assert result.reasons == []


def test_sync_video_skips_rejected_hint_candidate_and_uses_next_exact_match(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        generic_channel = Channel(name="Unknown Channel", slug="unknown-channel")
        hint_channel = Channel(name="shroud", slug="shroud")
        db.add_all([generic_channel, hint_channel])
        db.flush()

        known_path = tmp_path / "library" / "known-shroud.mp4"
        target_path = tmp_path / "library" / "target-jre.mp4"
        known_path.parent.mkdir(parents=True, exist_ok=True)
        known_path.write_bytes(b"known")
        target_path.write_bytes(b"target")

        known_video = Video(
            title="Previous shroud upload",
            slug="previous-shroud-upload",
            channel_id=hint_channel.id,
            created_at=datetime.utcnow() - timedelta(days=1),
            duration_seconds=1200,
            is_available=True,
        )
        target_video = Video(
            title="Joe Rogan Experience #2483 - Spencer Pratt",
            slug="joe-rogan-experience-2483-spencer-pratt-next-match",
            channel_id=generic_channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=2 * 3600 + 34 * 60 + 23,
            published_at=datetime(2026, 4, 16),
            is_available=True,
        )
        db.add_all([known_video, target_video])
        db.flush()
        db.add_all(
            [
                VideoFile(
                    video_id=known_video.id,
                    absolute_path=str(known_path),
                    relative_path="known-shroud.mp4",
                    file_size=known_path.stat().st_size,
                    fingerprint="shroud" * 9 + "ab",
                ),
                VideoFile(
                    video_id=target_video.id,
                    absolute_path=str(target_path),
                    relative_path="target-jre.mp4",
                    file_size=target_path.stat().st_size,
                    fingerprint="powerfuljre" * 6 + "abcd",
                ),
            ]
        )
        db.add(
            YouTubeMatch(
                video_id=known_video.id,
                youtube_video_id="knownshroud1",
                youtube_channel_id="channel-shroud",
                status="matched",
                confidence=0.98,
            )
        )
        db.add(
            YouTubeChannelSnapshot(
                youtube_channel_id="channel-shroud",
                title="shroud",
            )
        )
        db.commit()

        async def fake_fetch_recent_channel_upload_candidates_web(*args, **kwargs):
            return []

        async def fake_fetch_fallback_candidates(*args, **kwargs):
            return [
                {
                    "id": "wrongshroudhint1",
                    "snippet": {
                        "title": "Joe Rogan #2483 with Spencer Pratt",
                        "channelTitle": "shroud",
                        "channelId": "channel-shroud",
                        "publishedAt": "2026-04-16T12:00:00Z",
                    },
                    "statistics": {},
                    "_waytube_duration_seconds": 2 * 3600 + 34 * 60 + 23,
                    "_waytube_source": "watch-page",
                },
                {
                    "id": "jre2483exact1",
                    "snippet": {
                        "title": "Joe Rogan Experience #2483 - Spencer Pratt",
                        "channelTitle": "PowerfulJRE",
                        "channelId": "channel-jre",
                        "publishedAt": "2026-04-16T12:00:00Z",
                    },
                    "statistics": {},
                    "_waytube_duration_seconds": 2 * 3600 + 34 * 60 + 23,
                    "_waytube_source": "watch-page",
                },
            ]

        async def fake_apply_sync_item(
            db: Session,
            video: Video,
            item: dict,
            **kwargs,
        ) -> YouTubeMatch:
            match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
            if not match:
                match = YouTubeMatch(video_id=video.id)
                db.add(match)
                db.flush()
            match.youtube_video_id = item["id"]
            match.youtube_channel_id = item["snippet"]["channelId"]
            match.status = kwargs["status"]
            match.confidence = kwargs["confidence"]
            match.reasons = kwargs["reasons"]
            db.commit()
            db.refresh(match)
            return match

        monkeypatch.setattr(sync_service, "infer_channel_ids_from_neighbor_titles", lambda *args, **kwargs: ["channel-shroud"])
        monkeypatch.setattr(sync_service, "fetch_recent_channel_upload_candidates_web", fake_fetch_recent_channel_upload_candidates_web)
        monkeypatch.setattr(sync_service, "fetch_fallback_candidates", fake_fetch_fallback_candidates)
        monkeypatch.setattr(sync_service, "apply_sync_item", fake_apply_sync_item)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    target_video,
                    api_key=None,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        assert result.status == "matched"
        assert result.youtube_video_id == "jre2483exact1"
        assert result.youtube_channel_id == "channel-jre"


def test_sync_video_ignores_mismatched_known_channel_ids_without_api(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        local_channel = Channel(name="PGL", slug="pgl")
        db.add(local_channel)
        db.flush()

        matched_path = tmp_path / "library" / "pgl-existing.mp4"
        target_path = tmp_path / "library" / "pgl-target.mp4"
        matched_path.parent.mkdir(parents=True, exist_ok=True)
        matched_path.write_bytes(b"existing")
        target_path.write_bytes(b"target")

        matched_video = Video(
            title="[PGL Bucharest 2026] Quarterfinal Recap",
            slug="pgl-quarterfinal-recap",
            channel_id=local_channel.id,
            created_at=datetime.utcnow() - timedelta(days=1),
            duration_seconds=600,
            is_available=True,
        )
        target_video = Video(
            title="[PGL Bucharest 2026] Wingman Interview with FUT LauNX",
            slug="pgl-wingman-laux",
            channel_id=local_channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=484,
            is_available=True,
        )
        db.add_all([matched_video, target_video])
        db.flush()
        db.add_all(
            [
                VideoFile(
                    video_id=matched_video.id,
                    absolute_path=str(matched_path),
                    relative_path="pgl-existing.mp4",
                    file_size=matched_path.stat().st_size,
                    fingerprint="m" * 64,
                ),
                VideoFile(
                    video_id=target_video.id,
                    absolute_path=str(target_path),
                    relative_path="pgl-target.mp4",
                    file_size=target_path.stat().st_size,
                    fingerprint="n" * 64,
                ),
            ]
        )
        db.add(
            YouTubeMatch(
                video_id=matched_video.id,
                youtube_video_id="esl-wrong-1",
                youtube_channel_id="channel-esl",
                status="matched",
                confidence=0.92,
            )
        )
        db.add(
            YouTubeChannelSnapshot(
                youtube_channel_id="channel-esl",
                title="ESL Counter-Strike",
            )
        )
        db.commit()

        web_recent_called = False
        captured_queries: list[str] = []

        async def fake_fetch_recent_channel_upload_candidates_web(*args, **kwargs):
            nonlocal web_recent_called
            web_recent_called = True
            return []

        async def fake_fetch_fallback_candidates(client, queries, requests_per_second, status_callback=None):
            del client, requests_per_second, status_callback
            captured_queries.extend(queries)
            return [
                {
                    "id": "pgl-correct-1",
                    "snippet": {
                        "title": "[PGL Bucharest 2026] Wingman Interview with FUT LauNX",
                        "channelTitle": "PGL",
                        "channelId": "channel-pgl",
                        "publishedAt": "2026-04-14T12:00:00Z",
                    },
                    "statistics": {},
                    "_waytube_duration_seconds": 484,
                    "_waytube_source": "watch-page",
                }
            ]

        async def fake_apply_sync_item(
            db: Session,
            video: Video,
            item: dict,
            **kwargs,
        ) -> YouTubeMatch:
            match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
            if not match:
                match = YouTubeMatch(video_id=video.id)
                db.add(match)
                db.flush()
            match.youtube_video_id = item["id"]
            match.youtube_channel_id = item["snippet"]["channelId"]
            match.status = kwargs["status"]
            match.confidence = kwargs["confidence"]
            match.reasons = kwargs["reasons"]
            db.commit()
            db.refresh(match)
            return match

        monkeypatch.setattr(sync_service, "fetch_recent_channel_upload_candidates_web", fake_fetch_recent_channel_upload_candidates_web)
        monkeypatch.setattr(sync_service, "fetch_fallback_candidates", fake_fetch_fallback_candidates)
        monkeypatch.setattr(sync_service, "apply_sync_item", fake_apply_sync_item)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    target_video,
                    api_key=None,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        assert web_recent_called is False
        assert result.status == "matched"
        assert result.youtube_channel_id == "channel-pgl"
        assert any("PGL" in query for query in captured_queries)
        assert all("ESL Counter-Strike" not in query for query in captured_queries)


def test_sync_video_uses_series_neighbor_channel_hints_for_new_episode_without_api(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        known_channel = Channel(name="FRANKIEonPCin1080p", slug="frankieonpcin1080p")
        unknown_channel = Channel(name="Unknown Channel", slug="unknown-channel")
        series = Series(name="DayZ Mod (FRANKIEonPC)", slug="dayz-mod-frankieonpc")
        db.add_all([known_channel, unknown_channel, series])
        db.flush()

        matched_path = tmp_path / "library" / "series-known.mp4"
        matched_path.parent.mkdir(parents=True, exist_ok=True)
        matched_path.write_bytes(b"known")
        matched_video = Video(
            title="BATTLE OF THE BRIDGE! - Arma 2: DayZ Mod - Ep 48",
            slug="battle-of-the-bridge-ep-48",
            channel_id=known_channel.id,
            series_id=series.id,
            created_at=datetime.utcnow(),
            duration_seconds=1800,
            published_at=datetime(2026, 4, 11),
            is_available=True,
        )
        db.add(matched_video)
        db.flush()
        db.add(
            VideoFile(
                video_id=matched_video.id,
                absolute_path=str(matched_path),
                relative_path="series-known.mp4",
                file_size=matched_path.stat().st_size,
                fingerprint="e" * 64,
            )
        )
        db.add(
            YouTubeMatch(
                video_id=matched_video.id,
                youtube_video_id="bridge48",
                youtube_channel_id="channel-frankie",
                status="matched",
                confidence=1.0,
                reasons=["known-channel"],
            )
        )

        target_path = tmp_path / "library" / "series-new.mp4"
        target_path.write_bytes(b"target")
        target_video = Video(
            title="DEM DAYZ HACKZ! - Arma 2: DayZ Mod - Ep. 6.5",
            slug="dem-dayz-hackz-ep-6-5",
            channel_id=unknown_channel.id,
            series_id=series.id,
            created_at=datetime.utcnow(),
            duration_seconds=1795,
            published_at=datetime(2026, 4, 11),
            is_available=True,
        )
        db.add(target_video)
        db.flush()
        db.add(
            VideoFile(
                video_id=target_video.id,
                absolute_path=str(target_path),
                relative_path="series-new.mp4",
                file_size=target_path.stat().st_size,
                fingerprint="f" * 64,
            )
        )
        db.commit()

        captured_queries: list[str] = []

        async def fake_fetch_fallback_candidates(client, queries, requests_per_second, status_callback=None):
            captured_queries.extend(queries)
            return [
                {
                    "id": "hackz65",
                    "snippet": {
                        "title": "DEM DAYZ HACKZ! - Arma 2: DayZ Mod - Ep. 6.5",
                        "channelTitle": "FRANKIEonPCin1080p",
                        "channelId": "channel-frankie",
                        "publishedAt": "2026-04-11T12:00:00Z",
                    },
                    "statistics": {},
                    "_waytube_duration_seconds": 1795,
                    "_waytube_source": "watch-page",
                }
            ]

        async def fake_apply_sync_item(
            db: Session,
            video: Video,
            item: dict,
            **kwargs,
        ) -> YouTubeMatch:
            match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
            if not match:
                match = YouTubeMatch(video_id=video.id)
                db.add(match)
                db.flush()
            match.youtube_video_id = item["id"]
            match.youtube_channel_id = item["snippet"]["channelId"]
            match.status = kwargs["status"]
            match.confidence = kwargs["confidence"]
            match.reasons = kwargs["reasons"]
            db.commit()
            db.refresh(match)
            return match

        monkeypatch.setattr(sync_service, "fetch_fallback_candidates", fake_fetch_fallback_candidates)
        monkeypatch.setattr(sync_service, "apply_sync_item", fake_apply_sync_item)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    target_video,
                    api_key=None,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        assert result.status == "matched"
        assert result.youtube_channel_id == "channel-frankie"
        assert any("FRANKIEonPCin1080p" in query for query in captured_queries)


def test_sync_video_rejects_known_channel_mismatch(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="FRANKIEonPCin1080p", slug="frankieonpcin1080p")
        db.add(channel)
        db.flush()

        video_path = tmp_path / "library" / "frankie.mp4"
        video_path.parent.mkdir(parents=True, exist_ok=True)
        video_path.write_bytes(b"target")
        video = Video(
            title="LADY BANDITS! - Arma 2: DayZ Mod - Ep.30",
            slug="lady-bandits-arma-2-dayz-mod-ep-30",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=1443,
            published_at=datetime(2026, 4, 11),
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            VideoFile(
                video_id=video.id,
                absolute_path=str(video_path),
                relative_path="frankie.mp4",
                file_size=video_path.stat().st_size,
                fingerprint="f" * 64,
            )
        )
        db.commit()

        async def fake_fetch_fallback_candidates(*args, **kwargs):
            return [
                {
                    "id": "bizim123",
                    "snippet": {
                        "title": "LADY BANDITS! - Arma 2: DayZ Mod - Ep.30",
                        "channelTitle": "Bizim Kanal",
                        "channelId": "channel-bizim",
                        "publishedAt": "2026-04-11T12:00:00Z",
                    },
                    "statistics": {},
                    "_waytube_duration_seconds": 1443,
                    "_waytube_source": "watch-page",
                }
            ]

        monkeypatch.setattr(sync_service, "fetch_fallback_candidates", fake_fetch_fallback_candidates)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    video,
                    api_key=None,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        assert result.status == "unmatched"
        assert result.youtube_video_id is None
        assert result.youtube_channel_id is None
        assert result.reasons == []


def test_sync_video_ignores_review_rejections_during_automatic_matching(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="Asmongold TV", slug="asmongold-tv")
        db.add(channel)
        db.flush()

        video_path = tmp_path / "library" / "auto-ignore-review.mp4"
        video_path.parent.mkdir(parents=True, exist_ok=True)
        video_path.write_bytes(b"target")
        video = Video(
            title="Thank GOD we have body cams..",
            slug="thank-god-we-have-body-cams",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=845,
            published_at=datetime(2026, 4, 16),
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            VideoFile(
                video_id=video.id,
                absolute_path=str(video_path),
                relative_path="auto-ignore-review.mp4",
                file_size=video_path.stat().st_size,
                fingerprint="r" * 64,
            )
        )
        db.add(
            YouTubeMatch(
                video_id=video.id,
                youtube_video_id=None,
                youtube_channel_id=None,
                status="review",
                confidence=0.0,
                rejected_youtube_video_ids=["asmongold-bodycam-1"],
            )
        )
        db.commit()

        async def fake_fetch_recent_channel_upload_candidates_web(*args, **kwargs):
            return []

        async def fake_fetch_fallback_candidates(*args, **kwargs):
            return [
                {
                    "id": "asmongold-bodycam-1",
                    "snippet": {
                        "title": "Thank GOD we have body cams..",
                        "channelTitle": "Asmongold TV",
                        "channelId": "channel-asmongold",
                        "publishedAt": "2026-04-16T12:00:00Z",
                    },
                    "statistics": {},
                    "_waytube_duration_seconds": 845,
                    "_waytube_source": "watch-page",
                }
            ]

        async def fake_apply_sync_item(
            db: Session,
            video: Video,
            item: dict,
            **kwargs,
        ) -> YouTubeMatch:
            match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
            assert match is not None
            match.youtube_video_id = item["id"]
            match.youtube_channel_id = item["snippet"]["channelId"]
            match.status = kwargs["status"]
            match.confidence = kwargs["confidence"]
            match.reasons = kwargs["reasons"]
            match.rejected_youtube_video_ids = []
            db.commit()
            db.refresh(match)
            return match

        monkeypatch.setattr(sync_service, "fetch_recent_channel_upload_candidates_web", fake_fetch_recent_channel_upload_candidates_web)
        monkeypatch.setattr(sync_service, "fetch_fallback_candidates", fake_fetch_fallback_candidates)
        monkeypatch.setattr(sync_service, "apply_sync_item", fake_apply_sync_item)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    video,
                    api_key=None,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        assert result.status == "matched"
        assert result.youtube_video_id == "asmongold-bodycam-1"
        assert result.youtube_channel_id == "channel-asmongold"
        assert result.rejected_youtube_video_ids == []


def test_sync_video_prefers_fallback_match_before_api_search(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="FRANKIEonPCin1080p", slug="frankieonpcin1080p")
        db.add(channel)
        db.flush()

        video_path = tmp_path / "library" / "frankie-fallback.mp4"
        video_path.parent.mkdir(parents=True, exist_ok=True)
        video_path.write_bytes(b"target")
        video = Video(
            title="LADY BANDITS! - Arma 2: DayZ Mod - Ep.30",
            slug="lady-bandits-arma-2-dayz-mod-ep-30-fallback",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=1443,
            published_at=datetime(2026, 4, 11),
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            VideoFile(
                video_id=video.id,
                absolute_path=str(video_path),
                relative_path="frankie-fallback.mp4",
                file_size=video_path.stat().st_size,
                fingerprint="1" * 64,
            )
        )
        db.commit()

        async def fail_fetch_search_candidates(*args, **kwargs):
            raise AssertionError("video API search should not run when fallback already matched confidently")

        async def fail_fetch_channel_candidates(*args, **kwargs):
            raise AssertionError("channel API lookup should not run when fallback already matched confidently")

        async def fake_fetch_fallback_candidates(*args, **kwargs):
            return [
                {
                    "id": "frankie123",
                    "snippet": {
                        "title": "LADY BANDITS! - Arma 2: DayZ Mod - Ep.30",
                        "channelTitle": "FRANKIEonPCin1080p",
                        "channelId": "channel-frankie",
                        "publishedAt": "2026-04-11T12:00:00Z",
                    },
                    "statistics": {},
                    "_waytube_duration_seconds": 1443,
                    "_waytube_source": "watch-page",
                }
            ]

        async def fake_apply_sync_item(
            db: Session,
            video: Video,
            item: dict,
            **kwargs,
        ) -> YouTubeMatch:
            assert kwargs["api_key"] == "test-api-key"
            match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
            if not match:
                match = YouTubeMatch(video_id=video.id)
                db.add(match)
                db.flush()
            match.youtube_video_id = item["id"]
            match.youtube_channel_id = item["snippet"]["channelId"]
            match.status = kwargs["status"]
            match.confidence = kwargs["confidence"]
            match.reasons = kwargs["reasons"]
            db.commit()
            db.refresh(match)
            return match

        monkeypatch.setattr(sync_service, "fetch_channel_candidates", fail_fetch_channel_candidates)
        monkeypatch.setattr(sync_service, "fetch_search_candidates", fail_fetch_search_candidates)
        monkeypatch.setattr(sync_service, "fetch_fallback_candidates", fake_fetch_fallback_candidates)
        monkeypatch.setattr(sync_service, "apply_sync_item", fake_apply_sync_item)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    video,
                    api_key="test-api-key",
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        assert result.status == "matched"
        assert result.youtube_video_id == "frankie123"
        assert result.youtube_channel_id == "channel-frankie"


def test_sync_video_uses_channel_lookup_ids_and_broadens_api_search(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="PowerfulJRE", slug="powerfuljre")
        db.add(channel)
        db.flush()

        target_path = tmp_path / "library" / "jre.mp4"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"target")
        video = Video(
            title="Joe Rogan Experience #2483 - Spencer Pratt",
            slug="joe-rogan-experience-2483-spencer-pratt",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=2 * 3600 + 34 * 60 + 23,
            published_at=datetime(2026, 4, 14),
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            VideoFile(
                video_id=video.id,
                absolute_path=str(target_path),
                relative_path="jre.mp4",
                file_size=target_path.stat().st_size,
                fingerprint="lookup" * 10 + "12",
            )
        )
        db.commit()

        seen_channel_ids: list[list[str] | None] = []

        async def fake_fetch_channel_candidates(*args, **kwargs):
            return [
                {
                    "id": {"channelId": "channel-jre"},
                    "snippet": {"channelTitle": "PowerfulJRE"},
                }
            ]

        async def fake_fetch_search_candidates(
            client,
            api_key,
            queries,
            requests_per_second,
            channel_ids=None,
            status_callback=None,
        ):
            del client, api_key, queries, requests_per_second, status_callback
            seen_channel_ids.append(list(channel_ids) if channel_ids else None)
            if channel_ids:
                return [
                    {
                        "id": "clips2483ab",
                        "snippet": {
                            "title": "Spencer Pratt on Joe Rogan clips",
                            "channelTitle": "PowerfulJRE",
                            "channelId": "channel-jre",
                            "publishedAt": "2026-04-14T12:00:00Z",
                        },
                        "statistics": {},
                        "_waytube_duration_seconds": 9 * 60,
                        "_waytube_source": "youtube-api-search",
                    }
                ]
            return [
                {
                    "id": "jre2483abc1",
                    "snippet": {
                        "title": "Joe Rogan Experience #2483 - Spencer Pratt",
                        "channelTitle": "PowerfulJRE",
                        "channelId": "channel-jre",
                        "publishedAt": "2026-04-14T12:00:00Z",
                    },
                    "statistics": {},
                    "_waytube_duration_seconds": 2 * 3600 + 34 * 60 + 23,
                    "_waytube_source": "watch-page",
                }
            ]

        async def fake_fetch_recent_channel_upload_candidates(*args, **kwargs):
            return []

        async def fake_apply_sync_item(
            db: Session,
            video: Video,
            item: dict,
            **kwargs,
        ) -> YouTubeMatch:
            match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
            if not match:
                match = YouTubeMatch(video_id=video.id)
                db.add(match)
                db.flush()
            match.youtube_video_id = item["id"]
            match.youtube_channel_id = item["snippet"]["channelId"]
            match.status = kwargs["status"]
            match.confidence = kwargs["confidence"]
            match.reasons = kwargs["reasons"]
            db.commit()
            db.refresh(match)
            return match

        monkeypatch.setattr(sync_service, "fetch_channel_candidates", fake_fetch_channel_candidates)
        monkeypatch.setattr(sync_service, "fetch_search_candidates", fake_fetch_search_candidates)
        monkeypatch.setattr(sync_service, "fetch_recent_channel_upload_candidates", fake_fetch_recent_channel_upload_candidates)
        monkeypatch.setattr(sync_service, "apply_sync_item", fake_apply_sync_item)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    video,
                    api_key="test-api-key",
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        assert seen_channel_ids[0] == ["channel-jre"]
        assert seen_channel_ids[1] is None
        assert result.status == "matched"
        assert result.youtube_video_id == "jre2483abc1"
        assert result.youtube_channel_id == "channel-jre"


def test_sync_video_prefers_fallback_before_api_search_and_channel_lookup(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="UFD Tech", slug="ufd-tech")
        db.add(channel)
        db.flush()

        target_path = tmp_path / "library" / "ufd-fallback-first.mp4"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"target")
        video = Video(
            title="The Nvidia Warranty Situation is Crazy",
            slug="the-nvidia-warranty-situation-is-crazy-fallback-first",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=19 * 60 + 3,
            published_at=datetime(2026, 4, 14),
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            VideoFile(
                video_id=video.id,
                absolute_path=str(target_path),
                relative_path="ufd-fallback-first.mp4",
                file_size=target_path.stat().st_size,
                fingerprint="ufd-fallback-first" * 4,
            )
        )
        db.commit()

        async def fake_fetch_fallback_candidates(*args, **kwargs):
            return [
                {
                    "id": "ufd123abc45",
                    "snippet": {
                        "title": "The Nvidia Warranty Situation is Crazy",
                        "channelTitle": "UFD Tech",
                        "channelId": "channel-ufd-tech",
                        "publishedAt": "2026-04-14T12:00:00Z",
                    },
                    "statistics": {},
                    "_waytube_duration_seconds": 19 * 60 + 3,
                    "_waytube_source": "watch-page",
                }
            ]

        async def fail_fetch_channel_candidates(*args, **kwargs):
            raise AssertionError("channel API lookup should not run when fallback already matched confidently")

        async def fail_fetch_search_candidates(*args, **kwargs):
            raise AssertionError("video API search should not run when fallback already matched confidently")

        async def fake_apply_sync_item(
            db: Session,
            video: Video,
            item: dict,
            **kwargs,
        ) -> YouTubeMatch:
            match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
            if not match:
                match = YouTubeMatch(video_id=video.id)
                db.add(match)
                db.flush()
            match.youtube_video_id = item["id"]
            match.youtube_channel_id = item["snippet"]["channelId"]
            match.status = kwargs["status"]
            match.confidence = kwargs["confidence"]
            match.reasons = kwargs["reasons"]
            db.commit()
            db.refresh(match)
            return match

        monkeypatch.setattr(sync_service, "fetch_fallback_candidates", fake_fetch_fallback_candidates)
        monkeypatch.setattr(sync_service, "fetch_channel_candidates", fail_fetch_channel_candidates)
        monkeypatch.setattr(sync_service, "fetch_search_candidates", fail_fetch_search_candidates)
        monkeypatch.setattr(sync_service, "apply_sync_item", fake_apply_sync_item)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    video,
                    api_key="test-api-key",
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                    channel_cache={},
                    playlist_cache={},
                )

        result = asyncio.run(run())

        assert result.status == "matched"
        assert result.youtube_video_id == "ufd123abc45"
        assert result.youtube_channel_id == "channel-ufd-tech"


def test_sync_video_uses_api_by_id_to_fill_missing_fallback_stats(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="UFD Tech", slug="ufd-tech")
        db.add(channel)
        db.flush()

        target_path = tmp_path / "library" / "ufd-gap-fill.mp4"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"target")
        video = Video(
            title="The Nvidia Warranty Situation is Crazy",
            slug="the-nvidia-warranty-situation-is-crazy-gap-fill",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=19 * 60 + 3,
            published_at=datetime(2026, 4, 14),
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            VideoFile(
                video_id=video.id,
                absolute_path=str(target_path),
                relative_path="ufd-gap-fill.mp4",
                file_size=target_path.stat().st_size,
                fingerprint="ufd-gap-fill" * 5,
            )
        )
        db.commit()

        async def fake_fetch_fallback_candidates(*args, **kwargs):
            return [
                {
                    "id": "ufd123abc45",
                    "snippet": {
                        "title": "The Nvidia Warranty Situation is Crazy",
                        "channelTitle": "UFD Tech",
                        "channelId": "channel-ufd-tech",
                        "publishedAt": "2026-04-14T12:00:00Z",
                    },
                    "statistics": {},
                    "_waytube_duration_seconds": 19 * 60 + 3,
                    "_waytube_source": "watch-page",
                }
            ]

        async def fail_fetch_channel_candidates(*args, **kwargs):
            raise AssertionError("channel API lookup should not run when fallback already matched confidently")

        async def fail_fetch_search_candidates(*args, **kwargs):
            raise AssertionError("video API search should not run when fallback already matched confidently")

        async def fake_fetch_video_details_by_id(*args, **kwargs):
            return {
                "id": "ufd123abc45",
                "snippet": {
                    "title": "The Nvidia Warranty Situation is Crazy",
                    "channelTitle": "UFD Tech",
                    "channelId": "channel-ufd-tech",
                    "publishedAt": "2026-04-14T12:00:00Z",
                    "description": "Gap-filled description",
                    "thumbnails": {},
                },
                "statistics": {
                    "viewCount": "4500",
                    "likeCount": "321",
                },
                "_waytube_duration_seconds": 19 * 60 + 3,
                "_waytube_source": "youtube-api",
            }

        async def fake_apply_sync_item(
            db: Session,
            video: Video,
            item: dict,
            **kwargs,
        ) -> YouTubeMatch:
            assert item["statistics"]["viewCount"] == "4500"
            assert item["statistics"]["likeCount"] == "321"
            assert item["snippet"]["description"] == "Gap-filled description"
            match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
            if not match:
                match = YouTubeMatch(video_id=video.id)
                db.add(match)
                db.flush()
            match.youtube_video_id = item["id"]
            match.youtube_channel_id = item["snippet"]["channelId"]
            match.status = kwargs["status"]
            match.confidence = kwargs["confidence"]
            match.reasons = kwargs["reasons"]
            db.commit()
            db.refresh(match)
            return match

        monkeypatch.setattr(sync_service, "fetch_fallback_candidates", fake_fetch_fallback_candidates)
        monkeypatch.setattr(sync_service, "fetch_channel_candidates", fail_fetch_channel_candidates)
        monkeypatch.setattr(sync_service, "fetch_search_candidates", fail_fetch_search_candidates)
        monkeypatch.setattr(sync_service, "fetch_video_details_by_id", fake_fetch_video_details_by_id)
        monkeypatch.setattr(sync_service, "apply_sync_item", fake_apply_sync_item)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    video,
                    api_key="test-api-key",
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                    channel_cache={},
                    playlist_cache={},
                )

        result = asyncio.run(run())

        assert result.status == "matched"
        assert result.youtube_video_id == "ufd123abc45"
        assert result.youtube_channel_id == "channel-ufd-tech"


def test_sync_video_defers_api_search_for_recent_unmatched_video(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="Made The Cut", slug="made-the-cut")
        db.add(channel)
        db.flush()

        target_path = tmp_path / "library" / "made-the-cut-unmatched.mp4"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"target")
        video = Video(
            title="Made The Cut Yankees Update",
            slug="made-the-cut-yankees-update",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=11 * 60,
            published_at=datetime(2026, 4, 17),
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            VideoFile(
                video_id=video.id,
                absolute_path=str(target_path),
                relative_path="made-the-cut-unmatched.mp4",
                file_size=target_path.stat().st_size,
                fingerprint="made-the-cut-unmatched" * 3,
            )
        )
        db.add(
            YouTubeMatch(
                video_id=video.id,
                status="unmatched",
                confidence=0.0,
                reasons=[],
                stale=True,
                last_synced_at=datetime.utcnow(),
            )
        )
        db.commit()

        async def fake_fetch_fallback_candidates(*args, **kwargs):
            return []

        async def fail_fetch_channel_candidates(*args, **kwargs):
            raise AssertionError("channel API lookup should not run for recent unmatched retries")

        async def fail_fetch_search_candidates(*args, **kwargs):
            raise AssertionError("video API search should not run for recent unmatched retries")

        monkeypatch.setattr(sync_service, "fetch_fallback_candidates", fake_fetch_fallback_candidates)
        monkeypatch.setattr(sync_service, "fetch_channel_candidates", fail_fetch_channel_candidates)
        monkeypatch.setattr(sync_service, "fetch_search_candidates", fail_fetch_search_candidates)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    video,
                    api_key="test-api-key",
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        assert result.status == "unmatched"


def test_sync_scope_skips_recent_unmatched_auto_retry_without_api_discovery(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="Made The Cut", slug="made-the-cut")
        db.add(channel)
        db.flush()

        target_path = tmp_path / "library" / "made-the-cut-auto-retry.mp4"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"target")
        video = Video(
            title="Made The Cut Yankees Update",
            slug="made-the-cut-yankees-update-auto-retry",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=11 * 60,
            published_at=datetime(2026, 4, 17),
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            VideoFile(
                video_id=video.id,
                absolute_path=str(target_path),
                relative_path="made-the-cut-auto-retry.mp4",
                file_size=target_path.stat().st_size,
                fingerprint="made-the-cut-auto-retry" * 3,
            )
        )
        db.add(
            YouTubeMatch(
                video_id=video.id,
                status="unmatched",
                confidence=0.0,
                reasons=[],
                stale=True,
                last_synced_at=datetime.utcnow(),
            )
        )
        db.commit()

        called_video_ids: list[int] = []

        async def fake_sync_video(db, video, **kwargs):
            del db, kwargs
            called_video_ids.append(video.id)
            return YouTubeMatch(video_id=video.id, status="unmatched")

        monkeypatch.setattr(sync_service, "sync_video", fake_sync_video)

        asyncio.run(
            sync_scope(
                db,
                scope="library",
                target_id=None,
                api_key="test-api-key",
                allow_api_discovery=False,
            )
        )

        assert called_video_ids == []


def test_sync_video_uses_web_recent_channel_uploads_without_api_for_known_channel(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="FRANKIEonPCin1080p", slug="frankieonpcin1080p")
        db.add(channel)
        db.flush()

        existing_path = tmp_path / "library" / "frankie-existing.mp4"
        target_path = tmp_path / "library" / "frankie-new.mp4"
        existing_path.parent.mkdir(parents=True, exist_ok=True)
        existing_path.write_bytes(b"existing")
        target_path.write_bytes(b"target")

        existing_video = Video(
            title="CHERNO JOURNEY! - Arma 2: DayZ Mod - Ep.29",
            slug="cherno-journey-ep-29",
            channel_id=channel.id,
            created_at=datetime.utcnow() - timedelta(days=1),
            duration_seconds=1400,
            published_at=datetime(2026, 4, 10),
            is_available=True,
        )
        target_video = Video(
            title="LADY BANDITS! - Arma 2: DayZ Mod - Ep.30",
            slug="lady-bandits-ep-30",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=1443,
            published_at=datetime(2026, 4, 11),
            is_available=True,
        )
        db.add_all([existing_video, target_video])
        db.flush()
        db.add_all(
            [
                VideoFile(
                    video_id=existing_video.id,
                    absolute_path=str(existing_path),
                    relative_path="frankie-existing.mp4",
                    file_size=existing_path.stat().st_size,
                    fingerprint="a" * 64,
                ),
                VideoFile(
                    video_id=target_video.id,
                    absolute_path=str(target_path),
                    relative_path="frankie-new.mp4",
                    file_size=target_path.stat().st_size,
                    fingerprint="b" * 64,
                ),
            ]
        )
        db.add(
            YouTubeMatch(
                video_id=existing_video.id,
                youtube_video_id="frankie-old-1",
                youtube_channel_id="channel-frankie",
                status="matched",
                confidence=0.95,
            )
        )
        db.add(
            YouTubeChannelSnapshot(
                youtube_channel_id="channel-frankie",
                title="FRANKIEonPCin1080p",
            )
        )
        db.commit()

        web_recent_called = False

        async def fake_fetch_recent_channel_upload_candidates_web(*args, **kwargs):
            nonlocal web_recent_called
            web_recent_called = True
            return [
                {
                    "id": "frankie-new-1",
                    "snippet": {
                        "title": "LADY BANDITS! - Arma 2: DayZ Mod - Ep.30",
                        "channelTitle": "FRANKIEonPCin1080p",
                        "channelId": "channel-frankie",
                        "publishedAt": "2026-04-11T12:00:00Z",
                    },
                    "statistics": {},
                    "_waytube_duration_seconds": 1443,
                    "_waytube_source": "watch-page",
                }
            ]

        async def fake_fetch_fallback_candidates(*args, **kwargs):
            return []

        async def fake_apply_sync_item(
            db: Session,
            video: Video,
            item: dict,
            **kwargs,
        ) -> YouTubeMatch:
            assert kwargs["api_key"] is None
            match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
            if not match:
                match = YouTubeMatch(video_id=video.id)
                db.add(match)
                db.flush()
            match.youtube_video_id = item["id"]
            match.youtube_channel_id = item["snippet"]["channelId"]
            match.status = kwargs["status"]
            match.confidence = kwargs["confidence"]
            match.reasons = kwargs["reasons"]
            db.commit()
            db.refresh(match)
            return match

        monkeypatch.setattr(sync_service, "fetch_recent_channel_upload_candidates_web", fake_fetch_recent_channel_upload_candidates_web)
        monkeypatch.setattr(sync_service, "fetch_fallback_candidates", fake_fetch_fallback_candidates)
        monkeypatch.setattr(sync_service, "apply_sync_item", fake_apply_sync_item)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    target_video,
                    api_key=None,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        assert web_recent_called is True
        assert result.status == "matched"
        assert result.youtube_video_id == "frankie-new-1"
        assert result.youtube_channel_id == "channel-frankie"


def test_matched_youtube_channels_by_local_channel_skips_name_mismatches(tmp_path: Path):
    with make_session(tmp_path) as db:
        channel = Channel(name="PGL", slug="pgl")
        db.add(channel)
        db.flush()

        db.add(
            YouTubeChannelSnapshot(
                youtube_channel_id="channel-esl",
                title="ESL Counter-Strike",
            )
        )

        for index in range(2):
            video = Video(
                title=f"PGL upload {index}",
                slug=f"pgl-upload-{index}",
                channel_id=channel.id,
                created_at=datetime.utcnow() - timedelta(minutes=index),
                duration_seconds=300,
                is_available=True,
            )
            db.add(video)
            db.flush()
            db.add(
                YouTubeMatch(
                    video_id=video.id,
                    youtube_video_id=f"wrong-esl-{index}",
                    youtube_channel_id="channel-esl",
                    status="matched",
                    confidence=0.9,
                )
            )
        db.commit()

        assert sync_service.matched_youtube_channels_by_local_channel(db) == {}


def test_channel_names_confidently_match_rejects_clip_variants():
    assert sync_service.channel_names_confidently_match("Austin Evans", "Austin Evans") is True
    assert sync_service.channel_names_confidently_match("Asmongold TV", "Asmongold") is True
    assert sync_service.channel_names_confidently_match("Asmongold", "Asmongold TV") is True
    assert sync_service.channel_names_confidently_match("ohnePixel Raw", "ohnePixel") is True
    assert sync_service.channel_names_confidently_match("ohnePixel", "ohnePixel Raw") is False
    assert sync_service.channel_names_confidently_match("Austin Evans", "Austin Evans Clips") is False
    assert sync_service.channel_names_confidently_match("ohnePixel", "ohnePixel Clips") is False
    assert sync_service.channel_names_confidently_match("PGL", "PGL CS2") is False


def test_channel_names_search_match_allows_safe_variants_but_rejects_clips():
    assert sync_service.channel_names_search_match("Asmongold", "Asmongold TV") is True
    assert sync_service.channel_names_search_match("PGL", "PGL CS2") is True
    assert sync_service.channel_names_search_match("ohnePixel Raw", "ohnePixel") is True
    assert sync_service.channel_names_search_match("Austin Evans", "Austin Evans Clips") is False
    assert sync_service.channel_names_search_match("ohnePixel", "ohnePixel Highlights") is False


def test_matched_youtube_channels_by_local_channel_skips_clip_variants(tmp_path: Path):
    with make_session(tmp_path) as db:
        channel = Channel(name="Austin Evans", slug="austin-evans")
        db.add(channel)
        db.flush()

        db.add(
            YouTubeChannelSnapshot(
                youtube_channel_id="channel-austin-clips",
                title="Austin Evans Clips",
            )
        )

        for index in range(2):
            video = Video(
                title=f"Austin upload {index}",
                slug=f"austin-upload-{index}",
                channel_id=channel.id,
                created_at=datetime.utcnow() - timedelta(minutes=index),
                duration_seconds=600,
                is_available=True,
            )
            db.add(video)
            db.flush()
            db.add(
                YouTubeMatch(
                    video_id=video.id,
                    youtube_video_id=f"wrong-clips-{index}",
                    youtube_channel_id="channel-austin-clips",
                    status="matched",
                    confidence=0.92,
                )
            )
        db.commit()

        assert sync_service.matched_youtube_channels_by_local_channel(db) == {}


def test_refresh_live_streams_uses_web_lookup_even_with_api_key(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="PGL", slug="pgl")
        db.add(channel)
        db.flush()
        db.add(SyncSettings(live_tab_enabled=True))
        db.add(LiveMonitoredChannel(channel_id=channel.id))
        video = Video(
            title="PGL upload",
            slug="pgl-upload",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            YouTubeMatch(
                video_id=video.id,
                youtube_channel_id="channel-pgl",
                youtube_video_id="vod123",
                status="matched",
            )
        )
        db.add(
            YouTubeChannelSnapshot(
                youtube_channel_id="channel-pgl",
                title="PGL",
            )
        )
        db.add(
            YouTubeLiveStreamSnapshot(
                youtube_video_id="live123",
                youtube_channel_id="channel-pgl",
                channel_id=channel.id,
                title="Current stream",
                is_live=True,
                last_seen_at=datetime.utcnow(),
                fetched_at=datetime.utcnow(),
            )
        )
        db.commit()

        async def fail_live_api(*args, **kwargs):
            raise AssertionError("live refresh should not use youtube api helpers")

        async def fake_fetch_live_stream_candidates_web(*args, **kwargs):
            return (
                True,
                [
                    {
                        "id": "live123",
                        "snippet": {
                            "title": "Current stream",
                            "channelTitle": "PGL",
                            "channelId": "channel-pgl",
                            "liveBroadcastContent": "live",
                            "thumbnails": {},
                        },
                        "liveStreamingDetails": {
                            "actualStartTime": "2026-04-15T12:00:00Z",
                            "concurrentViewers": "4812",
                        },
                        "statistics": {},
                        "_waytube_live_web": True,
                        "_waytube_local_channel_id": channel.id,
                        "_waytube_checked_youtube_channel_id": "channel-pgl",
                    }
                ],
            )

        monkeypatch.setattr(sync_service, "fetch_channel_details", fail_live_api)
        monkeypatch.setattr(sync_service, "fetch_recent_upload_playlist_video_ids", fail_live_api)
        monkeypatch.setattr(sync_service, "fetch_live_video_details", fail_live_api)
        monkeypatch.setattr(sync_service, "fetch_live_stream_candidates_web", fake_fetch_live_stream_candidates_web)

        async def run():
            return await refresh_live_streams(db, api_key="test-api-key", requests_per_second=3)

        rows = asyncio.run(run())

        assert len(rows) == 1
        assert rows[0].youtube_video_id == "live123"
        assert rows[0].is_live is True
        assert rows[0].concurrent_viewers == 4812


def test_refresh_live_streams_marks_existing_live_row_stale_when_web_lookup_finds_no_valid_candidate(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="PGL", slug="pgl")
        db.add(channel)
        db.flush()
        db.add(SyncSettings(live_tab_enabled=True))
        db.add(LiveMonitoredChannel(channel_id=channel.id))
        video = Video(
            title="PGL upload",
            slug="pgl-upload-stale-live",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            YouTubeMatch(
                video_id=video.id,
                youtube_channel_id="channel-pgl",
                youtube_video_id="vod123",
                status="matched",
            )
        )
        db.add(
            YouTubeChannelSnapshot(
                youtube_channel_id="channel-pgl",
                title="PGL",
            )
        )
        db.add(
            YouTubeLiveStreamSnapshot(
                youtube_video_id="live123",
                youtube_channel_id="channel-pgl",
                channel_id=channel.id,
                title="Current stream",
                is_live=True,
                last_seen_at=datetime.utcnow(),
                fetched_at=datetime.utcnow(),
            )
        )
        db.commit()

        async def fail_live_api(*args, **kwargs):
            raise AssertionError("live refresh should not use youtube api helpers")

        async def fake_fetch_live_stream_candidates_web(*args, **kwargs):
            return True, []

        monkeypatch.setattr(sync_service, "fetch_channel_details", fail_live_api)
        monkeypatch.setattr(sync_service, "fetch_recent_upload_playlist_video_ids", fail_live_api)
        monkeypatch.setattr(sync_service, "fetch_live_video_details", fail_live_api)
        monkeypatch.setattr(sync_service, "fetch_live_stream_candidates_web", fake_fetch_live_stream_candidates_web)

        async def run():
            return await refresh_live_streams(db, api_key="test-api-key", requests_per_second=3)

        rows = asyncio.run(run())
        stale_row = db.scalar(
            select(YouTubeLiveStreamSnapshot).where(YouTubeLiveStreamSnapshot.youtube_video_id == "live123")
        )

        assert rows == []
        assert stale_row is not None
        assert stale_row.is_live is False
        assert stale_row.youtube_channel_id == "channel-pgl"


def test_refresh_live_streams_clears_rows_for_superseded_channel_mapping(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="PGL", slug="pgl")
        db.add(channel)
        db.flush()
        db.add(SyncSettings(live_tab_enabled=True))
        db.add(LiveMonitoredChannel(channel_id=channel.id))
        video = Video(
            title="PGL upload",
            slug="pgl-upload-live-remap",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            YouTubeMatch(
                video_id=video.id,
                youtube_channel_id="channel-pgl",
                youtube_video_id="vod123",
                status="matched",
            )
        )
        db.add(
            YouTubeChannelSnapshot(
                youtube_channel_id="channel-pgl",
                title="PGL",
            )
        )
        db.add(
            YouTubeLiveStreamSnapshot(
                youtube_video_id="stale-esl-live",
                youtube_channel_id="channel-esl",
                channel_id=channel.id,
                title="Wrong stream",
                is_live=True,
                last_seen_at=datetime.utcnow(),
                fetched_at=datetime.utcnow(),
            )
        )
        db.commit()

        async def fail_live_api(*args, **kwargs):
            raise AssertionError("live refresh should not use youtube api helpers")

        async def fake_fetch_live_stream_candidates_web(*args, **kwargs):
            return True, []

        monkeypatch.setattr(sync_service, "fetch_channel_details", fail_live_api)
        monkeypatch.setattr(sync_service, "fetch_recent_upload_playlist_video_ids", fail_live_api)
        monkeypatch.setattr(sync_service, "fetch_live_video_details", fail_live_api)
        monkeypatch.setattr(sync_service, "fetch_live_stream_candidates_web", fake_fetch_live_stream_candidates_web)

        async def run():
            return await refresh_live_streams(db, api_key="test-api-key", requests_per_second=3)

        rows = asyncio.run(run())
        stale_row = db.scalar(
            select(YouTubeLiveStreamSnapshot).where(YouTubeLiveStreamSnapshot.youtube_video_id == "stale-esl-live")
        )

        assert rows == []
        assert stale_row is not None
        assert stale_row.is_live is False
        assert stale_row.youtube_channel_id == "channel-esl"


def test_refresh_live_streams_uses_web_fallback_without_api_key(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="LVNDMARK", slug="lvndmark")
        db.add(channel)
        db.flush()
        db.add(SyncSettings(live_tab_enabled=True))
        db.add(LiveMonitoredChannel(channel_id=channel.id))
        video = Video(
            title="LVNDMARK upload",
            slug="lvndmark-upload",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            YouTubeMatch(
                video_id=video.id,
                youtube_channel_id="channel-lvndmark",
                youtube_video_id="vod456",
                status="matched",
            )
        )
        db.add(
            YouTubeChannelSnapshot(
                youtube_channel_id="channel-lvndmark",
                title="LVNDMARK",
            )
        )
        db.commit()

        async def fake_fetch_live_stream_candidates_web(*args, **kwargs):
            return (
                True,
                [
                    {
                        "id": "live-web-1",
                        "snippet": {
                            "title": "Checking out Bellum",
                            "channelTitle": "LVNDMARK",
                            "channelId": "channel-lvndmark",
                            "thumbnails": {},
                        },
                        "statistics": {},
                        "_waytube_live_web": True,
                        "_waytube_local_channel_id": channel.id,
                        "_waytube_checked_youtube_channel_id": "channel-lvndmark",
                    }
                ],
            )

        monkeypatch.setattr(sync_service, "fetch_live_stream_candidates_web", fake_fetch_live_stream_candidates_web)

        async def run():
            return await refresh_live_streams(db, api_key=None, requests_per_second=3)

        rows = asyncio.run(run())

        assert len(rows) == 1
        assert rows[0].youtube_video_id == "live-web-1"
        assert rows[0].youtube_channel_id == "channel-lvndmark"
        assert rows[0].channel_id == channel.id
        assert rows[0].is_live is True


def test_apply_sync_item_review_keeps_existing_channel_assignment(tmp_path: Path, monkeypatch):
    async def fake_ryd(*args, **kwargs):
        return None

    async def fake_channel_about(*args, **kwargs):
        return None

    monkeypatch.setattr(sync_service, "fetch_return_youtube_dislike_details", fake_ryd)
    monkeypatch.setattr(sync_service, "fetch_channel_about_details", fake_channel_about)
    monkeypatch.setattr(sync_service, "generate_thumbnail", lambda *args, **kwargs: None)

    with make_session(tmp_path) as db:
        channel = Channel(name="FRANKIEonPCin1080p", slug="frankieonpcin1080p")
        db.add(channel)
        db.flush()

        video = Video(
            title="LADY BANDITS! - Arma 2: DayZ Mod - Ep.30",
            slug="lady-bandits-arma-2-dayz-mod-ep-30",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=1443,
            is_available=True,
        )
        db.add(video)
        db.flush()

        item = {
            "id": "bizim123",
            "snippet": {
                "title": "LADY BANDITS! - Arma 2: DayZ Mod - Ep.30",
                "channelTitle": "Bizim Kanal",
                "channelId": "channel-bizim",
                "publishedAt": "2026-04-11T12:00:00Z",
                "description": "Wrong channel match",
                "thumbnails": {},
            },
            "statistics": {
                "viewCount": "90",
            },
            "_waytube_duration_seconds": 1443,
            "_waytube_source": "watch-page",
        }

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await apply_sync_item(
                    db,
                    video,
                    item,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                    api_key=None,
                    confidence=0.86,
                    reasons=["exact-title", "duration-tight", "channel-mismatch"],
                    status="review",
                )

        result = asyncio.run(run())
        refreshed_video = db.get(Video, video.id)
        created_channel = db.scalar(select(Channel).where(Channel.slug == "bizim-kanal"))
        created_snapshot = db.scalar(select(YouTubeVideoSnapshot).where(YouTubeVideoSnapshot.youtube_video_id == "bizim123"))
        created_channel_snapshot = db.scalar(
            select(YouTubeChannelSnapshot).where(YouTubeChannelSnapshot.youtube_channel_id == "channel-bizim")
        )

        assert result.status == "review"
        assert refreshed_video is not None
        assert refreshed_video.channel_id == channel.id
        assert created_channel is None
        assert created_snapshot is None
        assert created_channel_snapshot is None
        assert db.get(Channel, channel.id).name == "FRANKIEonPCin1080p"


def test_channel_snapshot_for_channel_ignores_review_matches(tmp_path: Path):
    with make_session(tmp_path) as db:
        channel = Channel(name="Correct Channel", slug="correct-channel")
        video = Video(
            title="Review target",
            slug="review-target",
            channel=channel,
            duration_seconds=600,
            is_available=True,
        )
        db.add_all([channel, video])
        db.flush()
        db.add(
            YouTubeMatch(
                video_id=video.id,
                youtube_video_id="review12345a",
                youtube_channel_id="wrong-channel",
                status="review",
                confidence=0.71,
            )
        )
        db.add(
            YouTubeChannelSnapshot(
                youtube_channel_id="wrong-channel",
                title="Wrong Channel",
            )
        )
        db.commit()

        assert routes_service._channel_snapshot_for_channel(db, channel.id) is None


def test_channel_snapshot_for_channel_ignores_mismatched_matched_snapshot(tmp_path: Path):
    with make_session(tmp_path) as db:
        channel = Channel(name="PGL", slug="pgl")
        video = Video(
            title="Quarterfinal recap",
            slug="quarterfinal-recap",
            channel=channel,
            duration_seconds=600,
            is_available=True,
        )
        db.add_all([channel, video])
        db.flush()
        db.add(
            YouTubeMatch(
                video_id=video.id,
                youtube_video_id="wrongmatched1",
                youtube_channel_id="channel-esl",
                status="matched",
                confidence=0.92,
            )
        )
        db.add(
            YouTubeChannelSnapshot(
                youtube_channel_id="channel-esl",
                title="ESL Counter-Strike",
            )
        )
        db.commit()

        assert routes_service._channel_snapshot_for_channel(db, channel.id) is None


def test_build_home_feed_ignores_review_channel_snapshot(tmp_path: Path):
    with make_session(tmp_path) as db:
        user = UserProfile(name="viewer", display_name="Viewer")
        channel = Channel(name="Correct Channel", slug="correct-channel")
        video = Video(
            title="Review target",
            slug="review-target",
            channel=channel,
            duration_seconds=600,
            is_available=True,
        )
        db.add_all([user, channel, video])
        db.flush()
        db.add(
            YouTubeMatch(
                video_id=video.id,
                youtube_video_id="review12345b",
                youtube_channel_id="wrong-channel",
                status="review",
                confidence=0.74,
            )
        )
        db.add(
            YouTubeChannelSnapshot(
                youtube_channel_id="wrong-channel",
                title="Wrong Channel",
                avatar_url="https://example.com/wrong.jpg",
            )
        )
        db.commit()

        sections = feed_service.build_home_feed(db, user.id)
        cards = [card for section in sections for card in section.items]
        target_card = next(card for card in cards if card.id == video.id)

        assert target_card.channel == "Correct Channel"


def test_build_home_feed_ignores_mismatched_matched_channel_snapshot(tmp_path: Path):
    with make_session(tmp_path) as db:
        user = UserProfile(name="viewer", display_name="Viewer")
        channel = Channel(name="PGL", slug="pgl")
        video = Video(
            title="Quarterfinal recap",
            slug="quarterfinal-recap-card",
            channel=channel,
            duration_seconds=600,
            is_available=True,
        )
        db.add_all([user, channel, video])
        db.flush()
        db.add(
            YouTubeMatch(
                video_id=video.id,
                youtube_video_id="wrongmatched2",
                youtube_channel_id="channel-esl",
                status="matched",
                confidence=0.92,
            )
        )
        db.add(
            YouTubeChannelSnapshot(
                youtube_channel_id="channel-esl",
                title="ESL Counter-Strike",
                avatar_url="https://example.com/wrong.jpg",
            )
        )
        db.commit()

        sections = feed_service.build_home_feed(db, user.id)
        cards = [card for section in sections for card in section.items]
        target_card = next(card for card in cards if card.id == video.id)

        assert target_card.channel == "PGL"
        assert target_card.watch_ref == str(video.id)


def test_video_ref_for_ignores_mismatched_matched_channel(tmp_path: Path):
    with make_session(tmp_path) as db:
        channel = Channel(name="PGL", slug="pgl")
        video = Video(
            title="Quarterfinal recap",
            slug="quarterfinal-recap-route",
            channel=channel,
            duration_seconds=600,
            is_available=True,
        )
        db.add_all([channel, video])
        db.flush()
        db.add(
            YouTubeMatch(
                video_id=video.id,
                youtube_video_id="wrongmatched-route",
                youtube_channel_id="channel-esl",
                status="matched",
                confidence=0.92,
            )
        )
        db.add(
            YouTubeChannelSnapshot(
                youtube_channel_id="channel-esl",
                title="ESL Counter-Strike",
            )
        )
        db.commit()

        refreshed_video = db.get(Video, video.id)
        assert refreshed_video is not None
        assert routes_service._video_ref_for(db, refreshed_video) == str(video.id)


def test_summarize_video_ignores_review_snapshot_metadata(tmp_path: Path):
    with make_session(tmp_path) as db:
        channel = Channel(name="Correct Channel", slug="correct-channel")
        video = Video(
            title="Review target",
            slug="review-target-summary",
            channel=channel,
            duration_seconds=600,
            published_at=datetime(2026, 4, 14),
            description="Local description",
            is_available=True,
        )
        db.add_all([channel, video])
        db.flush()
        db.add(
            YouTubeMatch(
                video_id=video.id,
                youtube_video_id="review12345summary",
                youtube_channel_id="wrong-channel",
                status="review",
                confidence=0.74,
            )
        )
        db.add(
            YouTubeVideoSnapshot(
                youtube_video_id="review12345summary",
                youtube_channel_id="wrong-channel",
                title="Wrong review target",
                description="Wrong review description",
                published_at=datetime(2026, 4, 15),
                published_at_source="watch-page",
                view_count=999999,
                like_count=12345,
                dislike_count=456,
                rating=1.1,
            )
        )
        db.commit()

        summary = feed_service.summarize_video(video, db=db)

        assert summary.description == "Local description"
        assert summary.published_at == datetime(2026, 4, 14)
        assert summary.youtube_view_count is None
        assert summary.youtube_like_count is None
        assert summary.youtube_dislike_count is None


def test_video_thumbnail_ignores_review_snapshot_remote_art(tmp_path: Path, monkeypatch):
    generated_thumb = tmp_path / "cache" / "thumbnails" / "local.jpg"
    generated_thumb.parent.mkdir(parents=True, exist_ok=True)
    generated_thumb.write_bytes(b"thumb")
    source_path = tmp_path / "library" / "video.mp4"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"video")

    def fail_download(*args, **kwargs):
        raise AssertionError("review snapshot thumbnail should not be downloaded")

    monkeypatch.setattr(routes_service, "download_thumbnail", fail_download)
    monkeypatch.setattr(routes_service, "generate_thumbnail", lambda *args, **kwargs: str(generated_thumb))

    with make_session(tmp_path) as db:
        channel = Channel(name="Correct Channel", slug="correct-channel")
        video = Video(
            title="Review target",
            slug="review-target",
            channel=channel,
            duration_seconds=600,
            is_available=True,
        )
        db.add_all([channel, video])
        db.flush()
        db.add(
            VideoFile(
                video_id=video.id,
                absolute_path=str(source_path),
                relative_path="video.mp4",
                file_size=source_path.stat().st_size,
                fingerprint="t" * 64,
            )
        )
        db.add(
            YouTubeMatch(
                video_id=video.id,
                youtube_video_id="review12345c",
                youtube_channel_id="wrong-channel",
                status="review",
                confidence=0.74,
            )
        )
        db.add(
            YouTubeVideoSnapshot(
                youtube_video_id="review12345c",
                youtube_channel_id="wrong-channel",
                title="Wrong review target",
                thumbnail_url="https://example.com/wrong.jpg",
            )
        )
        db.commit()

        response = routes_service.video_thumbnail(
            video.id,
            db=db,
            current_user=UserProfile(name="viewer", display_name="Viewer"),
        )
        refreshed_video = db.get(Video, video.id)

        assert refreshed_video is not None
        assert refreshed_video.thumbnail_path == str(generated_thumb)
        assert response.path == str(generated_thumb)


def test_sync_video_rejects_implausible_review_only_candidate_without_api(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        generic_channel = Channel(name="Unknown Channel", slug="unknown-channel")
        db.add(generic_channel)
        db.flush()

        target_path = tmp_path / "library" / "implausible-review.mp4"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"target")
        video = Video(
            title="This is so fucking stupid...",
            slug="this-is-so-fucking-stupid-review-reject",
            channel_id=generic_channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=22 * 60 + 43,
            published_at=datetime(2026, 4, 14),
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            VideoFile(
                video_id=video.id,
                absolute_path=str(target_path),
                relative_path="implausible-review.mp4",
                file_size=target_path.stat().st_size,
                fingerprint="reviewreject" * 6 + "ab",
            )
        )
        db.commit()

        async def fake_fetch_fallback_candidates(*args, **kwargs):
            return [
                {
                    "id": "wronghasan01",
                    "snippet": {
                        "title": "Hillary Clinton is so Fucking Stupid",
                        "channelTitle": "HasanAbi",
                        "channelId": "channel-hasan",
                        "publishedAt": "2026-04-14T12:00:00Z",
                    },
                    "statistics": {},
                    "_waytube_duration_seconds": 59 * 60 + 11,
                    "_waytube_source": "watch-page",
                }
            ]

        monkeypatch.setattr(sync_service, "fetch_fallback_candidates", fake_fetch_fallback_candidates)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    video,
                    api_key=None,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        assert result.status == "unmatched"
        assert result.youtube_video_id is None
        assert result.youtube_channel_id is None


def test_force_sync_refreshes_existing_match_by_id_before_researching(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="Asmongold TV", slug="asmongold-tv")
        db.add(channel)
        db.flush()

        target_path = tmp_path / "library" / "matched.mp4"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"target")
        video = Video(
            title="British Navy is a joke now",
            slug="british-navy-is-a-joke-now",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=900,
            published_at=datetime(2026, 4, 6),
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            VideoFile(
                video_id=video.id,
                absolute_path=str(target_path),
                relative_path="matched.mp4",
                file_size=target_path.stat().st_size,
                fingerprint="e" * 64,
            )
        )
        db.add(
            YouTubeMatch(
                video_id=video.id,
                youtube_video_id="knownmatch1",
                youtube_channel_id="channel-asmongold",
                status="matched",
                confidence=0.94,
                reasons=["channel", "duration-tight"],
            )
        )
        db.commit()

        search_called = False

        async def fake_fetch_video_details_by_id(*args, **kwargs):
            return {
                "id": "knownmatch1",
                "snippet": {
                    "title": "The state of British Navy is embarrassing",
                    "channelTitle": "Asmongold TV",
                    "channelId": "channel-asmongold",
                    "publishedAt": "2026-04-06T12:00:00Z",
                    "description": "Updated metadata",
                    "thumbnails": {},
                },
                "statistics": {},
                "_waytube_duration_seconds": 900,
                "_waytube_source": "youtube-api",
            }

        async def fake_fetch_search_candidates(*args, **kwargs):
            nonlocal search_called
            search_called = True
            return []

        async def fake_apply_sync_item(
            db: Session,
            video: Video,
            item: dict,
            **kwargs,
        ) -> YouTubeMatch:
            match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
            assert match is not None
            match.youtube_video_id = item["id"]
            match.youtube_channel_id = item["snippet"]["channelId"]
            match.status = kwargs["status"]
            match.confidence = kwargs["confidence"]
            match.reasons = kwargs["reasons"]
            db.commit()
            db.refresh(match)
            return match

        monkeypatch.setattr(sync_service, "fetch_video_details_by_id", fake_fetch_video_details_by_id)
        monkeypatch.setattr(sync_service, "fetch_search_candidates", fake_fetch_search_candidates)
        monkeypatch.setattr(sync_service, "apply_sync_item", fake_apply_sync_item)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    video,
                    api_key="test-api-key",
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                    force=True,
                )

        result = asyncio.run(run())

        assert result.status == "matched"
        assert result.youtube_video_id == "knownmatch1"
        assert "refresh-by-id" in (result.reasons or [])
        assert "force-refresh" in (result.reasons or [])
        assert search_called is False


def test_non_force_refresh_prefers_watch_page_for_existing_match(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="Asmongold TV", slug="asmongold-tv")
        db.add(channel)
        db.flush()

        target_path = tmp_path / "library" / "matched-watch-page.mp4"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"target")
        video = Video(
            title="British Navy is a joke now",
            slug="british-navy-is-a-joke-now-watch-page",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=900,
            published_at=datetime.utcnow(),
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            VideoFile(
                video_id=video.id,
                absolute_path=str(target_path),
                relative_path="matched-watch-page.mp4",
                file_size=target_path.stat().st_size,
                fingerprint="7" * 64,
            )
        )
        db.add(
            YouTubeMatch(
                video_id=video.id,
                youtube_video_id="knownmatch2",
                youtube_channel_id="channel-asmongold",
                status="matched",
                confidence=0.94,
                reasons=["channel", "duration-tight"],
            )
        )
        db.add(
            YouTubeVideoSnapshot(
                youtube_video_id="knownmatch2",
                youtube_channel_id="channel-asmongold",
                title="British Navy is a joke now",
                published_at=datetime.utcnow(),
                published_at_source="youtube-api",
                duration_seconds=900,
                thumbnail_url="https://example.com/thumb.jpg",
                view_count=100,
                like_count=50,
                dislike_count=2,
                fetched_at=datetime.utcnow() - timedelta(hours=7),
            )
        )
        db.commit()

        api_refresh_called = False

        async def fake_fetch_watch_page_candidate(*args, **kwargs):
            return {
                "id": "knownmatch2",
                "snippet": {
                    "title": "The state of British Navy is embarrassing",
                    "channelTitle": "Asmongold TV",
                    "channelId": "channel-asmongold",
                    "publishedAt": "2026-04-15T12:00:00Z",
                    "description": "Watch page refresh",
                    "thumbnails": {},
                },
                "statistics": {"viewCount": 12345, "likeCount": 678},
                "_waytube_duration_seconds": 900,
                "_waytube_source": "watch-page",
            }

        async def fake_fetch_video_details_by_id(*args, **kwargs):
            nonlocal api_refresh_called
            api_refresh_called = True
            return None

        async def fake_apply_sync_item(
            db: Session,
            video: Video,
            item: dict,
            **kwargs,
        ) -> YouTubeMatch:
            assert kwargs["api_key"] is None
            match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
            assert match is not None
            match.youtube_video_id = item["id"]
            match.youtube_channel_id = item["snippet"]["channelId"]
            match.status = kwargs["status"]
            match.confidence = kwargs["confidence"]
            match.reasons = kwargs["reasons"]
            db.commit()
            db.refresh(match)
            return match

        monkeypatch.setattr(sync_service, "fetch_watch_page_candidate", fake_fetch_watch_page_candidate)
        monkeypatch.setattr(sync_service, "fetch_video_details_by_id", fake_fetch_video_details_by_id)
        monkeypatch.setattr(sync_service, "apply_sync_item", fake_apply_sync_item)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    video,
                    api_key="test-api-key",
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        assert result.status == "matched"
        assert api_refresh_called is False
        assert "refresh-by-id" in (result.reasons or [])


def test_non_force_refresh_researches_implausible_existing_match_without_api(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        generic_channel = Channel(name="Unknown Channel", slug="unknown-channel")
        db.add(generic_channel)
        db.flush()

        target_path = tmp_path / "library" / "implausible-no-api.mp4"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"target")
        video = Video(
            title="Joe Rogan Experience #2483 - Spencer Pratt",
            slug="joe-rogan-experience-2483-spencer-pratt-implausible-no-api",
            channel_id=generic_channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=2 * 3600 + 34 * 60 + 23,
            published_at=datetime(2026, 4, 14),
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            VideoFile(
                video_id=video.id,
                absolute_path=str(target_path),
                relative_path="implausible-no-api.mp4",
                file_size=target_path.stat().st_size,
                fingerprint="8" * 64,
            )
        )
        db.add(
            YouTubeMatch(
                video_id=video.id,
                youtube_video_id="wrong-old-id",
                youtube_channel_id="channel-asmongold",
                status="matched",
                confidence=0.91,
                reasons=["known-channel"],
            )
        )
        db.add(
            YouTubeVideoSnapshot(
                youtube_video_id="wrong-old-id",
                youtube_channel_id="channel-asmongold",
                title="This is so f***ing stupid..",
                published_at=datetime(2026, 4, 14),
                published_at_source="watch-page",
                duration_seconds=17 * 60 + 52,
                thumbnail_url="https://example.com/wrong.jpg",
                view_count=100,
                like_count=50,
                dislike_count=2,
                fetched_at=datetime.utcnow(),
            )
        )
        db.add(
            YouTubeChannelSnapshot(
                youtube_channel_id="channel-asmongold",
                title="Asmongold TV",
            )
        )
        db.commit()

        search_called = False

        async def fake_fetch_watch_page_candidate(*args, **kwargs):
            return {
                "id": "wrong-old-id",
                "snippet": {
                    "title": "This is so f***ing stupid..",
                    "channelTitle": "Asmongold TV",
                    "channelId": "channel-asmongold",
                    "publishedAt": "2026-04-14T12:00:00Z",
                    "description": "Still wrong",
                    "thumbnails": {},
                },
                "statistics": {"viewCount": 100, "likeCount": 50},
                "_waytube_duration_seconds": 17 * 60 + 52,
                "_waytube_source": "watch-page",
            }

        async def fake_fetch_fallback_candidates(*args, **kwargs):
            nonlocal search_called
            search_called = True
            return [
                {
                    "id": "jre2483abc1",
                    "snippet": {
                        "title": "Joe Rogan Experience #2483 - Spencer Pratt",
                        "channelTitle": "PowerfulJRE",
                        "channelId": "channel-jre",
                        "publishedAt": "2026-04-14T12:00:00Z",
                    },
                    "statistics": {"viewCount": 235000, "likeCount": 12000},
                    "_waytube_duration_seconds": 2 * 3600 + 34 * 60 + 23,
                    "_waytube_source": "watch-page",
                }
            ]

        async def fake_apply_sync_item(
            db: Session,
            video: Video,
            item: dict,
            **kwargs,
        ) -> YouTubeMatch:
            match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
            assert match is not None
            match.youtube_video_id = item["id"]
            match.youtube_channel_id = item["snippet"]["channelId"]
            match.status = kwargs["status"]
            match.confidence = kwargs["confidence"]
            match.reasons = kwargs["reasons"]
            db.commit()
            db.refresh(match)
            return match

        monkeypatch.setattr(sync_service, "fetch_watch_page_candidate", fake_fetch_watch_page_candidate)
        monkeypatch.setattr(sync_service, "fetch_fallback_candidates", fake_fetch_fallback_candidates)
        monkeypatch.setattr(sync_service, "apply_sync_item", fake_apply_sync_item)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    video,
                    api_key=None,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        assert search_called is True
        assert result.status == "matched"
        assert result.youtube_video_id == "jre2483abc1"
        assert result.youtube_channel_id == "channel-jre"


def test_non_force_refresh_researches_known_channel_mismatch_without_api(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        channel = Channel(name="FRANKIEonPCin1080p", slug="frankieonpcin1080p")
        db.add(channel)
        db.flush()

        target_path = tmp_path / "library" / "frankie-refresh.mp4"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"target")
        video = Video(
            title="LADY BANDITS! - Arma 2: DayZ Mod - Ep.30",
            slug="lady-bandits-refresh",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=1443,
            published_at=datetime(2026, 4, 11),
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            VideoFile(
                video_id=video.id,
                absolute_path=str(target_path),
                relative_path="frankie-refresh.mp4",
                file_size=target_path.stat().st_size,
                fingerprint="frankierefresh" * 5,
            )
        )
        db.add(
            YouTubeMatch(
                video_id=video.id,
                youtube_video_id="wrong-bizim-id",
                youtube_channel_id="channel-bizim",
                status="matched",
                confidence=0.93,
                reasons=["exact-title", "duration-tight"],
            )
        )
        db.add(
            YouTubeVideoSnapshot(
                youtube_video_id="wrong-bizim-id",
                youtube_channel_id="channel-bizim",
                title="LADY BANDITS! - Arma 2: DayZ Mod - Ep.30",
                published_at=datetime(2026, 4, 11),
                published_at_source="watch-page",
                duration_seconds=1443,
                thumbnail_url="https://example.com/wrong-frankie.jpg",
                view_count=100,
                like_count=50,
                dislike_count=2,
                fetched_at=datetime.utcnow(),
            )
        )
        db.add(
            YouTubeChannelSnapshot(
                youtube_channel_id="channel-bizim",
                title="Bizim Kanal",
            )
        )
        db.commit()

        search_called = False

        async def fake_fetch_watch_page_candidate(*args, **kwargs):
            return {
                "id": "wrong-bizim-id",
                "snippet": {
                    "title": "LADY BANDITS! - Arma 2: DayZ Mod - Ep.30",
                    "channelTitle": "Bizim Kanal",
                    "channelId": "channel-bizim",
                    "publishedAt": "2026-04-11T12:00:00Z",
                    "description": "Still wrong",
                    "thumbnails": {},
                },
                "statistics": {"viewCount": 100, "likeCount": 50},
                "_waytube_duration_seconds": 1443,
                "_waytube_source": "watch-page",
            }

        async def fake_fetch_fallback_candidates(*args, **kwargs):
            nonlocal search_called
            search_called = True
            return [
                {
                    "id": "frankie-correct-1",
                    "snippet": {
                        "title": "LADY BANDITS! - Arma 2: DayZ Mod - Ep.30",
                        "channelTitle": "FRANKIEonPCin1080p",
                        "channelId": "channel-frankie",
                        "publishedAt": "2026-04-11T12:00:00Z",
                    },
                    "statistics": {"viewCount": 340000, "likeCount": 15000},
                    "_waytube_duration_seconds": 1443,
                    "_waytube_source": "watch-page",
                }
            ]

        async def fake_apply_sync_item(
            db: Session,
            video: Video,
            item: dict,
            **kwargs,
        ) -> YouTubeMatch:
            match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
            assert match is not None
            match.youtube_video_id = item["id"]
            match.youtube_channel_id = item["snippet"]["channelId"]
            match.status = kwargs["status"]
            match.confidence = kwargs["confidence"]
            match.reasons = kwargs["reasons"]
            db.commit()
            db.refresh(match)
            return match

        monkeypatch.setattr(sync_service, "fetch_watch_page_candidate", fake_fetch_watch_page_candidate)
        monkeypatch.setattr(sync_service, "fetch_fallback_candidates", fake_fetch_fallback_candidates)
        monkeypatch.setattr(sync_service, "apply_sync_item", fake_apply_sync_item)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    video,
                    api_key=None,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        assert search_called is True
        assert result.status == "matched"
        assert result.youtube_video_id == "frankie-correct-1"
        assert result.youtube_channel_id == "channel-frankie"


def test_video_requires_refresh_stops_periodic_refresh_for_old_matched_video(tmp_path: Path):
    with make_session(tmp_path) as db:
        channel = Channel(name="PGL", slug="pgl")
        db.add(channel)
        db.flush()

        video = Video(
            title="Older upload",
            slug="older-upload",
            channel_id=channel.id,
            created_at=datetime.utcnow() - timedelta(days=30),
            published_at=datetime.utcnow() - timedelta(days=30),
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            YouTubeMatch(
                video_id=video.id,
                youtube_video_id="oldmatch1",
                youtube_channel_id="channel-pgl",
                status="matched",
            )
        )
        db.add(
            YouTubeVideoSnapshot(
                youtube_video_id="oldmatch1",
                youtube_channel_id="channel-pgl",
                title="Older upload",
                published_at=datetime.utcnow() - timedelta(days=30),
                published_at_source="youtube-api",
                duration_seconds=600,
                thumbnail_url="https://example.com/thumb.jpg",
                view_count=1000,
                like_count=200,
                dislike_count=None,
                fetched_at=datetime.utcnow() - timedelta(hours=7),
            )
        )
        db.add(
            YouTubeChannelSnapshot(
                youtube_channel_id="channel-pgl",
                title="PGL",
                avatar_url="https://example.com/avatar.jpg",
                banner_url="https://example.com/banner.jpg",
            )
        )
        db.commit()

        assert (
            sync_service.video_requires_refresh(
                db,
                video,
                api_key_available=True,
                allow_fallback_art=False,
                prefer_high_res_banners=False,
            )
            is False
        )


def test_video_requires_refresh_skips_periodic_refresh_for_low_confidence_match(tmp_path: Path):
    with make_session(tmp_path) as db:
        channel = Channel(name="Unknown Channel", slug="unknown-channel")
        db.add(channel)
        db.flush()

        video = Video(
            title="Uncertain upload",
            slug="uncertain-upload",
            channel_id=channel.id,
            created_at=datetime.utcnow() - timedelta(days=1),
            published_at=datetime.utcnow() - timedelta(days=1),
            duration_seconds=900,
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            YouTubeMatch(
                video_id=video.id,
                youtube_video_id="uncertain123",
                youtube_channel_id="channel-uncertain",
                status="matched",
                confidence=0.74,
                reasons=["title", "duration-tight"],
            )
        )
        db.add(
            YouTubeVideoSnapshot(
                youtube_video_id="uncertain123",
                youtube_channel_id="channel-uncertain",
                title="Uncertain upload",
                published_at=datetime.utcnow() - timedelta(days=1),
                published_at_source="watch-page",
                duration_seconds=900,
                thumbnail_url="https://example.com/uncertain.jpg",
                view_count=1200,
                like_count=80,
                fetched_at=datetime.utcnow() - timedelta(hours=7),
            )
        )
        db.add(
            YouTubeChannelSnapshot(
                youtube_channel_id="channel-uncertain",
                title="Unknown Channel",
                avatar_url="https://example.com/uncertain-avatar.jpg",
                banner_url="https://example.com/uncertain-banner.jpg",
            )
        )
        db.commit()

        assert (
            sync_service.video_requires_refresh(
                db,
                video,
                api_key_available=True,
                allow_fallback_art=False,
                prefer_high_res_banners=False,
            )
            is False
        )


def test_force_refresh_researches_implausible_existing_match_after_api_refresh_rejection(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        generic_channel = Channel(name="Unknown Channel", slug="unknown-channel")
        db.add(generic_channel)
        db.flush()

        target_path = tmp_path / "library" / "implausible-force-api.mp4"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(b"target")
        video = Video(
            title="Joe Rogan Experience #2483 - Spencer Pratt",
            slug="joe-rogan-experience-2483-spencer-pratt-implausible-force-api",
            channel_id=generic_channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=2 * 3600 + 34 * 60 + 23,
            published_at=datetime(2026, 4, 14),
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            VideoFile(
                video_id=video.id,
                absolute_path=str(target_path),
                relative_path="implausible-force-api.mp4",
                file_size=target_path.stat().st_size,
                fingerprint="9" * 64,
            )
        )
        db.add(
            YouTubeMatch(
                video_id=video.id,
                youtube_video_id="wrong-force-id",
                youtube_channel_id="channel-asmongold",
                status="matched",
                confidence=0.91,
                reasons=["known-channel"],
            )
        )
        db.add(
            YouTubeVideoSnapshot(
                youtube_video_id="wrong-force-id",
                youtube_channel_id="channel-asmongold",
                title="This is so f***ing stupid..",
                published_at=datetime(2026, 4, 14),
                published_at_source="youtube-api",
                duration_seconds=17 * 60 + 52,
                thumbnail_url="https://example.com/wrong.jpg",
                view_count=100,
                like_count=50,
                dislike_count=2,
                fetched_at=datetime.utcnow(),
            )
        )
        db.add(
            YouTubeChannelSnapshot(
                youtube_channel_id="channel-asmongold",
                title="Asmongold TV",
            )
        )
        db.commit()

        search_called = False

        async def fake_fetch_video_details_by_id(*args, **kwargs):
            return {
                "id": "wrong-force-id",
                "snippet": {
                    "title": "This is so f***ing stupid..",
                    "channelTitle": "Asmongold TV",
                    "channelId": "channel-asmongold",
                    "publishedAt": "2026-04-14T12:00:00Z",
                    "description": "Still wrong",
                    "thumbnails": {},
                },
                "statistics": {"viewCount": 100, "likeCount": 50},
                "_waytube_duration_seconds": 17 * 60 + 52,
                "_waytube_source": "youtube-api",
            }

        async def fake_fetch_search_candidates(*args, **kwargs):
            nonlocal search_called
            search_called = True
            return [
                {
                    "id": "jre2483api1",
                    "snippet": {
                        "title": "Joe Rogan Experience #2483 - Spencer Pratt",
                        "channelTitle": "PowerfulJRE",
                        "channelId": "channel-jre",
                        "publishedAt": "2026-04-14T12:00:00Z",
                    },
                    "statistics": {"viewCount": 235000, "likeCount": 12000},
                    "_waytube_duration_seconds": 2 * 3600 + 34 * 60 + 23,
                    "_waytube_source": "youtube-api",
                }
            ]

        async def fake_fetch_recent_channel_upload_candidates(*args, **kwargs):
            return []

        async def fake_apply_sync_item(
            db: Session,
            video: Video,
            item: dict,
            **kwargs,
        ) -> YouTubeMatch:
            match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
            assert match is not None
            match.youtube_video_id = item["id"]
            match.youtube_channel_id = item["snippet"]["channelId"]
            match.status = kwargs["status"]
            match.confidence = kwargs["confidence"]
            match.reasons = kwargs["reasons"]
            db.commit()
            db.refresh(match)
            return match

        monkeypatch.setattr(sync_service, "fetch_video_details_by_id", fake_fetch_video_details_by_id)
        monkeypatch.setattr(sync_service, "fetch_search_candidates", fake_fetch_search_candidates)
        monkeypatch.setattr(sync_service, "fetch_recent_channel_upload_candidates", fake_fetch_recent_channel_upload_candidates)
        monkeypatch.setattr(sync_service, "apply_sync_item", fake_apply_sync_item)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    video,
                    api_key="test-api-key",
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                    force=True,
                )

        result = asyncio.run(run())

        assert search_called is True
        assert result.status == "matched"
        assert result.youtube_video_id == "jre2483api1"
        assert result.youtube_channel_id == "channel-jre"


def test_sync_scope_prioritizes_discovery_before_background_refresh(tmp_path: Path, monkeypatch):
    with make_session(tmp_path) as db:
        db.add(
            SyncSettings(
                automatic_detection_enabled=True,
                automatic_sync_enabled=True,
                scan_interval_seconds=900,
            )
        )
        db.flush()

        refresh_video = Video(
            title="Older matched",
            slug="older-matched",
            created_at=datetime.utcnow() - timedelta(days=1),
            published_at=datetime.utcnow(),
            duration_seconds=600,
            is_available=True,
        )
        discovery_video = Video(
            title="Brand new discovery",
            slug="brand-new-discovery",
            created_at=datetime.utcnow(),
            is_available=True,
        )
        db.add_all([refresh_video, discovery_video])
        db.flush()

        old_path = tmp_path / "library" / "older.mp4"
        new_path = tmp_path / "library" / "new.mp4"
        old_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.write_bytes(b"old")
        new_path.write_bytes(b"new")
        db.add_all(
            [
                VideoFile(
                    video_id=refresh_video.id,
                    absolute_path=str(old_path),
                    relative_path="older.mp4",
                    file_size=old_path.stat().st_size,
                    fingerprint="8" * 64,
                ),
                VideoFile(
                    video_id=discovery_video.id,
                    absolute_path=str(new_path),
                    relative_path="new.mp4",
                    file_size=new_path.stat().st_size,
                    fingerprint="9" * 64,
                ),
            ]
        )
        db.add(
            YouTubeMatch(
                video_id=refresh_video.id,
                youtube_video_id="known-refresh",
                youtube_channel_id="channel-refresh",
                status="matched",
                confidence=0.93,
                reasons=["title", "duration-tight"],
            )
        )
        db.add(
            YouTubeVideoSnapshot(
                youtube_video_id="known-refresh",
                youtube_channel_id="channel-refresh",
                title="Older matched",
                published_at=datetime.utcnow(),
                published_at_source="youtube-api",
                duration_seconds=600,
                thumbnail_url="https://example.com/thumb.jpg",
                view_count=200,
                like_count=50,
                dislike_count=1,
                fetched_at=datetime.utcnow() - timedelta(hours=7),
            )
        )
        db.commit()

        seen_titles: list[str] = []

        async def fake_sync_video(db, video, **kwargs):
            seen_titles.append(video.title)
            match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
            if not match:
                match = YouTubeMatch(video_id=video.id, status="matched", youtube_video_id=f"auto-{video.id}")
                db.add(match)
                db.flush()
            match.status = "matched"
            db.commit()
            db.refresh(match)
            return match

        monkeypatch.setattr(sync_service, "sync_video", fake_sync_video)

        asyncio.run(sync_scope(db, scope="library", target_id=None, api_key=None, quiet_if_idle=False))

        assert seen_titles == ["Brand new discovery"]


def test_background_auto_sync_clears_stale_running_jobs_before_library_sync(tmp_path: Path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'background.db'}", future=True)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    with session_factory() as db:
        db.add(
            SyncSettings(
                automatic_detection_enabled=False,
                automatic_sync_enabled=True,
                scan_interval_seconds=900,
            )
        )
        db.add(
            SyncJob(
                scope="library",
                status="running",
                created_at=datetime.utcnow() - timedelta(minutes=20),
                updated_at=datetime.utcnow() - timedelta(minutes=20),
                started_at=datetime.utcnow() - timedelta(minutes=20),
                details={},
            )
        )
        db.commit()

    monkeypatch.setattr(background_service, "SessionLocal", session_factory)
    called: list[str] = []

    async def fake_sync_scope(db, scope, target_id, api_key, **kwargs):
        called.append(scope)
        return SyncJob(scope=scope, status="completed", details={})

    monkeypatch.setattr(background_service, "sync_scope", fake_sync_scope)

    asyncio.run(background_service.background_auto_sync_once(Settings(background_tasks_enabled=False)))

    with session_factory() as db:
        stale_job = db.scalar(select(SyncJob).where(SyncJob.scope == "library"))
        assert stale_job is not None
        assert stale_job.status == "failed"
        assert stale_job.details.get("stale") is True

    assert called == ["library"]


def test_background_auto_sync_runs_orphans_without_api_discovery(tmp_path: Path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'background-orphans.db'}", future=True)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    with session_factory() as db:
        db.add(
            SyncSettings(
                automatic_detection_enabled=True,
                automatic_sync_enabled=False,
                scan_interval_seconds=900,
                youtube_api_key="configured-key",
            )
        )
        db.commit()

    monkeypatch.setattr(background_service, "SessionLocal", session_factory)
    calls: list[dict[str, object]] = []

    async def fake_sync_scope(db, scope, target_id, api_key, **kwargs):
        del db, target_id
        calls.append(
            {
                "scope": scope,
                "api_key": api_key,
                "allow_api_discovery": kwargs.get("allow_api_discovery"),
            }
        )
        return SyncJob(scope=scope, status="completed", details={})

    monkeypatch.setattr(background_service, "sync_scope", fake_sync_scope)

    asyncio.run(background_service.background_auto_sync_once(Settings(background_tasks_enabled=False)))

    assert calls == [
        {
            "scope": "orphans",
            "api_key": "configured-key",
            "allow_api_discovery": False,
        }
    ]


def test_background_auto_sync_runs_library_without_api_discovery(tmp_path: Path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'background-library.db'}", future=True)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

    with session_factory() as db:
        db.add(
            SyncSettings(
                automatic_detection_enabled=False,
                automatic_sync_enabled=True,
                scan_interval_seconds=900,
                youtube_api_key="configured-key",
            )
        )
        db.commit()

    monkeypatch.setattr(background_service, "SessionLocal", session_factory)
    calls: list[dict[str, object]] = []

    async def fake_sync_scope(db, scope, target_id, api_key, **kwargs):
        del db, target_id
        calls.append(
            {
                "scope": scope,
                "api_key": api_key,
                "allow_api_discovery": kwargs.get("allow_api_discovery"),
            }
        )
        return SyncJob(scope=scope, status="completed", details={})

    monkeypatch.setattr(background_service, "sync_scope", fake_sync_scope)

    asyncio.run(background_service.background_auto_sync_once(Settings(background_tasks_enabled=False)))

    assert calls == [
        {
            "scope": "library",
            "api_key": "configured-key",
            "allow_api_discovery": False,
        }
    ]


def test_apply_sync_item_auto_organizes_root_file_without_losing_match(tmp_path: Path, monkeypatch):
    library_root = tmp_path / "library"
    library_folder = library_root / "youtube"
    library_folder.mkdir(parents=True)
    source_path = library_folder / "British Navy is a joke now.mp4"
    source_path.write_bytes(b"source")

    def fake_settings() -> Settings:
        return Settings(
            mounted_roots=[library_root],
            config_dir=tmp_path / "config",
            cache_dir=tmp_path / "cache",
            background_tasks_enabled=False,
        )

    async def fake_ryd(*args, **kwargs):
        return None

    async def fake_channel_about(*args, **kwargs):
        return None

    monkeypatch.setattr(sync_service, "get_settings", fake_settings)
    monkeypatch.setattr(sync_service, "fetch_return_youtube_dislike_details", fake_ryd)
    monkeypatch.setattr(sync_service, "fetch_channel_about_details", fake_channel_about)
    monkeypatch.setattr(sync_service, "generate_thumbnail", lambda *args, **kwargs: None)

    with make_session(tmp_path) as db:
        root = LibraryRoot(label="Library", path=str(library_root), is_available=True)
        db.add(root)
        db.flush()
        db.add(SelectedFolder(root_id=root.id, relative_path="youtube", is_enabled=True))
        unknown_channel = Channel(name="Unknown Channel", slug="unknown-channel")
        db.add(unknown_channel)
        db.flush()

        video = Video(
            title="British Navy is a joke now",
            slug="british-navy-is-a-joke-now",
            channel_id=unknown_channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=900,
            is_available=True,
        )
        db.add(video)
        db.flush()
        video_file = VideoFile(
            video_id=video.id,
            absolute_path=str(source_path),
            relative_path="British Navy is a joke now.mp4",
            file_size=source_path.stat().st_size,
            fingerprint="f" * 64,
        )
        db.add(video_file)
        db.commit()
        db.refresh(video)

        item = {
            "id": "britnavy123",
            "snippet": {
                "title": "The state of British Navy is embarrassing",
                "channelTitle": "Asmongold TV",
                "channelId": "channel-asmongold",
                "publishedAt": "2026-04-06T12:00:00Z",
                "description": "Updated metadata",
                "thumbnails": {},
            },
            "statistics": {
                "viewCount": "1234",
                "likeCount": "56",
            },
            "_waytube_duration_seconds": 900,
            "_waytube_source": "watch-page",
        }

        async def run() -> None:
            async with httpx.AsyncClient() as client:
                await apply_sync_item(
                    db,
                    video,
                    item,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                    api_key=None,
                    confidence=0.91,
                    reasons=["channel", "duration-tight"],
                    status="matched",
                )

        asyncio.run(run())

        organized_path = library_folder / "asmongold-tv" / source_path.name
        refreshed_file = db.scalar(select(VideoFile).where(VideoFile.id == video_file.id))
        refreshed_match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
        channel_snapshot = db.scalar(
            select(YouTubeChannelSnapshot).where(YouTubeChannelSnapshot.youtube_channel_id == "channel-asmongold")
        )
        video_snapshot = db.scalar(
            select(YouTubeVideoSnapshot).where(YouTubeVideoSnapshot.youtube_video_id == "britnavy123")
        )

        assert refreshed_file is not None
        assert organized_path.exists()
        assert not source_path.exists()
        assert refreshed_file.absolute_path == str(organized_path)
        assert refreshed_file.relative_path == "youtube/asmongold-tv/British Navy is a joke now.mp4"
        assert refreshed_match is not None
        assert refreshed_match.status == "matched"
        assert refreshed_match.youtube_video_id == "britnavy123"
        assert channel_snapshot is not None
        assert video_snapshot is not None

        scan_selected_folders(db, [library_root])

        rescanned_videos = db.scalars(select(Video).order_by(Video.id.asc())).all()
        rescanned_match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
        rescanned_file = db.scalar(select(VideoFile).where(VideoFile.id == video_file.id))

        assert len(rescanned_videos) == 1
        assert rescanned_match is not None
        assert rescanned_match.youtube_video_id == "britnavy123"
        assert rescanned_file is not None
        assert rescanned_file.absolute_path == str(organized_path)
        assert rescanned_videos[0].channel is not None
        assert rescanned_videos[0].channel.slug == "asmongold-tv"


def test_apply_sync_item_assigns_series_from_playlist_membership(tmp_path: Path, monkeypatch):
    source_path = tmp_path / "library" / "battle.mp4"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"source")

    def fake_settings() -> Settings:
        return Settings(
            mounted_roots=[],
            config_dir=tmp_path / "config",
            cache_dir=tmp_path / "cache",
            background_tasks_enabled=False,
        )

    async def fake_ryd(*args, **kwargs):
        return None

    async def fake_channel_about(*args, **kwargs):
        return None

    async def fake_channel_details(*args, **kwargs):
        return {
            "snippet": {
                "title": "FRANKIEonPCin1080p",
                "description": "Channel description",
                "thumbnails": {},
            },
            "statistics": {
                "subscriberCount": "3400000",
                "videoCount": "214",
                "viewCount": "501000000",
            },
            "brandingSettings": {"image": {}},
        }

    async def fake_playlist_memberships(*args, **kwargs):
        return [
            {
                "id": "uploads",
                "title": "Uploads",
                "positions": {"bridge12345a": 47},
            },
            {
                "id": "dayz",
                "title": "DayZ Mod (FRANKIEonPC)",
                "positions": {"bridge12345a": 47},
            },
        ]

    async def fake_top_comments(*args, **kwargs):
        return []

    monkeypatch.setattr(sync_service, "get_settings", fake_settings)
    monkeypatch.setattr(sync_service, "fetch_return_youtube_dislike_details", fake_ryd)
    monkeypatch.setattr(sync_service, "fetch_channel_about_details", fake_channel_about)
    monkeypatch.setattr(sync_service, "fetch_channel_details", fake_channel_details)
    monkeypatch.setattr(sync_service, "fetch_channel_playlist_memberships", fake_playlist_memberships)
    monkeypatch.setattr(sync_service, "fetch_top_comments", fake_top_comments)
    monkeypatch.setattr(sync_service, "generate_thumbnail", lambda *args, **kwargs: None)

    with make_session(tmp_path) as db:
        channel = Channel(name="Unknown Channel", slug="unknown-channel")
        db.add(channel)
        db.flush()

        video = Video(
            title="BATTLE OF THE BRIDGE! - Arma 2: DayZ Mod - Ep 48",
            slug="battle-of-the-bridge",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=1200,
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            VideoFile(
                video_id=video.id,
                absolute_path=str(source_path),
                relative_path="battle.mp4",
                file_size=source_path.stat().st_size,
                fingerprint="p" * 64,
            )
        )
        db.commit()
        db.refresh(video)

        item = {
            "id": "bridge12345a",
            "snippet": {
                "title": "BATTLE OF THE BRIDGE! - Arma 2: DayZ Mod - Ep.48",
                "channelTitle": "FRANKIEonPCin1080p",
                "channelId": "channel-frankie",
                "publishedAt": "2026-04-11T12:00:00Z",
                "description": "Matched metadata",
                "thumbnails": {},
            },
            "statistics": {
                "viewCount": "1234",
                "likeCount": "56",
            },
            "_waytube_duration_seconds": 1200,
            "_waytube_source": "youtube-api",
        }

        async def run() -> None:
            async with httpx.AsyncClient() as client:
                await apply_sync_item(
                    db,
                    video,
                    item,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                    api_key="test-api-key",
                    playlist_cache={},
                    confidence=0.93,
                    reasons=["title", "duration-tight"],
                    status="matched",
                )

        asyncio.run(run())

        refreshed_video = db.get(Video, video.id)
        refreshed_match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.video_id == video.id))
        assigned_series = db.get(Series, refreshed_video.series_id) if refreshed_video and refreshed_video.series_id else None

        assert refreshed_video is not None
        assert refreshed_match is not None
        assert assigned_series is not None
        assert assigned_series.name == "DayZ Mod (FRANKIEonPC)"
        assert refreshed_video.episode_number == 48
        assert "playlist-membership" in (refreshed_match.reasons or [])


def test_apply_sync_item_enriches_channel_from_about_without_api(tmp_path: Path, monkeypatch):
    source_path = tmp_path / "library" / "asmongold.mp4"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"source")

    def fake_settings() -> Settings:
        return Settings(
            mounted_roots=[],
            config_dir=tmp_path / "config",
            cache_dir=tmp_path / "cache",
            background_tasks_enabled=False,
        )

    async def fake_ryd(*args, **kwargs):
        return None

    async def fake_channel_about(*args, **kwargs):
        return {
            "title": "Asmongold TV",
            "description": "Channel description from about",
            "joined_at": datetime(2019, 12, 9),
            "canonical_url": "https://www.youtube.com/@AsmonTV",
            "links": [{"title": "X", "url": "https://x.com/asmongold"}],
            "avatar_url": "https://yt3.googleusercontent.com/avatar=s176-c-k-c0x00ffffff-no-rj",
            "banner_url": "https://yt3.googleusercontent.com/banner=w2120",
            "subscriber_count": 4_490_000,
            "video_count": 6_945,
            "view_count": 5_135_286_525,
        }

    monkeypatch.setattr(sync_service, "get_settings", fake_settings)
    monkeypatch.setattr(sync_service, "fetch_return_youtube_dislike_details", fake_ryd)
    monkeypatch.setattr(sync_service, "fetch_channel_about_details", fake_channel_about)
    monkeypatch.setattr(sync_service, "generate_thumbnail", lambda *args, **kwargs: None)

    with make_session(tmp_path) as db:
        channel = Channel(name="Unknown Channel", slug="unknown-channel")
        db.add(channel)
        db.flush()

        video = Video(
            title="British Navy is a joke now",
            slug="british-navy-is-a-joke-now-channel-fallback",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=900,
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            VideoFile(
                video_id=video.id,
                absolute_path=str(source_path),
                relative_path="asmongold.mp4",
                file_size=source_path.stat().st_size,
                fingerprint="q" * 64,
            )
        )
        db.commit()
        db.refresh(video)

        item = {
            "id": "asmongold123",
            "snippet": {
                "title": "British Navy is a joke now",
                "channelTitle": "Asmongold TV",
                "channelId": "channel-asmongold",
                "publishedAt": "2026-04-06T12:00:00Z",
                "description": "Matched metadata",
                "thumbnails": {},
            },
            "statistics": {
                "viewCount": "1234",
                "likeCount": "56",
            },
            "_waytube_duration_seconds": 900,
            "_waytube_source": "watch-page",
        }

        async def run() -> None:
            async with httpx.AsyncClient() as client:
                await apply_sync_item(
                    db,
                    video,
                    item,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                    api_key=None,
                    allow_fallback_art=True,
                    confidence=0.93,
                    reasons=["channel", "duration-tight"],
                    status="matched",
                )

        asyncio.run(run())

        refreshed_video = db.get(Video, video.id)
        refreshed_channel = db.get(Channel, refreshed_video.channel_id) if refreshed_video else None
        channel_snapshot = db.scalar(
            select(YouTubeChannelSnapshot).where(YouTubeChannelSnapshot.youtube_channel_id == "channel-asmongold")
        )

        assert refreshed_channel is not None
        assert refreshed_channel.name == "Asmongold TV"
        assert refreshed_channel.description == "Channel description from about"
        assert refreshed_channel.avatar_url == "https://yt3.googleusercontent.com/avatar=s176-c-k-c0x00ffffff-no-rj"
        assert refreshed_channel.banner_url == "https://yt3.googleusercontent.com/banner=w2120"

        assert channel_snapshot is not None
        assert channel_snapshot.title == "Asmongold TV"
        assert channel_snapshot.description == "Channel description from about"
        assert channel_snapshot.avatar_url == "https://yt3.googleusercontent.com/avatar=s176-c-k-c0x00ffffff-no-rj"
        assert channel_snapshot.banner_url == "https://yt3.googleusercontent.com/banner=w2120"
        assert channel_snapshot.subscriber_count == 4_490_000
        assert channel_snapshot.video_count == 6_945
        assert channel_snapshot.view_count == 5_135_286_525
        assert channel_snapshot.canonical_url == "https://www.youtube.com/@AsmonTV"
        assert channel_snapshot.links == [{"title": "X", "url": "https://x.com/asmongold"}]


def test_apply_sync_item_skips_channel_detail_api_when_playlist_cache_is_disabled(tmp_path: Path, monkeypatch):
    source_path = tmp_path / "library" / "ufd-channel-refresh.mp4"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"source")

    def fake_settings() -> Settings:
        return Settings(
            mounted_roots=[],
            config_dir=tmp_path / "config",
            cache_dir=tmp_path / "cache",
            background_tasks_enabled=False,
        )

    async def fake_ryd(*args, **kwargs):
        return None

    async def fake_channel_about(*args, **kwargs):
        return {
            "title": "UFD Tech",
            "description": "Fallback about description",
            "avatar_url": "https://example.com/ufd-avatar.jpg",
            "banner_url": "https://example.com/ufd-banner.jpg",
            "subscriber_count": 215000,
            "video_count": 1940,
            "view_count": 45500000,
        }

    async def fail_channel_details(*args, **kwargs):
        raise AssertionError("channel detail API should not run when playlist cache is disabled")

    monkeypatch.setattr(sync_service, "get_settings", fake_settings)
    monkeypatch.setattr(sync_service, "fetch_return_youtube_dislike_details", fake_ryd)
    monkeypatch.setattr(sync_service, "fetch_channel_about_details", fake_channel_about)
    monkeypatch.setattr(sync_service, "fetch_channel_details", fail_channel_details)
    monkeypatch.setattr(sync_service, "generate_thumbnail", lambda *args, **kwargs: None)

    with make_session(tmp_path) as db:
        channel = Channel(name="Unknown Channel", slug="unknown-channel")
        db.add(channel)
        db.flush()

        video = Video(
            title="The Nvidia Warranty Situation is Crazy",
            slug="the-nvidia-warranty-situation-is-crazy-channel-refresh",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=19 * 60 + 3,
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            VideoFile(
                video_id=video.id,
                absolute_path=str(source_path),
                relative_path="ufd-channel-refresh.mp4",
                file_size=source_path.stat().st_size,
                fingerprint="ufdchannelrefresh" * 4,
            )
        )
        db.commit()
        db.refresh(video)

        item = {
            "id": "ufd123abc45",
            "snippet": {
                "title": "The Nvidia Warranty Situation is Crazy",
                "channelTitle": "UFD Tech",
                "channelId": "channel-ufd-tech",
                "publishedAt": "2026-04-14T12:00:00Z",
                "description": "Matched metadata",
                "thumbnails": {},
            },
            "statistics": {
                "viewCount": "4500",
                "likeCount": "321",
            },
            "_waytube_duration_seconds": 19 * 60 + 3,
            "_waytube_source": "watch-page",
        }

        async def run() -> None:
            async with httpx.AsyncClient() as client:
                await apply_sync_item(
                    db,
                    video,
                    item,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                    api_key="test-api-key",
                    playlist_cache=None,
                    allow_fallback_art=True,
                    confidence=0.94,
                    reasons=["exact-title", "duration-tight", "channel"],
                    status="matched",
                )

        asyncio.run(run())

        refreshed_video = db.get(Video, video.id)
        refreshed_channel = db.get(Channel, refreshed_video.channel_id) if refreshed_video else None
        channel_snapshot = db.scalar(
            select(YouTubeChannelSnapshot).where(YouTubeChannelSnapshot.youtube_channel_id == "channel-ufd-tech")
        )

        assert refreshed_channel is not None
        assert refreshed_channel.name == "UFD Tech"
        assert refreshed_channel.description == "Fallback about description"
        assert refreshed_channel.avatar_url == "https://example.com/ufd-avatar.jpg"
        assert refreshed_channel.banner_url == "https://example.com/ufd-banner.jpg"

        assert channel_snapshot is not None
        assert channel_snapshot.title == "UFD Tech"
        assert channel_snapshot.description == "Fallback about description"
        assert channel_snapshot.avatar_url == "https://example.com/ufd-avatar.jpg"
        assert channel_snapshot.banner_url == "https://example.com/ufd-banner.jpg"


def test_apply_sync_item_uses_ryd_without_api_for_like_dislike_counts(tmp_path: Path, monkeypatch):
    source_path = tmp_path / "library" / "ryd.mp4"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"source")

    def fake_settings() -> Settings:
        return Settings(
            mounted_roots=[],
            config_dir=tmp_path / "config",
            cache_dir=tmp_path / "cache",
            background_tasks_enabled=False,
        )

    async def fake_ryd(*args, **kwargs):
        return {
            "likes": 678,
            "rawLikes": 678,
            "dislikes": 21,
            "rating": 4.91,
        }

    async def fake_channel_about(*args, **kwargs):
        return None

    monkeypatch.setattr(sync_service, "get_settings", fake_settings)
    monkeypatch.setattr(sync_service, "fetch_return_youtube_dislike_details", fake_ryd)
    monkeypatch.setattr(sync_service, "fetch_channel_about_details", fake_channel_about)
    monkeypatch.setattr(sync_service, "generate_thumbnail", lambda *args, **kwargs: None)

    with make_session(tmp_path) as db:
        channel = Channel(name="Unknown Channel", slug="unknown-channel")
        db.add(channel)
        db.flush()

        video = Video(
            title="RYD Test Video",
            slug="ryd-test-video",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=900,
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            VideoFile(
                video_id=video.id,
                absolute_path=str(source_path),
                relative_path="ryd.mp4",
                file_size=source_path.stat().st_size,
                fingerprint="r" * 64,
            )
        )
        db.commit()
        db.refresh(video)

        item = {
            "id": "ryd12345678",
            "snippet": {
                "title": "RYD Test Video",
                "channelTitle": "RYD Channel",
                "channelId": "channel-ryd",
                "publishedAt": "2026-04-06T12:00:00Z",
                "description": "Matched metadata",
                "thumbnails": {},
            },
            "statistics": {
                "viewCount": "1234",
                "likeCount": "56",
            },
            "_waytube_duration_seconds": 900,
            "_waytube_source": "watch-page",
        }

        async def run() -> None:
            async with httpx.AsyncClient() as client:
                await apply_sync_item(
                    db,
                    video,
                    item,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                    api_key=None,
                    confidence=0.93,
                    reasons=["title", "duration-tight"],
                    status="matched",
                )

        asyncio.run(run())

        snapshot = db.scalar(select(YouTubeVideoSnapshot).where(YouTubeVideoSnapshot.youtube_video_id == "ryd12345678"))

        assert snapshot is not None
        assert snapshot.view_count == 1234
        assert snapshot.like_count == 678
        assert snapshot.dislike_count == 21
        assert snapshot.rating == 4.91


def test_sync_video_reorganizes_existing_matched_file_into_selected_library_folder(tmp_path: Path, monkeypatch):
    library_root = tmp_path / "library"
    library_folder = library_root / "youtube"
    library_folder.mkdir(parents=True)
    wrong_path = library_root / "retro-game-corps" / "Could Android Replace the Steam Deck.mp4"
    wrong_path.parent.mkdir(parents=True)
    wrong_path.write_bytes(b"source")

    def fake_settings() -> Settings:
        return Settings(
            mounted_roots=[library_root],
            config_dir=tmp_path / "config",
            cache_dir=tmp_path / "cache",
            background_tasks_enabled=False,
        )

    monkeypatch.setattr(sync_service, "get_settings", fake_settings)

    with make_session(tmp_path) as db:
        root = LibraryRoot(label="Library", path=str(library_root), is_available=True)
        db.add(root)
        db.flush()
        db.add(SelectedFolder(root_id=root.id, relative_path="youtube", is_enabled=True))
        channel = Channel(name="Retro Game Corps", slug="retro-game-corps", inferred_from_path=False)
        db.add(channel)
        db.flush()

        video = Video(
            title="Could Android Replace the Steam Deck?!",
            slug="could-android-replace-the-steam-deck",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=1200,
            is_available=True,
        )
        db.add(video)
        db.flush()
        video_file = VideoFile(
            video_id=video.id,
            absolute_path=str(wrong_path),
            relative_path="retro-game-corps/Could Android Replace the Steam Deck.mp4",
            file_size=wrong_path.stat().st_size,
            fingerprint="9" * 64,
        )
        db.add(video_file)
        db.add(
            YouTubeMatch(
                video_id=video.id,
                youtube_video_id="Q5v10lisr_o",
                youtube_channel_id="channel-rgc",
                status="matched",
                confidence=0.95,
                reasons=["exact-title", "channel"],
                last_synced_at=datetime.utcnow(),
            )
        )
        db.commit()
        db.refresh(video)

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    video,
                    api_key=None,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())

        organized_path = library_folder / "retro-game-corps" / "Could Android Replace the Steam Deck.mp4"
        refreshed_file = db.scalar(select(VideoFile).where(VideoFile.id == video_file.id))

        assert result.status == "matched"
        assert organized_path.exists()
        assert not wrong_path.exists()
        assert refreshed_file is not None
        assert refreshed_file.absolute_path == str(organized_path)
        assert refreshed_file.relative_path == "youtube/retro-game-corps/Could Android Replace the Steam Deck.mp4"


def test_auto_organize_channel_files_skips_live_retention_items(tmp_path: Path, monkeypatch):
    library_root = tmp_path / "library"
    library_root.mkdir(parents=True)
    source_path = library_root / "Incoming clip.mp4"
    source_path.write_bytes(b"source")

    def fake_settings() -> Settings:
        return Settings(
            mounted_roots=[library_root],
            config_dir=tmp_path / "config",
            cache_dir=tmp_path / "cache",
            background_tasks_enabled=False,
        )

    monkeypatch.setattr(sync_service, "get_settings", fake_settings)

    with make_session(tmp_path) as db:
        channel = Channel(name="Asmongold TV", slug="asmongold-tv", inferred_from_path=False)
        db.add(channel)
        db.flush()

        video = Video(
            title="Incoming clip",
            slug="incoming-clip",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            is_available=True,
        )
        db.add(video)
        db.flush()
        video_file = VideoFile(
            video_id=video.id,
            absolute_path=str(source_path),
            relative_path="Incoming clip.mp4",
            file_size=source_path.stat().st_size,
            fingerprint="1" * 64,
        )
        db.add(video_file)
        db.flush()
        db.add(
            RetentionItem(
                video_id=video.id,
                video_file_id=video_file.id,
                original_absolute_path=str(source_path),
                staged_absolute_path=str(library_root / ".halcyon-retention" / "token" / "Incoming clip.mp4"),
                original_relative_path=video_file.relative_path,
                file_size_bytes=source_path.stat().st_size,
                file_fingerprint=video_file.fingerprint,
                delete_after_at=datetime.utcnow() + timedelta(hours=1),
                status="error",
                last_error="Source file missing on disk",
            )
        )
        db.commit()
        db.refresh(video)

        moves = auto_organize_channel_files(db, video=video, channel=channel)
        refreshed_file = db.scalar(select(VideoFile).where(VideoFile.id == video_file.id))

        assert moves == []
        assert refreshed_file is not None
        assert refreshed_file.absolute_path == str(source_path)


def test_auto_organize_channel_files_skips_transient_download_artifacts(tmp_path: Path, monkeypatch):
    library_root = tmp_path / "library"
    library_folder = library_root / "youtube"
    library_folder.mkdir(parents=True)
    source_path = library_folder / "Could Android Replace the Steam Deck fragment.f401.mp4"
    source_path.write_bytes(b"fragment")

    def fake_settings() -> Settings:
        return Settings(
            mounted_roots=[library_root],
            config_dir=tmp_path / "config",
            cache_dir=tmp_path / "cache",
            background_tasks_enabled=False,
        )

    monkeypatch.setattr(sync_service, "get_settings", fake_settings)

    with make_session(tmp_path) as db:
        root = LibraryRoot(label="Library", path=str(library_root), is_available=True)
        db.add(root)
        db.flush()
        db.add(SelectedFolder(root_id=root.id, relative_path="youtube", is_enabled=True))
        channel = Channel(name="Retro Game Corps", slug="retro-game-corps", inferred_from_path=False)
        db.add(channel)
        db.flush()

        video = Video(
            title="Could Android Replace the Steam Deck",
            slug="could-android-replace-the-steam-deck",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            is_available=True,
        )
        db.add(video)
        db.flush()
        video_file = VideoFile(
            video_id=video.id,
            absolute_path=str(source_path),
            relative_path="youtube/Could Android Replace the Steam Deck fragment.f401.mp4",
            file_size=source_path.stat().st_size,
            fingerprint="2" * 64,
        )
        db.add(video_file)
        db.commit()
        db.refresh(video)

        moves = auto_organize_channel_files(db, video=video, channel=channel)
        refreshed_file = db.scalar(select(VideoFile).where(VideoFile.id == video_file.id))

        assert moves == []
        assert refreshed_file is not None
        assert refreshed_file.absolute_path == str(source_path)
        assert source_path.exists()


def test_auto_organize_channel_files_relinks_existing_canonical_duplicate(tmp_path: Path, monkeypatch):
    library_root = tmp_path / "library"
    library_folder = library_root / "youtube"
    library_folder.mkdir(parents=True)
    source_path = library_root / "asmongold-tv" / "Why I got banned on Twitch.mp4"
    target_path = library_folder / "asmongold-tv" / "Why I got banned on Twitch.mp4"
    source_path.parent.mkdir(parents=True)
    target_path.parent.mkdir(parents=True)
    source_path.write_bytes(b"same-video")
    target_path.write_bytes(b"same-video")

    def fake_settings() -> Settings:
        return Settings(
            mounted_roots=[library_root],
            config_dir=tmp_path / "config",
            cache_dir=tmp_path / "cache",
            background_tasks_enabled=False,
        )

    monkeypatch.setattr(sync_service, "get_settings", fake_settings)

    with make_session(tmp_path) as db:
        root = LibraryRoot(label="Library", path=str(library_root), is_available=True)
        db.add(root)
        db.flush()
        db.add(SelectedFolder(root_id=root.id, relative_path="youtube", is_enabled=True))
        channel = Channel(name="Asmongold TV", slug="asmongold-tv", inferred_from_path=False)
        db.add(channel)
        db.flush()

        video = Video(
            title="Why I got banned on Twitch",
            slug="why-i-got-banned-on-twitch",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            is_available=True,
        )
        db.add(video)
        db.flush()
        video_file = VideoFile(
            video_id=video.id,
            absolute_path=str(source_path),
            relative_path="asmongold-tv/Why I got banned on Twitch.mp4",
            file_size=source_path.stat().st_size,
            fingerprint=fingerprint_file(source_path),
        )
        db.add(video_file)
        db.commit()
        db.refresh(video)

        moves = auto_organize_channel_files(db, video=video, channel=channel)
        refreshed_file = db.scalar(select(VideoFile).where(VideoFile.id == video_file.id))

        assert moves == []
        assert refreshed_file is not None
        assert refreshed_file.absolute_path == str(target_path)
        assert refreshed_file.relative_path == "youtube/asmongold-tv/Why I got banned on Twitch.mp4"
        assert target_path.exists()
        assert not source_path.exists()


def test_sync_scope_orphans_reorganizes_existing_matched_file(tmp_path: Path, monkeypatch):
    library_root = tmp_path / "library"
    library_folder = library_root / "youtube"
    library_folder.mkdir(parents=True)
    wrong_path = library_root / "the-phawx" / "Asus Zenbook A16 Review - Snapdragon X2 Elite Extreme.mp4"
    wrong_path.parent.mkdir(parents=True)
    wrong_path.write_bytes(b"source")

    def fake_settings() -> Settings:
        return Settings(
            mounted_roots=[library_root],
            config_dir=tmp_path / "config",
            cache_dir=tmp_path / "cache",
            background_tasks_enabled=False,
        )

    monkeypatch.setattr(sync_service, "get_settings", fake_settings)

    with make_session(tmp_path) as db:
        root = LibraryRoot(label="Library", path=str(library_root), is_available=True)
        db.add(root)
        db.flush()
        db.add(SelectedFolder(root_id=root.id, relative_path="youtube", is_enabled=True))
        settings_row = SyncSettings(automatic_detection_enabled=True, automatic_sync_enabled=False)
        db.add(settings_row)
        channel = Channel(name="The Phawx", slug="the-phawx", inferred_from_path=False)
        db.add(channel)
        db.flush()

        video = Video(
            title="Asus Zenbook A16 Review - Snapdragon X2 Elite Extreme",
            slug="asus-zenbook-a16-review-snapdragon-x2-elite-extreme",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=1500,
            is_available=True,
        )
        db.add(video)
        db.flush()
        video_file = VideoFile(
            video_id=video.id,
            absolute_path=str(wrong_path),
            relative_path="the-phawx/Asus Zenbook A16 Review - Snapdragon X2 Elite Extreme.mp4",
            file_size=wrong_path.stat().st_size,
            fingerprint="3" * 64,
        )
        db.add(video_file)
        db.add(
            YouTubeMatch(
                video_id=video.id,
                youtube_video_id="-KeYWbfhixo",
                youtube_channel_id="channel-the-phawx",
                status="matched",
                confidence=0.99,
                reasons=["known-channel"],
                last_synced_at=datetime.utcnow(),
            )
        )
        db.add(
            YouTubeVideoSnapshot(
                youtube_video_id="-KeYWbfhixo",
                youtube_channel_id="channel-the-phawx",
                title="Asus Zenbook A16 Review - Snapdragon X2 Elite Extreme",
                published_at=datetime.utcnow(),
                published_at_source="youtube-api",
                duration_seconds=1500,
                thumbnail_url="https://example.com/the-phawx-thumb.jpg",
                view_count=1000,
                like_count=120,
                dislike_count=4,
                fetched_at=datetime.utcnow(),
            )
        )
        db.add(
            YouTubeChannelSnapshot(
                youtube_channel_id="channel-the-phawx",
                title="The Phawx",
                avatar_url="https://example.com/the-phawx-avatar.jpg",
                banner_url="https://example.com/the-phawx-banner.jpg",
            )
        )
        db.commit()

        async def run() -> SyncJob:
            return await sync_scope(db, scope="orphans", target_id=None, api_key=None)

        job = asyncio.run(run())
        organized_path = library_folder / "the-phawx" / "Asus Zenbook A16 Review - Snapdragon X2 Elite Extreme.mp4"
        refreshed_file = db.scalar(select(VideoFile).where(VideoFile.id == video_file.id))

        assert job.status == "completed"
        assert organized_path.exists()
        assert not wrong_path.exists()
        assert refreshed_file is not None
        assert refreshed_file.absolute_path == str(organized_path)
        assert refreshed_file.relative_path == "youtube/the-phawx/Asus Zenbook A16 Review - Snapdragon X2 Elite Extreme.mp4"


def test_apply_sync_item_merges_duplicate_youtube_video_records(tmp_path: Path, monkeypatch):
    target_path = tmp_path / "library" / "target.mp4"
    duplicate_path = tmp_path / "library" / "duplicate.mp4"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(b"same-video")
    duplicate_path.write_bytes(b"same-video")

    def fake_settings() -> Settings:
        return Settings(
            mounted_roots=[],
            config_dir=tmp_path / "config",
            cache_dir=tmp_path / "cache",
            background_tasks_enabled=False,
        )

    async def fake_ryd(*args, **kwargs):
        return None

    async def fake_channel_about(*args, **kwargs):
        return None

    monkeypatch.setattr(sync_service, "get_settings", fake_settings)
    monkeypatch.setattr(sync_service, "fetch_return_youtube_dislike_details", fake_ryd)
    monkeypatch.setattr(sync_service, "fetch_channel_about_details", fake_channel_about)
    monkeypatch.setattr(sync_service, "generate_thumbnail", lambda *args, **kwargs: None)

    with make_session(tmp_path) as db:
        user = UserProfile(name="tester", display_name="Tester", accent_color="#fff")
        channel = Channel(name="Unknown Channel", slug="unknown-channel")
        db.add_all([user, channel])
        db.flush()

        target_video = Video(
            title="DayZ Part 1",
            slug="dayz-part-1",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=900,
            is_available=True,
        )
        duplicate_video = Video(
            title="DayZ Part 1 duplicate",
            slug="dayz-part-1-duplicate",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=900,
            is_available=True,
        )
        db.add_all([target_video, duplicate_video])
        db.flush()

        db.add_all(
            [
                VideoFile(
                    video_id=target_video.id,
                    absolute_path=str(target_path),
                    relative_path="target.mp4",
                    file_size=target_path.stat().st_size,
                    fingerprint="a" * 64,
                ),
                VideoFile(
                    video_id=duplicate_video.id,
                    absolute_path=str(duplicate_path),
                    relative_path="duplicate.mp4",
                    file_size=duplicate_path.stat().st_size,
                    fingerprint="b" * 64,
                ),
                YouTubeMatch(
                    video_id=duplicate_video.id,
                    youtube_video_id="dup123",
                    youtube_channel_id="channel-psi",
                    status="matched",
                    confidence=0.91,
                    reasons=["title"],
                ),
                WatchProgress(
                    user_id=user.id,
                    video_id=duplicate_video.id,
                    position_seconds=321,
                    completed=False,
                ),
            ]
        )
        db.commit()
        db.refresh(target_video)

        item = {
            "id": "dup123",
            "snippet": {
                "title": "ARMA 2 DayZ Overpoch Mod - Series 2 - Part 1 - The Curse!",
                "channelTitle": "PsiSyndicate",
                "channelId": "channel-psi",
                "publishedAt": "2026-04-11T12:00:00Z",
                "description": "Matched metadata",
                "thumbnails": {},
            },
            "statistics": {
                "viewCount": "1234",
                "likeCount": "56",
            },
            "_waytube_duration_seconds": 900,
            "_waytube_source": "watch-page",
        }

        async def run() -> None:
            async with httpx.AsyncClient() as client:
                await apply_sync_item(
                    db,
                    target_video,
                    item,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                    api_key=None,
                    confidence=0.93,
                    reasons=["title", "duration-tight"],
                    status="matched",
                )

        asyncio.run(run())

        remaining_videos = db.scalars(select(Video).order_by(Video.id.asc())).all()
        merged_match = db.scalar(select(YouTubeMatch).where(YouTubeMatch.youtube_video_id == "dup123"))
        merged_progress = db.scalar(
            select(WatchProgress).where(
                WatchProgress.user_id == user.id,
                WatchProgress.video_id == target_video.id,
            )
        )

        assert len(remaining_videos) == 1
        assert merged_match is not None
        assert merged_match.video_id == target_video.id
        assert merged_progress is not None
        assert merged_progress.position_seconds == 321
        assert target_path.exists()
        assert not duplicate_path.exists()


def test_apply_sync_item_clears_stale_duplicate_match_before_assigning(tmp_path: Path, monkeypatch):
    def fake_settings() -> Settings:
        return Settings(
            mounted_roots=[],
            config_dir=tmp_path / "config",
            cache_dir=tmp_path / "cache",
            background_tasks_enabled=False,
        )

    async def fake_ryd(*args, **kwargs):
        return None

    async def fake_channel_about(*args, **kwargs):
        return None

    monkeypatch.setattr(sync_service, "get_settings", fake_settings)
    monkeypatch.setattr(sync_service, "fetch_return_youtube_dislike_details", fake_ryd)
    monkeypatch.setattr(sync_service, "fetch_channel_about_details", fake_channel_about)
    monkeypatch.setattr(sync_service, "generate_thumbnail", lambda *args, **kwargs: None)

    with make_session(tmp_path) as db:
        channel = Channel(name="Unknown Channel", slug="unknown-channel")
        db.add(channel)
        db.flush()

        target_video = Video(
            title="Target video",
            slug="target-video",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=900,
            is_available=True,
        )
        db.add(target_video)
        db.flush()

        db.add(
            YouTubeMatch(
                video_id=target_video.id + 999,
                youtube_video_id="dup-stale-123",
                youtube_channel_id="channel-psi",
                status="matched",
                confidence=0.5,
                reasons=["stale"],
            )
        )
        db.commit()
        db.refresh(target_video)

        item = {
            "id": "dup-stale-123",
            "snippet": {
                "title": "Target video",
                "channelTitle": "PsiSyndicate",
                "channelId": "channel-psi",
                "publishedAt": "2026-04-11T12:00:00Z",
                "description": "Matched metadata",
                "thumbnails": {},
            },
            "statistics": {
                "viewCount": "1234",
                "likeCount": "56",
            },
            "_waytube_duration_seconds": 900,
            "_waytube_source": "watch-page",
        }

        async def run() -> None:
            async with httpx.AsyncClient() as client:
                await apply_sync_item(
                    db,
                    target_video,
                    item,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                    api_key=None,
                    confidence=0.93,
                    reasons=["title", "duration-tight"],
                    status="matched",
                )

        asyncio.run(run())

        surviving_matches = db.scalars(
            select(YouTubeMatch).where(YouTubeMatch.youtube_video_id == "dup-stale-123").order_by(YouTubeMatch.id.asc())
        ).all()

        assert len(surviving_matches) == 1
        assert surviving_matches[0].video_id == target_video.id


def test_apply_sync_item_skips_comment_enrichment_until_engagement_refresh_is_due(tmp_path: Path, monkeypatch):
    def fake_settings() -> Settings:
        return Settings(
            mounted_roots=[],
            config_dir=tmp_path / "config",
            cache_dir=tmp_path / "cache",
            background_tasks_enabled=False,
        )

    async def fake_ryd(*args, **kwargs):
        return None

    async def fake_channel_about(*args, **kwargs):
        return None

    async def fake_channel_details(*args, **kwargs):
        return {
            "snippet": {
                "title": "Initial Channel",
                "description": "Initial channel description",
                "thumbnails": {},
            },
            "statistics": {},
            "brandingSettings": {"image": {}},
            "contentDetails": {"relatedPlaylists": {}},
        }

    async def fail_fetch_top_comments(*args, **kwargs):
        raise AssertionError("comments should not be fetched on initial metadata sync")

    monkeypatch.setattr(sync_service, "get_settings", fake_settings)
    monkeypatch.setattr(sync_service, "fetch_return_youtube_dislike_details", fake_ryd)
    monkeypatch.setattr(sync_service, "fetch_channel_about_details", fake_channel_about)
    monkeypatch.setattr(sync_service, "fetch_channel_details", fake_channel_details)
    monkeypatch.setattr(sync_service, "fetch_top_comments", fail_fetch_top_comments)
    monkeypatch.setattr(sync_service, "generate_thumbnail", lambda *args, **kwargs: None)

    with make_session(tmp_path) as db:
        channel = Channel(name="Unknown Channel", slug="unknown-channel")
        db.add(channel)
        db.flush()

        video = Video(
            title="Initial metadata sync",
            slug="initial-metadata-sync",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=900,
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            VideoFile(
                video_id=video.id,
                absolute_path=str(tmp_path / "initial-sync.mp4"),
                relative_path="initial-sync.mp4",
                file_size=123,
                fingerprint="d" * 64,
            )
        )
        db.commit()
        db.refresh(video)

        item = {
            "id": "initial-sync-yt",
            "snippet": {
                "title": "Initial metadata sync",
                "channelTitle": "Initial Channel",
                "channelId": "initial-channel-id",
                "publishedAt": "2026-04-14T12:00:00Z",
                "description": "Initial metadata",
                "thumbnails": {},
            },
            "statistics": {
                "viewCount": "1234",
                "likeCount": "56",
            },
            "_waytube_duration_seconds": 900,
            "_waytube_source": "youtube-api",
        }

        async def run() -> None:
            async with httpx.AsyncClient() as client:
                await apply_sync_item(
                    db,
                    video,
                    item,
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                    api_key="test-api-key",
                    confidence=0.93,
                    reasons=["title", "duration-tight"],
                    status="matched",
                )

        asyncio.run(run())

        stored_comments = db.scalars(
            select(YouTubeCommentSnapshot).where(YouTubeCommentSnapshot.youtube_video_id == "initial-sync-yt")
        ).all()

        assert stored_comments == []


def test_video_requires_refresh_ignores_periodic_engagement_window_without_api_key(tmp_path: Path):
    with make_session(tmp_path) as db:
        channel = Channel(name="PowerfulJRE", slug="powerfuljre")
        db.add(channel)
        db.flush()

        video = Video(
            title="Joe Rogan Experience #2484 - David Cross",
            slug="joe-rogan-experience-2484-david-cross",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            published_at=datetime.utcnow() - timedelta(hours=8),
            duration_seconds=2 * 3600 + 23 * 60 + 4,
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            YouTubeMatch(
                video_id=video.id,
                youtube_video_id="jre2484abc1",
                youtube_channel_id="channel-jre",
                status="matched",
                confidence=0.96,
            )
        )
        db.add(
            YouTubeVideoSnapshot(
                youtube_video_id="jre2484abc1",
                youtube_channel_id="channel-jre",
                title="Joe Rogan Experience #2484 - David Cross",
                duration_seconds=2 * 3600 + 23 * 60 + 4,
                thumbnail_url="https://i.ytimg.com/vi/jre2484abc1/hqdefault.jpg",
                published_at=datetime.utcnow() - timedelta(hours=8),
                published_at_source="youtube-api",
                view_count=32000,
                like_count=1200,
                fetched_at=datetime.utcnow() - timedelta(hours=7),
            )
        )
        db.add(
            YouTubeChannelSnapshot(
                youtube_channel_id="channel-jre",
                title="PowerfulJRE",
            )
        )
        db.commit()

        assert sync_service.video_requires_refresh(db, video, api_key_available=False) is False


def test_sync_video_uses_api_refresh_for_periodic_engagement_window(tmp_path: Path, monkeypatch):
    def fake_settings() -> Settings:
        return Settings(
            mounted_roots=[],
            config_dir=tmp_path / "config",
            cache_dir=tmp_path / "cache",
            background_tasks_enabled=False,
        )

    async def fail_watch_page(*args, **kwargs):
        raise AssertionError("watch-page refresh should not run for engagement-only refresh")

    async def fake_video_details(*args, **kwargs):
        return {
            "id": "jre2484abc1",
            "snippet": {
                "title": "Joe Rogan Experience #2484 - David Cross",
                "channelTitle": "PowerfulJRE",
                "channelId": "channel-jre",
                "publishedAt": "2026-04-16T01:00:00Z",
                "description": "Updated metadata",
                "thumbnails": {},
            },
            "statistics": {
                "viewCount": "33000",
                "likeCount": "1300",
            },
            "_waytube_duration_seconds": 2 * 3600 + 23 * 60 + 4,
            "_waytube_source": "youtube-api",
        }

    async def fake_top_comments(*args, **kwargs):
        return [
            {
                "snippet": {
                    "topLevelComment": {
                        "id": "comment-1",
                        "snippet": {
                            "authorDisplayName": "Commenter",
                            "textDisplay": "Fresh comment",
                            "likeCount": 5,
                            "publishedAt": "2026-04-16T07:00:00Z",
                        },
                    },
                    "totalReplyCount": 0,
                }
            }
        ]

    async def fake_ryd(*args, **kwargs):
        return None

    async def fail_channel_enrichment(*args, **kwargs):
        raise AssertionError("channel metadata should stay settled during engagement refresh")

    monkeypatch.setattr(sync_service, "get_settings", fake_settings)
    monkeypatch.setattr(sync_service, "fetch_watch_page_candidate", fail_watch_page)
    monkeypatch.setattr(sync_service, "fetch_video_details_by_id", fake_video_details)
    monkeypatch.setattr(sync_service, "fetch_top_comments", fake_top_comments)
    monkeypatch.setattr(sync_service, "fetch_return_youtube_dislike_details", fake_ryd)
    monkeypatch.setattr(sync_service, "fetch_channel_details", fail_channel_enrichment)
    monkeypatch.setattr(sync_service, "fetch_channel_about_details", fail_channel_enrichment)
    monkeypatch.setattr(sync_service, "generate_thumbnail", lambda *args, **kwargs: None)

    with make_session(tmp_path) as db:
        channel = Channel(name="PowerfulJRE", slug="powerfuljre")
        db.add(channel)
        db.flush()

        video = Video(
            title="Joe Rogan Experience #2484 - David Cross",
            slug="joe-rogan-experience-2484-david-cross-refresh",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            published_at=datetime.utcnow() - timedelta(hours=8),
            duration_seconds=2 * 3600 + 23 * 60 + 4,
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            VideoFile(
                video_id=video.id,
                absolute_path=str(tmp_path / "jre-refresh.mp4"),
                relative_path="jre-refresh.mp4",
                file_size=123,
                fingerprint="e" * 64,
            )
        )
        db.add(
            YouTubeMatch(
                video_id=video.id,
                youtube_video_id="jre2484abc1",
                youtube_channel_id="channel-jre",
                status="matched",
                confidence=0.96,
                reasons=["exact-title", "duration-tight", "channel"],
            )
        )
        db.add(
            YouTubeVideoSnapshot(
                youtube_video_id="jre2484abc1",
                youtube_channel_id="channel-jre",
                title="Joe Rogan Experience #2484 - David Cross",
                description="Existing metadata",
                duration_seconds=2 * 3600 + 23 * 60 + 4,
                thumbnail_url="https://i.ytimg.com/vi/jre2484abc1/hqdefault.jpg",
                published_at=datetime.utcnow() - timedelta(hours=8),
                published_at_source="youtube-api",
                view_count=32000,
                like_count=1200,
                fetched_at=datetime.utcnow() - timedelta(hours=7),
            )
        )
        db.add(
            YouTubeChannelSnapshot(
                youtube_channel_id="channel-jre",
                title="PowerfulJRE",
            )
        )
        db.commit()

        async def run() -> YouTubeMatch:
            async with httpx.AsyncClient() as client:
                return await sync_video(
                    db,
                    video,
                    api_key="test-api-key",
                    comment_limit=25,
                    requests_per_second=3,
                    client=client,
                )

        result = asyncio.run(run())
        stored_comments = db.scalars(
            select(YouTubeCommentSnapshot).where(YouTubeCommentSnapshot.youtube_video_id == "jre2484abc1")
        ).all()
        refreshed_snapshot = db.scalar(
            select(YouTubeVideoSnapshot).where(YouTubeVideoSnapshot.youtube_video_id == "jre2484abc1")
        )

        assert result.status == "matched"
        assert refreshed_snapshot is not None
        assert refreshed_snapshot.title == "Joe Rogan Experience #2484 - David Cross"
        assert refreshed_snapshot.description == "Existing metadata"
        assert refreshed_snapshot.published_at_source == "youtube-api"
        assert refreshed_snapshot.thumbnail_url == "https://i.ytimg.com/vi/jre2484abc1/hqdefault.jpg"
        assert refreshed_snapshot.view_count == 33000
        assert refreshed_snapshot.like_count == 1300
        assert len(stored_comments) == 1
        assert stored_comments[0].body == "Fresh comment"


def test_apply_sync_item_fetches_replies_beyond_inline_batch(tmp_path: Path, monkeypatch):
    def fake_settings() -> Settings:
        return Settings(
            mounted_roots=[],
            config_dir=tmp_path / "config",
            cache_dir=tmp_path / "cache",
            background_tasks_enabled=False,
        )

    async def fake_ryd(*args, **kwargs):
        return None

    async def fail_channel_enrichment(*args, **kwargs):
        raise AssertionError("engagement refresh should not re-fetch channel metadata")

    async def fake_fetch_top_comments(*args, **kwargs):
        return [
            {
                "snippet": {
                    "totalReplyCount": 7,
                    "topLevelComment": {
                        "id": "top-comment-1",
                        "snippet": {
                            "authorDisplayName": "Top Commenter",
                            "textDisplay": "Top level comment",
                            "likeCount": 9,
                            "publishedAt": "2026-04-14T12:00:00Z",
                        },
                    },
                },
                "replies": {
                    "comments": [
                        {
                            "id": f"reply-{index}",
                            "snippet": {
                                "authorDisplayName": f"Reply {index}",
                                "textDisplay": f"Reply body {index}",
                                "likeCount": index,
                                "publishedAt": "2026-04-14T12:00:00Z",
                            },
                        }
                        for index in range(1, 6)
                    ]
                },
            }
        ]

    async def fake_fetch_comment_replies(*args, **kwargs):
        return [
            {
                "id": f"reply-{index}",
                "snippet": {
                    "authorDisplayName": f"Reply {index}",
                    "textDisplay": f"Reply body {index}",
                    "likeCount": index,
                    "publishedAt": "2026-04-14T12:00:00Z",
                },
            }
            for index in range(1, 8)
        ]

    monkeypatch.setattr(sync_service, "get_settings", fake_settings)
    monkeypatch.setattr(sync_service, "fetch_return_youtube_dislike_details", fake_ryd)
    monkeypatch.setattr(sync_service, "fetch_channel_about_details", fail_channel_enrichment)
    monkeypatch.setattr(sync_service, "fetch_channel_details", fail_channel_enrichment)
    monkeypatch.setattr(sync_service, "fetch_channel_playlist_memberships", fail_channel_enrichment)
    monkeypatch.setattr(sync_service, "fetch_top_comments", fake_fetch_top_comments)
    monkeypatch.setattr(sync_service, "fetch_comment_replies", fake_fetch_comment_replies)
    monkeypatch.setattr(sync_service, "generate_thumbnail", lambda *args, **kwargs: None)

    with make_session(tmp_path) as db:
        channel = Channel(name="Unknown Channel", slug="unknown-channel")
        db.add(channel)
        db.flush()

        video = Video(
            title="Reply test video",
            slug="reply-test-video",
            channel_id=channel.id,
            created_at=datetime.utcnow(),
            duration_seconds=900,
            is_available=True,
        )
        db.add(video)
        db.flush()
        db.add(
            VideoFile(
                video_id=video.id,
                absolute_path=str(tmp_path / "reply-test.mp4"),
                relative_path="reply-test.mp4",
                file_size=123,
                fingerprint="c" * 64,
            )
        )
        db.add(
            YouTubeMatch(
                video_id=video.id,
                youtube_video_id="reply-test-yt",
                youtube_channel_id="reply-channel-id",
                status="matched",
                confidence=0.93,
                reasons=["title", "duration-tight"],
            )
        )
        db.add(
            YouTubeVideoSnapshot(
                youtube_video_id="reply-test-yt",
                youtube_channel_id="reply-channel-id",
                title="Reply test video",
                published_at=datetime.utcnow() - timedelta(days=1),
                published_at_source="watch-page",
                duration_seconds=900,
                thumbnail_url="https://example.com/reply-thumb.jpg",
                view_count=1234,
                like_count=56,
                fetched_at=datetime.utcnow() - timedelta(hours=7),
            )
        )
        db.add(
            YouTubeChannelSnapshot(
                youtube_channel_id="reply-channel-id",
                title="Reply Channel",
            )
        )
        db.commit()
        db.refresh(video)

        item = {
            "id": "reply-test-yt",
            "snippet": {
                "title": "Reply test video",
                "channelTitle": "Reply Channel",
                "channelId": "reply-channel-id",
                "publishedAt": "2026-04-14T12:00:00Z",
                "description": "Reply test",
                "thumbnails": {},
            },
            "statistics": {
                "viewCount": "1234",
                "likeCount": "56",
            },
            "_waytube_duration_seconds": 900,
            "_waytube_source": "watch-page",
        }

        async def run() -> None:
            async with httpx.AsyncClient() as client:
                await apply_sync_item(
                    db,
                    video,
                    item,
                    comment_limit=25,
                    max_replies_per_comment=7,
                    requests_per_second=3,
                    client=client,
                    api_key="test-api-key",
                    playlist_cache={},
                    confidence=0.93,
                    reasons=["title", "duration-tight", "refresh-by-id"],
                    status="matched",
                )

        asyncio.run(run())

        stored_comment = db.scalar(select(YouTubeCommentSnapshot).where(YouTubeCommentSnapshot.youtube_video_id == "reply-test-yt"))
        stored_replies = db.scalars(
            select(YouTubeCommentReplySnapshot)
            .where(YouTubeCommentReplySnapshot.youtube_video_id == "reply-test-yt")
            .order_by(YouTubeCommentReplySnapshot.position.asc(), YouTubeCommentReplySnapshot.id.asc())
        ).all()

        assert stored_comment is not None
        assert stored_comment.reply_count == 7
        assert len(stored_replies) == 7
        assert [reply.youtube_reply_id for reply in stored_replies] == [f"reply-{index}" for index in range(1, 8)]
