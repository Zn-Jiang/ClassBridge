from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Tuple
from urllib.request import Request, urlopen


@dataclass
class Challenge:
    id: int
    question: str
    is_fallback: bool = False

    @classmethod
    def fetch(
        cls,
        challenge_url: str,
        fallback_question: str = "密码",
        fallback_answer: str = "change-me",
    ) -> "Challenge":
        try:
            request = Request(challenge_url, method="GET")
            with urlopen(request, timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("status") == "success":
                return cls(id=int(payload.get("id", 0)), question=str(payload.get("question", fallback_question)))
        except Exception:
            pass
        return cls(id=0, question=fallback_question, is_fallback=True)


def verify_with_challenge(
    challenge: Challenge,
    answer: str,
    verify_url: str = "http://127.0.0.1:1002/verify",
    fallback_answer: str = "change-me",
) -> Tuple[bool, str]:
    if challenge.is_fallback:
        ok = answer == fallback_answer
        return ok, "密码错误，验证失败。" if not ok else "验证成功。"

    payload = json.dumps({"id": challenge.id, "answer": answer}).encode("utf-8")
    request = Request(
        verify_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=3) as response:
            data = json.loads(response.read().decode("utf-8"))
        if data.get("status") == "success":
            return True, "验证成功。"
        return False, str(data.get("message", "验证失败。"))
    except Exception as e:
        if "403" in str(e):
            return False, "答案错误，验证失败。"
        return False, "验证失败，且无法连接验证服务。"
