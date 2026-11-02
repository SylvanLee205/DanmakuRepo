# 交接文档 · DanmakuRepo（AI 接手请先读这一份）

> 最后更新：**2026-11-03 01:40**（写这份时主播「TG家里蹲」「掉了颗兔牙」正在录制中）
>
> 本文件是**给下一个接手者（尤其是 AI）**的入口。先读这一份，再按第 1 节指路读别的。
> 读完能回答"这项目在干什么、现在什么状态、哪些事不能做、出事该查哪" = 交接完成。

---

## 0. 一句话

**DanmakuRepo（DMR）**：把 B 站直播流录下来 → 拉弹幕转成 `.ass` 字幕 → 渲染成"弹幕版"视频 →
推送到百度网盘，同时微信推送到手机通知开播。Windows 上无人值守长期运行。

| 项 | 值 |
|---|---|
| 本机目录 | `F:\DanmakuRepo` |
| 公开仓库（代码） | `github.com/SylvanLee205/DanmakuRepo` —— 默认分支 **main** |
| 私有仓库（含配置+状态库） | `github.com/SylvanLee205/DanmakuRepo-Full` —— 分支 **main** |
| Python | 3.11，项目内 venv：`.venv\Scripts\python.exe` |
| 主程序入口 | `main.py`（`python main.py`） |
| 外部依赖 | ffmpeg / ffprobe、rclone、biliup、stream-gears、CD2 应用（WebDAV 网盘通道）、Server酱³ |

**克隆要看仓库**：公开仓库**不含** `configs/`（用户配置被 .gitignore 排除）、
不含 `.temp/` 和状态库。要能直接跑起来，克隆**私有仓库**（或公开仓库 + 自己填 `configs/`）。
公开仓库里有 `configs/global.example.yml` 可作模板。

---

## 1. 30 秒上手：按这个顺序读

1. **本文件**（你在这）
2. `docs/maintainer-log.md` —— **完整历史**：每个功能为什么这么做、踩过什么坑、验证证据。
   末尾"2026-11-02（下午～晚间）"是最近的大改（目录新布局 / CD2 出殡 / 通知重写 / 一键重启）。
3. `docs/project_state.md` —— 当前状态与设计取舍
4. `README.md` —— 怎么用、一键脚本清单、常见问题表

工具脚本索引在 `tools/README.md`（**52 个脚本**，按"我要干什么"分组，不按字母序）。

---

## 2. 当前状态快照（2026-11-03 01:40）

| 项 | 状态 |
|---|---|
| DMR 主进程 | 运行中（PID 会变，看 `logs/DMR-*.log`） |
| 守护进程 | 运行中（`DMR_Boot` 计划任务，session 0 常驻） |
| 正在录制 | **TG家里蹲**（01:07 起）、**掉了颗兔牙**（00:20 起）—— 交接时仍在写盘 |
| 任务配置 | `configs/DMR-*.yml` 共 **33 个**（配置在 `.gitignore` 里，不入仓库） |
| 状态库 | `logs/dmr_state.db`（SQLite，135 行产物：cleaned 70 / uploaded 24 / missing 20 / skipped 19 / recording 2） |
| 录制目录 | `直播回放/原视频/<主播>/`（源视频+ass）、`直播回放/弹幕版/<主播>/`（成品） |
| 磁盘 | F 盘 926G，用 15G（2%） |
| 日志 | `logs/DMR-<日期>.log`；轮转策略 14 天 / 500MB |

**计划任务**（全部已调通，别手改错）：

| 任务 | 作用 | 动作 |
|---|---|---|
| `DMR_Boot` | 开机兜底（session 0 常驻守护） | `F:\DanmakuRepo\Run_DMR.bat` |
| `DMR_SessionWatch` | 登录+解锁触发，跨会话迁移 DMR | `pythonw tools\autostart_launch.py` |
| `DMR_Desktop` | 手动把 DMR 搬到当前会话 | `pythonw tools\autostart_launch.py` |
| `DMR_云端上传审计` | 云端 vs 本地对账（抓 WebDAV 假成功） | `python tools\audit_cloud.py --log logs\cloud_audit.log` |
| `DMR_配置快照` | 配置快照（保留 20 份） | `python tools\backup_configs.py snapshot --keep 20` |

另有 Startup 目录的 `DMR_AutoUp.cmd`（登录自启，L1 层）—— **手工文件，不由脚本重建，换机要手放**。

---

## 3. 架构地图（每个模块一句话）

