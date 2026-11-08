from __future__ import annotations

from datetime import date
from pathlib import Path
import threading

from PySide6.QtCore import QObject, Signal

from .merge import DisconnectedFrame, MergeResult, SequenceAssembler
from .native import NativeError, diagnostic, make_adapter
from .parser import LayoutError, parse_frame
from .recovery import (
    RecoveryController, RecoveryExhausted, RecoveryStopped,
    build_observation, nudge_to_overlap, overlaps_edge,
)
from .storage import SessionStore
from .scrolling import ScrollResult


class Events(QObject):
    status = Signal(str)
    messages = Signal(object)
    finished = Signal(str)
    failure = Signal(str)
    diagnostic_ready = Signal(str)
    ai_ready = Signal(str)


HARD_ERROR_NAMES = {"ActivationError", "PermissionRestoreError"}
RECOVERABLE_ERRORS = (DisconnectedFrame, LayoutError, NativeError, RuntimeError)


def is_hard_error(error):
    # Activation and permission failures need a restart, not a UI nudge.
    return any(cls.__name__ in HARD_ERROR_NAMES for cls in type(error).__mro__)


def classify_capture_error(error):
    if is_hard_error(error):
        return None
    text = str(error)
    if isinstance(error, NativeError) and ("打开的群" in text or "群与" in text or "群名" in text):
        return None
    if isinstance(error, NativeError) and ("离开前台" in text or "前台" in text):
        return None
    if isinstance(error, DisconnectedFrame):
        if "没有重叠" in text:
            return "gap"
        if "连续相同" in text or "只有相同" in text:
            return "duplicate"
        if "没有识别到" in text:
            return "empty"
        return "gap"
    if isinstance(error, LayoutError):
        return "layout"
    return "scroll"


