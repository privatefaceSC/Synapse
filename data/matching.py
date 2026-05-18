import unicodedata
from difflib import SequenceMatcher

MATCH_THRESHOLD = 0.7


def normalize(s: str) -> str:
    s = s.lower().strip()
    return ''.join(
        ch for ch in s
        if unicodedata.category(ch).startswith(('L', 'N'))
    )


def split_group_sender(sender_raw: str):
    if not sender_raw or ":" not in sender_raw:
        return None
    prefix, _, member = sender_raw.partition(":")
    prefix, member = prefix.strip(), member.strip()
    if not prefix or not member:
        return None
    return prefix, member


def display_author(sender_raw: str, contact_display_name: str) -> str:
    if not sender_raw or not contact_display_name:
        return sender_raw
    parsed = split_group_sender(sender_raw)
    if parsed is None:
        return sender_raw
    prefix, member = parsed
    if prefix == contact_display_name.strip():
        return member
    return sender_raw


def similarity_score(a_norm: str, b_norm: str) -> float:
    if not a_norm and not b_norm:
        return 1.0
    if not a_norm or not b_norm:
        return 0.0
    return SequenceMatcher(None, a_norm, b_norm).ratio()


def suggest_merges_for_handle(db, new_handle) -> int:
    from .contacts import MergeSuggestion, MessengerHandle

    candidates = (
        db.query(MessengerHandle)
        .filter(
            MessengerHandle.user_id == new_handle.user_id,
            MessengerHandle.contact_id != new_handle.contact_id,
            MessengerHandle.id != new_handle.id,
        )
        .all()
    )

    created = 0
    for cand in candidates:
        score = similarity_score(new_handle.sender_normalized, cand.sender_normalized)
        if score < MATCH_THRESHOLD:
            continue
        exists = (
            db.query(MergeSuggestion)
            .filter(
                MergeSuggestion.source_handle_id == new_handle.id,
                MergeSuggestion.target_contact_id == cand.contact_id,
            )
            .first()
        )
        if exists:
            continue
        db.add(MergeSuggestion(
            user_id=new_handle.user_id,
            source_handle_id=new_handle.id,
            target_contact_id=cand.contact_id,
            score=score,
            status="pending",
        ))
        created += 1

    if created:
        db.commit()
    return created
