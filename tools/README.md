# tools 目录索引

96 个脚本。**按用途分组**，不按字母序 —— 找东西时按"我要干什么"翻。

约定：**凡是会改文件/删文件的，默认都是预览模式**，加 `--apply` / `--force`
才真动手。不确定就先裸跑一次看它打算做什么。

---

## 一、日常必用（桌面快捷方式指向这里）

| 脚本 | 干什么 |
|---|---|
| `add_room.py` | 读剪贴板直播间链接 → 自动取名 → 写 `configs/DMR-<名字>.yml`，热加载不用重启 |
| `stop_dmr.py` | **优雅停止**：给 ffmpeg 发 `q` 让它写完当前段并改名，超时才硬杀 |
| `doctor.py` | 一键体检（12 项），`--export` 打诊断包 |
| `build_index.py` | 生成录播索引页 `index.html` |
| `watch_log.py` | 实时看日志 + 周期状态行（没有控制台窗口时用） |
| `post_boot_check.py` | 重启后自检：自动登录四要素、DMR 起在哪个会话 |
| `dmr-status.ps1` | 状态面板：进程、会话位置、窗口可见性 |

## 二、安装与环境

| 脚本 | 干什么 |
|---|---|
| `bootstrap.py` | 首次安装 / 环境修复（Setup.bat 调它）：venv、依赖、ffmpeg、rclone |
| `install_boot_task.py` | 注册/注销开机计划任务（运行时生成，不依赖 XML 模板） |
| `install_desktop_links.py` | 桌面建 .lnk 快捷方式 |
| `install_session_tasks.ps1` | 幂等重建 SessionWatch / Desktop 计划任务 |
| `check_env.py` | 依赖自检（`check_pypi`，main.py 启动时调） |
| `mem_probe_inproc.py` | 进程内内存诊断（设 `DMR_MEM_PROBE=1` 启用） |

## 三、启动链

| 脚本 | 干什么 |
|---|---|
| `autostart_guard.py` | 判断"是不是已经有实例在跑"，避免双开 |
| `autostart_launch.py` | 登录自启 + 跨会话迁移（窗口跟着人走） |
| `safe_restart_dmr.py` | 安全重启（先停再起，带确认） |
| `gen_restart_notify.py` | 启动成功后发通知（主播列表、CD2 状态、磁盘剩余） |

## 四、备份与发布

| 脚本 | 干什么 |
|---|---|
| `backup_configs.py` | 配置快照：snapshot / verify / list / restore |
| `backup_to_disk.py` | 整个项目异盘备份（自动挑盘） |
| `pack_cloud_backup.py` | 打包代码 + git 历史 + bundle，传网盘做异地副本 |
| `sync_full_backup.py` | 同步 configs/ + 状态库到私有完整副本 |
| `push_all.py` | 一条命令推两个仓库（公开脱敏 + 私有完全体） |

## 五、状态库与文件修复

| 脚本 | 干什么 |
|---|---|
| `rename_orphans.py` | 把 `[正在录制]` 孤儿登记进状态库，启动对账自动接管 |
| `purge_task_state.py` | 清停用任务的状态库记录（先备份 .db） |
| `purge_test_task_state.py` | 同上，测试用 |
| `fix_stuck_states.py` | 修卡在 rendering/uploading 的状态 |
| `clean_skipped_backlog.py` | 清"命中跳过上传规则却永不清理"的积压源视频 |
| `clean_sources.py` | 安全的源文件清理（完整防护：跳过录制中、要求有弹幕版…） |
| `clean_confirmed_uploaded.py` | 确认已上传后清理源文件 |
| `cleanup_stale.py` | 清理过期中间产物 |
| `clean_emoji_and_upload.py` | 清 emoji 文件名并补传 |
| `recover_part.py` | 恢复中断录制的 `.part` 文件 |
| `recover_ffmpeg_segments.py` | 从 ffmpeg 分段恢复 |
| `restore_dm_videos_from_cloud.py` | 从云端恢复弹幕版 |
| `retry_failed_uploads.py` | 重试失败的上传 |
| `stop_uploads_cooldown.py` | 解除上传冷却 |
| `rerender_failed.py` | 重跑失败清单里的渲染（`failed_renders.json`） |

## 六、渲染与编码器