```
main.py
└── DMR/__init__.py        主控：配置热加载、60s 轮询维护、崩溃快照钩子
    ├── DMR/engine.py      任务引擎（按配置起停任务、调度上传/渲染/清理）
    ├── DMR/Config/        配置加载与热加载
    ├── DMR/Task/          liveevents：直播事件 → 下载 → 渲染 → 上传 → 清理 的编排
    ├── DMR/Downloader/    流下载器（stream-gears / yutto / virtual）
    │   └── Danmaku/       弹幕抓取 + glyphs.py（无字形/彩色 emoji 清洗）
    ├── DMR/Render/        ffmpeg 渲染（QSV 硬编，失败降级 libx265）
    ├── DMR/Uploader/      推送到 rclone → CD2(WebDAV) → 百度网盘
    ├── DMR/Cleaner/       上传后清理源视频（"弹幕版永不删"硬保护在这）
    ├── DMR/state/         SQLite 状态库 + 启动对账 reconciler（★核心资产）
    ├── DMR/WebService/    本地 Web 界面（配置管理、失败重试）
    ├── DMR/notify.py      Server酱³ 推送（requests + 双端点回退）
    ├── DMR/maintenance.py 日志轮转 / 磁盘水位 / 每日体检 / 索引页
    ├── DMR/crashdump.py   崩溃快照（三个钩子，traceback+线程栈+日志尾）
    └── DMR/paths.py       ★所有路径的唯一来源
```

**数据流**：直播开始 → 下载器落 `直播回放/原视频/<主播>/[正在录制]xxx.ts`
→ 弹幕转 `.ass` → ffmpeg 渲染 → 成品落 `直播回放/弹幕版/<主播>/xxx（弹幕版）.mp4`
→ rclone 上传 → Cleaner 删源视频 → 通知手机。

---

## 4. 铁律（违反会出事，改代码前必读）

