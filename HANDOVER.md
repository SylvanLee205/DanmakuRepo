# 交接文档 · DanmakuRepo（AI 接手请先读这一份）

> 最后更新：**2026-11-09 10:40**（11-09 例行体检 + 跳过上传机制复核；状态快照见第 2 节）
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
| 私有仓库（含配置+状态库） | `github.com/SylvanLee205/DanmakuRepo-Full` —— 远端分支 **main**；本地 worktree `F:\DanmakuRepo-full` 在 **`full-backup`** 分支（`push_all.py` 生成完整副本快照后推 `full/main`） |
| **当前发行版** | **`v6.1.0`**（2026-11-04）—— 说明见 `docs/RELEASE-v6.1.0.md` |
| Python | 3.11，项目内 venv：`.venv\Scripts\python.exe` |
| 主程序入口 | `main.py`（`python main.py`） |
| 外部依赖 | ffmpeg / ffprobe、rclone、biliup、stream-gears、CD2 应用（WebDAV 网盘通道）、Server酱³ |

**发行版 tag 打在 `main`（不是 `master`）**：`main` 含 `configs/global.example.yml`
等示例配置，别人下载 Source code 就能直接当模板用。`master` 是生产分支，
不含任何示例配置。**v6.0.0 的 tag 当时打错了（打在 master），已作废不用。**

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

## 2. 当前状态快照（2026-11-09 12:21，优雅重启后）

| 项 | 状态 |
|---|---|
| DMR 主进程 | 运行中（PID 18536/20356，起于 **11-09 12:21 优雅重启**；旧进程连跑 6 天后退役） |
| 守护进程 | 运行中（`DMR_Boot` 计划任务，session 0 常驻） |
| 正在录制 | 重启选在无活跃录制的空窗期；启动对账补派了 1 个渲染任务（vvu 11-07 14:19） |
| 任务配置 | `configs/DMR-*.yml` 共 **43 个**（配置在 `.gitignore` 里，不入仓库）；停用归档在 `configs/temp/` |
| 状态库 | `logs/dmr_state.db`（159 产物 / 33 场次 / 128 上传组）；启动对账按 D4 规则级联清理了「已结束且全产物终态」的场次（386→159，**设计行为非误删**），载荷全部可解析 |
| 录制目录 | `直播回放/原视频/<主播>/`（源视频+ass）、`直播回放/弹幕版/<主播>/`（成品） |
| 磁盘 | F 盘 926G，用 121G（14%）—— 弹幕版永久留本地是硬性要求，增长属预期 |
| 日志 | 新进程写 `logs/DMR-20261109.log`；旧 `DMR-20261103(2).log` 留存至轮转；策略 14 天 / 500MB |
| 今日产出 | 弹幕版累计 174 个成品；近 24h 渲染失败 **0 次**、真实 QSV 降级 0 次 |
| 延迟清理队列 | 32 条，全部未到期（0 条过期）—— 队列机制本身健康 |

**11-09 体检修掉的问题**（详见第 5 节坑速查）：
1. `onTaskError` 失败不写库 → 产物永远卡 `rendering`（已修 + 5 个测试 + 反向验证）
2. `clean_sources.py` 只认老布局 → 静默变成"无文件可清"（已修，双布局兼容）
3. 同工具**无视 24h 队列** → 一次列出 66 个未上传文件（40.9GB）为"可删"（已加保护）
4. 同工具云端校验用截断前缀 → 会把"渲染失败从未上传"的判成"云端已有"（已改严格比对）
5. 状态库 2 条卡 `rendering` 的记录已按磁盘事实修正为 `recorded`
6. 状态库 53 条载荷 JSON 非法转义已修复
7. 清理 9 个真僵尸文件（2.91GB），删除前逐条核实成品**已上传网盘**
   （这 9 个是历史积压；**通用判据是「本地有弹幕版成品」**，命中跳过上传的
   分段云端永远查不到，源视频却本来就该删 —— 见第 5 节坑速查）

**11-09 12:21 优雅重启**：新代码已加载（启动日志 `流水线开关检查（共 43 个任务）`、
`恢复 19 个未完成产物，已重新派发 1 个任务`）。心跳实时任务数、
`onTaskError` 失败落库两处修复自此生效；启动横幅只出现 1 次，无崩溃循环。