| 脚本 | 干什么 |
|---|---|
| `isolate_render_failure.py` | **分层隔离**渲染失败原因（`-f null` / libx265 / 硬件编码） |
| `chunked_render.py` | 分段渲染（长视频拆开跑，避免单次超时） |
| `ab_test_engines.py` | 编码器 A/B 对比 |
| `cmp_encoder_quality.py` | 编码质量对比（SSIM/PSNR） |
| `compare_engines.py` | 引擎参数对比 |
| `test_render_encoder.py` | 手工验证：各编码器可用性 |
| `test_render_withsubs.py` | 手工验证：带字幕渲染 |
| `test_hwdecode.py` | 手工验证：硬件解码 |

## 七、云盘 / CloudDrive2

| 脚本 | 干什么 |
|---|---|
| `cd2_watchdog.py` | CD2 挂载盘守护（掉线自动重挂） |
| `cd2_queue_health.py` | CD2 上传队列健康度 |
| `cd2_stability_report.py` | CD2 稳定性报告 |
| `clean_cd2_queue.py` | 清 CD2 卡死的队列 |
| `audit_cloud.py` | 云端 vs 本地对账（哪些没传上去） |

## 八、代码与配置审计

| 脚本 | 干什么 |
|---|---|
| `check_imports.py` | 模块导入完整性 + 真实清理测试 |
| `check_undefined_names.py` | 未定义名字（AST + 运行时） |
| `audit_undefined_names.py` | 同上，另一套实现 |
| `check_critical_names.py` | 关键名字是否可获取（防渲染/上传静默失败） |
| `audit_configs.py` | 配置审计 |
| `audit_startup_chain.py` | 启动链审计（三层自启是否正确） |
| `audit_orphan_methods.py` | 找出"定义了但没人调用"的方法 |
| `full_audit.py` | 全面审计 |
| `health_check.py` / `selfcheck.py` | 快速健康检查 |
| `feature_audit.py` | 功能开关审计 |
| `regression_check.py` | 回归检查（跑一遍关键流程） |

## 九、日志与诊断

| 脚本 | 干什么 |
|---|---|
| `analyze_console_volume.py` | 分析日志噪音来源 |
| `explain_log_levels.py` | 解释各级别日志的含义 |
| `check_dmr_alive.py` | 进程是否活着 |
| `check_recordings.py` | 录制产物检查 |
| `check_danmaku_file.py` | `.ass` 文件检查 |
| `check_skip_rule.py` | 跳过上传规则验证 |
| `check_flv_overlap.py` | FLV 分段是否重叠 |
| `check_orphan_flv.py` | 孤儿 FLV 检查 |
| `compare_duplicates.py` | 重复文件 |
| `compare_orphan3.py` / `deep_name_scan.py` | 名字/孤儿深度扫描 |
| `trace_flv_rename.py` | 追踪 FLV 改名链路 |
| `trace_missing_render.py` | 追踪"没有弹幕版"的分段 |
| `diag_stuck_recording.py` | 录制卡住诊断 |
| `prescan.release.py` | 发布前扫描（脱敏检查） |
| `compare_cd2_versions.py` | CD2 版本对比 |
| `update_bat_dynamic_notify.py` | 批量更新 bat 里的通知逻辑 |

## 十、手工验证脚本（`test_*.py`）

这些**不是单元测试**（单元测试在 `unit_tests/`，用 unittest 跑）。
它们是"想看某个功能到底通不通"时手动执行的，会真的动文件/网络：

`test_baidu_upload` `test_clean_order` `test_danmaku_pairing` `test_dm_type`
`test_dual_upload` `test_emoji_cmd` `test_emoji_name` `test_pending_clean`
`test_rclone_upload` `test_skip_upload` `test_skip_upload_config`
`test_skip_upload_flow` `test_startup` `test_task_error_converge`
`test_truncation` `test_upload_decoupled` `test_upload_e2e` `test_watchdog`

---

## 2026-11-02 清理记录

删除了 **56 个一次性脚本**，都是"只为某次具体故障服务、且问题已修复"的：

- `diag_*`（9 个）：针对 10 月初的清理/分段/上游问题
- `diagnose_*`（3 个）：针对具体主播的损坏文件
- `fix_*`（4 个）：修完后逻辑已并入主代码
- `final_* / verify_*`（12 个）：验证某次修复完成，任务结束
- `rerender_* / upload_* / reupload_*`（9 个）：针对具体分段的补渲染/补传
- `switch_to_service_version` / `finish_service_switch` / `optimize_service` 等 CD2 切换期脚本（6 个）
- 各种 `rewrite_bat_ascii` / `simplify_bat` / `survey_configs` 等临时工具

需要时用 git 找回：

```bat
git log --diff-filter=D --name-only -- tools/ | findstr diag_
git show <commit>:tools/diag_xxx.py > tools/diag_xxx.py
```
