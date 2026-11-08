# 群聊记录助手 · Win R4 还原版

本目录是根据 `GroupChatReader-WinR4.exe` 逆向还原的完整 Python 源码。界面、解析、滚动、合并、存储和 Windows 控件激活逻辑均按 R4 的 11 个 `groupreader` 模块逐函数还原，`app.py` 与 R4 的入口一致。

当前版本在 R4 基础上增加了两项功能：**交界歧义 AI 自动恢复并继续读取**，以及**固定接口的 AI 模型接入（只需填 API Key）**。消息识别、合并和存储规则没有改变。

## 还原校验

11 个模块最初与 R4 打包的 `.pyc` 逐代码对象一致。新增功能修改了 `deepseek`、`gui`、`native`、`worker` 并新增 `recovery.py`，因此这几个模块现在有意与 R4 不同；其余模块仍保持一致：

```
__init__                 OK
merge                    OK
model                    OK
parser                   OK
scrolling                OK
storage                  OK
windows_accessibility    OK
deepseek                 DIFF（固定接口、AI 恢复支持）
gui                      DIFF（AI 接入页、自动恢复开关）
native                   DIFF（nudge 微调）
worker                   DIFF（恢复控制流程）
recovery                 新增
```

校验工具为 `tools/verify_r4.py`，需要 Python 3.13（R4 使用 3.13 字节码）。先把 R4 解包得到 PYZ 中的 `groupreader/*.pyc`，再运行：

```bash
set R4_PYC_DIR=<解包目录>\groupreader
python tools/verify_r4.py
```

`tools/disdiff.py` 可按函数查看 R4 与当前源码的反汇编差异，便于继续核对。

## 运行环境

- Windows 10/11 x64。
- 企业微信桌面版 **5.0.13.6005、32 位**（`wxwork.exe` 与 `duilib.dll` 的 SHA-256 必须与内置值一致）。
- 助手与企业微信使用**同一个 Windows 用户**、相同权限运行；不需要管理员权限。
- 源码运行需要 64 位 Python 3.11+，推荐 3.13。
- 依赖：PySide6-Essentials、certifi、pywinauto、pywin32。

## 快速开始（源码）

双击 `Start-Windows.bat`。脚本会创建 `.venv-win` 并安装依赖，然后启动 `app.py`。也可以手动：

```bat
py -3.13 -m venv .venv-win
.venv-win\Scripts\python -m pip install -e .
.venv-win\Scripts\python app.py
```

## 使用步骤

1. 登录并打开企业微信，进入目标群的「查找聊天记录 → 全部」。
2. 清空关键词、发送人和日期筛选，把列表停在开始读取的位置。采集期间不要操作鼠标。
3. 在「读取与预览」填写**企业微信中的完整群名**，选择时间参考日与读取方向。
4. 点「开始 / 继续读取」；或点「仅读取当前屏」只采集当前可见屏。
5. 程序逐屏读取控件树、解析消息、合并重叠屏、写入 SQLite 会话，并按消息行实际位移滚动。
6. 随时可点「暂停」。达到屏数上限、日期边界、连续多屏无变化或到达列表边界时停止。
7. 遇到交界歧义时，勾选“交界歧义时自动调整并继续”后，程序会先反向微调或缩小步长再继续；无法自动解决时才保留现场并暂停。
8. 「导出 Markdown…」导出当前筛选结果；「恢复本地会话…」载入之前保存的 SQLite 会话。

「导出界面结构诊断…」只导出控件结构、连接阶段与错误信息，消息正文会省略。

## Windows 兼容激活

R4 的读取依赖 DuiLib 的 `OnGetObject` 入口。程序启动采集时会：

1. 按版本号与 SHA-256 核对 `wxwork.exe`、`duilib.dll`。
2. 从 `WeWorkWindow` 的 `GWLP_USERDATA` 取得 32 位控件对象，在虚表中定位 `OnGetObject`。
3. 核对消息处理函数、代码洞和 3 个开关字节的原始状态。
4. 在界面线程挂起期间写入跳板与开关字节，并刷新指令缓存；退出时不做常驻修改以外的操作。
5. 任一预检不匹配即中止；写入中途失败会回滚已修改项。若权限无法恢复，会提示彻底退出并重启企业微信。

该逻辑只适用于上述已核对版本。企业微信升级后请勿使用，需重新核对偏移、哈希与机器码。

## AI 模型接入

「AI 模型接入」页固定使用 `https://api.deepseek.com` 与 `deepseek-flash`，只需填写 API Key；Key 只保存在本次运行内存中。主动点击“分析所选消息 / 分析当前筛选结果”并确认发送范围后，才会把所选聊天文字发送到接口。导出的 Markdown 不调用模型。

## 交界歧义 AI 自动恢复

勾选「读取与预览」中的“交界歧义时自动调整并继续”（默认开启）后，读取遇到以下可恢复情况时不会直接结束：

- 交界处存在连续相同消息，无法确认条数；
- 两屏没有重叠，或当前屏没有识别到成员消息；
- 界面结构短暂不可读、滚动位移无法核对、界面未再移动。

本地规则会先尝试反向微调、缩小步长、重读或等待。填写 API Key 后，由模型根据故障类型、读取方向、当前步长、滚动位置、可见行数等**状态信息**选择恢复动作，然后程序继续读取剩余消息；消息识别、去重、合并和存储仍走原有规则。

发送给模型的内容不含聊天正文、发送者、时间或群名。每次恢复最多执行 12 个动作；仍无法解决时会保留现场并显示原来的暂停提示。未填写 API Key 时只使用本地规则。

## 本地数据

会话数据库和设置位于 Qt 的 `AppLocalDataLocation`，Windows 通常为：

```
%LOCALAPPDATA%\LocalTools\GroupChatReader\
```

源码、构建产物和本仓库不包含真实聊天、会话数据库或密钥。

## 构建 Windows 单文件目录

double-click `Build-Windows.bat`（等价于 `Start-Windows.bat --build`），产物为：

```
dist\GroupChatReader-WinR4\GroupChatReader-WinR4.exe
```

分发时需保留整个 `GroupChatReader-WinR4` 文件夹。

## 目录

- `app.py`：入口。
- `groupreader/gui.py`：PySide6 界面。
- `groupreader/native.py`：Windows/macOS 平台适配与滚动。
- `groupreader/windows_accessibility.py`：版本核对、控件定位与内存激活。
- `groupreader/parser.py`：控件树解析。
- `groupreader/merge.py`：重叠屏衔接。
- `groupreader/scrolling.py`：滚动位置与位移计算。
- `groupreader/storage.py`：SQLite 会话。
- `groupreader/deepseek.py`：固定接口的模型客户端与分析。
- `groupreader/recovery.py`：交界歧义的有界恢复动作（本地规则或模型选择）。
- `groupreader/worker.py`：采集循环与自动续读。
- `tools/`：R4 字节码校验工具。

## 许可

Windows 控件激活思路改编自 MIT 项目 [wecom-automation](https://github.com/nfsqking/wecom-automation) 的 `dfc1a7157ef9962c987b541c286010a60e968083`，许可全文见 `groupreader/licenses/wecom-automation-MIT.txt`。
