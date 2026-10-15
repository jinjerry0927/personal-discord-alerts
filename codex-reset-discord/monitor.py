"""One-shot Codex reset monitor. Python 3.11+, standard library only."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

SOURCE = "https://codex-reset.com"
KST = timezone(timedelta(hours=9))
MAX_RESPONSE_BYTES = 2_000_000
RULES_VERSION = 2


class MonitorError(Exception):
    """Errors whose messages may safely appear in public workflow logs."""

    def __init__(self, message: str, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except ValueError:
        return None


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError) as exc:
        raise MonitorError(f"JSON 파일을 읽을 수 없습니다: {path.name}") from exc


def request_json(request: Request, label: str) -> dict:
    try:
        with build_opener(NoRedirect).open(request, timeout=25) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise MonitorError(f"{label}: 응답 크기 제한 초과")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except HTTPError as exc:
        # Never log the exception itself: a webhook URL contains a secret token.
        retry_after = exc.headers.get("Retry-After", "")
        retry_seconds = None
        if exc.code == 429:
            try:
                retry_seconds = max(60, math.ceil(float(retry_after)))
            except (ValueError, OverflowError):
                try:
                    retry_date = parsedate_to_datetime(retry_after)
                    retry_seconds = max(60, math.ceil((retry_date - datetime.now(timezone.utc)).total_seconds()))
                except (TypeError, ValueError, OverflowError):
                    retry_seconds = 600
        retry_note = f", Retry-After={retry_seconds}s" if retry_seconds else ""
        raise MonitorError(f"{label}: HTTP {exc.code}{retry_note}", retry_seconds) from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise MonitorError(f"{label}: 네트워크 또는 JSON 응답 오류") from None


def fetch_snapshot(user_agent: str) -> dict:
    return {
        endpoint: request_json(
            Request(f"{SOURCE}/api/{endpoint}", headers={"User-Agent": user_agent}),
            f"공개 API {endpoint}",
        )
        for endpoint in ("timeline", "feed")
    }


def validate_snapshot(snapshot: dict, now: datetime, max_age_minutes: int = 60) -> None:
    timeline, feed = snapshot.get("timeline"), snapshot.get("feed")
    if not isinstance(timeline, dict) or not isinstance(feed, dict):
        raise MonitorError("timeline/feed 응답 구조가 바뀌었습니다. 전송하지 않습니다.")
    if not isinstance(timeline.get("events"), list) or not isinstance(feed.get("tweets"), list):
        raise MonitorError("events/tweets 배열이 없습니다. 전송하지 않습니다.")
    if feed.get("stale") is not False:
        raise MonitorError("피드가 오래되었거나 신선도를 확인할 수 없습니다. 전송하지 않습니다.")
    for label, value in (("timeline", timeline.get("updated_at")), ("feed", feed.get("fetched_at"))):
        timestamp = parse_time(value)
        if timestamp is None or not -300 <= (now - timestamp).total_seconds() <= max_age_minutes * 60:
            raise MonitorError(f"{label}: 갱신 시각이 누락되었거나 허용 범위를 벗어났습니다.")


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("’", "'").replace("‘", "'")).strip()


def has(pattern: str, text: str) -> bool:
    return re.search(pattern, text, re.IGNORECASE) is not None


def ambiguous(text: str) -> bool:
    # Conservative exclusions. These rules are not general language understanding.
    return has(
        r"\?|\b(?:if|unless|might|maybe|perhaps|could|would|either|wish|hope|hoping|consider\w*|discuss\w*|debat\w*|asking|rumou?rs?|cancel\w*|postpon\w*|jok\w*)\b"
        r"|\b(?:not|never|no|won't|can't|cannot|isn't|wasn't|aren't|weren't|don't|doesn't|didn't)\b",
        text,
    )


@dataclass(frozen=True)
class Alert:
    source_id: str
    category: str
    stage: str
    at: datetime
    evidence: str
    url: str | None
    evidence_label: str
    posted_at: datetime | None = None
    detected_at: datetime | None = None
    api_fetched_at: datetime | None = None
    related_url: str | None = None
    related_evidence: str | None = None
    deadline: datetime | None = None
    thread_id: str | None = None

    @property
    def key(self) -> str:
        # A linked reset thread can progress from scheduled to observed/confirmed.
        identity = self.thread_id if self.category in ("reset_notice", "reset_done") else self.source_id
        return f"{identity or self.source_id}:{self.category}:{self.stage}"


def classify_event(event: dict, tweet: dict | None = None) -> Alert | None:
    source_id = event.get("id")
    at = parse_time(event.get("announced_at"))
    summary = event.get("summary")
    if not isinstance(source_id, (str, int)) or not str(source_id) or at is None or not isinstance(summary, str):
        return None
    group = event.get("group")
    text = normalize(summary)
    evidence = summary
    evidence_label = "추적 서비스의 영어 요약"
    if tweet and isinstance(tweet.get("text"), str) and str(tweet.get("id")) == str(source_id):
        evidence = tweet["text"]
        evidence_label = "Tibo 영어 원문"
    raw_url = event.get("url")
    url = raw_url if isinstance(raw_url, str) and raw_url.startswith("https://x.com/thsottiaux/status/") else None

    def alert(category: str, stage: str) -> Alert:
        posted_at = parse_time(tweet.get("at")) if tweet else None
        return Alert(str(source_id), category, stage, at, evidence, url, evidence_label,
                     posted_at=posted_at)

    if group == "reset" and event.get("announcement_state") == "announced":
        # The documented tracker signal is not necessarily a Tibo confirmation.
        observed = event.get("source") == "operator-observed" or event.get("reset_verification_status") == "pending"
        return alert("reset_done", "observed" if observed else "confirmed")

    if group == "credits":
        if not has(r"\bbanked\s+(?:codex\s+)?resets?\b", text):
            return None
        state = event.get("banked_state")
        if state in ("announced", "arriving", "available"):
            return alert("banked", state)
        if not ambiguous(text) and has(r"\b(?:load(?:ed|ing)?|grant(?:ed|ing)?|give|giving|land(?:ed|ing)?|receiv(?:e|ed)|add(?:ed|ing)?)\b", text):
            # Unknown delivery state must not be upgraded to "available".
            return alert("banked", "announced")
        return None

    if group != "reset" or event.get("announcement_state") != "none":
        return None
    if ambiguous(text) or has(r"\b(?:banked|password|device|factory)\b", text):
        return None
    target = r"(?:global|full|usage(?:[- ]limits?)?)\s+resets?"
    commitment = has(
        rf"\b{target}\s+(?:(?:is|are|will(?:\s+be)?)\s+)?(?:land(?:ing)?|arriv(?:e|ing)|com(?:e|ing)|scheduled|planned|happen(?:ing)?|by)\b"
        rf"|^(?:a\s+)?{target}\s+(?:today|tomorrow|tonight)[.!]?$"
        rf"|\b(?:we'll|we will|i'll|i will|we are going to)\s+(?:do|give|apply|ship|perform|provide)\s+(?:a\s+|the\s+|another\s+)?{target}\b"
        r"|\b(?:we'll|we will|i'll|i will|we are going to)\s+reset\s+(?:the\s+)?usage\b"
        r"|\bwe are resetting\s+(?:the\s+)?usage\b",
        text,
    )
    completed = has(r"\b(?:done|processed|propagated|already|completed|landed)\b", text)
    if commitment and not completed:
        return alert("reset_notice", "announced")
    return None


def post_url(tweet: dict) -> str | None:
    value = tweet.get("url")
    identity = str(tweet.get("id", ""))
    expected = f"https://x.com/thsottiaux/status/{identity}"
    return value if re.fullmatch(r"\d{1,20}", identity) and isinstance(value, str) and value == expected else None


def reset_context(text: str) -> bool:
    text = normalize(text)
    return not ambiguous(text) and not has(r"\b(?:banked|router|password|factory|device|sleep)\b", text) and has(
        r"\b(?:global|full|usage(?:[- ]limits?)?|codex)\s+resets?\b"
        r"|\breset\s+button\b|\bday\s*\d+\s*/\s*reset\b", text)


def reply_context(tweet: dict, tweets: dict[str, dict]) -> tuple[dict | None, str | None, bool]:
    """Follow only explicit, same-author reply links; never guess from nearby posts."""
    immediate = None
    root = str(tweet.get("id", ""))
    found = False
    visited = {root}
    cursor = tweet
    posted = parse_time(tweet.get("at"))
    for _ in range(4):
        parent_id = str(cursor.get("in_reply_to_tweet_id", ""))
        parent = tweets.get(parent_id)
        if parent_id in visited or not parent or not post_url(parent):
            break
        parent_at = parse_time(parent.get("at"))
        if posted is None or parent_at is None or not timedelta(0) <= posted - parent_at <= timedelta(hours=24):
            break
        visited.add(parent_id)
        immediate = immediate or parent
        root = parent_id
        found = found or reset_context(str(parent.get("text", "")))
        cursor = parent
    return immediate, root, found


def relative_deadline(text: str, posted: datetime) -> datetime | None:
    match = re.search(r"\bwithin\s+(?:the\s+next\s+)?(\d{1,2})\s+(hours?|minutes?)\b", text, re.I)
    if not match:
        return None
    count = int(match[1])
    return posted + (timedelta(hours=count) if match[2].lower().startswith("hour") else timedelta(minutes=count))


def classify_post(tweet: dict, tweets: dict[str, dict]) -> Alert | None:
    posted = parse_time(tweet.get("at"))
    if posted is None or not post_url(tweet) or not isinstance(tweet.get("text"), str):
        return None
    text = normalize(tweet["text"])
    parent, root, contextual_reset = reply_context(tweet, tweets)
    event = {"id": str(tweet["id"]), "announced_at": posted.isoformat(), "summary": text,
             "url": tweet["url"], "group": "reset", "announcement_state": "none"}
    alert = classify_event(event, tweet)
    if not ambiguous(text):
        timing_reply = contextual_reset and has(
            r"^(?:it(?:'s| is)\s+)?(?:landing|arriving|coming)\s+(?:within|in)\s+"
            r"(?:the\s+next\s+)?\d{1,2}\s+(?:hours?|minutes?)[.!]?$", text)
        done_reply = contextual_reset and has(
            r"^(?:(?:it's|it is|all)\s+)?(?:done(?:\s+and\s+dusted)?|processed|propagated|completed|landed)[.!]?$", text)
        future = has(r"\b(?:will|should|tomorrow|tonight|within|soon|scheduled|planned|expect\w*|later)\b", text)
        explicit_done = not future and (
            (reset_context(text) and has(r"\b(?:done|processed|propagated|completed|landed)\b", text)) or
            has(r"\b(?:we|i)\s+(?:have\s+|just\s+|have\s+now\s+)reset\s+(?:everyone's\s+|all\s+|the\s+)?usage(?:\s+limits?)?\b", text))
        if timing_reply:
            alert = Alert(str(tweet["id"]), "reset_notice", "announced", posted,
                          tweet["text"], tweet["url"], "Tibo 영어 원문", posted_at=posted)
        elif done_reply or explicit_done:
            alert = Alert(str(tweet["id"]), "reset_done", "tibo_confirmed", posted,
                          tweet["text"], tweet["url"], "Tibo 영어 원문", posted_at=posted)
        elif has(r"\bbanked\s+(?:codex\s+)?resets?\b", text):
            alert = classify_event(dict(event, group="credits", banked_state="unknown"), tweet)
    if alert:
        alert = replace(alert, related_url=post_url(parent) if parent else None,
                        related_evidence=parent.get("text") if parent else None,
                        deadline=relative_deadline(text, posted) if alert.category == "reset_notice" else None,
                        thread_id=root)
    return alert


def collect_alerts(snapshot: dict) -> list[Alert]:
    tweets = {str(t["id"]): t for t in snapshot["feed"]["tweets"] if isinstance(t, dict) and "id" in t}
    alerts = []
    for event in snapshot["timeline"]["events"]:
        if not isinstance(event, dict):
            continue
        alert = classify_event(event, tweets.get(str(event.get("id"))))
        if alert:
            tweet = tweets.get(alert.source_id)
            if tweet:
                parent, root, _ = reply_context(tweet, tweets)
                alert = replace(alert, thread_id=root, related_url=post_url(parent) if parent else None,
                                related_evidence=parent.get("text") if parent else None)
            alerts.append(alert)
    alerts.extend(alert for tweet in tweets.values() if (alert := classify_post(tweet, tweets)))
    # An author-confirmed reply supersedes an operator observation in the same thread.
    priority = {"announced": 0, "arriving": 1, "observed": 1, "confirmed": 2, "available": 2, "tibo_confirmed": 3}
    grouped = {}
    for alert in alerts:
        key = (alert.thread_id or alert.source_id, alert.category)
        old = grouped.get(key)
        if old is None or (priority.get(alert.stage, 0), alert.at) > (priority.get(old.stage, 0), old.at):
            grouped[key] = alert
    completed = {identity for identity, category in grouped if category == "reset_done"}
    return sorted((alert for (identity, category), alert in grouped.items()
                   if category != "reset_notice" or identity not in completed), key=lambda item: (item.at, item.key))


def collect_posts(snapshot: dict) -> list[Alert]:
    """Relay every available Tibo text post, without a Codex/reset keyword filter."""
    posts = {}
    for tweet in snapshot["feed"]["tweets"]:
        if not isinstance(tweet, dict) or not post_url(tweet) or not isinstance(tweet.get("text"), str):
            continue
        posted = parse_time(tweet.get("at"))
        if posted:
            post = Alert(str(tweet["id"]), "tibo_post", "raw", posted, tweet["text"],
                         tweet["url"], "Tibo 영어 원문", posted_at=posted)
            posts[post.key] = post
    return sorted(posts.values(), key=lambda item: (item.at, item.key))


def record_first_seen(alerts: list[Alert], path: Path, now: datetime) -> dict[str, str]:
    state = read_json(path) if path.exists() else {"version": 1, "first_seen": {}}
    entries = state.get("first_seen")
    if state.get("version") != 1 or not isinstance(entries, dict) or any(
        not isinstance(key, str) or parse_time(value) is None for key, value in entries.items()
    ):
        raise MonitorError("최초 감지 시각 기록이 손상되었습니다.")
    changed = False
    for alert in alerts:
        if alert.source_id not in entries:
            entries[alert.source_id] = now.isoformat()
            changed = True
    if changed:
        save_state(path, state)
    return entries


def clean_evidence(text: str) -> str:
    # External text is evidence only, never executable instructions or mentions.
    text = text.replace("@", "＠").replace("`", "'")
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)[:850]


def format_kst(value: datetime) -> str:
    return value.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST")


def timing_fields(alert: Alert, sent_at: datetime | None) -> list[dict]:
    fields = [{"name": "X 게시 시각", "value": format_kst(alert.posted_at) if alert.posted_at else "API에 원문 게시 시각이 없습니다."},
              {"name": "우리 프로그램 최초 감지", "value": format_kst(alert.detected_at) if alert.detected_at else "미리보기 · 운영 실행 때 기록합니다."}]
    if alert.posted_at and alert.detected_at:
        seconds = max(0, int((alert.detected_at - alert.posted_at).total_seconds()))
        fields.append({"name": "게시 → 최초 감지", "value": f"{seconds // 60}분 {seconds % 60}초 · API 반영 대기와 예약 대기 포함"})
    if sent_at:
        fields.append({"name": "이번 전송 요청 시각", "value": format_kst(sent_at)})
    if alert.api_fetched_at:
        fields.append({"name": "API 피드 갱신 시각", "value": format_kst(alert.api_fetched_at)})
    if alert.category != "tibo_post" and alert.at != alert.posted_at:
        fields.append({"name": "추적 서비스 기록 시각", "value": format_kst(alert.at)})
    return fields


def build_payload(alert: Alert, *, demo: bool = False, sent_at: datetime | None = None) -> dict:
    if alert.category == "tibo_post":
        # Keep raw wording (including @ and backticks); allowed_mentions blocks pings.
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", alert.evidence)
        truncated = len(text) > 3800
        description = text[:3800] + ("\n…원문이 길어 일부만 표시합니다. X 링크에서 전체를 확인하세요." if truncated else "")
        return {"username": "Tibo 원문", "allowed_mentions": {"parse": []}, "embeds": [{
            "title": ("[데모] " if demo else "") + "📝 Tibo 새 포스팅 · 분류 없는 원문",
            "description": description or "텍스트가 없는 포스팅입니다. 원문 링크를 확인하세요.",
            "url": alert.url, "color": 0x5865F2,
            "fields": timing_fields(alert, sent_at) + [{"name": "출처", "value": f"[Data: codex-reset.com]({SOURCE})"}],
            "footer": {"text": "API에 수집된 글 전체 · 리셋 여부를 판단하지 않습니다 · 이미지/영상은 X 링크에서 확인"},
        }]}
    if alert.category == "reset_notice":
        title, color = "🔔 리셋 예정", 0xF0B232
        description = "Tibo의 게시물과 연결된 답글에서 사용량 리셋 예고를 감지했습니다.\n적용 완료는 아직 확인되지 않았습니다."
    elif alert.category == "reset_done":
        title, color = "✅ 리셋 적용", 0x2ECC71
        if alert.stage == "tibo_confirmed":
            description = "Tibo의 게시물 또는 같은 리셋 대화의 답글에서 적용 확인 문구를 감지했습니다."
        elif alert.stage == "observed":
            description = "추적 서비스가 리셋 적용을 관측했습니다. Tibo의 직접 확인과는 구분됩니다."
        else:
            description = "추적 서비스가 문서화한 기준에 따라 리셋 적용을 기록했습니다."
        description += "\n본인 계정의 실제 사용량도 확인하세요."
    else:
        title, color = "🎟️ 리셋권 지급 안내", 0x9B59B6
        stage = {"announced": "지급 안내", "arriving": "지급 진행/예정", "available": "추적 서비스에서 지급 확인"}[alert.stage]
        description = f"저장해 두었다가 사용하는 초기화권 소식입니다.\n상태: **{stage}**\n자동 리셋 완료와는 별개이며, 본인 계정에서 지급 여부를 확인하세요."
    fields = timing_fields(alert, sent_at) + [
        {"name": alert.evidence_label, "value": clean_evidence(alert.evidence)},
        {"name": "출처", "value": f"[Data: codex-reset.com]({SOURCE})"},
    ]
    if alert.deadline:
        fields.append({"name": "원문 기준 적용 예정 기한", "value": format_kst(alert.deadline) + "까지 · 게시 시각 + 원문의 상대 시간, 실제 적용 보장 아님"})
    if alert.category == "reset_done":
        basis = {"tibo_confirmed": "Tibo 적용 확인 문구", "observed": "추적 서비스 관측 · Tibo 확인 대기", "confirmed": "추적 서비스의 적용 기록"}[alert.stage]
        fields.append({"name": "적용 판단 근거", "value": basis})
    if alert.related_url:
        fields.append({"name": "연결된 원글/이전 답글", "value": f"[연결된 글 열기]({alert.related_url})\n{clean_evidence(alert.related_evidence or '')}"})
    if alert.url:
        fields.append({"name": "관련 X 게시물", "value": f"[원문 열기]({alert.url})"})
    return {
        "username": "Codex Reset 알림",
        "allowed_mentions": {"parse": []},
        "embeds": [{
            "title": ("[데모] " if demo else "") + title,
            "description": description,
            "color": color,
            "fields": fields,
            "footer": {"text": "공개 API + 원글/답글 규칙 분류 · AI 미사용 · 전체 원문 번역 아님"},
        }],
    }


def send_payload(webhook_url: str, payload: dict) -> None:
    parts = urlsplit(webhook_url)
    if parts.scheme != "https" or parts.hostname != "discord.com" or not re.fullmatch(r"/api(?:/v\d+)?/webhooks/\d+/[\w.-]+", parts.path):
        raise MonitorError("Discord 웹훅 URL 형식이 올바르지 않습니다.")
    query = dict(parse_qsl(parts.query))
    query["wait"] = "true"
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
    receipt = request_json(Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={
        "Content-Type": "application/json", "User-Agent": "CodexResetDiscordPrototype/0.1"
    }, method="POST"), "Discord 전송")
    if not receipt.get("id"):
        raise MonitorError("Discord 전송 확인 응답에 메시지 ID가 없습니다.")


def build_connection_test_payload() -> dict:
    return {
        "username": "Codex Reset 알림",
        "allowed_mentions": {"parse": []},
        "embeds": [{
            "title": "🧪 연결 테스트 · 실제 리셋 소식이 아닙니다",
            "description": "GitHub 실행 환경에서 Discord 웹훅으로 테스트 메시지 1건을 전송했습니다.\n이 메시지를 확인했다면 게시 연결은 정상입니다.\n휴대폰 알림도 도착하는지 확인하세요.",
            "color": 0x5865F2,
            "footer": {"text": "연결 테스트 전용 · 공개 API 조회/리셋 상태 변경 없음"},
        }],
    }


def load_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    state = read_json(path)
    if state.get("version") != 1 or not isinstance(state.get("seen"), list) or any(not isinstance(key, str) for key in state["seen"]):
        raise MonitorError("중복 방지 상태 파일이 손상되었습니다. 초기화하지 않고 중단합니다.")
    if parse_time(state.get("started_at")) is None:
        raise MonitorError("상태 파일의 시작 시각이 올바르지 않습니다.")
    return state


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def process(alerts: list[Alert], state_path: Path, now: datetime, sender: Callable[[dict], None], *, max_age_hours: int = 24) -> tuple[int, bool]:
    state = load_state(state_path)
    if state is None:
        save_state(state_path, {"version": 1, "rules_version": RULES_VERSION, "started_at": now.isoformat(), "seen": [item.key for item in alerts]})
        return 0, True  # Establish a baseline; do not flood Discord with history.
    if state.get("rules_version") != RULES_VERSION:
        # New classification rules must not replay old notices or observed resets.
        state["seen"] = sorted(set(state["seen"]) | {item.key for item in alerts})
        state["rules_version"] = RULES_VERSION
        save_state(state_path, state)
        return 0, True
    seen = set(state["seen"])
    sent = 0
    for alert in alerts:
        if alert.key in seen:
            continue
        if alert.at < now - timedelta(hours=max_age_hours) or alert.at > now + timedelta(minutes=5):
            continue
        # A previously unclassified post can later gain a confirmed source state.
        sender(build_payload(replace(alert, detected_at=alert.detected_at or now),
                             sent_at=datetime.now(timezone.utc)))
        seen.add(alert.key)
        state["seen"] = sorted(seen)
        save_state(state_path, state)  # Each acknowledged message is checkpointed.
        sent += 1
    return sent, False


def main() -> int:
    parser = argparse.ArgumentParser(description="Codex 리셋 3종 알림 (기본: 전송 없는 미리보기)")
    parser.add_argument("--send", action="store_true", help="실제 Discord 전송. 최초에는 기준점만 저장")
    parser.add_argument("--demo", action="store_true", help="가상 데이터 3종 미리보기. 네트워크/상태 저장 없음")
    parser.add_argument("--test-webhook", action="store_true", help="연결 테스트 메시지 1건을 Discord에 실제 전송")
    parser.add_argument("--test-feed-webhook", action="store_true", help="원문 채널 연결 테스트 메시지 1건 전송")
    parser.add_argument("--snapshot-dir", type=Path, help="timeline.json/feed.json을 읽는 오프라인 검증")
    parser.add_argument("--state", type=Path, default=Path("state/state.json"))
    parser.add_argument("--now", help="오프라인 검증용 현재 시각 (타임존 포함 ISO 8601)")
    args = parser.parse_args()
    cooldown_path = args.state.parent / "cooldown.json"
    try:
        if args.test_webhook or args.test_feed_webhook:
            if args.send or args.demo or args.snapshot_dir or args.now or (args.test_webhook and args.test_feed_webhook):
                raise MonitorError("웹훅 연결 테스트는 다른 실행 모드와 함께 사용할 수 없습니다.")
            secret_name = "TIBO_FEED_WEBHOOK_URL" if args.test_feed_webhook else "DISCORD_WEBHOOK_URL"
            webhook_url = os.getenv(secret_name, "")
            if not webhook_url:
                raise MonitorError(f"{secret_name}이 없습니다. 전송하지 않습니다.")
            send_payload(webhook_url, build_connection_test_payload())
            print(json.dumps({"mode": "test-feed" if args.test_feed_webhook else "test", "sent": 1}))
            return 0
        if args.send and (args.demo or args.snapshot_dir or args.now):
            raise MonitorError("오프라인/데모 데이터는 실제 전송할 수 없습니다.")
        now = parse_time(args.now) if args.now else datetime.now(timezone.utc)
        if now is None:
            raise MonitorError("--now에는 타임존 포함 ISO 8601 시각이 필요합니다.")
        if args.demo:
            snapshot = read_json(Path(__file__).parent / "examples/demo-input.json")
            now = parse_time(snapshot["timeline"]["updated_at"])
            assert now is not None
        elif args.snapshot_dir:
            snapshot = {name: read_json(args.snapshot_dir / f"{name}.json") for name in ("timeline", "feed")}
        else:
            if args.send and cooldown_path.exists():
                until = parse_time(read_json(cooldown_path).get("next_allowed_check_at"))
                if until is None:
                    raise MonitorError("재시도 대기 상태가 손상되었습니다.")
                if now < until:
                    print(json.dumps({"skipped": "rate_limit_cooldown", "next_allowed_check_at": until.isoformat()}))
                    return 0
            repository = os.getenv("GITHUB_REPOSITORY", "")
            user_agent = os.getenv("SOURCE_USER_AGENT") or (
                f"CodexResetDiscordPrototype/0.1 (+https://github.com/{repository})" if repository else
                "CodexResetDiscordPrototype/0.1 (personal local evaluation)"
            )
            snapshot = fetch_snapshot(user_agent)
            now = datetime.now(timezone.utc)
        validate_snapshot(snapshot, now)
        alerts = collect_alerts(snapshot)
        posts = collect_posts(snapshot)
        fetched_at = parse_time(snapshot["feed"].get("fetched_at"))
        if not args.send:
            print(json.dumps({"mode": "demo" if args.demo else "preview", "classified_count": len(alerts),
                              "messages": [build_payload(replace(item, api_fetched_at=fetched_at), demo=args.demo) for item in alerts],
                              "raw_post_count": len(posts), "raw_messages": [build_payload(replace(item, api_fetched_at=fetched_at), demo=args.demo) for item in posts]}, ensure_ascii=False, indent=2))
            return 0
        webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "")
        if not webhook_url:
            raise MonitorError("DISCORD_WEBHOOK_URL이 없습니다. 전송하지 않습니다.")
        first_seen = record_first_seen(alerts + posts, args.state.parent / "observations.json", now)
        def enrich(items):
            return [replace(item, detected_at=parse_time(first_seen[item.source_id]), api_fetched_at=fetched_at) for item in items]
        # Separate receipts and independent attempts keep a raw-channel failure from
        # losing an acknowledged reset alert (or preventing a raw post delivery).
        results = {}
        failures = []
        channels = [("alerts", webhook_url, enrich(alerts), args.state)]
        feed_url = os.getenv("TIBO_FEED_WEBHOOK_URL", "")
        if feed_url:
            if feed_url == webhook_url:
                raise MonitorError("원문 채널과 리셋 채널에는 서로 다른 웹훅을 지정하세요.")
            channels.append(("feed", feed_url, enrich(posts), args.state.parent / "feed-state.json"))
        for channel, url, items, path in channels:
            try:
                sent, initialized = process(items, path, now, lambda payload, target=url: send_payload(target, payload))
                results[channel] = {"sent": sent, "initialized": initialized}
            except MonitorError as exc:
                failures.append(exc)
                results[channel] = {"error": str(exc)}
        print(json.dumps({"channels": results, "raw_channel_enabled": bool(feed_url), "classified_count": len(alerts),
                          "raw_post_count": len(posts), "api_fetched_at": snapshot["feed"].get("fetched_at"), "checked_at": now.isoformat()}, ensure_ascii=False))
        if failures:
            raise max(failures, key=lambda exc: exc.retry_after_seconds or 0)
        return 0
    except MonitorError as exc:
        if args.send and exc.retry_after_seconds:
            save_state(cooldown_path, {
                "next_allowed_check_at": (datetime.now(timezone.utc) + timedelta(seconds=exc.retry_after_seconds)).isoformat()
            })
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
