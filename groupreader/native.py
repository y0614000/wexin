from __future__ import annotations

import ctypes
import ctypes.util
from ctypes import wintypes
from pathlib import PureWindowsPath
import platform
import struct
import time

from . import WINDOWS_BUILD
from .model import Node
from .parser import FLAT_ROW, LayoutError, ROW_ROLES, TABLE_ROLES, history_tables, locate_history
from .scrolling import advance_viewport


MAC_ROLES = {"AXStaticText": "Text", "AXTextArea": "TextArea", "AXTextField": "Edit",
             "AXButton": "Button", "AXRow": "Row", "AXCell": "Cell", "AXTable": "Table",
             "AXList": "List", "AXScrollArea": "ScrollArea", "AXScrollBar": "ScrollBar",
             "AXGroup": "Group", "AXSplitGroup": "Group", "AXWindow": "Window"}
MAC_OUTLINE_TEXT = {"AXStaticText", "AXTextField", "AXUnknown", "AXGroup", "AXSplitGroup", "AXScrollBar"}
MAC_OUTLINE_LEAVES = TABLE_ROLES | ROW_ROLES | {"Cell", "AXColumn", "ScrollBar", "TextArea"}
MAC_SNAPSHOT_SECONDS = 12
MAC_SNAPSHOT_NODES = 10_000
MAC_SNAPSHOT_DEPTH = 45


class NativeError(RuntimeError):
    pass


def parent_of(root: Node, target: Node) -> Node | None:
    for node in root.walk():
        if any(child is target for child in node.children):
            return node
    return None


def carrier(root: Node, table: Node) -> Node:
    candidate = table
    while (parent := parent_of(root, candidate)) is not None:
        if parent.role in {"ScrollArea", "Pane"}:
            return parent
        candidate = parent
    return table


