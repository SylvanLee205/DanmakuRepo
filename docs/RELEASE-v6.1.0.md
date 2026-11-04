# v6.1.0 —— 无人值守稳定性大修 + 全链路可观测

> 发布日期：2026-11-04
> 基线：v5.1.0（2026-10-15）→ 本版 31 个提交、226 个文件变更
> 适用范围：Windows 无人值守长期运行。配置**向后兼容**，不需要改 `configs/`。

---

## 一句话

把"能录"做到"**出问题你自己能查出来**"：目录结构分家、崩溃自愈、一键重启、
状态库对账、AI 交接文档。顺带修掉 10 多个会让流水线静默停摆的真实缺陷。

---

## 重点一：录制目录分家（最容易"看着累"的地方）

以前源视频和成品混在一个目录里。现在分开：

```
直播回放/
├── 原视频/<主播>/    ← 源 .ts + 弹幕 .ass（24 小时后会被清理）
└── 弹幕版/<主播>/    ← 渲染成品 .mp4（永不删除）
```

- 老配置**不用改**：`DMR/paths.py` 的 `danmaku_dir_for()` 会自动把老路径归位。
- 批量迁移：`python tools/migrate_layout.py --apply`（默认预览，自动备份状态库）。
- **上传、清理都不受影响**：上传按文件走，清理按 `（弹幕版）` 文件名判据。

## 重点二：崩溃能自愈，不用守着

| 能力 | 说明 |
|---|---|
| 开机自启 | `DMR_Boot` 计划任务在 session 0 常驻守护，无人登录也兜底 |
| 跨会话迁移 | RDP / UU / RustDesk 连进来时自动把 DMR 搬到你当前桌面 |
| **一键重启** | `Restart_DMR.bat`：停守护 → 优雅停 → **清残留标志** → 拉起 → 轮询验证 |
| 优雅停止 | ffmpeg 收到 `q`，写完当前段并改名，不留半截文件 |
| 孤儿分段接管 | 进程被杀留下的 `[正在录制]xxx.ts`，重启后自动改名 + 渲染 + 上传 |
| 启动对账 | reconciler 扫描目录里的孤儿源视频，自动补派渲染 |

> 一键重启里"清残留标志"这一步是必须的：`graceful_stop_*` 标志不清理，
> 新实例启动即退出，守护会把它反复重拉成崩溃循环。

## 重点三：修掉一批"静默死"

这些缺陷的共同点是**不报错、只是不动了**，最难查：

- **开播通知死了两周**：`_notify_last` 未初始化 → `AttributeError` → 被裸
  `except: pass` 吞掉。现在全项目禁止裸 `except: pass`，一律 `logger.warning`。
- **通知时好时坏**：本机跑 TUN 代理，Windows 自带 curl（Schannel）跟
  `push.ft07.com` TLS 握手失败。改用 `requests`（OpenSSL）+ **双端点自动回退**
  （`sctapi.ftqq.com` ↔ `push.ft07.com`）。
- **DMR 突然起不来**：`autostart_guard.py` 用裸 `powershell` 列进程，
  venv 进程 PATH 里没有 System32 → 保守跳过启动。改 `psutil`。
- **渲染失败记录永远清不掉**：11 条 `rendering` 状态记录，源视频全不存在，
  `load_failed_tasks()` 无条件加载。现在加载时校验 `video.path` 存在性，
  不存在即丢弃并落盘。
- **两处非原子写**（`maintenance._save_state`、webapi 配置保存）：写一半崩溃
  就是坏配置。改成 temp + `os.replace`。
- **维护 tick 静默失败**：debug 级看不到，升级为 warning。

## 重点四：弹幕字符导致整场渲染失败（两个独立真凶）

`libass` 的 `imgutils.c:351` 断言，看起来是同一条错，其实是**两个不同原因**：

1. **无字形字符**：注音 `ㄓ` U+3103 等，系统里没有任何字体覆盖它。
2. **彩色 emoji 字体**：国旗指示符 `U+1F1E8`、🐈🎂 等回退到 SegoeUIEmoji
   （COLR/CBDT 彩色位图字体），libass 对彩色位图 stride 有缺陷。

由 `DMR/Downloader/Danmaku/glyphs.py` 在写 `.ass` **之前**清洗。
判据是 **`color - primary`**：主字体有字形就不会回退。
（`color` 太宽会连 ASCII 一起删；`color-plain` 太窄会漏。）

> **排查这类崩溃要分层验证**：`-f null`（不编码）跑通 = 字幕层没问题；
> 换 libx265 跑通 = 软件编码没问题；两者都通只有 `hevc_qsv` 崩 = 编码器问题。
> 看到同一条断言**不要认定是同一个原因**。

## 重点五：可移植性 —— 放哪个盘都能跑

- **路径只认 `DMR/paths.py`**：`ROOT/TOOLS/CONFIGS/LOGS/TEMP/BACKUPS` +
  `p()` / `system32()` / `powershell_exe()`。新代码不许写盘符。
