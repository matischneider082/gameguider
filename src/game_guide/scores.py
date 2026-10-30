"""Critic aggregates, saved locally after an explicit refresh: from Wikidata with no setup, or IGDB when configured."""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import REPO

CACHE_NAME = ".game-guider-scores.json"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
USER_AGENT = f"GameGuider (https://github.com/{REPO})"  # Wikimedia rejects requests without one
VIDEO_GAME = "Q7889"
# Review aggregators as Wikidata items, preferred first. Single outlets (IGN, GameSpot…) are not aggregates.
AGGREGATORS = {"Q21039459": "OpenCritic", "Q150248": "Metacritic", "Q131917281": "CriticDB"}
OUT_OF_100 = re.compile(r"\s*(\d{1,3}(?:\.\d+)?)\s*/\s*100\s*")


def read_scores(directory: Path) -> dict:
    try:
        data = json.loads((directory / CACHE_NAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def score_for(game, cached: dict) -> dict | None:
    item = cached.get(game.slug)
    if not isinstance(item, dict) or item.get("name") != game.name:
        return None
    score, count = item.get("score"), item.get("count")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 100:
        return None
    if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count <= 0):
        return None
    source = item.get("source")
    return {"score": score, "count": count, "source": source if source in (*AGGREGATORS.values(), "IGDB") else "IGDB"}


def refresh_score(game, directory: Path) -> dict:
    client_id = os.environ.get("IGDB_CLIENT_ID", "").strip()
    token = os.environ.get("IGDB_ACCESS_TOKEN", "").strip()
    candidate = _igdb(game, client_id, token) if client_id and token else _wikidata(game)
    score = score_for(game, {game.slug: candidate})
    if not score:
        raise ValueError("There is no critic score for this game yet.")
    cached = read_scores(directory)
    cached[game.slug] = candidate
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, prefix=".scores-", delete=False) as handle:
        temp_path = Path(handle.name)
        try:
            json.dump(cached, handle, ensure_ascii=False, indent=2)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
    try:
        os.replace(temp_path, directory / CACHE_NAME)
    finally:
        temp_path.unlink(missing_ok=True)
    return score


def _normalize(name: str) -> str:
    return " ".join(name.casefold().split())


def _igdb(game, client_id: str, token: str) -> dict:
    query = f'search {json.dumps(game.name)}; fields name,aggregated_rating,aggregated_rating_count; limit 50;'
    request = urllib.request.Request(
        "https://api.igdb.com/v4/games", data=query.encode("utf-8"),
        headers={"Client-ID": client_id, "Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "text/plain"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            results = json.loads(response.read(1024 * 1024).decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ValueError("Could not fetch the IGDB score. Check your credentials and connection, then try again.") from exc
    if not isinstance(results, list):
        raise ValueError("IGDB returned an unexpected response.")
    matches = [item for item in results if isinstance(item, dict) and isinstance(item.get("name"), str) and _normalize(item["name"]) == _normalize(game.name)]
    if len(matches) != 1:
        raise ValueError("No unique exact title match on IGDB. Check the game title and try again.")
    match = matches[0]
    return {"name": game.name, "score": match.get("aggregated_rating"), "count": match.get("aggregated_rating_count"), "source": "IGDB"}


def _wikidata_get(params: dict) -> dict:
    url = WIKIDATA_API + "?" + urllib.parse.urlencode({**params, "format": "json"})
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        data = json.loads(response.read(4 * 1024 * 1024).decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("unexpected Wikidata response")
    return data


def _wikidata(game) -> dict:
    """The best review aggregate Wikidata records for the video game whose label is exactly the game's name."""
    try:
        found = _wikidata_get({"action": "wbsearchentities", "search": game.name, "language": "en", "type": "item", "limit": "10"})
        ids = [r["id"] for r in found.get("search", []) if isinstance(r, dict) and isinstance(r.get("id"), str)
               and _normalize(str(r.get("label", ""))) == _normalize(game.name)]
        entities = _wikidata_get({"action": "wbgetentities", "ids": "|".join(ids), "props": "claims"}).get("entities", {}) if ids else {}
    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        raise ValueError("Could not fetch a critic score. Check your connection and try again.") from exc
    # shortcut: several games can share a title (remakes); the first in Wikidata's search order wins. Edit the title to pick another.
    for qid in ids:
        claims = entities.get(qid, {}).get("claims", {}) if isinstance(entities.get(qid), dict) else {}
        if VIDEO_GAME not in {_item_id(c.get("mainsnak")) for c in claims.get("P31", [])}:
            continue
        best = None
        for claim in claims.get("P444", []):
            snak, qualifiers = claim.get("mainsnak") or {}, claim.get("qualifiers") or {}
            value = (snak.get("datavalue") or {}).get("value")
            source = next((AGGREGATORS[i] for i in map(_item_id, qualifiers.get("P447", [])) if i in AGGREGATORS), None)
            match = OUT_OF_100.fullmatch(value) if isinstance(value, str) else None
            if claim.get("rank") == "deprecated" or not source or not match:
                continue
            score = float(match.group(1))
            count = next((_amount(q) for q in qualifiers.get("P7887", []) if _amount(q)), None)
            rank = (list(AGGREGATORS.values()).index(source), -(count or 0))  # preferred aggregator, then most reviews
            if best is None or rank < best[0]:
                best = (rank, {"name": game.name, "score": int(score) if score.is_integer() else score, "count": count, "source": source})
        if best:
            return best[1]
        raise ValueError("Wikidata has no critic score for this game yet.")
    raise ValueError("No video game with this exact title on Wikidata. Check the game title and try again.")


def _item_id(snak) -> str | None:
    value = ((snak or {}).get("datavalue") or {}).get("value")
    return value.get("id") if isinstance(value, dict) else None


def _amount(snak) -> int | None:
    value = ((snak or {}).get("datavalue") or {}).get("value")
    try:
        amount = int(float(value["amount"]))
    except (TypeError, KeyError, ValueError, OverflowError):
        return None
    return amount if amount > 0 else None
