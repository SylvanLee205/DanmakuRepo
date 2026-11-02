# AGENTS.md · DanmakuRepo

> **给 AI 助手的入口文件**（Codex / Cursor / Claude Code / Cline / Windsurf 等
> 工具约定会自动读取仓库根目录的 `AGENTS.md`）。
> 人类维护者请看 [`HANDOVER.md`](HANDOVER.md)，那才是完整交接文档。

## 你接手的是什么

**DanmakuRepo（DMR）**：Windows 上的无人值守直播归档系统。
自动发现 B 站/抖音开播 → 录制 → 拉弹幕转 `.ass` → ffmpeg 渲染"弹幕版" →
推百度网盘 → 微信通知。7×24 跑着，33 个主播。

**生产目录**：`F:\DanmakuRepo`（本机）· Python 3.11 venv · 主入口 `main.py`

## 动手之前，按这个顺序读

1. **`HANDOVER.md`** —— 状态快照、架构地图、**12 条铁律**、坑速查表、待办、命令速查
2. **`docs/maintainer-log.md`** —— 完整历史：每处改动的原因与踩过的坑（末尾是最近的大改）
3. **`docs/project_state.md`** —— 设计取舍
4. **`tools/README.md`** —— 52 个工具脚本按用途分组（找东西按"我要干什么"翻）

读完后你必须能回答：*这项目在干什么 / 现在什么状态 / 哪些事不能做 / 出事查哪*。
答不上来就别改代码，先继续读。

## 绝对不要做的事

| ❌ 禁止 | 原因 |
|---|---|
| 删 `直播回放/弹幕版/**` 任何文件 | 成品，用户要求永久保留（判据：文件名含 `（弹幕版）`） |
| `rm` / `rmtree` 删录像、配置、状态库 | 不可恢复的数据丢失 |
| 把 `configs/`、`rclone.conf`、Server酱 key 提交进 git | 泄露（`.gitignore` 已排除，别绕过） |
| 改 `DMR/` 包名 / `DMR-*.log` 前缀 / `configs/DMR-*.yml` 前缀 | 缩写 DMR 是有意保留的，改包名要动 363 处 import |
| 在无人值守路径里用 `input()` | 会挂死（session 0 无控制台） |
| 用 `except: pass` 掩盖错误 | 会静默死（开播通知曾因此死两周） |
| 硬编码盘符或绝对路径 | 一律走 `DMR/paths.py` |
| 改 bat/cmd 不用 Edit 工具后不做 CRLF 校验 | 编辑器会把 CRLF 换成 LF → cmd 静默失败 |
| 绕过 `DMR/utils/toolsmgr.py` 直接找 ffmpeg/rclone | 本机工具不在 PATH 里，要读注册表 PATH |
| 提交 `logs/` `.temp/` `直播回放/` `.venv/` | 运行时产物，不入库 |

## 改代码的规矩

- 路径：`from .paths import p, CONFIGS, LOGS, TEMP, system32, powershell_exe`
- 外部工具：`from .utils.toolsmgr import find_tool` → `find_tool('ffmpeg')`
- 写文件（配置/状态）：**原子写**（temp + `os.replace`）
- 异常：一律 `logger.warning(...)`，不吞
- **`DMR/` 下代码不热加载** —— 改完必须重启：`python tools\restart_dmr.py`
  （桌面「DMR-重启」）。别手打 Stop+Start，会留下孤儿分段。

## 验证

```bat
python -m unittest discover -s unit_tests -t .   :: 514 个测试
python tools\check_imports.py                     :: 模块导入完整性
python tools\check_undefined_names.py             :: 未定义名字
python tools\doctor.py                            :: 体检（--export 出诊断包）
```

## 当前状态（2026-11-03 01:40 快照，细节看 HANDOVER.md 第 2 节）

- DMR + 守护进程运行中；**TG家里蹲、掉了颗兔牙正在录制**
- 任务配置 33 个；状态库 `logs/dmr_state.db`
- 录制目录：`直播回放/原视频/<主播>/` + `直播回放/弹幕版/<主播>/`
- 计划任务：`DMR_Boot` / `DMR_SessionWatch` / `DMR_Desktop` / `DMR_云端上传审计` / `DMR_配置快照`

## 未验证 / 待办（别当成已完成）

1. 目录新布局（`原视频/` + `弹幕版/`）代码有测试兜底，但**尚未经一次真实开播端到端验证**
2. `hevc_qsv` 硬件编码不稳定（与 Intel Arc 蓝屏 0x113 可能同源）—— 建议更新显卡驱动
3. Server酱³ 的 key 曾在代码里硬编码并进了 git 历史 → **已外置到 `.login_info/notify_key.txt`，
   但旧 key 仍需去 Server酱³ 后台重置**（历史里的那份要作废）
4. Linux 移植未做（启动层是 bat/计划任务/COM），清单见 `docs/跨平台.md`
