"""One-shot Codex reset monitor. Python 3.11+, standard library only."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from dataclasses import dataclass
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

    @property
    def key(self) -> str:
        # The same post can first announce a reset, then become confirmed.
        return f"{self.source_id}:{self.category}:{self.stage}"


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
        return Alert(str(source_id), category, stage, at, evidence, url, evidence_label)

    if group == "reset" and event.get("announcement_state") == "announced":
        # The source's documented completion rule, not the English keyword alone.
        return alert("reset_done", "confirmed")

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


def collect_alerts(snapshot: dict) -> list[Alert]:
    tweets = {str(t["id"]): t for t in snapshot["feed"]["tweets"] if isinstance(t, dict) and "id" in t}
    alerts = {}
    for event in snapshot["timeline"]["events"]:
        if not isinstance(event, dict):
            continue
        alert = classify_event(event, tweets.get(str(event.get("id"))))
        if alert:
            alerts[alert.key] = alert
    return sorted(alerts.values(), key=lambda item: (item.at, item.key))


def clean_evidence(text: str) -> str:
    # External text is evidence only, never executable instructions or mentions.
    text = text.replace("@", "＠").replace("`", "'")
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)[:850]


def build_payload(alert: Alert, *, demo: bool = False) -> dict:
    if alert.category == "reset_notice":
        title, color = "🔔 리셋 예고", 0xF0B232
        description = "전체 사용량 리셋을 예고하는 명시적인 공지가 감지되었습니다.\n적용 완료는 아직 확인되지 않았습니다."
    elif alert.category == "reset_done":
        title, color = "✅ 리셋 완료", 0x2ECC71
        description = "추적 서비스가 전체 사용량 리셋 완료를 기록했습니다.\n본인 계정의 실제 사용량도 확인하세요."
    else:
        title, color = "🎟️ 초기화권 지급 안내", 0x9B59B6
        stage = {"announced": "지급 안내", "arriving": "지급 진행/예정", "available": "추적 서비스에서 지급 확인"}[alert.stage]
        description = f"저장해 두었다가 사용하는 초기화권 소식입니다.\n상태: **{stage}**\n자동 리셋 완료와는 별개이며, 본인 계정에서 지급 여부를 확인하세요."
    fields = [
        {"name": "게시/기록 시각 · 한국 시간", "value": alert.at.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")},
        {"name": "시각 안내", "value": "위 시각은 기록 시각입니다. 예고된 적용 시각은 아래 영어 근거/링크에서 확인하세요."},
        {"name": alert.evidence_label, "value": clean_evidence(alert.evidence)},
        {"name": "출처", "value": f"[Data: codex-reset.com]({SOURCE})"},
    ]
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
            "footer": {"text": "공개 API + 보수적 규칙 분류 · 정형 한국어 요약 · 전체 원문 번역 아님"},
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
        save_state(state_path, {"version": 1, "started_at": now.isoformat(), "seen": [item.key for item in alerts]})
        return 0, True  # Establish a baseline; do not flood Discord with history.
    seen = set(state["seen"])
    sent = 0
    for alert in alerts:
        if alert.key in seen:
            continue
        if alert.at < now - timedelta(hours=max_age_hours) or alert.at > now + timedelta(minutes=5):
            continue
        # A previously unclassified post can later gain a confirmed source state.
        sender(build_payload(alert))
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
    parser.add_argument("--snapshot-dir", type=Path, help="timeline.json/feed.json을 읽는 오프라인 검증")
    parser.add_argument("--state", type=Path, default=Path("state/state.json"))
    parser.add_argument("--now", help="오프라인 검증용 현재 시각 (타임존 포함 ISO 8601)")
    args = parser.parse_args()
    cooldown_path = args.state.parent / "cooldown.json"
    try:
        if args.test_webhook:
            if args.send or args.demo or args.snapshot_dir or args.now:
                raise MonitorError("웹훅 연결 테스트는 다른 실행 모드와 함께 사용할 수 없습니다.")
            webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "")
            if not webhook_url:
                raise MonitorError("DISCORD_WEBHOOK_URL이 없습니다. 전송하지 않습니다.")
            send_payload(webhook_url, build_connection_test_payload())
            print(json.dumps({"mode": "test", "sent": 1}))
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
        validate_snapshot(snapshot, now)
        alerts = collect_alerts(snapshot)
        if not args.send:
            print(json.dumps({"mode": "demo" if args.demo else "preview", "classified_count": len(alerts),
                              "messages": [build_payload(item, demo=args.demo) for item in alerts]}, ensure_ascii=False, indent=2))
            return 0
        webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "")
        if not webhook_url:
            raise MonitorError("DISCORD_WEBHOOK_URL이 없습니다. 전송하지 않습니다.")
        sent, initialized = process(alerts, args.state, now, lambda payload: send_payload(webhook_url, payload))
        print(json.dumps({"sent": sent, "initialized": initialized, "classified_count": len(alerts)}))
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
