from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import re

from .model import Node, Message, plain_links


TIME = re.compile(
    r"(?:(?P<year>20\d{2})[年/.-])?(?P<month>\d{1,2})[月/.-]"
    r"(?P<day>\d{1,2})日?\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})"
)
FLAT_TIME = (r"(?:(?:20\d{2}[年/.-])?\d{1,2}[月/.-]\d{1,2}日?|今天|昨天|前天)"
             r"[ \t]+\d{1,2}:\d{2}(?::[0-5]\d)?")
FLAT_ROW = re.compile(r"^(?P<sender>[^\r\n]{1,240}?)\s{1,8}(?P<time>" + FLAT_TIME +
                      r")(?=$|\s)(?:[ \t]*\r?\n|[ \t])?(?P<body>[\s\S]*)$")
DATE_DIVIDER = re.compile(r"(?:(?:20\d{2}[年/.-])?\d{1,2}[月/.-]\d{1,2}日?|今天|昨天|前天)")
FILE = re.compile(r".+\.(?:pdf|docx?|xlsx?|csv|txt|zip|rar|7z|png|jpe?g|md|exe)(?:\s+\S+)?$", re.I)
QUOTE_AUTHOR = re.compile(r"([^\r\n:：]{1,120})[:：]")
HEADERS = {"查找聊天记录", "查找聊天内容", "聊天记录", "搜索聊天记录"}
TABLE_ROLES = {"Table", "List", "DataGrid", "Tree"}
ROW_ROLES = {"Row", "ListItem", "DataItem", "TreeItem"}


class LayoutError(ValueError):
    pass


@dataclass
class ParsedFrame:
    messages: list[Message]
    system_count: int
    unresolved: list[str]
    table: Node


def parse_time(value: str, reference: date, observed_on: date | None = None) -> tuple[str, str] | None:
    observed_on = observed_on or reference
    match = TIME.search(value)
    if match:
        data = match.groupdict()
        year = int(data["year"] or reference.year)
        month, day = int(data["month"]), int(data["day"])
        # A date within the selected year can be later than the starting
        # point. Only roll back when it would be in the future on capture day.
        if (not data["year"] and year == observed_on.year
                and (month, day) > (observed_on.month, observed_on.day)):
            year -= 1
        try:
            stamp = datetime(year, month, day, int(data["hour"]), int(data["minute"]))
        except ValueError:
            return None
        return stamp.isoformat(timespec="minutes"), match.group()
    relative = re.search(r"(今天|昨天|前天)\s*(\d{1,2}):(\d{2})", value)
    if relative:
        offset = {"今天": 0, "昨天": 1, "前天": 2}[relative[1]]
        target = observed_on - timedelta(days=offset)
        try:
            stamp = datetime.combine(target, datetime.min.time()).replace(
                hour=int(relative[2]), minute=int(relative[3]))
        except ValueError:
            return None
        return stamp.isoformat(timespec="minutes"), relative.group()
    return None


def history_tables(root: Node) -> list[Node]:
    flattened = list(root.walk())
    markers = [i for i, node in enumerate(flattened) if node.text.strip() in HEADERS
               and node.role not in {"Button", "MenuItem"}]
    if not markers:
        raise LayoutError("请在企业微信目标群中打开“查找聊天记录”，选择“全部”，清空搜索条件。")
    return [node for node in flattened[markers[-1] + 1:] if node.role in TABLE_ROLES]


def locate_history(root: Node) -> Node:
    for node in history_tables(root):
        if any(n.role in ROW_ROLES for n in node.walk()):
            return node
    raise LayoutError("没有找到聊天记录列表。当前企业微信版本的界面可能需要适配。")


