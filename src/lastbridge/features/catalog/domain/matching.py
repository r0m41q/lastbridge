from lastbridge.features.catalog.domain.models import Candidate, Recording
from lastbridge.shared.errors import ProviderError
from lastbridge.shared.recordings import normalize, normalize_title


def match_recording(
    recording: Recording, candidates: list[Candidate]
) -> Candidate:
    """Fail closed on ambiguous versions; never strip live/remix qualifiers."""
    matches = {}
    for candidate in candidates:
        if not candidate.catalog_audio:
            continue
        if normalize_title(candidate.title) != normalize_title(
            recording.title
        ):
            continue
        if normalize(recording.artist) not in {
            normalize(artist) for artist in candidate.artists
        }:
            continue
        if recording.album and normalize(candidate.album or "") != normalize(
            recording.album
        ):
            continue
        if recording.duration_seconds is not None and (
            candidate.duration_seconds is None
            or abs(candidate.duration_seconds - recording.duration_seconds) > 3
        ):
            continue
        matches[candidate.source_id] = candidate
    if not matches:
        raise ProviderError("no_match")
    if len(matches) > 1:
        raise ProviderError("ambiguous_match")
    return next(iter(matches.values()))
