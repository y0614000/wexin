"""Enable the UIA provider of the verified WeCom 5.0.13.6005 x86 build.

Provider/vtable resolution is adapted from nfsqking/wecom-automation,
commit dfc1a7157ef9962c987b541c286010a60e968083 (MIT).
See licenses/wecom-automation-MIT.txt.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import ctypes
from ctypes import wintypes
import hashlib
from pathlib import Path, PureWindowsPath
import struct


VERSION = "5.0.13.6005"
EXE_SHA256 = "9f7dcb2a085c9a379adbc9a3375d6d314f6cf101f2517b211f1902a261c9f242"
DLL_SHA256 = "13a1b6ea220d22bf442e1aca28f7d51e5297d11f370b84a7f60832896780e281"
TEXT_RVA = 0x1000
TEXT_END = 0xB241AC8
TEXT_FILE_OFFSET = 0x600
ONGET_RVA = 0xEFBA0
FLAG_RVAS = (0x47827C, 0x47827D, 0x47827E)
CAVE_RVA = 0x44515FC
CAVE_SIZE = 84
MARKER = b"GCRUIA01"


class ActivationError(RuntimeError):
    pass


class PermissionRestoreError(ActivationError):
    pass


@dataclass(frozen=True)
class Patch:
    address: int
    before: bytes
    after: bytes
    executable: bool = False


def displaced_prologue(code: bytes) -> bytes:
    if len(code) < 5:
        raise ActivationError("消息处理函数指令不完整。")
    offset = 2 if code.startswith((b"\x66\x90", b"\x8b\xff")) else 0
    if code[offset:offset + 3] != b"\x55\x8b\xec":
        raise ActivationError("当前窗口的消息处理函数结构不匹配，已停止兼容激活。")
    offset += 3
    while offset < 5:
        if code[offset] in range(0x50, 0x58):
            offset += 1
        elif code[offset] == 0x6A:
            offset += 2
        elif code[offset:offset + 2] == b"\x83\xec":
            offset += 3
        elif code[offset:offset + 2] == b"\x81\xec":
            offset += 6
        else:
            raise ActivationError("消息处理函数的开头指令尚未适配，已停止兼容激活。")
    if offset > len(code):
        raise ActivationError("消息处理函数指令不完整。")
    return code[:offset]


def build_stub(entry: int, address: int, slot: int, prologue: bytes) -> bytes:
    if not 0 <= slot < 20:
        raise ActivationError("无障碍接口虚表位置不匹配。")
    code = bytearray(bytes.fromhex("837c24043d"))
    first_jump = len(code)
    code += b"\x75\x00"
    code += bytes.fromhex("837c240cfc")
    second_jump = len(code)
    code += b"\x75\x00"
    # Save this (ECX), reserve bHandled, then pass the four OnGetObject args.
    code += bytes.fromhex("5183ec04c60424008d042450")
    code += bytes.fromhex("ff742418ff742418ff7424188b01")
    code += bytes([0xFF, 0x50, slot * 4])
    code += bytes.fromhex("803c2400")
    handled_jump = len(code)
    code += b"\x75\x00"
    code += bytes.fromhex("83c40459")
    fallback = len(code)
    code += prologue
    code += b"\xe9" + struct.pack("<i", entry + len(prologue) - (address + len(code) + 5))
    handled = len(code)
    code += bytes.fromhex("83c40459c20c00")
    for location, destination in ((first_jump, fallback), (second_jump, fallback), (handled_jump, handled)):
        code[location + 1] = destination - location - 2
    return bytes(code)


def apply_patches(remote, patches: list[Patch]):
    # Check all preimages before the first write. The caller holds the UI
    # thread and a per-process mutex for the complete commit/rollback.
    for patch in patches:
        if remote.read(patch.address, len(patch.before)) != patch.before:
            raise ActivationError("目标进程的状态已变化，兼容激活已停止。")
    applied = []
    try:
        for patch in patches:
            applied.append(patch)
            remote.replace(patch.address, patch.after, patch.executable)
    except Exception as error:
        failures = []
        for patch in reversed(applied):
            try:
                remote.replace(patch.address, patch.before, patch.executable)
            except Exception:
                failures.append(patch.address)
        if failures or isinstance(error, PermissionRestoreError):
            raise ActivationError("兼容激活未能完整恢复，请重启企业微信后再试。") from error
        raise ActivationError("兼容激活失败，此次内存修改已撤回。") from error


class RemoteProcess:
    def __init__(self, pid: int):
        import win32api
        self.handle = win32api.OpenProcess(1080, False, pid)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "ReadProcessMemory": ([wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                   ctypes.POINTER(ctypes.c_size_t)], wintypes.BOOL),
            "WriteProcessMemory": ([wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                    ctypes.POINTER(ctypes.c_size_t)], wintypes.BOOL),
            "VirtualProtectEx": ([wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD,
                                  ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            "FlushInstructionCache": ([wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result

    def close(self):
        self.handle.Close()

    def read(self, address: int, size: int) -> bytes:
        value = ctypes.create_string_buffer(size)
        count = ctypes.c_size_t()
        if not self.kernel.ReadProcessMemory(
                int(self.handle), address, value, size, ctypes.byref(count)) or count.value != size:
            raise ActivationError("无法核对企业微信运行状态，请确认两者使用相同用户和权限。")
        return value.raw

    def read_u32(self, address: int) -> int:
        return struct.unpack("<I", self.read(address, 4))[0]

    def replace(self, address: int, value: bytes, executable: bool):
        old = wintypes.DWORD()
        if executable and not self.kernel.VirtualProtectEx(
                int(self.handle), address, len(value), 64, ctypes.byref(old)):
            raise ActivationError("无法临时调整控件接口的内存权限。")
        try:
            count = ctypes.c_size_t()
            if not self.kernel.WriteProcessMemory(
                    int(self.handle), address, value, len(value), ctypes.byref(count)) or count.value != len(value):
                raise ActivationError("控件接口的内存更新未完成。")
            if self.read(address, len(value)) != value:
                raise ActivationError("控件接口更新后的校验失败。")
            if executable and not self.kernel.FlushInstructionCache(int(self.handle), address, len(value)):
                raise ActivationError("控件接口的指令缓存刷新失败。")
        finally:
            if executable:
                ignored = wintypes.DWORD()
                if not self.kernel.VirtualProtectEx(
                        int(self.handle), address, len(value), old.value, ctypes.byref(ignored)):
                    raise PermissionRestoreError("控件接口的内存权限恢复失败，请重启企业微信。")


@contextmanager
def activation_lock(pid: int):
    import win32api
    import win32event
    mutex = win32event.CreateMutex(None, False, f"Local\\GroupChatReader.UIA.{pid}")
    acquired = False
    try:
        status = win32event.WaitForSingleObject(mutex, 5000)
        if status not in (0, 0x80):
            raise ActivationError("另一个助手正在激活此企业微信窗口，请稍后再试。")
        acquired = True
        yield
    finally:
        if acquired:
            win32event.ReleaseMutex(mutex)
        win32api.CloseHandle(mutex)


@contextmanager
def suspended_thread(tid: int):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenThread.restype = wintypes.HANDLE
    kernel.SuspendThread.argtypes = [wintypes.HANDLE]
    kernel.SuspendThread.restype = wintypes.DWORD
    kernel.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel.ResumeThread.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.Wow64GetThreadContext.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel.Wow64GetThreadContext.restype = wintypes.BOOL
    handle = kernel.OpenThread(0xA, False, tid)
    if not handle:
        raise ActivationError("无法取得企业微信界面线程，已停止兼容激活。")
    suspended = False
    try:
        if kernel.SuspendThread(handle) == 0xFFFFFFFF:
            raise ActivationError("无法暂时挂起企业微信界面线程。")
        suspended = True
        context = ctypes.create_string_buffer(716)
        struct.pack_into("<I", context, 0, 0x10001)  # WOW64_CONTEXT_CONTROL
        if not kernel.Wow64GetThreadContext(handle, context):
            raise ActivationError("无法核对企业微信界面线程的位置，已停止兼容激活。")
        yield struct.unpack_from("<I", context, 184)[0]
    finally:
        try:
            if suspended and kernel.ResumeThread(handle) == 0xFFFFFFFF:
                raise ActivationError("企业微信界面线程恢复失败，请重启企业微信。")
        finally:
            kernel.CloseHandle(handle)


def _verified_modules(remote):
    import win32process
    modules = {PureWindowsPath(path).name.casefold(): (int(handle), Path(path))
               for handle in win32process.EnumProcessModulesEx(int(remote.handle), 3)
               if (path := win32process.GetModuleFileNameEx(int(remote.handle), handle))}
    if not {"wxwork.exe", "duilib.dll"} <= modules.keys():
        raise ActivationError("未找到已适配的企业微信程序和 DuiLib 控件模块。")
    for name, expected in (("wxwork.exe", EXE_SHA256), ("duilib.dll", DLL_SHA256)):
        with modules[name][1].open("rb") as source:
            actual = hashlib.file_digest(source, "sha256").hexdigest()
        if actual != expected:
            raise ActivationError(f"当前 {name} 与已核对的 {VERSION} 版本不匹配，已停止兼容激活。")
    return modules


def _resolves_to(remote, address: int, target: int, base: int) -> bool:
    if address == target:
        return True
    if not base + TEXT_RVA <= address < base + TEXT_END:
        return False
    code = remote.read(address, 6)
    if code[:2] == b"\xff\x25":
        return remote.read_u32(struct.unpack("<I", code[2:6])[0]) == target
    if code[0] == 0xE9:
        return address + 5 + struct.unpack("<i", code[1:5])[0] == target
    return struct.unpack("<I", code[:4])[0] == target


def plan_activation(remote, hwnd: int) -> list[Patch]:
    modules = _verified_modules(remote)
    exe_base, exe_path = modules["wxwork.exe"]
    dll_base, _ = modules["duilib.dll"]
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    this = user.GetWindowLongPtrW(hwnd, -21) & 0xFFFFFFFF
    if not this:
        raise ActivationError("当前窗口未提供已适配的 32 位控件对象。")
    vtable = remote.read_u32(this)
    entries = struct.unpack("<20I", remote.read(vtable, 80))
    slots = [index for index, value in enumerate(entries)
             if _resolves_to(remote, value, dll_base + ONGET_RVA, exe_base)]
    if len(slots) != 1 or slots[0] < 5:
        raise ActivationError("当前窗口的控件接口虚表结构尚未适配。")
    slot = slots[0]
    entry = entries[slot - 5]
    if not exe_base + TEXT_RVA <= entry < exe_base + TEXT_END:
        raise ActivationError("窗口消息处理函数不在已核对的程序代码区内。")
    with exe_path.open("rb") as source:
        source.seek(TEXT_FILE_OFFSET + entry - exe_base - TEXT_RVA)
        prologue = displaced_prologue(source.read(16))
    address = exe_base + CAVE_RVA + len(MARKER)
    stub = build_stub(entry, address, slot, prologue)
    payload = MARKER + stub
    if len(payload) > CAVE_SIZE:
        raise ActivationError("当前接口兼容代码超过已核对的预留空间。")
    branch = b"\xe9" + struct.pack("<i", address - entry - 5) + b"\x90" * (len(prologue) - 5)
    original_entry = remote.read(entry, len(prologue))
    if original_entry == branch:
        if remote.read(exe_base + CAVE_RVA, len(payload)) != payload:
            raise ActivationError("已有控件接口修改与本工具不匹配。")
        patches = []
    else:
        if original_entry != prologue:
            raise ActivationError("窗口消息处理函数已有其他修改，已停止兼容激活。")
        original_cave = remote.read(exe_base + CAVE_RVA, len(payload))
        if original_cave != b"\xcc" * len(payload):
            raise ActivationError("已核对的控件接口预留空间正在使用。")
        patches = [Patch(exe_base + CAVE_RVA, original_cave, payload, True),
                   Patch(entry, prologue, branch, True)]
    flags = []
    for rva in FLAG_RVAS:
        old = remote.read(dll_base + rva, 1)
        if old not in (b"\x00", b"\x01"):
            raise ActivationError("控件接口开关状态与已核对的版本不符。")
        if old == b"\x00":
            flags.append(Patch(dll_base + rva, old, b"\x01"))
    return flags + patches


def activate(hwnd: int, *, dry_run: bool = False) -> str:
    import win32gui
    import win32process
    if win32gui.GetClassName(hwnd) != "WeWorkWindow":
        raise ActivationError("当前窗口不是已适配的企业微信主窗口。")
    tid, pid = win32process.GetWindowThreadProcessId(hwnd)
    with activation_lock(pid):
        remote = RemoteProcess(pid)
        try:
            patches = plan_activation(remote, hwnd)
            if patches and not dry_run:
                with suspended_thread(tid) as instruction:
                    if any(p.executable and p.address <= instruction < p.address + len(p.after) for p in patches):
                        raise ActivationError("企业微信正在使用待激活的控件入口，请稍后再试。")
                    apply_patches(remote, patches)
        finally:
            remote.close()
    if dry_run:
        return f"{VERSION} 控件接口结构已核验"
    return f"{VERSION} 控件接口兼容激活已完成"
