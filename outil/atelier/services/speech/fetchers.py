"""Clients publics bornés pour paroles (RSS / YouTube meta / Bluesky ATProto)."""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

from django.conf import settings

from atelier.services.feeds.registry import FeedSpec


class FetcherError(RuntimeError):
    pass


def _http_get(url: str, *, max_bytes: int = 2 * 1024 * 1024) -> bytes:
    """GET HTTPS borné via le même contrôle d'hôtes que le collecteur."""
    from atelier.services.collector import CollectionError, UnsafeDestination, _request_once, validate_source_url
    from atelier.services.network_policy import require_collection_transport

    require_collection_transport()
    validate_source_url(url)
    headers = {
        "Accept": "application/json, application/atom+xml, application/rss+xml, application/xml, text/xml, text/html;q=0.8, */*;q=0.1",
        "Accept-Encoding": "identity",
        "Connection": "close",
        "User-Agent": "TruthPoliticians-private-collector/0.1",
    }
    connection, response = _request_once(url, headers, timeout=30)
    try:
        if response.status != 200:
            raise FetcherError(f"http_{response.status}")
        length = response.getheader("Content-Length")
        if length and int(length) > max_bytes:
            raise FetcherError("content_length_exceeded")
        chunks = []
        size = 0
        while True:
            block = response.read(min(64 * 1024, max_bytes - size + 1))
            if not block:
                break
            size += len(block)
            if size > max_bytes:
                raise FetcherError("stream_limit_exceeded")
            chunks.append(block)
        return b"".join(chunks)
    finally:
        connection.close()


def _parse_feed_xml(raw: bytes) -> list[dict[str, Any]]:
    root = ET.fromstring(raw)
    items = []
    nodes = list(root.findall(".//item")) + list(root.findall(".//{http://www.w3.org/2005/Atom}entry"))
    for node in nodes:
        title = (node.findtext("title") or node.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
        link = ""
        link_el = node.find("link")
        if link_el is not None:
            link = (link_el.get("href") or (link_el.text or "")).strip()
        if not link:
            atom_link = node.find("{http://www.w3.org/2005/Atom}link")
            if atom_link is not None:
                link = (atom_link.get("href") or "").strip()
        description = (
            node.findtext("description")
            or node.findtext("{http://www.w3.org/2005/Atom}summary")
            or ""
        ).strip()
        pub = (
            node.findtext("pubDate")
            or node.findtext("{http://www.w3.org/2005/Atom}updated")
            or node.findtext("{http://www.w3.org/2005/Atom}published")
            or ""
        ).strip()
        author = (
            node.findtext("author")
            or node.findtext("{http://www.w3.org/2005/Atom}author/{http://www.w3.org/2005/Atom}name")
            or ""
        ).strip()
        items.append(
            {
                "stable_id": f"rss:{abs(hash((link, title))) % 10**12}",
                "speaker_label": author or "Source RSS",
                "title": title,
                "exact_quote": description[:1500] or title,
                "excerpt": description[:1500],
                "source_url": link,
                "published": pub,
                "source_role": "relay",
                "capacity": "citation presse/flux",
            }
        )
    return items


def fetch_rss_urls(urls: list[str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for url in urls:
        url = url.strip()
        if not url:
            continue
        raw = _http_get(url)
        items.extend(_parse_feed_xml(raw))
    return items


def fetch_youtube_channel_rss(channel_ids: list[str]) -> list[dict[str, Any]]:
    """Métadonnées via flux RSS public YouTube (pas d'API key, pas de vidéo complète)."""
    items: list[dict[str, Any]] = []
    for channel_id in channel_ids:
        channel_id = channel_id.strip()
        if not channel_id:
            continue
        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        raw = _http_get(url)
        for entry in _parse_feed_xml(raw):
            items.append(
                {
                    **entry,
                    "speaker_label": entry.get("speaker_label") or channel_id,
                    "capacity": "chaîne YouTube officielle",
                    "transcript": "",
                    "transcript_missing": True,
                    "video_id": entry.get("source_url", "").rsplit("=", 1)[-1],
                    "full_video_retained": False,
                    "allow_title_as_quote": False,
                }
            )
    return items


def fetch_bluesky_handles(handles: list[str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for handle in handles:
        handle = handle.strip().lstrip("@")
        if not handle:
            continue
        # Résolution DID puis fil auteur — endpoints publics AT Proto.
        resolve_url = f"https://public.api.bsky.app/xrpc/com.atproto.identity.resolveHandle?{urlencode({'handle': handle})}"
        resolved = json.loads(_http_get(resolve_url).decode("utf-8"))
        did = resolved.get("did")
        if not did:
            raise FetcherError("bluesky_did_missing")
        feed_url = (
            "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed?"
            + urlencode({"actor": did, "limit": "20"})
        )
        payload = json.loads(_http_get(feed_url).decode("utf-8"))
        for row in payload.get("feed") or []:
            post = (row.get("post") or {})
            record = post.get("record") or {}
            text = (record.get("text") or "").strip()
            uri = post.get("uri") or ""
            created = record.get("createdAt") or ""
            items.append(
                {
                    "stable_id": f"bsky:{uri}"[:200],
                    "handle": handle,
                    "speaker_label": handle,
                    "text": text,
                    "exact_quote": text,
                    "occurred_on": created[:10] if created else "",
                    "source_url": f"https://bsky.app/profile/{handle}",
                    "post_id": uri,
                    "repost": bool(row.get("reason")),
                    "capacity": "compte Bluesky public",
                }
            )
    return items


def fetch_configured_feed(spec: FeedSpec) -> dict[str, Any]:
    if spec.adapter == "press_rss":
        urls = list(getattr(settings, "DV_FEED_RSS_URLS", []) or [])
        return {"feed_id": spec.feed_id, "items": fetch_rss_urls(urls)}
    if spec.adapter == "podcast_rss":
        urls = list(getattr(settings, "DV_FEED_PODCAST_URLS", []) or [])
        return {"feed_id": spec.feed_id, "items": fetch_rss_urls(urls)}
    if spec.adapter == "youtube_meta":
        channels = list(getattr(settings, "DV_FEED_YOUTUBE_CHANNELS", []) or [])
        # Autoriser youtube.com dans les hôtes pour ce flux.
        return {"feed_id": spec.feed_id, "items": fetch_youtube_channel_rss(channels)}
    if spec.adapter == "bluesky_public":
        handles = list(getattr(settings, "DV_FEED_BLUESKY_HANDLES", []) or [])
        return {"feed_id": spec.feed_id, "items": fetch_bluesky_handles(handles)}
    raise FetcherError(f"unsupported_adapter:{spec.adapter}")