class MacAdapter:
    def __init__(self):
        import ApplicationServices as AS
        import AppKit
        import Quartz
        self.AS, self.AppKit, self.Quartz = AS, AppKit, Quartz
        self.application = None
        self.running = None

    def connect(self):
        if not self.AS.AXIsProcessTrusted():
            raise NativeError(
                "尚未获得辅助功能权限。请在系统设置→隐私与安全性→辅助功能中，"
                "允许“群聊记录助手”（源码运行时允许对应的 Python/终端），然后重新打开工具。"
            )
        candidates = [app for app in self.AppKit.NSWorkspace.sharedWorkspace().runningApplications()
                      if app.localizedName() in {"企业微信", "WeCom", "WeWork"}]
        if len(candidates) != 1:
            raise NativeError("请先登录企业微信桌面客户端，并打开一个目标群。")
        self.running = candidates[0]
        self.application = self.AS.AXUIElementCreateApplication(self.running.processIdentifier())
        self.AS.AXUIElementSetMessagingTimeout(self.application, 1.0)
        self.running.activateWithOptions_(self.AppKit.NSApplicationActivateIgnoringOtherApps)

    def get(self, element, attribute):
        error, value = self.AS.AXUIElementCopyAttributeValue(element, attribute, None)
        return value if error == 0 else None

    def snapshot(self, group: str, *, full=False) -> Node:
        front = self.AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if front.processIdentifier() != self.running.processIdentifier():
            raise NativeError("企业微信已离开前台，采集已暂停。回到目标群后再继续。")
        windows = self.get(self.application, "AXWindows") or []
        if not windows:
            raise NativeError("没有找到企业微信主窗口。")
        return self._snapshot_window(windows[0], group, full=full)

    def _text(self, element, role):
        if role in {"AXStaticText", "AXTextArea", "AXTextField", "AXScrollBar"}:
            value = self.get(element, "AXValue")
            if isinstance(value, str) or (role == "AXScrollBar" and isinstance(value, (int, float))):
                return str(value)
            return self.get(element, "AXTitle") or self.get(element, "AXDescription") or ""
        label = self.get(element, "AXTitle") or self.get(element, "AXDescription")
        if label:
            return label
        value = self.get(element, "AXValue")
        return value if isinstance(value, str) else ""

    def _snapshot_window(self, window, group: str, *, full=False) -> Node:
        count, deadline = 0, time.monotonic() + MAC_SNAPSHOT_SECONDS
        remedy = "此前成功读取的记录已保留。请等企业微信加载完成，保留“查找聊天记录”面板，回到上次读取边界后继续。"

        def check_time():
            if time.monotonic() > deadline:
                raise NativeError(f"本屏读取企业微信界面超过 {MAC_SNAPSHOT_SECONDS} 秒。{remedy}")

        def read(element, depth=0, *, outline=False):
            nonlocal count
            count += 1
            check_time()
            if count > MAC_SNAPSHOT_NODES:
                raise NativeError(f"本屏扫描的企业微信控件超过 {MAC_SNAPSHOT_NODES:,} 个。{remedy}")
            if depth > MAC_SNAPSHOT_DEPTH:
                raise NativeError(f"本屏企业微信控件嵌套超过 {MAC_SNAPSHOT_DEPTH} 层。{remedy}")
            role = self.get(element, "AXRole") or "Unknown"
            text = self._text(element, role) if not outline or role in MAC_OUTLINE_TEXT else ""
            normalized_role = MAC_ROLES.get(role, role)
            if role == "AXUnknown" and text:
                normalized_role = "Text"
            node = Node(normalized_role, str(text), native=element)
            if not outline or normalized_role not in MAC_OUTLINE_LEAVES:
                for child in self.get(element, "AXChildren") or []:
                    node.children.append(read(child, depth + 1, outline=outline))
            return node

        def confirm_group(root):
            if not any(n.role == "Edit" and n.text.strip() == group for n in root.walk()):
                raise NativeError(f"当前打开的群与“{group}”不匹配，已停止读取。请确认群名完全一致。")

        if not full:
            # Inspect the window layout without descending into conversation
            # lists or the main chat. Keep the native scroll area and bar.
            outline = read(window, outline=True)
            confirm_group(outline)
            for table in history_tables(outline):
                visible = self.get(table.native, "AXVisibleRows")
                if visible is None:
                    visible = self._rows_in_view(table, carrier(outline, table), check_time)
                rows = [read(element) for element in visible]
                if any(row.role in ROW_ROLES for row in rows):
                    table.children = rows
                    check_time()
                    return outline
            check_time()
            raise LayoutError("聊天记录面板还没有可见的消息。请等企业微信加载完成后再试。")

        # Full window reads are reserved for explicit diagnostics.
        root = read(window)
        check_time()
        confirm_group(root)
        return root

    def _rect(self, element):
        # PyObjC does not consistently wrap AXValueGetValue. Use the public
        # C function for CGPoint/CGSize instead of depending on object reprs.
        import objc
        class Pair(ctypes.Structure):
            _fields_ = [("a", ctypes.c_double), ("b", ctypes.c_double)]
        library = ctypes.CDLL(ctypes.util.find_library("ApplicationServices"))
        function = library.AXValueGetValue
        function.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        function.restype = ctypes.c_bool
        position, size = Pair(), Pair()
        pv, sv = self.get(element, "AXPosition"), self.get(element, "AXSize")
        if pv is None or sv is None:
            raise NativeError("无法取得聊天记录区域的位置。")
        if not function(objc.pyobjc_id(pv), 1, ctypes.byref(position)) or not function(
                objc.pyobjc_id(sv), 2, ctypes.byref(size)):
            raise NativeError("无法解析聊天记录区域的位置。")
        return position.a, position.b, size.a, size.b

    def _rows_in_view(self, table: Node, area: Node, check_time):
        # Older layouts may only expose all row handles. Read their positions
        # before requesting any message content.
        handles = self.get(table.native, "AXRows")
        direct_children = handles is None
        if direct_children:
            handles = self.get(table.native, "AXChildren")
        if not handles:
            return []
        _, top, _, height = self._rect(area.native)
        visible = []
        for element in handles:
            check_time()
            if direct_children:
                role = self.get(element, "AXRole")
                if MAC_ROLES.get(role, role) not in ROW_ROLES:
                    continue
            _, y, _, row_height = self._rect(element)
            if row_height > 0 and y < top + height and y + row_height > top:
                visible.append(element)
        return visible

    def _scroll_distance(self, table: Node, direction: str, pixels: int, top: float, height: float) -> int:
        # Leave the two rows on the advancing edge in view where possible.
        distance = max(1, min(pixels, int(height / 2)))
        rows = [row for row in table.children if row.role in ROW_ROLES]
        if not rows:
            return distance
        edge = min(2, len(rows))
        anchor = rows[edge - 1] if direction == "older" else rows[-edge]
        try:
            _, y, _, row_height = self._rect(anchor.native)
        except NativeError:
            return distance
        available = top + height - y if direction == "older" else y + row_height - top
        return max(1, min(distance, int(available) - 8))

    def nudge(self, root, direction, *, stop=None):
        if stop is not None and stop.is_set():
            return
        self.scroll(root, direction, 60, stop=stop)

    def scroll(self, root: Node, direction: str, pixels: int, *, stop=None):
        if stop is not None and stop.is_set():
            return
        table = locate_history(root)
        area = carrier(root, table)
        x, y, width, height = self._rect(area.native)
        if width <= 0 or height <= 0:
            raise NativeError("聊天记录窗口不可见，请展开窗口。")
        quartz = self.Quartz
        point = (x + width * .55, y + height * .55)
        move = quartz.CGEventCreateMouseEvent(None, quartz.kCGEventMouseMoved, point, 0)
        quartz.CGEventPost(quartz.kCGHIDEventTap, move)
        distance = self._scroll_distance(table, direction, pixels, y, height)
        delta = distance if direction == "older" else -distance
        event = quartz.CGEventCreateScrollWheelEvent(None, quartz.kCGScrollEventUnitPixel, 1, delta)
        quartz.CGEventSetLocation(event, point)
        quartz.CGEventPost(quartz.kCGHIDEventTap, event)

    def position(self, root: Node):
        area = carrier(root, locate_history(root))
        for node in area.children:
            if node.role == "ScrollBar":
                try:
                    return round(float(node.text), 6)
                except ValueError:
                    pass
        return None

    def close(self):
        pass