- **外部工具只认 `DMR/utils/toolsmgr.py`**：
  `find_tool('ffmpeg'|'rclone'|'biliup')`。查找顺序：环境变量 → 项目 `tools/` →
  PATH → **注册表 PATH** → 常见安装目录。本机工具装在 `C:\User Program Files\`
  不在进程 PATH 里，必须读注册表才找得到。不校验版本号、零 `input()`。
- **bat 必须 CRLF + 纯 ASCII**，用 `%DMR_PY%`（`call "%~dp0_env.bat"`）。
  LF 的 bat 带 `goto` 时 cmd **静默失败**。
- `python tools/bootstrap.py` 可自动装环境；`Setup.bat` 双击即可。

## 重点六：文档体系（给下一个接手的人，也给自己半年后）

- **`HANDOVER.md`** —— AI/人接手的唯一入口：状态快照、架构地图、**12 条铁律**、
  坑速查表、待办、命令速查、禁止事项。README 顶部指向它。
- **`AGENTS.md`** —— AI 通用入口（不同工具的约定）。
- **`docs/maintainer-log.md`** —— 完整历史：每个功能为什么这么做、踩过什么坑、
  验证证据是什么。**这是这个项目最有价值的文件**。
- **`tools/make_handover_bundle.py`** —— 生成瘦身交接包（脱敏 + 排除录播/venv/exe，
  自检无明文 key）。
- `docs/archive/` 归档了 10 份已过时的历史文档，带索引说明为什么归档。

## 重点七：安全

- **Server酱 key 从代码里外置**（曾硬编码在 `notify.py`，且公开仓库可见）。
  现在按优先级读：`.login_info/notify_key.txt` → 环境变量 `DMR_SCT_KEY` →
  `configs/global.yml` 的 `notify.sct_key`。
- 删除了同样带 key 的一次性脚本 `tools/update_bat_dynamic_notify.py`。
- `doctor.py` 的通知自检改读实际端点，不再硬编码 host。

> ⚠️ **历史里那份旧 key 仍然有效**。代码已外置，但 git 历史抹不掉。
> 请去 Server酱³ 后台**重置一次**，把新 key 写进 `.login_info/notify_key.txt`
> （第一行 key、第二行 uid）。这是本版唯一需要用户本人处理的事项。

---

## 新增工具

| 工具 | 用途 |
|---|---|
| `tools/bootstrap.py` | 自动装环境（新机零依赖引导） |
| `tools/doctor.py` | 一键体检，出问题先跑它（`--export` 出诊断包） |
| `tools/restart_dmr.py` | 一键重启（优雅停 → 清标志 → 拉起 → 验证） |
| `tools/migrate_layout.py` | 目录新布局迁移（默认预览） |
| `tools/build_index.py` | 重建录播索引页 |
| `tools/audit_cloud.py` | 云端 vs 本地对账（抓 WebDAV 假成功） |
| `tools/purge_task_state.py` | 停用主播后清状态库（默认预览） |
| `tools/rename_orphans.py` | 孤儿 `[正在录制]` 分段登记 |
| `tools/install_desktop_links.py` | 重建桌面快捷方式（真 .lnk + 自绘图标） |
| `tools/install_boot_task.py` | 安装开机任务（运行时生成，不用 XML 模板） |
| `tools/make_handover_bundle.py` | 生成瘦身交接包 |
| `tools/push_all.py` | 一条命令推两个仓库（公开脱敏 + 私有完全体） |

## 新增桌面入口

启动 / **重启** / 停止 / 看实时日志 / 添加主播 / 重启后检查 / 体检 —— 7 个真 `.lnk`。

## 升级注意

1. **老配置不用改**。`configs/DMR-*.yml` 里的 `output_dir` 会自动归位到新布局。
2. 想立即把已有录播迁到新布局：`python tools/migrate_layout.py --apply`
   （默认预览，会自动备份状态库）。
3. **改了 `DMR/` 下代码必须重启**（没有热加载）：双击桌面「DMR-重启」。
4. **停用主播的正确姿势是两步**：① 配置挪进 `configs/temp/` ② 跑
   `python tools/purge_task_state.py --apply`。只挪配置会崩溃循环
   （状态库里还有该任务的会话 → 启动一致性检查拒绝启动）。
5. Server酱 旧 key 重置（见上文「安全」）。

## 已知问题

- **`hevc_qsv` 偶发不稳定**（与 2026-10-20 Intel Arc 蓝屏 `0x113` 可能同源）。
  DMR 有"qsv 失败降级 libx265"兜底，但每次要先炸一次。
  下一步：更新 Intel Arc 驱动（本机 `32.0.101.9150`，可试 DDU 干净卸载后装官方版）。
- **Windows 更新自动重启**是蓝屏链条的一环（更新 → 重启 → 18 分钟后驱动炸）。
  建议改「不自动重启」。
- Linux 移植暂缓：障碍全在启动层（bat / 计划任务 / COM），依赖层有 manylinux 轮子。

---

## 质量

- **514 个单元测试**，全绿（5 个 expected failure）。
- 实盘验证（2026-11-04）：

  ```
  21:04  原视频落盘 801MB + .ass
  22:07  渲染完成（hevc_qsv，1 小时素材渲 3 分钟）
  22:15  上传成功 → 24h 延迟清理队列
  ```

- 实盘规模：35 个主播同时监控，连续运行数月。

## 完整历史

每个功能为什么这么做、踩过什么坑、怎么验证的，都在
[`docs/maintainer-log.md`](maintainer-log.md)。建议接手前读一遍，
能省掉大量重复踩坑。
