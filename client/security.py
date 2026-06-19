from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Tuple
from urllib.error import URLError
from urllib.request import Request, urlopen


DEFAULT_QUESTION = "密码"
DEFAULT_ANSWER = "wzn090614"
CHALLENGE_URL = "http://47.115.166.227:1002/challenge"
VERIFY_URL = "http://47.115.166.227:1002/verify"


@dataclass
class Challenge:
    id: int
    question: str
    is_fallback: bool = False

    @classmethod
    def fetch(cls) -> "Challenge":
        try:
            request = Request(CHALLENGE_URL, method="GET")
            with urlopen(request, timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("status") == "success":
                return cls(id=int(payload.get("id", 0)), question=str(payload.get("question", "密码")))
        except Exception:
            pass
        return cls(id=0, question=DEFAULT_QUESTION, is_fallback=True)


def verify_with_challenge(challenge: Challenge, answer: str) -> Tuple[bool, str]:
    if challenge.is_fallback:
        ok = answer == DEFAULT_ANSWER
        return ok, "密码错误，验证失败。" if not ok else "验证成功。"

    payload = json.dumps({"id": challenge.id, "answer": answer}).encode("utf-8")
    request = Request(
        VERIFY_URL,
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
        if e == "HTTP Error 403: FORBIDDEN":
            return False, str(data.get("message", "答案错误，验证失败。"))
        return False, "验证失败，且无法连接验证服务。"