**全链路实测证据（2026-11-09 03:41→03:44）**：
```
03:41  原视频/妲己崽崽/02点42分.ts 落盘（分段 4）
03:41  渲染派发（源 3541 秒 → 超时上限 36005 秒）
03:44  渲染完成 → 弹幕版/…/02点42分（弹幕版）.mp4
03:44  rclone 上传成功（~3 秒）
03:44  延迟清理落盘（24.0 小时后执行）
```


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
| `push_all.py` 说"公开=nochange" | 先别当漏推：`master` 与 `main` 是两条独立历史、hash 必然不同，nochange 只表示公开 worktree 的树与 master 无差异。核对用 `git ls-remote origin refs/heads/main` 比提交标题，再看公开 worktree `git status -sb` |
| **`push_all.py` 说"工作区有未提交改动"但 `git diff` 是空的** | 全是**未跟踪**的垃圾文件（`.DS_Store` 等）。`master_dirty()` 只看 `git status --porcelain` 有没有输出，`??` 也算脏。`git diff` 看不到未跟踪文件，所以极易误判。用 `git status --porcelain` 看 `??` 那几行 |
| `.DS_Store` 越清越多、清不完 | 从 Mac / Finder 浏览这个 SMB 共享**必然**在 Windows 盘生成。只能靠 `.gitignore` 挡住（已加 `.DS_Store` / `._*` / `Thumbs.db` / `desktop.ini`），不要指望手动删干净 |
| 心跳日志里的主播数跟 `configs/` 实际数量对不上 | `main.py` 心跳曾用启动时快照 `n_tasks`，热加载增删主播后不刷新（实测写 33、实际 35）。已改成每次现取 `dmr.engine.task_dict` |
| `requirements.txt` 显示"内容相同却算改动" | `core.autocrlf=true`（PortableGit 全局配置），工作区 CRLF / 索引 LF 来回抖。**已修**：`.gitattributes` 已补 `*.txt` / `*.json` / `*.toml` / `*.ini` 和 `.gitignore` / `.gitattributes` 自身 `text eol=lf`；`*.bat` / `*.cmd` / `*.ps1` 仍锁 `eol=crlf`，别把它们扫进去 |
| **双击「添加主播」报退出码 `-1073741819`，控制台一行输出都没有** | 这个码 = `0xC0000005` 访问冲突（段错误）。真因是 `tools/add_room.py` 读剪贴板时 **`GetClipboardData` 没声明 `restype`** → ctypes 默认按 `c_int`（32 位）处理 → 64 位 HANDLE 高位被**静默截断** → 残指针传给 `GlobalLock` → 崩。**不是**当初以为的"剪贴板被占用"（10-23 加固错了方向，所以 11-06 又崩一次）。修法：所有返回/接收句柄的 API 显式声明 `c_void_p`，字符串用 `ctypes.wstring_at` 手动读，别用 `c_wchar_p` 作 restype。已锁进 `unit_tests/test_add_room.py` |
| **任何 `ctypes` 调 Win32 API 崩得莫名其妙（无 traceback）** | 先怀疑**指针位宽**：不写 `restype` 就是 `c_int`=32 位，64 位下一切指针/句柄都会被截断。对照正确范式：`DMR/utils/instance_lock.py` 用 `wintypes.HANDLE`。这类崩溃发生在 C 层，Python 抓不到，所以**没有任何日志** —— 看到"双击后什么都没发生"就往这想 |
| PowerShell 脚本块 `{ }` 不执行 | 包在 `{}` 里只构造不执行，要平铺语句 |
| **`clean_sources.py` 报"没有可清理的文件"但磁盘明明堆着源视频** | 它按**老布局**扫目录（`直播回放/<主播>/` + `<主播>（弹幕版）/`），11-02 改成 `原视频/<主播>/` + `弹幕版/<主播>/` 后一个都对不上，整个工具静默退化。更狠的是它原来**无视 24h 延迟清理队列** —— 会把刚渲染完还没上传的也列成"可删"（实盘一次列了 66 个/40.9GB）。已在 11-09 修：双布局兼容 + 队列保护 + 崩溃时按"全部保护"处理 |
| **`clean_sources.py` 说"云端已有"就删了源视频** | 原实现 `n.startswith(stem[:len(stem)-6])` —— 截掉末 6 字把 `14点19分` 削成 `...11月07日`，当天任意一场录制都能蒙对。险情：vvu 11-07 14:19、君君 11-06 18:52 这两段**渲染失败从未上传**，被误判成"云端已有"。已改为严格同名比对（含 `(n)` 变体），并禁止再出现截断前缀 |
| **产物永远卡在 `rendering`，源视频永不清理，且重启后才"好转"一轮又复现** | `LiveEvents.onTaskError` 只改内存状态、**从不写库** → `_STATUS_TO_STATE['failed']` 形同虚设（全库 `state='failed'` 记录数长期为 0）。进程连续跑 6 天不重启，所以永远等不到 reconciler 兜底。已修：失败时 `_persist_failed()` 落库 FAILED + `last_error` + `attempts+1`。历史卡住的用 `tools/fix_stuck_artifacts.py --apply` 修 |
| 状态库 `artifacts.data` 解析报 `Invalid \escape` | 旧版拼路径时一条用 `\`、一条用 `\\`，JSON 里出现 `\v` 非法转义。`decode_payload` 有容错（按空载荷处理）**不会挡启动**，但那行记录的全部字段会被静默丢弃。修：`tools/fix_state_payload.py --apply`（实盘 53 条，全是终态，不影响生产） |
| **看到"云端没有这个成品"就判定清理出问题 / 把"已上传"当删除前提** | 有「跳过上传」机制：`render_args.dmrender.skip_upload_rule` = 横屏必选 + (码率≥2500kbps 或 帧率≥40fps) 任一命中 → **三条里命中两条就整组不上传**（B站+网盘都不传）。配置写 `skip_min_matches: 1` 是因为横屏被排除在计数外，别误读成"命中一条"。`global.yml` 原文：**跳过上传不影响本地文件 —— 原视频照常按 clean_args 删，弹幕版永久留本地（不上传也不删）**。所以云端查不到 ≠ 该留着源视频；反过来把云端当硬门槛会让这批源文件永久滞留。`tools/audit_cloud.py` 已正确排除命中的文件，可参考它的 `should_skip_upload()` |
| **磁盘越用越多，想缩短清理延迟 / 把弹幕版挪去冷存** | 弹幕版**永久留本地是用户硬性要求**（`tools/clean_confirmed_uploaded.py` 开头），缩短 `delay` 只影响原视频周转区，对增长无效。单调增长是预期行为。真风险是命中跳过上传的成品**只有本地一份、无云端备份** —— 该做异地备份，不是删本地。另：`clean_sources.py` 默认后悔期必须是 1440 分钟（24h），因为跳过上传的分段不产生上传事件、可能没进 `pending_clean.json`，队列防护兜不住 |

---

## 6. 待办 / 未验证（下一个可以接着做的）

> 2026-11-04 更新：原第 1 条（新布局未验证）**已划掉** —— 见第 2 节的实测证据链。

1. ~~**端到端未验证**：目录新布局（`原视频/` + `弹幕版/`）~~ **✅ 2026-11-04 已验证**：
   同一主播两个目录已成对（`原视频/SHU SDO温州店/` 12 文件 ↔ `弹幕版/SHU SDO温州店/` 7 成品），
   新录像落原视频、成品落弹幕版，上传按文件走、Cleaner 按 `（弹幕版）` 文件名判据，均无影响。
2. **`hevc_qsv` 本身不稳定**（与 10-20 Intel Arc 蓝屏 `0x113` 可能同源）。
   DMR 有"qsv 失败降级 libx265"兜底，但每次都要先炸一次。**下一步：更新 Intel Arc 驱动**
   （本机 `32.0.101.9150`，比官网 WHQL 还新；可试 DDU 干净卸载后装官方版）。
3. **Server酱³ 的 key 必须重置**：旧 key 曾硬编码在 `notify.py` 里并进了 git 历史
   （公开仓库可见）。代码已改成从 `.login_info/notify_key.txt` 读，但**历史里那份
   仍有效** —— 去 Server酱³ 后台重置，把新 key 写进那个文件（第一行 key、第二行 uid）。
   **这是唯一必须由用户本人完成的事项**（安全项）。⚠️ 2026-11-09 用户明确表态：
   "serve酱不急，那个几乎没有额度啥的，安全的很，我那小仓库肯定想让人看到都难" ——
   **按用户判断不急，别催**；但重置动作本身仍只能由用户本人在后台完成。
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
python tools\clean_sources.py [--apply]      :: ★ 清滞留源视频（默认预览；24h 队列保护 + 24h 后悔期）
python tools\fix_stuck_artifacts.py [--apply]:: ★ 修"卡在 rendering/uploading"的产物记录（按磁盘事实对账）
python tools\fix_state_payload.py [--apply]  :: ★ 修状态库载荷的非法 JSON 转义
python tools\restart_dmr.py --dry-run        :: 看重启将影响什么
python tools\audit_cloud.py                  :: 云端 vs 本地对账
python tools\build_index.py                  :: 重建录播索引页 index.html
python tools\push_all.py [--apply]           :: ★ 一条命令推两个仓库（默认预览）
python -m DMR.notify                         :: 实测通知链路
python -m unittest discover -s unit_tests -t .   :: 566 个测试
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

生产目录持续在涨（2026-11-09 实测 **F 盘用 123 GB / 14%**，原视频约 46G、
弹幕版约 76G），不能直接打包发。三种方式：

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
