"""Bounded UI recovery actions selected by local rules or an AI model."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
import random
import re

from .deepseek import APIConfigurationError, APIError


ACTIONS = ("nudge", "wait", "shrink", "reread")
_PARAMS = {
    "nudge": frozenset(),
    "wait": frozenset({"seconds"}),
    "shrink": frozenset({"step_px"}),
    "reread": frozenset(),
}
_LABELS = {
    "nudge": "反向微调",
    "wait": "等待后重读",
    "shrink": "缩小步长",
    "reread": "重读当前屏",
}
_ISSUES = {
    "gap": "两屏没有重叠",
    "duplicate": "交界处有重复消息",
    "empty": "当前屏没有可识别消息",
    "layout": "界面结构暂不可读",
    "scroll": "滚动结果无法核对",
    "stationary": "界面未再移动",
}
RECOVERY_SYSTEM = (
    "你是企业微信采集恢复控制器。你只决定界面调整动作，"
    "不解析、不改写、不复述、不推测、不要求聊天内容。"
    "只输出规定的 JSON，不输出解释、Markdown 或代码块。"
)


class InvalidAction(ValueError):
    pass


class RecoveryStopped(Exception):
    pass


class RecoveryExhausted(Exception):
    def __init__(self, error=None):
        super().__init__(str(error) if error is not None else "本地恢复动作用完")
        self.error = error


@dataclass(frozen=True)
class RecoveryAction:
    name: str
    seconds: float = 1.5
    step_px: int | None = None
    reason: str = ""


def sanitize_detail(value, sensitive=()):
    text = str(value or "")
    for token in sensitive:
        if token:
            text = text.replace(str(token), "[已省略]")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:180]


def build_observation(*, mode, issue, error_kind="", detail="", sensitive=(),
                      direction="newer", step_px=0, interval_s=0.0, attempt=0,
                      previous_actions=(), position=None, visible_rows=0,
                      scroll_reason=None, has_anchor=False, anchored=False,
                      stationary_reads=0, at_list_end=False, can_nudge=False,
                      allowed_actions=(), one_frame=False):
    """Build the only payload that may be sent to the recovery model.

    Message text, senders, timestamps and the group name never enter this
    payload. The model only chooses an interface adjustment.
    """
    return {
        "mode": mode,
        "issue": issue,
        "error": error_kind,
        "detail": sanitize_detail(detail, sensitive),
        "direction": direction,
        "step_px": int(step_px or 0),
        "interval_s": round(float(interval_s or 0), 3),
        "attempt": int(attempt),
        "previous_actions": [str(action) for action in previous_actions][-6:],
        "position": None if position is None else round(float(position), 6),
        "visible_rows": int(visible_rows),
        "scroll_reason": scroll_reason,
        "has_anchor": bool(has_anchor),
        "anchored": bool(anchored),
        "stationary_reads": int(stationary_reads),
        "at_list_end": bool(at_list_end),
        "can_nudge": bool(can_nudge),
        "allowed_actions": [str(action) for action in allowed_actions],
        "one_frame": bool(one_frame),
    }


def _number(value):
    return type(value) in (int, float) and not isinstance(value, bool)


def _clamp(value, low, high):
    return max(low, min(high, value))


def parse_action(raw, allowed, current_step=0):
    if not isinstance(raw, str):
        raise InvalidAction("模型输出不是文本。")
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if (len(lines) >= 3 and lines[0].strip().lower() in {"```", "```json"}
                and lines[-1].strip() == "```"):
            text = "\n".join(lines[1:-1]).strip()
    try:
        data = json.loads(text)
    except (TypeError, ValueError) as error:
        raise InvalidAction("模型输出不是完整 JSON。") from error
    if not isinstance(data, dict):
        raise InvalidAction("模型输出必须是单个 JSON 对象。")
    if set(data) - {"action", "params", "reason"}:
        raise InvalidAction("模型输出包含未知字段。")
    name = data.get("action")
    if not isinstance(name, str) or name not in allowed or name not in _PARAMS:
        raise InvalidAction("模型选择了未允许的动作。")
    params = data.get("params") or {}
    if not isinstance(params, dict) or set(params) - _PARAMS[name]:
        raise InvalidAction("模型返回了未知动作参数。")
    reason = data.get("reason") or ""
    if not isinstance(reason, str):
        reason = ""
    reason = re.sub(r"\s+", " ", reason).strip()[:120]

    seconds, step_px = 1.5, None
    if name == "wait" and "seconds" in params:
        value = params["seconds"]
        if not _number(value):
            raise InvalidAction("wait.seconds 必须是数字。")
        seconds = round(_clamp(float(value), .5, 10), 2)
    if name == "shrink" and "step_px" in params:
        value = params["step_px"]
        if type(value) is not int:
            raise InvalidAction("缩步长参数必须是整数。")
        high = max(60, int(current_step or 0) - 1)
        step_px = _clamp(value, 60, high)
    elif name == "shrink":
        step_px = max(60, int(current_step or 0) // 2)
    return RecoveryAction(name, seconds=seconds, step_px=step_px, reason=reason)


def request_action(client, observation, allowed, feedback=""):
    payload = {
        "observation": observation,
        "allowed_actions": list(allowed),
        "action_help": {
            "nudge": "向已读方向做一次很小的反向滚动，让交界处出现可区分的内容",
            "shrink": "缩小下一步滚动步长，并做一次反向微调以恢复重叠",
            "reread": "保持位置，重新读取当前屏",
            "wait": "等待片刻后重新读取当前屏",
        },
        "output_schema": {
            "action": "one of allowed_actions",
            "params": "optional object; wait: seconds, shrink: step_px",
            "reason": "short Chinese text, at most 120 characters",
        },
    }
    if feedback:
        payload["feedback"] = feedback[:180]
    raw = client.complete(
        json.dumps(payload, ensure_ascii=False),
        RECOVERY_SYSTEM,
        timeout=15, attempts=1, max_tokens=256,
    )
    return parse_action(raw, allowed, observation.get("step_px", 0))


def _api_delay(failures):
    delay = min(30.0, 1.5 * 2 ** min(max(failures, 1) - 1, 4))
    return delay * random.uniform(.8, 1.2)


def _action_delay(attempt):
    delay = min(12.0, .8 * 2 ** min(max(attempt, 1) - 1, 4))
    return delay * random.uniform(.8, 1.2)


def _label(action):
    text = _LABELS.get(action.name, action.name)
    return text + (f"（{action.reason}）" if action.reason else "")


class RecoveryController:
    """Retry recoverable failures until one recovery action succeeds."""

    def __init__(self, client=None, *, stop, status=None, mode="capture", max_actions=12):
        self.client = client
        self.stop = stop
        self.status = status or (lambda _text: None)
        self.mode = mode
        self.max_actions = max_actions
        self.history = deque(maxlen=6)

    def run(self, *, issue, error, allowed, local_actions, observe, execute):
        attempt = 0
        local_index = 0
        api_failures = 0
        feedback = ""
        while True:
            if self.stop.is_set():
                raise RecoveryStopped()
            if attempt >= self.max_actions:
                raise RecoveryExhausted(error)
            allowed = tuple(dict.fromkeys(allowed))
            local_actions = tuple(dict.fromkeys(local_actions))
            observation = observe(attempt, list(self.history))
            if self.client is not None:
                self.status(
                    f"AI 恢复：{_ISSUES.get(issue, issue)}，"
                    f"正在请求模型决策（第 {attempt + api_failures + 1} 次）。")
                try:
                    action = request_action(self.client, observation, allowed, feedback)
                except APIConfigurationError:
                    raise
                except (APIError, InvalidAction) as caught:
                    api_failures += 1
                    feedback = str(caught)
                    delay = _api_delay(api_failures)
                    self.status(f"模型接口暂不可用或返回无效，{delay:.1f} 秒后重试。")
                    if api_failures >= 4:
                        raise RecoveryExhausted(error) from caught
                    if self.stop.wait(delay):
                        raise RecoveryStopped() from caught
                    continue
                api_failures = 0
            else:
                if local_index >= len(local_actions):
                    raise RecoveryExhausted(error)
                action = RecoveryAction(local_actions[local_index])
                local_index += 1
            self.history.append(action.name)
            prefix = "AI 恢复" if self.client is not None else "本地恢复"
            self.status(f"{prefix}：{_label(action)}。")
            outcome = execute(action, observation)
            if outcome is not None:
                self.status("已恢复，继续读取。")
                return outcome
            attempt += 1
            if self.client is None:
                if local_index >= len(local_actions):
                    raise RecoveryExhausted(error)
                delay = .2 * attempt
            else:
                delay = _action_delay(attempt)
                self.status(f"恢复动作未生效，{delay:.1f} 秒后重试。")
            if self.stop.wait(delay):
                raise RecoveryStopped()


def overlaps_edge(known, fresh, direction):
    if known == fresh and len(set(known)) > 1:
        return True
    left, right = (known, fresh) if direction == "newer" else (fresh, known)
    for count in range(min(len(left), len(right)), 1, -1):
        if left[-count:] == right[:count] and len(set(right[:count])) > 1:
            return True
    return False


def nudge_to_overlap(adapter, read, anchored, direction, stop, *, accept=None):
    accept = accept or anchored
    reverse = "older" if direction == "newer" else "newer"
    # At most three single wheel inputs, each based on a fresh verified
    # viewport. Never move the interface when the group cannot be read.
    for index in range(4):
        if stop.is_set():
            return None
        frame = read()
        if accept(frame):
            return frame
        if index == 3:
            break
        adapter.nudge(frame[0], reverse, stop=stop)
        if stop.wait(.35):
            return None
    return None
