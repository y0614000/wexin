"""Control wheel scrolling using observed viewport displacement."""

from dataclasses import dataclass
import time


@dataclass(frozen=True)
class ScrollResult:
    requested: int
    target: int
    distance: float | None
    pulses: int
    reason: str

    @property
    def summary(self):
        if self.distance is None:
            return f"已发送 {self.pulses} 次滚轮输入，客户端位移未能确认"
        text = f"滚动 {round(self.distance)} px / 目标 {self.target} px"
        if self.target < self.requested:
            text += "（保留消息重叠）"
        return text


def advance_viewport(requested, limit, pulse, measure, *, stop=None,
                     clock=time.monotonic, sleep=time.sleep, budget=6.0):
    """Send separate detents; a client may ignore the size of one wheel event.

    ``measure`` returns total forward displacement from the original frame,
    or None when its anchors are no longer identifiable. Never continue
    blindly after losing those anchors.
    """
    target = max(1, min(int(requested), int(limit)))
    started = clock()
    distance, pulses, step, stationary = 0.0, 0, None, 0
    last_progress_pulse = 0

    def cancelled():
        return stop is not None and stop.is_set()

    def result(reason, value=...):
        return ScrollResult(requested, target, distance if value is ... else value, pulses, reason)

    while pulses < 600 and clock() - started < budget:
        if cancelled():
            return result("cancelled")
        if distance >= target:
            return result("target")
        if step:
            if distance + step > limit:
                return result("overlap")
            # Stop at the nearest attainable wheel quantum.
            if target - distance <= step / 2:
                return result("target")
        pulse()
        pulses += 1
        settle_deadline = min(started + budget, clock() + .18)
        observed = None
        while clock() < settle_deadline:
            if cancelled():
                return result("cancelled")
            if stop is not None:
                if stop.wait(.012):
                    return result("cancelled")
            else:
                sleep(.012)
            observed = measure()
            if observed is not None and abs(observed - distance) >= .5:
                break
        if observed is None:
            return result("unmeasured", None)
        if observed < distance - .5:
            raise RuntimeError("消息列表的滚动方向或位置发生变化，已暂停。请回到已读边界后继续。")
        if observed > limit + .5:
            # One unusually large native detent can exceed the overlap
            # margin before it can be calibrated. Let the caller recover.
            return result("overshoot", observed)
        if observed > distance + .5:
            step = (observed - distance) / (pulses - last_progress_pulse)
            last_progress_pulse = pulses
            distance, stationary = observed, 0
        else:
            stationary += 1
            if stationary >= 2:
                return result("stationary")
    return result("budget")