| # | 铁律 | 违反后果 |
|---|---|---|
| 1 | **路径只认 `DMR/paths.py`**：`ROOT/TOOLS/CONFIGS/LOGS/TEMP/BACKUPS` + `p()` / `system32()` / `powershell_exe()` | 换机/换目录就跑不起来 |
| 2 | **外部工具只认 `DMR/utils/toolsmgr.py`**：`find_tool('ffmpeg'\|'rclone'\|'biliup')`。顺序：环境变量→项目 `tools/`→PATH→**注册表 PATH**→常见安装目录 | 本机工具装在 `C:\User Program Files\`，不在进程 PATH 里，只查 PATH 会找不到 |
| 3 | **改 `DMR/` 下代码必须重启**（无热加载）：双击桌面「DMR-重启」 | 改了不起效，误以为没改对 |
| 4 | **禁止裸 `except: pass`**：一律 `logger.warning(...)` | 会静默死（开播通知曾因此死两周） |
| 5 | **bat 必须 CRLF + 纯 ASCII**；用 `%DMR_PY%`（`call "%~dp0_env.bat"`），不许硬编码 `.venv\Scripts\python.exe` | LF 的 bat 带 goto 时 cmd **静默失败**；编辑器改 bat 会把 CRLF 换成 LF，改完必须用 python 换回来并验证 |
| 6 | **改配置/状态一律原子写**（temp + `os.replace`） | 写一半崩溃 → 坏配置 → 任务凭空消失 |
| 7 | **`subprocess` 调 `powershell`/`schtasks`/`curl.exe` 必须走 `powershell_exe()` / `system32()`** | venv 进程 PATH 里没有 System32 |
| 8 | **不要改包名 `DMR/`**：缩写不变是有意的（改包名要动 363 处 import，收益为零） | 大规模无意义改动 |
| 9 | **`configs/` 不入 git**：用户配置绝不入库（.gitignore 已排除） | 泄露/冲突 |
| 10 | **磁盘水位**：F 盘 <60G 删"已渲出弹幕版的最旧源视频"，<20G 暂停新录（不打断在录） | —— |
| 11 | **弹幕版成品永不删除**（判据：文件名含 `（弹幕版）` 后缀） | 不可恢复的数据丢失 |
| 12 | **`tools/__init__.py` 是 `from .check_env import *`** —— import 它有副作用，会跑环境检查。要用 tools 里的模块请用 importlib 按路径加载 | 意外执行环境检查 |

---

## 5. 坑速查（完整版在 `docs/maintainer-log.md`）

| 现象 | 真因 |
|---|---|
| 通知时好时坏 / 发不出去 | 这台机器跑 TUN 代理，Windows 自带 curl（Schannel）跟 `push.ft07.com` 握手失败。已改 requests + 双端点回退 |
| DMR 突然起不来 | `autostart_guard.py` 曾用裸 powershell 列进程 → PATH 没有 → 保守跳过启动。已改 psutil |
| 桌面图标显示成文件夹 | 用了 `shell32.dll,3`（那是文件夹图标）。已自绘 ico（`tools/make_icons.py`） |
| 改完 bat 不生效但没报错 | CRLF 被换成 LF，cmd 静默失败 |
| `boot_task.xml` 创建任务报"XML 格式错误" | PowerShell 变量名不区分大小写，`$new`/`$NEW` 互相覆盖 |
| 一键重启后崩溃循环 | 残留的 `graceful_stop_*` 标志没清 → 新实例启动即退出 → 守护反复重拉 |
| 整场渲染失败（`imgutils.c:351` 断言） | ① 无字形字符（注音 ㄓ 等）② 彩色 emoji 字体回退。已由 `glyphs.py` 清洗，判据是 `color - primary` |
| 同一条断言再次出现 | **不要认定是同一个原因**：用分层验证（`-f null` → libx265 → hevc_qsv）区分字幕层/软件编码/硬件编码 |
| 某主播一直重试渲染 | 只清 `failed_renders.json` 不够，reconciler 会补派目录里的孤儿源视频 —— 必须把源视频移出目录 |
| 停用主播后崩溃循环刷日志 | 状态库里还有该任务会话 → `_check_task_set_consistency` 拒绝启动。正确姿势：配置挪进 `configs/temp/` **且** 跑 `tools/purge_task_state.py --apply` |
| `push_all.py` 说"公开=nochange" | 不一定没推，用 `git status -sb` 看 ahead/behind |
| PowerShell 脚本块 `{ }` 不执行 | 包在 `{}` 里只构造不执行，要平铺语句 |

---

## 6. 待办 / 未验证（下一个可以接着做的）

1. **端到端未验证**：目录新布局（`原视频/` + `弹幕版/`）代码有测试兜底，但**还没被一次真实开播验证过**。
   下次有主播开播时确认：新录像落 `直播回放/原视频/<主播>/`，成品落 `直播回放/弹幕版/<主播>/`。
2. **`hevc_qsv` 本身不稳定**（与 10-20 Intel Arc 蓝屏 `0x113` 可能同源）。
   DMR 有"qsv 失败降级 libx265"兜底，但每次都要先炸一次。**下一步：更新 Intel Arc 驱动**
   （本机 `32.0.101.9150`，比官网 WHQL 还新；可试 DDU 干净卸载后装官方版）。
3. **Server酱³ 的 key 必须重置**：旧 key 曾硬编码在 `notify.py` 里并进了 git 历史
   （公开仓库可见）。代码已改成从 `.login_info/notify_key.txt` 读，但**历史里那份
   仍有效** —— 去 Server酱³ 后台重置，把新 key 写进那个文件（第一行 key、第二行 uid）。
4. **Windows 更新改成不自动重启**（蓝屏根因链条 = 更新→重启→18 分钟后显卡驱动炸）。
5. **Linux 移植**：用户暂缓。障碍全在启动层（bat/计划任务/COM），依赖层有 manylinux 轮子。
   清单见 `docs/跨平台.md`。**不建议仅为"更稳"重装** —— 实测 45 天仅 1 次蓝屏，且是 GPU 驱动。
6. `tools/resync_state.py` 未建（状态库把已归档产物标 `missing` 终态，若需重建索引可做）。

---

## 7. 命令速查

```bat
:: 启动 / 停止 / 重启（改完代码用重启）
Start_DMR.bat          :: 启动（守护循环，崩溃自动重拉）
Stop_DMR.bat           :: 优雅停止（ffmpeg 写完当前段并改名）
Restart_DMR.bat        :: ★ 一键重启：停守护→优雅停→清残留标志→拉起→验证
Doctor.bat             :: 体检（--export 出诊断包）
Check_After_Reboot.bat :: 重启后自检
Add_Room.bat           :: 加主播（读剪贴板，自动取名）
Watch_DMR_Log.bat      :: 看实时日志
```

```bat
:: 工具（都在 .venv\Scripts\python.exe 下跑）
python tools\purge_task_state.py --apply     :: 停用主播后清状态库（默认预览）
python tools\rename_orphans.py --apply       :: 孤儿 [正在录制] 分段登记（下次启动自动改名+渲染+上传）
python tools\restart_dmr.py --dry-run        :: 看重启将影响什么
python tools\audit_cloud.py                  :: 云端 vs 本地对账
python tools\build_index.py                  :: 重建录播索引页 index.html
python tools\push_all.py [--apply]           :: ★ 一条命令推两个仓库（默认预览）
python -m DMR.notify                         :: 实测通知链路
python -m unittest discover -s unit_tests -t .   :: 514 个测试
python tools\check_imports.py                :: 模块导入完整性
python tools\check_undefined_names.py        :: 未定义名字
```

**停止某个主播**：① 配置挪进 `configs/temp/`（DMR 不递归加载它）
② 跑 `tools/purge_task_state.py --apply`（自动备份 .db，只删状态库行，**不碰磁盘录像**）。

---

## 8. 外部依赖与账号

| 依赖 | 用途 | 备注 |
|---|---|---|
| ffmpeg / ffprobe | 录制、渲染、探测 | `C:\User Program Files\ffmpeg\`（不在 PATH，走 toolsmgr 找） |
| rclone | 上传 | `cd2:` = CD2 的 WebDAV（→百度网盘）、`123pan:` = 123云盘。配置在 `%APPDATA%\rclone\rclone.conf`（含密码，别提交） |
| CD2 (`clouddrive.exe`) | WebDAV 网盘通道 | **以 Windows 服务运行**。若它挂掉上传会失败（进 `failed_uploads.json`，不丢文件），修好后重试补传 |
| biliup | B 站投稿 | 可选 |
| stream-gears | B 站流下载 | 依赖 wheels，注意平台匹配 |
| Server酱³ | 微信通知 | **key 不在代码里**（2026-11-03 起）：`.login_info/notify_key.txt`（第一行 key、第二行 uid）或环境变量 `DMR_SCT_KEY` / `configs/global.yml` 的 `notify.sct_key`。该目录已 gitignore |
| 桌面快捷方式 8 个 | 启动/重启/停止/日志/加主播/重启后检查/体检/推送 | `tools\install_desktop_links.py` 幂等重建 |

> ⚠️ **Server酱 key 曾硬编码在 `notify.py` 里并进了 git 历史**（公开仓库可见）。
> 代码已改成读配置，但**旧 key 必须去 Server酱³ 后台重置**才作废。

---

## 8b. 想把项目"交给"别人 / 另一个 AI 时怎么做

生产目录有 **14 GB**（绝大部分是录播），不能直接打包发。三种方式：

| 方式 | 命令 | 说明 |
|---|---|---|
| **瘦身包（推荐）** | `python tools\make_handover_bundle.py` | 出 `F:\DanmakuRepo-交接-<时间戳>.zip`，约 **4.2 MB / 505 文件**，含完整 git 历史、全部文档、源码、工具脚本、脱敏后的任务配置。自动自检"包内无明文 key" |
| 直接给文件夹 | `python tools\make_handover_bundle.py --out-dir D:\handover` | 同上但解压成目录，本地给 AI 读更方便 |
| 只给仓库 | clone `github.com/SylvanLee205/DanmakuRepo` | 只有代码，**不含 configs/**（用户配置不入库）、不含状态库 |

**接手方的第一句话**（不管是哪个 AI）：

> 接手 DanmakuRepo 项目。先读仓库根的 `HANDOVER.md` 和 `AGENTS.md`，
> 再读 `docs/maintainer-log.md`。改代码前看完 HANDOVER.md 第 4 节的铁律
> 和第 5 节的坑速查表。

`AGENTS.md` 是给 AI 工具的入口约定（Codex / Cursor / Claude Code / Cline 等
会自动读仓库根的 AGENTS.md），`HANDOVER.md` 是给人看的完整交接文档。


---

## 9. 禁止事项（红线）

- ❌ 不要 `rm` / `rmtree` 删用户的录像、配置、状态库
- ❌ 不要把 `configs/`、`rclone.conf`、Server酱 key 提交进 git
- ❌ 不要改 `DMR/` 包名或日志前缀 `DMR-*.log`
- ❌ 不要在无人值守路径里用 `input()`（会挂死）
- ❌ 不要删 `直播回放/弹幕版/**` 下的任何文件
- ❌ 不要用 `except: pass` 掩盖错误
- ❌ 不要硬编码盘符/绝对路径（新代码一律走 `DMR/paths.py`）

---

## 10. 怎么把这次改动推上去

```bat
python tools\push_all.py            :: 预览（默认）
python tools\push_all.py --apply    :: 实推：公开 main + 私有完整副本快照
```

master 有未提交改动时它会拒绝推。网络不稳时 GitHub 推送可能被代理掐断（TLS eof），
**重试即可**；提交在本地是安全的。验证：`git status -sb` 看 ahead/behind。

---

*本文件由上一位维护者（AI 助手）在 2026-11-03 凌晨交接时写就。
后续维护请在 `docs/maintainer-log.md` 续写，并同步更新本文件的"当前状态快照"。*