def capture(events: Events, stop: threading.Event, *, group: str, reference: date,
            direction: str, pixels: int, interval: float, max_frames: int,
            session_path: Path | None, previous: list, one_frame=False, diagnose=False,
            diagnostic_path: Path | None = None, auto_recover=True, recovery_client=None,
            adapter_factory=None):
    adapter, store = None, None
    try:
        adapter = (adapter_factory or make_adapter)()
        if diagnose and hasattr(adapter, "collect_diagnostic"):
            report = adapter.collect_diagnostic(group)
            if diagnostic_path and diagnostic_path.is_file():
                report += "\n\n--- 上一次采集失败记录 ---\n" + diagnostic_path.read_text(encoding="utf-8")
            events.diagnostic_ready.emit(report)
            events.finished.emit("本地诊断已生成，包含连接状态；消息正文已省略。")
            return
        adapter.connect()
        if stop.wait(.7):
            return
        if diagnose:
            root = adapter.snapshot(group, full=True)
            events.diagnostic_ready.emit(diagnostic(root, group, getattr(adapter, "activation_report", "")))
            events.finished.emit("本地结构诊断已生成，消息正文已省略。")
            return
        store = SessionStore(session_path)
        assembler = SequenceAssembler(previous)
        previous_observation, stable = None, 0
        local_recovery_streak = 0
        last_keys, step = [], pixels
        last_parsed = None
        last_position = None
        last_scroll_reason = None
        stuck_observation = None
        stuck_keys = None
        stuck_rows = 0
        controller = RecoveryController(
            recovery_client, stop=stop, status=events.status.emit, mode="capture")

        def read():
            observed = date.today()
            root = adapter.snapshot(group)
            return root, parse_frame(root, reference, observed)

        def frame_observation(root, parsed):
            try:
                position = adapter.position(root)
            except Exception:
                position = None
            geometry = tuple(row.rect for row in parsed.table.children if row.rect is not None)
            return (tuple(message.key for message in parsed.messages), position, geometry)

        def remember_failure(root, parsed):
            nonlocal stuck_observation, stuck_keys, stuck_rows
            stuck_observation = frame_observation(root, parsed)
            stuck_keys = list(stuck_observation[0])
            stuck_rows = len(parsed.messages)

        def merge_frame(frame):
            """Call the unchanged merge rules; remember the failed viewport on error."""
            root, parsed = frame
            observation = frame_observation(root, parsed)
            try:
                result = assembler.merge(parsed.messages, direction)
            except RECOVERABLE_ERRORS:
                remember_failure(root, parsed)
                raise
            return root, parsed, result, observation

        def read_merge():
            return merge_frame(read())

        def anchored(view):
            fresh = [message.key for message in view[1].messages]
            return bool(last_keys) and overlaps_edge(last_keys, fresh, direction)

        def changed(view):
            return stuck_observation is None or frame_observation(*view) != stuck_observation

        def at_boundary(position):
            return position is not None and (
                (direction == "older" and position <= .001)
                or (direction == "newer" and position >= .999))

        def allowed_for(issue):
            can_nudge = callable(getattr(adapter, "nudge", None))
            if issue == "layout":
                return ("wait", "reread")
            actions = ["wait", "reread", "shrink"]
            if can_nudge:
                actions.insert(0, "nudge")
            return tuple(actions)

        def local_for(issue):
            can_nudge = callable(getattr(adapter, "nudge", None))
            if issue == "layout":
                return ("reread", "wait")
            if issue == "stationary":
                return ()
            if issue == "duplicate":
                actions = ["shrink", "nudge", "wait"] if can_nudge else ["shrink", "wait"]
            else:
                actions = ["nudge", "shrink", "wait"] if can_nudge else ["shrink", "wait"]
            return tuple(actions)

        def recover(error, issue, *, stationary=False):
            nonlocal local_recovery_streak, stuck_observation
            if recovery_client is None:
                local_recovery_streak += 1
                if local_recovery_streak > 6:
                    raise RecoveryExhausted(error)
            # Local rules retry once per recovery request. Without a fresh
            # failure frame, require an actual viewport change before treating
            # an action as successful.
            if stuck_observation is None:
                stuck_observation = previous_observation
            allowed = allowed_for(issue)
            local_actions = local_for(issue)
            if stationary and recovery_client is None:
                local_actions = ()

            def observe(attempt, history):
                visible_keys = stuck_keys or (last_parsed and [m.key for m in last_parsed.messages]) or []
                position = stuck_observation[1] if stuck_observation else last_position
                detail = "界面结构暂不可读" if issue == "layout" else str(error)
                return build_observation(
                    mode="capture", issue=issue, error_kind=type(error).__name__,
                    detail=detail, sensitive=(group,),
                    direction=direction, step_px=step, interval_s=interval,
                    attempt=attempt, previous_actions=history,
                    position=position,
                    visible_rows=stuck_rows or (len(last_parsed.messages) if last_parsed else 0),
                    scroll_reason=last_scroll_reason,
                    has_anchor=bool(last_keys),
                    anchored=bool(last_keys and visible_keys
                                  and overlaps_edge(last_keys, visible_keys, direction)),
                    stationary_reads=stable, at_list_end=at_boundary(position),
                    can_nudge=callable(getattr(adapter, "nudge", None)),
                    allowed_actions=allowed,
                    one_frame=one_frame,
                )

            def settled(view):
                # A scroll failure happens after the current page was already
                # committed. Re-merging that identical page can look like a
                # duplicate boundary, so accept it as an untouched checkpoint.
                root, parsed = view
                observation = frame_observation(root, parsed)
                if issue == "scroll" and observation == previous_observation:
                    return (root, parsed, MergeResult(0, "contained"), observation)
                return None

            def soft_read_merge():
                try:
                    return read_merge()
                except Exception as caught:
                    if is_hard_error(caught):
                        raise
                    if issue != "scroll":
                        return None
                    try:
                        return settled(read())
                    except Exception as caught_again:
                        if is_hard_error(caught_again):
                            raise
                        return None

            def merge_candidate(candidate):
                try:
                    return merge_frame(candidate)
                except Exception as caught:
                    if is_hard_error(caught):
                        raise
                    return settled(candidate)

            def execute(action, observation):
                nonlocal step
                if action.name == "wait":
                    if stop.wait(action.seconds):
                        raise RecoveryStopped()
                    return soft_read_merge()
                if action.name == "reread":
                    return soft_read_merge()
                if action.name == "shrink":
                    new_step = action.step_px if action.step_px is not None else max(60, step // 2)
                    new_step = max(60, min(int(new_step), step - 1))
                    if step <= 60 or new_step >= step:
                        return None
                    step = new_step
                if action.name in {"nudge", "shrink"}:
                    if not callable(getattr(adapter, "nudge", None)):
                        return None
                    accept = anchored if issue in {"gap", "empty"} else changed
                    try:
                        candidate = nudge_to_overlap(
                            adapter, read, anchored, direction, stop, accept=accept)
                    except Exception as caught:
                        if is_hard_error(caught):
                            raise
                        return None
                    if candidate is None:
                        return soft_read_merge() if action.name == "shrink" else None
                    return merge_candidate(candidate)
                return None

            return controller.run(
                issue=issue, error=error, allowed=allowed,
                local_actions=local_actions, observe=observe, execute=execute)

        def commit(root, parsed, result, observation):
            nonlocal last_keys, last_parsed, last_position, previous_observation, stable
            nonlocal stuck_observation, stuck_keys, stuck_rows, local_recovery_streak
            store.save(group, reference.isoformat(), assembler.messages, parsed.messages, direction)
            events.messages.emit(list(assembler.messages))
            last_keys = list(observation[0])
            last_parsed = parsed
            last_position = observation[1]
            stuck_observation = stuck_keys = None
            stuck_rows = 0
            if result.added:
                local_recovery_streak = 0
            stable = stable + 1 if observation == previous_observation else 0
            previous_observation = observation
            retained = sum(not message.is_placeholder for message in assembler.messages)
            events.status.emit(f"已保留 {retained} 条 · 第 {count + 1} 屏 · 本屏新增 {result.added} 条")
            return retained

        limit = 1 if one_frame else max_frames
        pending = None
        for count in range(limit):
            if stop.is_set():
                events.finished.emit("已暂停，当前记录已自动保存。")
                return
            if pending is not None:
                frame, pending = pending, None
            else:
                try:
                    frame = read_merge()
                except Exception as error:
                    if is_hard_error(error):
                        raise
                    issue = classify_capture_error(error)
                    if not auto_recover or one_frame or issue is None:
                        raise
                    try:
                        frame = recover(error, issue)
                    except RecoveryStopped:
                        events.finished.emit("已暂停，当前记录已自动保存。")
                        return
                    except RecoveryExhausted:
                        raise error
            root, parsed, result, observation = frame
            retained = commit(root, parsed, result, observation)
            if one_frame:
                if all(message.is_placeholder for message in parsed.messages):
                    events.finished.emit("当前屏仅有已自动排除的非文本消息。")
                else:
                    events.finished.emit("当前屏已读取并自动保存。")
                return
            if stable >= 6:
                if at_boundary(last_position):
                    events.finished.emit("已到达当前可读取历史的边界，请核对后导出。")
                    return
                if not auto_recover:
                    events.finished.emit("界面未再移动，已暂停。请核对滚动位置后继续；本次不判定为读取完整。")
                    return
                try:
                    root, parsed, result, observation = recover(
                        NativeError("列表未再移动，且尚未核验到历史边界。"), "stationary",
                        stationary=True)
                except RecoveryStopped:
                    events.finished.emit("已暂停，当前记录已自动保存。")
                    return
                except RecoveryExhausted:
                    events.finished.emit("界面未再移动，已暂停。请核对滚动位置后继续；本次不判定为读取完整。")
                    return
                stable = 0
            scrolled = None
            try:
                scrolled = adapter.scroll(root, direction, step, stop=stop)
            except Exception as error:
                if is_hard_error(error):
                    raise
                if not auto_recover:
                    raise
                issue = classify_capture_error(error) or "scroll"
                try:
                    pending = recover(error, issue)
                except RecoveryStopped:
                    events.finished.emit("已暂停，当前记录已自动保存。")
                    return
                except RecoveryExhausted:
                    raise error
                continue
            if isinstance(scrolled, ScrollResult):
                last_scroll_reason = scrolled.reason
                events.status.emit(f"已保留 {retained} 条 · 第 {count + 1} 屏 · {scrolled.summary}")
                if scrolled.reason == "cancelled":
                    events.finished.emit("已暂停，当前记录已自动保存。")
                    return
                if scrolled.reason in {"stationary", "unmeasured", "overshoot", "budget"}:
                    if not auto_recover:
                        events.finished.emit("界面未再移动，已暂停。请核对滚动位置后继续；本次不判定为读取完整。")
                        return
                    issue = "stationary" if scrolled.reason == "stationary" else "scroll"
                    try:
                        pending = recover(
                            NativeError("滚动结果未能核对：" + scrolled.reason), issue,
                            stationary=scrolled.reason == "stationary")
                    except RecoveryStopped:
                        events.finished.emit("已暂停，当前记录已自动保存。")
                        return
                    except RecoveryExhausted:
                        events.finished.emit("界面未再移动，已暂停。请核对滚动位置后继续；本次不判定为读取完整。")
                        return
            if stop.wait(interval):
                events.finished.emit("已暂停，当前记录已自动保存。")
                return
        events.finished.emit("已达到本次采集屏数上限，记录已保存。可继续采集或导出当前结果。")
    except RecoveryStopped:
        events.finished.emit("已暂停，当前记录已自动保存。")
    except Exception as exc:
        message = str(exc)
        if adapter and hasattr(adapter, "failure_diagnostic") and diagnostic_path:
            try:
                diagnostic_path.parent.mkdir(parents=True, exist_ok=True)
                diagnostic_path.write_text(adapter.failure_diagnostic(group, exc), encoding="utf-8")
                message += "\n失败诊断已自动保存，可通过“导出界面结构诊断”一并导出。"
            except OSError:
                message += "\n自动保存失败诊断未成功。"
        events.failure.emit(message)
    finally:
        if store:
            store.close()
        if adapter:
            adapter.close()