def parse_row(row: Node, reference: date, observed_on: date | None = None) -> Message | None:
    nodes = [n for n in row.walk() if n is not row]
    buttons = [n.text.strip() for n in nodes if n.role == "Button" and n.text.strip()
               and n.text.strip() not in {"按钮", "Button", "播放", "更多", "转文字"}]
    labels = [n.text for n in nodes if n.role in {"Text", "Label", "TextArea", "Edit"} and n.text]
    sender = buttons[0] if buttons else ""
    named_sender = next((n.text for n in nodes if n.control_name == "chatSender" and n.text.strip()), "")
    if named_sender:
        # DuiLib exposes the visible organization label as a rich-text icon.
        sender = re.sub(r"</?c(?:\s+[^>]*)?>", "", named_sender).strip()
        if "<i common/" in sender:
            sender, metadata = sender.split("<i common/", 1)
            organization = re.search(r"\[text='([^']+)'", metadata)
            sender = sender.strip() + ("@" + organization[1] if organization else "")
    # Windows can expose the entire message as one accessible name, with
    # either spaces or newlines between sender, timestamp and content.
    flat_body, flat_time = "", ""
    if not sender and row.text:
        match = FLAT_ROW.fullmatch(row.text)
        if match:
            sender = match["sender"].strip()
            flat_time, flat_body = match["time"], match["body"]
            labels = [flat_time] + labels
    if not sender:
        # Only explicit system notices are excluded. Unknown rows stop the
        # capture rather than silently disappearing from the export.
        combined = "\n".join([row.text] + labels)
        if any(word in combined for word in ("撤回了一条消息", "加入了", "修改群名", "此群为外部群")):
            return None
        values = [text.strip() for text in [row.text] + labels if text.strip()]
        if not values or all(DATE_DIVIDER.fullmatch(text) for text in values):
            return None
        raise LayoutError("有消息未识别到发送者；请导出结构诊断进行适配。")
    # Search timestamps only in labels, never in the message body. A log can
    # itself contain many request dates unrelated to the message's send time.
    time_labels = [n.text for n in nodes if n.role in {"Text", "Label"}]
    named_times = [n.text for n in nodes if n.control_name == "chatSendTime"]
    time_labels = named_times + time_labels
    if flat_time:
        time_labels = [flat_time] + time_labels
    elif row.text:
        time_labels = row.text.splitlines()[:2] + time_labels
    parsed = next((result for text in time_labels if (result := parse_time(text, reference, observed_on))), None)
    if not parsed:
        raise LayoutError(f"“{sender}”的一条消息未识别到具体时间，请使用查找聊天记录的“全部”列表。")
    timestamp, raw_time = parsed
    if flat_time:
        raw_time = flat_time
    quote_sender = next((n.text.rstrip(":：").strip() for n in nodes
                         if n.control_name == "quote_message_sender" and n.text.strip()), "")
    for label in time_labels:
        match = TIME.search(label)
        if match:
            trailing = label[match.end():].strip()
            if trailing.endswith((":", "：")):
                quote_sender = trailing[:-1].strip()
    body_nodes = [n for n in nodes if n.role in {"TextArea", "Edit"} and n.text]
    if named_sender:
        # A DuiLib parent can repeat its children's combined ValuePattern.
        # Keep the leaf values so a quoted message is not repeated twice.
        body_nodes = [n for n in body_nodes if not any(
            child is not n and child.role in {"TextArea", "Edit"} and child.text for child in n.walk())]
    bodies = [plain_links(n.text) for n in body_nodes]
    attachments = [n.text for n in nodes if n.role in {"Text", "Label"}
                   and FILE.fullmatch(n.text.strip()) and not parse_time(n.text, reference, observed_on)]
    if not quote_sender and body_nodes:
        # WeCom can expose time, quoted author, quote, and reply as separate
        # controls. The quoted author must be a header before the first body.
        header = [n.text for n in nodes[:nodes.index(body_nodes[0])] if n.role in {"Text", "Label"}]
        for index, label in enumerate(header[:-1]):
            if label.strip() == raw_time and (author := QUOTE_AUTHOR.fullmatch(header[index + 1].strip())):
                if len(bodies) > 1 or any(value in attachments for value in header[index + 2:]):
                    quote_sender = author[1].strip()
                break
    body = bodies[-1] if bodies else plain_links(flat_body)
    quote = "\n".join(bodies[:-1]) if len(bodies) > 1 and quote_sender else ""
    kind = "text"
    if body.strip() == "[非文本消息]":
        kind = "nontext"
    elif not body and attachments and not quote_sender:
        kind, body = "file", "[文件] " + "\n".join(attachments)
    elif not body:
        audio = any("play action" in n.text or "语音" in n.text for n in nodes)
        if audio:
            duration = next((m.group() for t in labels if (m := re.match(r"\d\d:\d\d", t))), "")
            kind, body = "audio", f"[语音 {duration}，未转写]"
        else:
            kind, body = "nontext", "[非文本消息]"
    if quote_sender and attachments:
        quote = (quote + "\n" if quote else "") + "[文件] " + "\n".join(attachments)
    # UIA Text controls sometimes carry the actual message text. Only use
    # separate leaf labels after a standalone timestamp, with a known sender.
    if kind == "nontext" and not quote_sender:
        candidates = [n.text for n in nodes if n.role in {"Text", "Label"} and n.text
                      and not parse_time(n.text, reference, observed_on)
                      and not re.fullmatch(r"\d{1,2}/\d{1,2}|\d\d:\d\d(?:\s+\d+/\d+)?", n.text)
                      and n.text.strip() != sender and not FILE.fullmatch(n.text.strip())]
        if candidates:
            body, kind = plain_links("\n".join(candidates)), "text"
    return Message(sender, timestamp, body, kind, quote_sender, quote, raw_time)


def parse_frame(root: Node, reference: date, observed_on: date | None = None) -> ParsedFrame:
    table = locate_history(root)
    # Avoid recursively treating nested quoted rows as separate messages.
    rows = []
    def visit(node):
        if node.role in ROW_ROLES:
            rows.append(node)
        else:
            for child in node.children:
                visit(child)
    visit(table)
    messages, unresolved, system = [], [], 0
    for row in rows:
        try:
            message = parse_row(row, reference, observed_on)
            if message:
                messages.append(message)
            else:
                system += 1
        except LayoutError as exc:
            unresolved.append(str(exc))
    if unresolved:
        raise LayoutError(unresolved[0] + f"（本屏共 {len(unresolved)} 条待适配）")
    if not messages:
        raise LayoutError("当前屏没有识别到带发送者和时间的成员消息，请稍微滚动聊天记录后重试。")
    return ParsedFrame(messages, system, unresolved, table)