class WindowsAdapter:
    def __init__(self):
        self.window = None
        self.com_initialized = False
        self.activation_report = ""
        self.activation_attempted = False
        self.window_inventory = []
        self.last_root = None
        self.stage = "未开始连接"
        self.scroll_report = ""

    def _inventory(self):
        """Collect native window evidence without importing the UIA backend."""
        import win32gui
        import win32process
        handles, lines, processes = [], [], {}
        win32gui.EnumWindows(lambda handle, _: handles.append(handle), None)
        for handle in handles:
            try:
                _, pid = win32process.GetWindowThreadProcessId(handle)
                if pid not in processes:
                    processes[pid] = self._process_name(pid)
                executable = processes[pid]
                class_name = win32gui.GetClassName(handle)
                if executable not in {"wxwork.exe", "wecom.exe", "wework.exe"} and class_name.casefold() not in {"weworkwindow", "wechatlogin"}:
                    continue
                left, top, right, bottom = win32gui.GetWindowRect(handle)
                lines.append(f"hwnd={handle} pid={pid} exe={executable or '<query-denied>'} class={class_name} "
                             f"visible={bool(win32gui.IsWindowVisible(handle))} minimized={bool(win32gui.IsIconic(handle))} "
                             f"rect={left},{top},{right-left},{bottom-top}")
            except Exception:
                continue
        return [f"native_windows={len(handles)} wecom_windows={len(lines)}"] + lines

    def connect(self, *, focus=True):
        self.stage = "枚举原生窗口"
        try:
            self.window_inventory = self._inventory()
        except Exception as error:
            self.window_inventory = ["native_inventory_error=" + type(error).__name__ + ": " + str(error)]
        self.stage = "初始化 UI Automation"
        # UI Automation must be initialized in the capture worker thread.
        # Match pywinauto's MTA mode. This worker has no STA message loop.
        result = ctypes.windll.ole32.CoInitializeEx(None, 0)
        self.com_initialized = result in {0, 1}
        if not self.com_initialized:
            raise NativeError(f"Windows UI Automation 的 COM 初始化失败（0x{result & 0xffffffff:08x}），请导出诊断。")
        from pywinauto import Desktop
        desktop = Desktop(backend="win32")
        native = self._find_window(desktop.windows(visible_only=True),
                                   ctypes.windll.user32.GetForegroundWindow())
        if native is None:
            raise NativeError("没有识别到企业微信主窗口。请展开已登录的企业微信；工具和企业微信需使用同一用户及相同权限运行。")
        if native.is_minimized():
            native.restore()
        self.stage = "连接主窗口"
        self.window = Desktop(backend="uia").window(handle=native.handle).wrapper_object()
        if focus:
            self.window.set_focus()

    def failure_diagnostic(self, group: str, error=None):
        lines = [f"build={WINDOWS_BUILD}", f"platform={platform.system()} python_bits={struct.calcsize('P') * 8}",
                 f"stage={self.stage}", f"target_name_length={len(group)}"]
        lines.extend(self.window_inventory or ["native_inventory=not-collected"])
        if error is not None:
            lines.append("error=" + type(error).__name__ + ": " + str(error))
        if self.activation_report:
            lines.append(self.activation_report)
        if self.scroll_report:
            lines.append(self.scroll_report)
        if self.last_root is not None:
            lines.append(diagnostic(self.last_root, group, hide_text=True))
        return "\n".join(lines)

    def collect_diagnostic(self, group: str):
        # Export evidence even when the window cannot be connected.
        error = None
        try:
            self.connect(focus=False)
            self.last_root = self.snapshot(group, full=True)
        except Exception as caught:
            error = caught
        return self.failure_diagnostic(group, error)

    def _request_accessibility(self, root: Node):
        # Request the documented MSAA client object. Qt and other providers
        # may initialize their accessible content in response to this call.
        from comtypes import GUID
        from comtypes.automation import IDispatch
        function = ctypes.OleDLL("oleacc").AccessibleObjectFromWindow
        function.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.POINTER(GUID),
                             ctypes.POINTER(ctypes.POINTER(IDispatch))]
        function.restype = ctypes.c_long
        handles = {node.native.handle for node in root.walk()
                   if node.native is not None and getattr(node.native, "handle", 0)}
        for handle in sorted(handles)[:16]:
            pointer = ctypes.POINTER(IDispatch)()
            try:
                function(handle, 0xFFFFFFFC, ctypes.byref(IDispatch._iid_), ctypes.byref(pointer))
            except OSError:
                continue

    def _refresh_window(self):
        from pywinauto import Desktop
        self.window = Desktop(backend="uia").window(handle=self.window.handle).wrapper_object()

    def _activate_provider(self, *, dry_run=False):
        from .windows_accessibility import activate
        try:
            self.activation_report = activate(self.window.handle, dry_run=dry_run)
        except Exception as error:
            self.activation_report = "控件接口兼容激活未完成：" + str(error)
            raise NativeError(self.activation_report) from error

    @staticmethod
    def _needs_activation(root: Node) -> bool:
        try:
            return not history_tables(root)
        except LayoutError:
            return True

    @staticmethod
    def _group_headings(root: Node, group: str):
        x, y, width, height = root.rect
        bottom = y + height * .25
        markers = [n.rect[1] for n in root.walk() if n.text.strip() in
                   {"查找聊天记录", "查找聊天内容", "聊天记录", "搜索聊天记录"} and n.rect and n.rect[1] > y]
        if markers:
            bottom = min(bottom, min(markers))
        # Activation can expose the visible title as a Group/Custom.
        return [n for n in root.walk() if n.text.strip() == group and n.rect and
                n.role in {"Text", "Edit", "Label", "Button", "Group", "Pane", "Custom"} and
                n.rect[2] > 0 and n.rect[3] > 0 and n.rect[0] > x + width * .2 and
                y <= n.rect[1] < bottom]

    @staticmethod
    def _control_text(wrapper, name: str) -> tuple[str, bool]:
        for attribute in ("iface_value", "iface_legacy_iaccessible"):
            try:
                value = getattr(wrapper, attribute).CurrentValue
                if isinstance(value, str) and value:
                    return value, True
            except Exception:
                pass
        return name, False

    @staticmethod
    def _visible_rows(table: Node, area: Node):
        wrapper = table.native
        try:
            from pywinauto.uia_defines import IUIA
            from pywinauto.uia_element_info import elements_from_uia_array
            from pywinauto.controls.uiawrapper import UIAWrapper
            uia = IUIA()
            condition = uia.iuia.CreatePropertyCondition(uia.UIA_dll.UIA_IsOffscreenPropertyId, False)
            items = wrapper.element_info.element.FindAll(uia.tree_scope["children"], condition)
            children = [UIAWrapper(info) for info in elements_from_uia_array(items, False)]
        except Exception:
            children = wrapper.children()
        x, y, width, height = area.rect
        tx, ty, tw, th = table.rect
        right, bottom = min(x + width, tx + tw), min(y + height, ty + th)
        x, y = max(x, tx), max(y, ty)
        visible = []
        for child in children:
            rect = child.rectangle()
            if rect.width() > 0 and rect.height() > 0 and rect.left < right and rect.right > x and rect.top < bottom and rect.bottom > y:
                visible.append(child)
        return visible

    @staticmethod
    def _empty_content(root: Node) -> bool:
        return all(node.role in {"Window", "Pane", "Group", "TitleBar", "Border"} and not node.text.strip()
                   for node in root.walk() if node is not root)

    @staticmethod
    def _process_name(pid: int) -> str:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return PureWindowsPath(buffer.value).name.casefold()
            return ""
        finally:
            kernel.CloseHandle(handle)

    def _find_window(self, windows, foreground):
        processes = {}
        candidates = []
        for window in windows:
            try:
                pid = window.process_id()
                if pid not in processes:
                    processes[pid] = self._process_name(pid)
                executable = processes[pid]
                class_name = window.class_name().casefold()
                if class_name == "wechatlogin":
                    continue
                known_class = class_name == "weworkwindow"
                if executable not in {"wxwork.exe", "wecom.exe", "wework.exe"}:
                    if executable or not known_class:
                        continue
                rectangle = window.rectangle()
                area = max(0, rectangle.width()) * max(0, rectangle.height())
                rank = (known_class, area, window.handle == foreground,
                        window.window_text() in {"企业微信", "WeCom", "WeWork"})
                candidates.append((rank, window))
            except Exception:
                # Desktop enumeration can race with windows closing or
                # processes denying limited information access.
                continue
        return max(candidates, key=lambda item: item[0])[1] if candidates else None

    def snapshot(self, group: str, *, full=False) -> Node:
        if not full and ctypes.windll.user32.GetForegroundWindow() != self.window.handle:
            raise NativeError("企业微信已离开前台，采集已暂停。回到目标群后继续。")
        self.stage = "读取控件结构"
        count, deadline = 0, time.monotonic() + 15
        def read(wrapper, depth=0, *, outline=False, in_row=False, force_row=False):
            nonlocal count
            count += 1
            if count > 10000 or depth > 45 or time.monotonic() > deadline:
                raise NativeError("Windows UI Automation 读取超时，请关闭不必要的企业微信侧栏后重试。")
            info = wrapper.element_info
            role, name = info.control_type, info.name or ""
            if force_row and role not in {"ScrollBar", "Header", "Separator"}:
                role = role if role in ROW_ROLES else "ListItem"
            in_row = in_row or role in ROW_ROLES
            text, has_value = self._control_text(wrapper, name)
            if role == "Document":
                role = "TextArea"
            if in_row:
                if name == "chatSender":
                    role = "Button"
                elif name == "chatSendTime":
                    role = "Text"
                elif name == "quote_message_sender":
                    role, text = "Text", text.rstrip(":：") + "："
                elif has_value and role in {"Text", "Edit", "Custom", "Pane"}:
                    role = "Text" if name.lower().endswith(("filename", "filesize")) else "TextArea"
                elif not has_value and name in {"chatMessageParent", "HorizontalLayoutUI", "compound_message_item_layout",
                                               "VerticalLayoutUI", "chat_content", "whole_item"}:
                    text = ""
            rectangle = wrapper.rectangle()
            node = Node(role, str(text), rect=(rectangle.left, rectangle.top,
                        rectangle.width(), rectangle.height()), native=wrapper, control_name=name)
            if not outline or role not in TABLE_ROLES | ROW_ROLES | {"TextArea"}:
                for child in wrapper.children():
                    node.children.append(read(child, depth + 1, outline=outline, in_row=in_row))
            return node
        root = read(self.window, outline=not full)
        self.last_root = root
        if self._empty_content(root):
            try:
                self._request_accessibility(root)
                self._refresh_window()
                root = read(self.window, outline=not full)
                self.last_root = root
            except (OSError, RuntimeError):
                # Preserve the original structure for diagnostics if the
                # application's provider cannot be activated.
                pass
        if full:
            if self._needs_activation(root):
                try:
                    self._activate_provider(dry_run=True)
                except NativeError:
                    pass
            return root
        if self._needs_activation(root) and not self.activation_attempted:
            self.activation_attempted = True
            self.stage = "兼容激活控件接口"
            self._activate_provider()
            self._request_accessibility(root)
            count, deadline = 0, time.monotonic() + 15
            for attempt in range(3):
                self._refresh_window()
                root = read(self.window, outline=True)
                self.last_root = root
                if not self._needs_activation(root):
                    break
                if attempt < 2:
                    time.sleep(.2)
        if self._empty_content(root):
            raise NativeError("兼容激活后仍未读到企业微信消息控件。请导出结构诊断；重启企业微信可恢复客户端状态。")
        self.stage = "确认标题栏群名"
        headings = self._group_headings(root, group)
        if not headings:
            raise NativeError(f"已连接企业微信，但没有从标题栏控件读到“{group}”。这是界面识别未完成，不能据此判断群名填错；请导出诊断。")
        self.stage = "读取可见历史消息"
        for table in history_tables(root):
            area = carrier(root, table)
            table.children = [read(row, in_row=True, force_row=True) for row in self._visible_rows(table, area)]
            if any(node.role in ROW_ROLES for node in table.children):
                return root
        raise NativeError("当前聊天记录面板没有可见的成员消息，请等待加载完成后重试。")

    @staticmethod
    def _scroll_limit(rows, direction, top, height):
        limit = max(1, height - 8)
        if rows:
            anchor = rows[min(1, len(rows) - 1)] if direction == "older" else rows[-min(2, len(rows))]
            _, y, _, row_height = anchor.rect
            available = top + height - y if direction == "older" else y + row_height - top
            limit = max(1, min(limit, available - 8))
        return int(limit)

    def _wheel_pulse(self, point, direction):
        import win32api
        import win32con
        if ctypes.windll.user32.GetForegroundWindow() != self.window.handle:
            raise NativeError("企业微信已离开前台，采集已暂停。回到目标群后继续。")
        win32api.SetCursorPos(point)
        delta = 120 if direction == "older" else -120
        win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, delta, 0)

    def _history_viewport(self, root):
        table = locate_history(root)
        area = carrier(root, table)
        x, y, width, height = area.rect
        tx, ty, tw, th = table.rect
        right, bottom = min(x + width, tx + tw), min(y + height, ty + th)
        x, y = max(x, tx), max(y, ty)
        width, height = right - x, bottom - y
        if width <= 0 or height <= 0:
            raise NativeError("聊天记录列表不可见。")
        return table, area, (x, y, width, height)

    def nudge(self, root, direction, *, stop=None):
        if stop is not None and stop.is_set():
            return
        _, _, (x, y, width, height) = self._history_viewport(root)
        self._wheel_pulse((int(x + width * .55), int(y + height * .55)), direction)

    def scroll(self, root: Node, direction: str, pixels: int, *, stop=None):
        if stop is not None and stop.is_set():
            return
        table = locate_history(root)
        area = carrier(root, table)
        x, y, width, height = area.rect
        tx, ty, tw, th = table.rect
        right, bottom = min(x + width, tx + tw), min(y + height, ty + th)
        x, y = max(x, tx), max(y, ty)
        width, height = right - x, bottom - y
        if width <= 0 or height <= 0:
            raise NativeError("聊天记录列表不可见。")
        rows = [row for row in table.children if row.role in ROW_ROLES and row.rect and
                (FLAT_ROW.fullmatch(row.text) or any(n.role == "Button" or n.control_name == "chatSender"
                                                   for n in row.walk()))]
        rows.sort(key=lambda row: row.rect[1])
        if not rows:
            raise NativeError("未能取得已读消息的位置，已暂停，避免滚动后失去衔接。")
        limit = self._scroll_limit(rows, direction, y, height)
        samples = rows[:3] if direction == "older" else rows[-3:]
        sign = -1 if direction == "older" else 1
        original = [(row, row.native, row.control_name, row.rect[1]) for row in samples]
        names = [row.control_name for row in rows]

        def displacement():
            values = []
            for row, wrapper, name, old_y in original:
                try:
                    if (wrapper.element_info.name or "") != name:
                        continue
                    rectangle = wrapper.rectangle()
                    if rectangle.width() > 0 and rectangle.height() > 0:
                        values.append(sign * (old_y - rectangle.top))
                except Exception:
                    continue
            if values:
                return max(values)
            # Virtualized lists may replace UIA objects while scrolling.
            # Reacquire only visible handles and match unique known names.
            for wrapper in self._visible_rows(table, area):
                try:
                    name = wrapper.element_info.name
                    if not name or names.count(name) != 1:
                        continue
                    for index, (row, _, old_name, old_y) in enumerate(original):
                        if name == old_name:
                            rectangle = wrapper.rectangle()
                            original[index] = (row, wrapper, name, old_y)
                            values.append(sign * (old_y - rectangle.top))
                            break
                except Exception:
                    continue
            return max(values) if values else None

        point = (int(x + width * .55), int(y + height * .55))
        self.stage = "按消息行实际位移滚动"
        self.scroll_report = f"scroll_requested={pixels} target={min(pixels, limit)}"
        result = advance_viewport(pixels, limit, lambda: self._wheel_pulse(point, direction),
                                  displacement, stop=stop)
        self.scroll_report = (f"scroll_requested={pixels} target={result.target} moved={result.distance} "
                              f"pulses={result.pulses} reason={result.reason}")
        if result.reason == "overshoot":
            # Recover the initial viewport rather than accept a gap. A
            # single reverse detent undoes the oversized first detent.
            self._wheel_pulse(point, "newer" if direction == "older" else "older")
            raise NativeError("客户端单次滚轮超过可保留的消息重叠范围，已尝试回退最后一步并暂停。请展开聊天记录窗口后继续。")
        if result.reason == "unmeasured":
            raise NativeError("未能确认滚动后的消息位置，已暂停保留记录。请回到已有消息边界后继续。")
        return result

    def position(self, root: Node):
        table = locate_history(root)
        area = carrier(root, table)
        for node in (area, table):
            try:
                interface = node.native.iface_scroll
                if interface.CurrentVerticallyScrollable:
                    return round(interface.CurrentVerticalScrollPercent / 100, 6)
            except Exception:
                pass
        return None

    def close(self):
        if self.com_initialized:
            ctypes.windll.ole32.CoUninitialize()


