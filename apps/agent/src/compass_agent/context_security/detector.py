"""Deterministic high-confidence rules; ordinary user corrections remain valid."""

import re
from dataclasses import dataclass
from typing import Protocol

from compass_agent.context_security.policy import TrustLevel


@dataclass(frozen=True, slots=True)
class DetectionHit:
    severity: str
    rule_code: str
    stripped_text: str | None = None


class InjectionDetector(Protocol):
    def detect(self, text: str, trust: TrustLevel) -> DetectionHit | None: ...


ROLE = re.compile(
    r"<\|(?:im_start|start_header_id)\|>\s*(?:system|developer)|"
    r"\[INST\]\s*<<SYS>>|</?\s*(?:system|developer)\s*>",
    re.I,
)
EXTRACT = re.compile(
    r"(?:输出|打印|透露|泄露|展示|复述|获取).{0,16}(?:系统提示词|系统指令原文|开发者提示词)|"
    r"(?:reveal|print|expose|repeat|show).{0,30}(?:system|developer)\s+(?:prompt|instructions)",
    re.I,
)
OVERRIDE = re.compile(
    r"(?:忽略|无视|覆盖|绕过).{0,20}(?:系统|开发者|安全|权限).{0,12}(?:指令|规则|限制|校验)|"
    r"(?:ignore|override|bypass).{0,24}(?:previous|prior|system|developer|security)"
    r".{0,24}(?:instructions|rules|checks)|(?:扮演|你现在是).{0,12}(?:系统管理员|开发者)",
    re.I,
)
SUSPICIOUS = re.compile(
    r"(?:忽略|无视).{0,12}(?:之前|上述|前述).{0,8}(?:要求|指令)|"
    r"(?:follow these instructions|do not tell the user|不要告诉用户)",
    re.I,
)


class RuleDetector:
    def detect(self, text: str, trust: TrustLevel) -> DetectionHit | None:
        if trust not in {"actor", "observation"}:
            return None
        if ROLE.search(text):
            return DetectionHit("high", "role_forgery")
        if EXTRACT.search(text):
            return DetectionHit("high", "system_prompt_extraction")
        if trust == "observation":
            if OVERRIDE.search(text):
                return DetectionHit("high", "instruction_override")
            if not SUSPICIOUS.search(text):
                return None
            sentences = re.split(r"(?<=[。！？!?\n])", text)
            kept = [s for s in sentences if not SUSPICIOUS.search(s)]
            if len(kept) != len(sentences):
                return DetectionHit("medium", "suspicious_instruction", "".join(kept))
        return None
