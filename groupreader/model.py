from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from functools import cached_property
import hashlib
import json
import re


@dataclass
class Node:
    role: str
    text: str = ""
    children: list["Node"] = field(default_factory=list)
    rect: tuple[float, float, float, float] | None = None
    native: object = field(default=None, repr=False, compare=False)
    control_name: str = ""

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass(frozen=True)
class Message:
    sender: str
    timestamp: str
    text: str
    kind: str = "text"
    quote_sender: str = ""
    quote: str = ""
    raw_time: str = ""

    @property
    def is_placeholder(self) -> bool:
        return self.kind == "nontext"

    @cached_property
    def key(self) -> str:
        # An observation fingerprint, never a database uniqueness constraint.
        # Two identical adjacent messages must remain two separate records.
        values = [self.sender, self.timestamp, self.text, self.kind,
                  self.quote_sender, self.quote]
        return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()

    def to_dict(self):
        return asdict(self)


def plain_links(value: str) -> str:
    return re.sub(r"\[([^\]\n]+)\]\([^\n)]*\)", r"\1", value)


def fenced(value: str) -> str:
    length = max([3] + [len(m.group()) + 1 for m in re.finditer(r"`+", value)])
    fence = "`" * length
    return f"{fence}text\n{value}\n{fence}"


def markdown(messages: list[Message]) -> str:
    # Python's stable sort preserves the captured order within the same minute.
    ordered = sorted((m for m in messages if not m.is_placeholder), key=lambda m: m.timestamp)
    parts = []
    for message in ordered:
        text = f"### {message.timestamp.replace('T', ' ')}｜{message.sender}\n\n"
        if message.quote_sender:
            text += f"> 引用 {message.quote_sender}\n\n"
        if message.quote:
            text += fenced(message.quote) + "\n\n"
        text += fenced(message.text) + "\n"
        parts.append(text)
    return "\n".join(parts)


def validate_message(message: Message):
    if not message.sender.strip():
        raise ValueError("消息没有发送者")
    datetime.fromisoformat(message.timestamp)