def make_adapter():
    if platform.system() == "Darwin":
        return MacAdapter()
    if platform.system() == "Windows":
        return WindowsAdapter()
    raise NativeError("当前原型只支持 macOS 和 Windows 桌面版企业微信。")


def diagnostic(root: Node, group: str = "", activation: str = "", *, hide_text=False) -> str:
    """A redacted structure dump for local version adaptation."""
    lines = [f"nodes={sum(1 for _ in root.walk())}"]
    if activation:
        lines.append(activation)
    def visit(node, depth=0):
        text = node.text
        if (hide_text and text not in {"", "企业微信", "WeCom", "WeWork", "查找聊天记录", "聊天记录", "查找聊天内容", "搜索聊天记录"}) or node.role in {"TextArea", "Edit", "Document"} or (node.control_name and text != node.control_name):
            text = f"<内容省略，{len(text)}字>"
        elif node.role not in {"Button", "Window", "Group", "Pane", "Table", "List"}:
            text = "<文本省略>" if text else ""
        details = []
        if node.role in ROW_ROLES and node.text:
            details.append(f"text_lines={len(node.text.splitlines())}")
            details.append(f"flat_header={bool(FLAT_ROW.fullmatch(node.text))}")
        if node.control_name in {"chatSender", "chatSendTime", "quote_message_sender", "chatMessageParent",
                                 "HorizontalLayoutUI", "VerticalLayoutUI", "compound_message_item_layout", "chat_content", "whole_item"}:
            details.append("field=" + node.control_name)
        if node.rect:
            details.append("rect=" + ",".join(str(round(value)) for value in node.rect))
        if group and node.text.strip() == group:
            details.append("target-name-match=true")
        info = getattr(node.native, "element_info", None)
        if info is not None:
            for key in ("class_name", "framework_id", "automation_id"):
                try:
                    value = getattr(info, key, "")
                    if value:
                        details.append(f"{key}={str(value)[:120]}")
                except Exception:
                    pass
        suffix = " [" + " ".join(details) + "]" if details else ""
        lines.append("  " * depth + node.role + " " + text[:80] + suffix)
        for child in node.children:
            visit(child, depth + 1)
    visit(root)
    return "\n".join(lines)
