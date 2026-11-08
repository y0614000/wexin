from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from .model import Message


class DisconnectedFrame(ValueError):
    pass


@dataclass
class MergeResult:
    added: int
    status: str


class SequenceAssembler:
    """Stitch overlapping UI viewports without global content deduplication."""

    def __init__(self, messages: list[Message] | None = None):
        self.messages = list(messages or [])

    def _quote_updates(self, incoming: list[Message], known_keys: set[str]) -> dict[int, Message]:
        candidates = [m for m in incoming if m.quote and m.quote_sender and m.key not in known_keys]
        if not candidates:
            return {}

        def content(message):
            return message.sender, message.timestamp, message.kind, message.text

        saved_counts = Counter(map(content, self.messages))
        incoming_counts = Counter(map(content, incoming))
        by_content = {content(m): m for m in candidates
                      if saved_counts[content(m)] == incoming_counts[content(m)] == 1}
        updates = {}
        for index, old in enumerate(self.messages):
            new = by_content.get(content(old))
            if new and not old.quote and old.quote_sender in {"", new.quote_sender}:
                updates[index] = replace(old, quote_sender=new.quote_sender, quote=new.quote)
        return updates

    def _apply_quote_updates(self, updates: dict[int, Message], start: int, count: int):
        for index, message in updates.items():
            if start <= index < start + count:
                self.messages[index] = message

    def merge(self, incoming: list[Message], direction: str) -> MergeResult:
        if not incoming:
            raise DisconnectedFrame("当前屏没有识别到成员消息，已暂停。")
        if not self.messages:
            self.messages = list(incoming)
            return MergeResult(sum(not m.is_placeholder for m in incoming), "first")

        known = [m.key for m in self.messages]
        fresh = [m.key for m in incoming]
        updates = self._quote_updates(incoming, set(known))
        for index, message in updates.items():
            known[index] = message.key

        def anchored(start, count):
            # A newly discovered quote alone does not prove that two
            # otherwise identical messages are the same observation.
            return not updates or any(index not in updates for index in range(start, start + count))

        # A long message or unchanged viewport can be observed many times.
        for start in range(len(known) - len(fresh) + 1):
            if known[start:start + len(fresh)] == fresh and anchored(start, len(fresh)):
                if (not incoming[0].is_placeholder and len(set(fresh)) == 1
                        and (len(fresh) > 1 or len(incoming[0].text) < 200)
                        and ((direction == "newer" and known[-1] == fresh[-1])
                             or (direction == "older" and known[0] == fresh[0]))):
                    raise DisconnectedFrame(
                        "这一屏只有相同的短消息，无法确认是否是新的一条。"
                        "已暂停，请让边界附近的其他消息一起显示后继续。"
                    )
                self._apply_quote_updates(updates, start, len(fresh))
                return MergeResult(0, "contained")

        if direction == "older":
            left, right = fresh, known
        else:
            left, right = known, fresh
        overlap = 0
        for count in range(min(len(left), len(right)), 0, -1):
            start = 0 if direction == "older" else len(known) - count
            if left[-count:] == right[:count] and anchored(start, count):
                overlap = count
                break
        if not overlap:
            raise DisconnectedFrame(
                "当前屏与已有记录没有重叠，可能跳屏或切换了聊天。记录已保存；"
                "请滚回上次边界，减小滚动步长后继续。"
            )
        # If the overlap consists only of identical placeholders, choosing
        # between one or several matches cannot affect the retained messages.
        edge = incoming[-1] if direction == "older" else incoming[0]
        if (not edge.is_placeholder and len(set(left[-overlap:])) == 1
                and (left.count(left[-1]) > 1 or right.count(left[-1]) > 1)):
            raise DisconnectedFrame(
                "交界处存在连续相同消息，无法确认条数。已暂停保留现场；"
                "请缩小步长，让两屏同时包含其他消息后继续。"
            )
        if direction == "older":
            additions = incoming[:-overlap]
            self._apply_quote_updates(updates, 0, overlap)
            self.messages = additions + self.messages
        else:
            additions = incoming[overlap:]
            self._apply_quote_updates(updates, len(known) - overlap, overlap)
            self.messages.extend(additions)
        return MergeResult(sum(not m.is_placeholder for m in additions), "merged")
