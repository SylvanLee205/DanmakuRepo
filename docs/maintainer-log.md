# 维护记录（已完成工作明细）

> **面向开发者与后续维护会话。**
> 相关文档：[更新说明](CHANGELOG.md) ·
> [项目当前状态](project_state.md) ·
> [待办清单](TODO.md) ·
> [测试说明](../unit_tests/README.md) ·
> [返回 README](../README.md)
>
> 本文件记录**已完成**工作的技术细节：根因、实现要点、验证证据、踩过的坑与取舍。
> 目的是让后续维护者不必重新推导一遍。

---

## 1. 状态持久化与启动对账（P1）

### 1.1 新增模块 `DMR/state/`

单一功能模块（未新建顶层目录）：

| 文件 | 职责 |
|---|---|
| `__init__.py` | 对外唯一入口 |
| `enums.py` | `ArtifactKind` / `ArtifactState` / `UploadGroupState` + 取值容错 |
| `schema.py` | DDL、模式版本（**v2**）、迁移框架 |
| `repository.py` | `StateRepository`：会话/产物/上传组读写，`RLock` 线程安全 |
| `payload.py` | 产物可变字段的 JSON 载荷编解码 |
| `keys.py` | 产物标识格式单一出处 |
| `reconciler.py` | 启动对账：磁盘对齐、中断处置、过期回收、收集待恢复 |
| `resume.py` | 由产物 + 场次记录重建 `VideoInfo` |

存储：`logs/dmr_state.db`（SQLite + WAL，5 张表：`schema_version`/`sessions`/
`artifacts`/`upload_groups`/`meta`）。

### 1.2 产物存储：「键列 + JSON 载荷」（模式 v2）

**动机**：产物字段会持续扩展（分辨率、编码信息、自定义元数据…）。每个字段建一列
意味着每次扩展都要改表 + 写迁移。

**判据**：会变或未来可能新增、且**不需要**进 SQL 的 `WHERE`/`ORDER BY` 的字段放 JSON。

| 列上（可查询/需约束） | `data` JSON（易变/会扩展） |
|---|---|
| `artifact_id`、`session_id`、`segment_idx`、`kind`、`state`、`updated_at` | `path`、`part_path`、`danmaku_path`、`size`、`duration`、`resolution`、`origin`、`attempts`、`last_error`、`video_info` |

反例：`state` 必须能 `WHERE state NOT IN (...)`，所以留列并建索引。

**迁移 v1 → v2**（`MIGRATIONS[2]`）：`ADD COLUMN data` → `json_object()` 搬迁九个老列
→ 逐个 `DROP COLUMN`。空库路径改为**直接按当前版本建表**，使新库与升级后的老库结构一致。

**容错**（坏数据不能让程序起不来）：

| 情况 | 处理 |
|---|---|
| `data` 为 NULL/空 | 空载荷，全字段走默认值 |
| 非法 JSON / 非对象 | 记 warning，按空载荷处理 |
| 载荷含本版本不认识的键 | **读取时过滤**（否则 `ArtifactRecord(**kwargs)` 抛 `TypeError`）；写入时剔除 |
| 载荷缺键 | 由 dataclass 默认值补齐 |
| `resolution` 字符串 | 读回时还原为 `(w, h)` 元组 |

### 1.3 产物语义（易搞反，已核实）

- `src_video_pre` = 转码**前**的原始录制流
- `src_video` = 转码**结果**，**替代**前者；不转码时它就是原始录制流
- `dm_video` = 渲染后的弹幕版
- `w_srcfile` = "清理弹幕版时连带清理源文件"；`w_srcpre` = "被替代的原始流可清理"
- **弹幕 `.ass` 不是独立产物**：它单独存在没有意义，记录在 `artifacts.danmaku_path` 附属字段

### 1.4 启动对账的分组

| 组 | 内容 |
|---|---|
| 第 0 步 | 净化运行中状态：`RECORDING`→`INTERRUPTED`，`RENDERING`/`UPLOADING`→`RECORDED` |
| A 组 | 磁盘校验：文件是否存在、中断产物处置、缺失标记 |
| C 组 | 上传组封存 |
| D 组 | 回收：`stale_after` 过期抛弃；**已结束且全部产物终态的场次 → 删除**（级联删产物） |

**D4 的推论**：`cleaned` / `uploaded` 都是**终态**，不阻止场次回收。所以"`cleaned` 记录
要不要删"的答案是**已经在自动删**——条件是①场次已结束 ②没有非终态产物。
若场次一直 `ended_at=None`（下播检测没走到），记录才会长期保留。

---

## 2. 恢复重驱动：重启后继续处理未完成的产物

**服务器实测现象**：重启后日志显示"已恢复为 recorded"，但文件**既没改名也没上传**。

**根因是两个独立缺口**：

1. `reconcile()` **只对账、不重跑** —— 它把状态修正到"与磁盘一致"，但不把产物送回处理链；
2. 文件仍是 `[正在录制]...Part000-...` **中间名** —— 分段式引擎只在"下一个分段开始时"
   才由 `segment_callback` 改名，进程在那之前退出就永远停在中间名上。

**隐性前提**：**重建不出 `VideoInfo`**。输出文件名与标题依赖录制当时的元信息
（主播/标题/时间），而库里原本只有路径/大小/时长。

**实现**：

- 产物载荷新增 `video_info`（存整个 `VideoInfo`）。它是 `cpdict`（dict 子类）可直接
  JSON 化，但 `datetime`(`ctime`) 与 `tuple`(`resolution`) 必须显式转换 ——
  **`ctime` 若读回 str，`{CTIME.YEAR}` 这类文件名模板会直接抛异常**。
- `rebuild_video_info()`：首选载荷；缺失或损坏时用**产物字段 + 场次记录**回退重建。
  `streamer` 必须还原为 `StreamerInfo` 而非 dict，否则下游属性访问失败。
- `RecoveryManager.collect_resumable()`：挑出 `RECORDED` + `RENDERED`，累加 `attempts`。
- `LiveEvents.resume_recovered()`：按**原场次**还原成 `state_dict` 分段（状态 `ready`）、
  补做中间名改名，然后交给**既有的** `_check_for_upload` 走同一条链路。

**为什么复用 `_check_for_upload` 而不另写流程**：两条路径（正常录制 vs 恢复）各写一份
渲染/上传/清理逻辑，行为迟早漂移。恢复只负责"把产物还原成正常状态"。

**安全性**：终态永不重跑；`INTERRUPTED` 不自动重跑；只处理当前加载的任务；
改名失败沿用原名继续上传（能上传比名字漂亮重要）；单条失败不连坐。

**已知取舍**：`uploading` 在重启时会被净化回 `recorded`（无法确定远端是否真的收下），
因此**崩溃前已完成的投稿可能被重新上传一次**。跨重启的精确去重做不到。

---

## 3. 恢复判定：不依赖 ffprobe（最终方案）

用户判断：时长探测失败说明**文件坏了、不用恢复**；且判定**不应依赖 ffprobe**。

**规则**（`is_recovered_file_usable`）：

1. 文件名以 `[正在录制]` 开头 —— 分段式引擎写出中间名，说明确实开始录了；已改名的同样正常
2. 文件大小 ≥ `recovery.min_valid_size_mb`（默认 **30MB**）—— 读一次 `stat` 即可

**上传长度门槛同步改**（`liveevents._passes_length_gate`，实时与非实时共用）：
时长已知按 `min_length`；时长**未知或为 0** 时改用文件大小判断 ——
否则恢复出的产物永远传不上去。`0` 与 `None` 同等看待（对已封闭文件，"时长 0 秒"
只意味着元信息没写上）。

**演进过程（保留教训）**：

| 轮次 | 做法 | 问题 |
|---|---|---|
| 第 1 轮 | 用 ffprobe 探测补齐时长 | 探测失败既不等于文件损坏，也让恢复依赖 ffprobe |
| 第 2 轮 | 探测不出就判"文件损坏"并 `skipped` | 仍依赖 ffprobe；没装 ffprobe 的机器上产物全被判死 |
| 最终 | 文件名标记 + 文件大小 | 确定的信号，无需解析容器 |

### ⚠️ 踩坑：哨兵值当返回值

`FFprobe.get_duration` 失败时返回 **`-1`**（不是异常、也不是 `None`）。
若把它当真实时长写进 `VideoInfo`，下游与 `min_length` 比较后判定"太短"，
**产物静默不上传**。凡是"用哨兵值表示失败"的接口，容错时必须显式排除。

### ⚠️ 踩坑：`title=None` 会被渲染成字符串 `"None"`

`replace_keywords` 用 `format_map`：模板里的键在 dict 中**存在**就取其值，
因此 `{'title': None}` 会渲染出 `korooonx-None.mkv`。恢复流程很容易遇到
"载荷里标题是 null"，所以重建时要把空文本字段归一化，并回落场次记录的标题。

---

## 4. 消息路由不再依赖 `replay/` 前缀（生产事故根因）

**现象**：B站稿件**已上传成功**，但状态库里大量产物停在 `recorded`/`uploading`，
`cleaned` 很少，清理不触发。

**根因**：上传器把消息来源原样当作回包目标：

```python
Uploader/__init__.py:167   'source': msg.source          # 来源
Uploader/__init__.py:307   target=task['source']          # 回包时原样当 target
```

而引擎**旧实现只认 `replay/` 前缀**：

```python
elif target.startswith('replay/'):   # 裸任务名落到 else
else:
    self.logger.error(f'Unknown target {target}.')   # 消息丢弃，只留一行日志
```

于是：上传成功 → 回包 `Unknown target` → `LiveEvents.onUploadEnd` **从不执行**
→ 状态不进入 `uploaded` → `_check_for_clean` 只处理 `uploaded` → **清理永不派发**。

**两份日志坐实版本差异**：

| 日志 | liveevents 发出的 source | 结果 |
|---|---|---|
| `.log.2026-10-05`（旧） | `'source': 'twitchpros-yrvz72-0'` | `Unknown target twitchpros-yrvz72-0.` |
| `(3).log`（新） | `'source': 'replay/dds-0'` | 正常路由，清理完整执行 |

状态库时间线吻合：18:47 那批全卡 `uploading`；升级后 19:49–20:02 立刻出现 `cleaned`。

**`replay/` 前缀到底有没有用**（已核实：**没有**）

- 真正解析它的**只有** `engine.pipeSend` 一处；
- `replaytask.py` 判断前缀后**又用裸 `msg.event` 查表**，前缀没参与事件查找；
- `replaytask.py:35` 拼出的 `replay/<task>/<event>` 与 `event_dict` 的键**对不上**；
- 赋给 `msg.source` 的前缀**全代码库无人解析**（只作为 return address 被原样回传）。

结论：它只是命名空间仪式；而"同一件事三种写法 + 无统一自检"正是事故成因。

**修法**：`engine._resolve_target()` 按确定顺序解析 ——
`engine` → `replay/<name>`（**按前缀剥离**，不再 `split('/')[1]`）→ 插件名
（**保留标识优先于同名任务**）→ **裸任务名** → unknown（error 日志带 `source`/`event`）。

---

## 5. 上传组稿件归属

重启后同一上传组复用状态库中的 `remote_id`（bvid），新分段追加到既有稿件。
`biliwebapi` 与 `biliuprs` 均已支持（`resume_bvid` / `get_remote_bvid`）。

**测试抓到的真 bug**：`biliwebapi` 刷新远端数据失败时被误判为"稿件不存在"，
网络抖动即丢失稿件归属（会把一场直播裂成两个稿件）。修法：三态
（成功 / 不存在 / **保持既有**）+ `_KEEP_EXISTING` 哨兵。

**注意**：B站两个引擎都是**整文件重传**（每次新 `upload_id`）；只有 YouTube 支持
字节级续传（`retry_resume` 仅存在于 `upload_args.youtube`）。"继续"的真实含义是
**复用既有稿件 id**，不是断点续传字节。

**不记分 P 清单**：上传组只存 `remote_id`。用户可在 B站增删分 P，记成员清单只会失真。

---

## 6. cookies 过期导致上传失败

上传器实例会被**长期复用**（默认空闲 300 秒释放、最长 7 天）。实例构造时把 cookies
读进内存，期间用户重新登录或 `biliup renew` 刷新了 cookies 文件后，实例内旧 cookies
会让上传失败。

**修法**：`biliwebapi.refresh_cookies()`，**每次 `upload()` 前重新从文件读取**；
读取或解析失败时**保留当前 cookies 并继续尝试**，不让刷新本身中断上传。

---

## 7. 下载引擎内存优化

| 问题 | 实测（修复前） | 修法 |
|---|---|---|
| `Popen(..., bufsize=10**8)` 按该值分配管道缓冲，`stdout=PIPE` 时每个子进程一份 | `Popen` 后 Python 堆 **+190.74 MB**（`bufsize=-1`/`0`/`65536` 均约 0.1 MB）。**只测 RSS 会漏掉这个 bug** | 改为行缓冲 `bufsize=1`（代码本就按行 `readline()` 消费） |
| ffmpeg 监控线程 → 主循环的队列无上界，主循环每次只取 1 行 | 生产快消费慢时 10 秒积压 **1,522,823 行**、RSS **+804.7 MB** | 队列加上界（4096），满时丢弃**最旧**的行 |
| 用 `log += line` 累积整段 ffmpeg 输出 | 15 秒窗口内无行数上界 | 改为固定长度滚动尾缓冲 |
| `streamlink.py` 用 `logfile.read()` 整体读入临时日志 | 56.4 MB 日志 → 堆 **56.4 MB、峰值 170.2 MB**，实际只用到末尾 1000 字符 | 限定读取字节数与尾部读取。峰值降到 **0.13 MB** |

---

## 8. 其他修复

### `_check_for_upload` 空候选列表 `IndexError`
非实时分支在取 `up_videos[0]` 前未判空，全部分段短于 `min_length` 时抛异常并
**打断整个事件处理**（连带跳过 `_free_state_memory`）。改为判空跳过。

### `_check_for_clean` 无条件把状态改成 `cleaned`
赋值语句位于清理规则循环**之外**，只要产物是 `uploaded` 就被置为 `cleaned` ——
**即使没有任何规则命中**，导致"没清理的文件被当作已清理"而提前释放状态。
改为不匹配的规则提前跳过，只在真正派发清理消息时才更新状态。

### `DMREngine.stop()` 在未 `start()` 时崩溃
无条件使用 `self.recv_queue`，未启动时为 `None`。已加保护。

### 斗鱼登录态取流
- 新增 `douyu_watch_cookies`，支持 cookies 字符串 / cookies 字典的 json 文件 /
  浏览器导出的文本文件。
- **cookies 只用于取流请求**；开播判断与房间信息（betard）**始终匿名**。
- 带登录 cookies 时取流走 `JSEngine 签名 + www getH5Play（POST）` —— 实测这是唯一
  能拿到**原画 + 长期有效地址（`expire=0`）**的路径；免签名的 V1 接口带 cookies 会直接失败。
- 默认路径不存在时只记 `debug`；只有用户**显式配置却读不到**时才告警。

### XML 弹幕支持
`render_args.dmrender.dmconvert` 配置段，使**渲染器**与**虚拟录制**都能处理
B站录制格式的 `.xml` 弹幕（可选依赖 `dmconvert`）。本程序自行录制的 `.ass` 不走这里。

---

## 9. 测试基座

`unit_tests/`，标准库 `unittest`，**零新增依赖**。目录名不用 `tests/` 是因为
`.gitignore` 的 `/test**` 会忽略它。

```bash
python -B -m unittest discover -s unit_tests -t .
```

**约定**：`@unittest.expectedFailure` 用于**当前代码的已知缺陷** —— 用例断言的是
"正确行为"，修复后会变成"意外成功"从而提醒移除标记。当前 5 个
（B7.3 两例、B5 三例）。

**测试运行器**（均位于 `unit_tests/`）：

- `unit_tests/_p1_dryrun_runner.py`：`dryrun.py` 的外壳，额外把日志落到固定路径
  `logs/p1-dryrun.log`，便于脚本按固定名字读取。
- `unit_tests/_p1_fullflow_runner.py`：真实执行下载→渲染→上传→清理的验证运行器
  （**会上传**，需授权）。

**目录职责约定**（已整理）：

| 目录 | 放什么 |
|---|---|
| `tools/` | 仅"调用第三方软件的材料" + `tools` 包本身（`check_env.py` 的 `check_pypi`/`check_update` 被 `main.py`/`dryrun.py`/`update.py` 引用，`mem_probe_inproc.py` 被 `main.py` 以 `DMR_MEM_PROBE=1` 可选启用） |
| `unit_tests/` | 全部测试与测试运行器 |
| `.dev/` | 开发/诊断脚本（`mem_probe.py`、`check_douyu_stream.py`）；`/. **` 使其被 git 忽略 |

> `tools/` 在 `[INST]` 中被描述为"第三方工具"，但它在代码里同时是**Python 包**
> （`from tools import check_pypi`），因此不能只放二进制材料 —— 这一点已按实际依赖保留。

---

## 10. 事故与教训

### 10.1 dryrun 曾把测试片段真实上传到账号

早期 `dryrun.py` 把上传 `min_length` 设为 `0`，于是测试片段被**真实上传**到 B站账号
（已确认稿件 `BV1sUHL63EC6`）。

**当前做法**：保留"按当前配置加载全部任务"的原意（这是 dryrun 存在的意义），
但把 `min_length` 抬到测试分段无法满足的值（常量 `DRYRUN_MIN_LENGTH`）——
上传流程不会真正提交稿件。需要测上传链路时把该常量改为 `0`。

> 期间曾一度改成"生成隔离配置 + 隔离状态库"，但那会让 dryrun 不再验证**真实配置**，
> 与它的用途相悖，已还原。

### 10.2 文档被脚本截断

整理文档结构时曾用脚本把 `TODO.md` 从 1214 行截断到 645 行，**误删了若干章节**。
该文件当时未被 git 跟踪且没有备份，无法完整还原。

**教训**：整理文档结构应逐段编辑，或先复制副本；对未跟踪文件尤其不能做破坏性重写。
（该教训在后续文档重组中再次适用 —— `TODO.md` 现位于 `docs/`。）

### 10.3 真机验证发现的两个 bug（模块级测试没抓到）

| # | 问题 | 后果 |
|---|---|---|
| 1 | `_register_session()` 在 `room_info` 赋值**之前**调用 | `AttributeError` 被兜底日志吞掉 → 场次登记失败 → 产物写入触发**外键约束错误** |
| 2 | `_handle_interrupted` 要求"`.part` 不存在"才恢复 | 引擎**直接写最终名**（`part_path == path`）时永远走不到恢复分支 → `keep` 策略形同虚设 |

**教训**：模块级测试用假文件、假引擎，覆盖不到"真实引擎的文件命名时序"与
"属性初始化顺序"。真机跑一次的价值高于再写十个单测。

---

## 11. 验证记录

| 类型 | 方式 | 结论 |
|---|---|---|
| 状态持久化 | 真实直播间 dryrun（分段时间 120s） | 场次与产物落库、对账幂等、`ask`/`keep`/`discard` 三策略正确 |
| 全流程 | 真实执行下载→渲染→上传→清理（5 段 × 300s） | 四阶段全部成功；**5 段全部进同一稿件**；产物全部终态 |
| 恢复重驱动 | 用日志里的真实中间名文件复刻 | 成功改名并派发上传 |
| 恢复判定 | 复刻损坏文件场景（440MB / 10MB） | 大小达标正常派发；不足则标记 `skipped` 且不再重试 |
| 消息路由 | 复刻 uploader 裸任务名回包 | 产物状态 `uploading → uploaded`（修复前消息被丢弃） |

---

## 2026-11-02 · 弹幕字符字形安全清洗 + 优雅停止 + 孤儿分段登记

完整排查过程见 [排查记录-20261102-渲染崩溃三个真凶.md](排查记录-20261102-渲染崩溃三个真凶.md)，
这里只记实现要点与取舍。

### 1. `DMR/Downloader/Danmaku/glyphs.py`（新增）

**为什么不用 PIL**：`ImageFont.getbbox()` 对缺字形返回 .notdef 方框，
尺寸与正常字符一样（实测 U+3103 返回 `(0,12,40,52)`，与正常汉字同量级），
区分不出来。只有 fontTools 查 cmap 可靠。

**两张表**：
- `covered` = 所有字体的 cmap 并集（本机 167 个字体 → 144083 个码位，扫描约 5 秒）
- `unsafe` = `color - primary`，即"会回退到彩色字体"的码位（本机 1563 个）

**判据演进（三次才对，附为什么）**：

| 判据 | 结果 |
|---|---|
| `color` | 太宽。彩色字体（seguiemj）的 cmap 含完整 ASCII 区，`sanitize('a,b:c')` → `''` |
| `color - plain` | 太窄。U+1F1E8/U+1F408 在 SegoeUISymbol 的 cmap 里也有声明，被判安全；但 libass 按自己优先级实际选 seguiemj，照样崩 |
| `color - primary` | 正确。主字体有字形就根本不会触发回退 |

`primary` 从 `configs/global.yml` 的 `download_args.live.font` 读（默认
`Source Han Sans SC`），按字体 name 表的 nameID 16/1 匹配。
**认不出主字体时退化成"只过滤无字形字符"**，不会误删 ASCII。

**性能**：纯 ASCII 走快速路径零开销；非 ASCII 每条弹幕 O(len) 次 set 查询。
覆盖表按字体目录指纹（路径+大小+mtime 的 sha1）缓存到
`.temp/glyph_coverage.json`，装/删字体自动重建；内存里 1 小时过期。

**降级**：检测任何一步失败都返回 None，调用方**原样放行** ——
绝不因为检测挂了把弹幕全删了。

**接入点**：`danmaku.py` 在 `dm_available()` **之前**清洗
`uname`/`content`/`text`（模板展开后再清一次 text）。
所以**只防"写入 .ass 之前"**，已写坏的不回溯。

### 2. 优雅停止（`main.py` + `tools/stop_dmr.py`）

**DMR 原本就有优雅链路**，只是 `stop_dmr.py` 用 psutil 硬杀没走它：

```
Ctrl+C → main.py KeyboardInterrupt → dmr.stop()
  → engine.stop() → del_task() → replay task stop
    → stream_downloader.stop() → stop_once()
      → downloader.stop() → ffmpeg communicate('q')   ← ffmpeg.py:308
      → segment_callback(thisfile)                    ← 改名成正式名
```

**实现**：`main.py` 主循环从 `sleep(60)` 改成 0.2s 粒度轮询
`.temp/graceful_stop_<PID>`。标志文件**带 PID** —— 万一残留也只会让
对应实例退出，不会误伤别的；且 DMR 退出时自删 + `stop_dmr.py` 兜底清理
（残留标志会让 DMR 一启动就退出，陷入崩溃循环，所以兜底清理是必须的）。

`stop_dmr.py`：默认优雅（最多等 180s，`--timeout` 可调），超时才硬杀；
`--force` 跳过优雅。结束时报「有没有 `[正在录制]` 残留」——
这是优雅收尾是否奏效的直接证据。

**踩坑**：改 `DMR/` 下代码必须重启才生效。这次优雅停止第一次没生效，
就是因为 DMR 跑的是 10-30 启动的旧代码。

### 3. `tools/rename_orphans.py`（新增）

孤儿 `[正在录制]` 分段在状态库里没记录，reconciler 不管。而 reconciler 的
`_promote_intermediate_name()` 本来就会按 `output_name` 模板改名并补派渲染，
前提只是状态库里有记录 —— 该工具就是补这条记录（`INSERT OR IGNORE`，幂等）。

**踩坑**：`glob.glob('*[正在录制]*.ts')` 会把方括号当字符类，一个都匹配不到
（这个坑 2026-10-20 踩过一次，这次又踩了）。必须 `os.listdir` + `startswith`。

**安全约束**：默认预览；`--apply` 才写；写前自动备份 `.db`；DMR 运行时拒绝写
（SQLite 并发写会锁，除非 `--force`）；只 INSERT 不更新不删除；
跳过 10 分钟内还在写的文件（正在录的分段不能登记，会和 ffmpeg 抢同一个文件）。

### 4. 实测验证

| 项目 | 结果 |
|---|---|
| 字形清洗（事故那场 .ass） | 剔除 586 字符：无字形 4 种 40 个（U+3103×4、U+FFF4×20、U+FE0B×15、U+2065×1）+ 彩色回退 91 种 546 个（🐈×89、🎂×46、❤×46、🌈×43…） |
| ASCII 不误删 | `sanitize("hello, world: 123")` 原样返回；.ass 里逗号/冒号一个没少 |
| 孤儿接管 | 3 个 `[正在录制]` 全部被 reconciler 自动改名成正式名并补派渲染 |
| 单元测试 | 20 个（`unit_tests/test_glyphs.py`），全 mock 覆盖表，不依赖真实字体 |
| 全量测试 | 503 个，36 个 error 全是沙箱缺 ffmpeg（与 10-30 基线一致），无回归 |

### 5. 手工重渲事故那场的完整命令（可复用）

源视频 50-55 分钟有损坏帧，单次渲染必崩，用 `-f segment` 连续解码分段 +
`scale` 修 stride：

```bash
ffmpeg -y -fflags +discardcorrupt+genpts \
  -i src.ts \
  -vf "subtitles=filename='src.ass',scale=720:1280,format=yuv420p" \
  -c:v libx264 -preset medium -crf 28 -pix_fmt yuv420p -g 80 \
  -c:a aac -b:a 128k -noautoscale \
  -f segment -segment_time 600 -segment_format mp4 -reset_timestamps 1 seg_%02d.mp4
# 再 concat
ffmpeg -y -f concat -safe 0 -i list.txt -c copy -movflags +faststart out.mp4
```

**为什么用 `-f segment` 而不是 `-ss` 分段**：源 ts 时间轴有跳跃，
`-ss X -t 600` 的实际输出时长不可控（实测出现过 1004 秒和 816 秒的段），
拼接后总时长比源多 6 分钟。`-f segment` 是连续解码一次、按输出时间切段，
每段精确 600 秒，`-reset_timestamps 1` 让每段时间戳归零便于 concat。

**`scale` 为什么必须加**：`format=yuv420p` 只转像素格式，不重算 stride；
`scale=720:1280` 强制重新分配缓冲，stride 才正确。

---

## 2026-11-02（下午～晚间）· 可移植性 + 一键重启 + 目录新布局 + CD2 出殡 + 通知重写

> 覆盖当天 14:00–19:30 的全部改动。上午的 glyphs / 优雅停止 / 孤儿分段见上一节。

### 1. 一键重启 `Restart_DMR.bat` → `tools/restart_dmr.py`

改完 `DMR/` 下的代码**必须重启才生效**（无热加载）。以前要手动 Stop+Start，
现在双击桌面「DMR-重启」即可。流程：停守护 cmd → 优雅停 DMR
（graceful_stop 标志，ffmpeg 写完当前段）→ **清残留 graceful_stop 标志**
（关键！不清的话新实例启动即退出 + 守护重拉 = 崩溃循环）→ explorer 拉起
Start_Render.bat → 轮询验证（5s×18）。参数：`--force` / `--timeout N` /
`--stop-only` / `--dry-run` / `--verify`。

**踩坑：`from tools import stop_dmr` 有副作用** —— `tools/__init__.py` 是
`from .check_env import *`，import 它会跑环境检查。restart_dmr 用
importlib 按文件路径加载 stop_dmr，复用其进程探测 / 优雅停函数。

### 2. 通知链路两轮修复（`DMR/notify.py` 重写）

**第一轮（并发竞态）**：多个任务同开时共用固定文件 `notify_title.txt`
互相覆盖 → 每次发送独立临时目录。

**第二轮（传输层，当晚实盘）**：这台机器跑着 TUN 模式代理，DNS 全解析成
198.18.x.x Fake-IP。对 `push.ft07.com`（Server酱³ 真实推送端点）：

- Windows 自带 curl（SChannel TLS）**握手失败**（上游 CDN 换了证书链）
- Git Bash curl（OpenSSL）能通
- 结论：**curl 在这台机器上不可信**，改用 `requests`（OpenSSL）+
  **双端点自动回退**（`sctapi.ftqq.com` 通就用它，失败切 `push.ft07.com`），
  失败原因记 `last_error` 并打 warning。这解释了此前"通知时好时坏"
  —— 是代理出口的锅，不是代码的锅。

### 3. 录制目录新布局（`tools/migrate_layout.py` 已执行迁移）

```
直播回放/
  ├── 原视频/<主播>/    源视频 + .ass（34 个任务配置同步改写）
  └── 弹幕版/<主播>/    渲染成品
```

- 路径计算单一出处：`DMR/paths.py` 的 `danmaku_dir_for()` /
  `source_root()` / `DANMAKU_SUBDIR`。**老配置没迁也会正确归位**（有测试）。
- 迁移同时改状态库 117 条历史路径（改前自动备份 `.db.bak-*`）。
- Cleaner 的"弹幕版永不删"保护走**文件名**判据（`（弹幕版）`后缀），
  与目录布局无关，零影响。上传按文件传、网盘目录用 `{TASKNAME}` 变量，
  同样零影响。
- 新增主播：`add_room.py` 模板已更新，自动落 `原视频/<名字>/`。

### 4. CD2 挂载盘方案出殡（上传链路**没有**变）

上传链路一直是：**DMR 的 rclone → CD2 应用（WebDAV，clouddrive.exe 以
Windows 服务跑）→ 百度网盘**。`cd2:` 只是 rclone 里这个 WebDAV remote 的
名字。废弃的是**旧方案**（CD2 挂盘符 + 直写盘符），随删 7 个脚本：
`cd2_watchdog` / `cd2_queue_health` / `cd2_stability_report` /
`clean_cd2_queue` / `compare_cd2_versions` / `test_watchdog` /
`stop_uploads_cooldown`（判据：CD2 计划任务早已不存在 + 全仓 grep 无调用者）。
上传冷却实际在 `.temp/failed_uploads.json` + Uploader 内部重试。

**已知风险（不改，audit 兜底）**：WebDAV 有"假成功"前科 —— 百度限流时
CD2 可能吞数据但 rclone 退出码 0 → DMR 误判成功。`DMR_云端上传审计`
计划任务（`tools/audit_cloud.py`，在用）定期对账云端 vs 本地，专抓这个。

### 5. 杂项修复与踩坑速查

| 坑 | 教训 |
|---|---|
| 编辑器改 bat/cmd 会把 CRLF 悄悄换成 LF | 改完必须用 python 把 `\n` 统一回 `\r\n` 并验证（改 Startup 的 `DMR_AutoUp.cmd` 实踩） |
| `autostart_guard.py` 用裸 powershell 列进程 | venv / 沙箱 PATH 没有 powershell → FileNotFoundError → 保守跳过 → **DMR 起不来**。已改 psutil。`autostart_launch.py` 本来就是 which+psutil 双兜底 |
| `push_all.py` 输出"公开=nochange" | 不一定没推 —— 它先同步 prodsync worktree 再推，用 `git status -sb` 验证 |
| 两处非原子写 | `maintenance._save_state`、webapi 配置保存（Web 改配置写一半 → 坏 yml → 热加载丢任务）。均改 temp + `os.replace` |
| 维护 tick 失败只记 debug | 静默死必须能查，升级 warning |
| `.temp/failed_renders.json` 陈旧失败记录 | 清空成 `{}` 而不是删文件（Render 启动会读它）；11 条旧路径记录备份在 `backups/failed_renders-20261102-purge.json` |
| PowerShell 变量名不区分大小写 | `$new` 与 `$NEW` 同名，循环里互相覆盖产生畸形 XML。改用不同名字 |
| `boot_task.xml` 的 `<URI>` 必须与 schtasks 任务名一致 | 改任务名三处同步：bat 常量、XML URI、文档 |
| Startup 的 `DMR_AutoUp.cmd` 是手工产物 | `install_session_tasks.ps1` 只重建 L2/L3 计划任务，**L1 换机要手放** |

### 6. 桌面快捷方式（8 个，自绘图标）

启动 / 重启 / 停止 / 看实时日志 / 添加主播 / 重启后检查 / 体检
（`--all` 再加推送全部）。`tools/install_desktop_links.py` 幂等重建
（同名 .lnk 无法覆盖 → 先 `[System.IO.File]::Delete()` 硬删）。
图标 `assets/icons/*.ico` 由 `tools/make_icons.py`（Pillow）生成 ——
之前用 `shell32.dll,3`，显示成文件夹图标。

### 7. 验证

- 514 个单元测试全过（含 8 个新布局契约测试 `unit_tests/test_paths_layout.py`）
- `check_imports.py` / `check_undefined_names.py` 全绿
- 重启实测多次：60s 优雅超时内自退、5s 新实例就位、零孤儿分段
- 通知实测发送成功（手机收到）
- 端到端遗留：等下次真实开播，确认新录像落 `原视频/<主播>/`、成品落 `弹幕版/<主播>/`

---

## 2026-11-03 凌晨 · 交接体系建设 + 旧产物大清理

> 用户额度用完、准备换 AI 接手 → 目标：**让项目文件夹本身自解释**，
> 接手方（或 AI）检索一遍就知道来龙去脉，且不会被过时文件误导。

### 1. 交接体系（三个新文件）

| 文件 | 给谁看 | 作用 |
|---|---|---|
| `HANDOVER.md`（仓库根） | 人 | 完整交接：状态快照、架构地图、**12 条铁律**、坑速查表、待办、命令速查、红线 |
| `AGENTS.md`（仓库根） | AI | Codex / Cursor / Claude Code / Cline 等**会自动读**仓库根的 AGENTS.md → 把"先读哪三份文档、绝对不要做什么"写进去 |
| `tools/make_handover_bundle.py` | 两者 | 一键出**瘦身交接包**：4.2 MB / 505 文件，含完整 git 历史 + 文档 + 源码 + 脱敏配置，自动排除录播/venv/exe/登录态，打包后自检"无明文 key" |

README 顶部与「〇、项目文件地图」一节给出**哪是活的、哪是历史**的分层，
`docs/archive/README.md` 给归档的 10 份文档做索引（每份是什么、什么时候看）。

### 2. 旧产物大清理

| 删了什么 | 数量 | 判断依据 |
|---|---|---|
| tools/ 一次性诊断与对比脚本 | 40 | 全仓 grep 无引用，且被 `check_imports` / `doctor` 等现役工具取代 |
| tools/ 一次性 `test_*.py` | 17 | 验证的是具体历史 bug；保留长期有用的 `test_render_encoder` / `test_render_withsubs` / `test_hwdecode` |
| docs/archive 过时文档 | 13 | 旧 README 副本、CD2 方案文档、**已被 HANDOVER.md 取代的旧交接文档**、重复的日志错误报告 |
| 根目录残留 | 3 | `notify_title.txt` / `notify_content.txt`（旧固定文件通知机制的遗留）、`Dockerfile`（上游遗留且版本过时） |
| 早期日志 | 4 天 | 10-19~10-30 的日志原文，结论已归档进 `docs/archive/` |

**保留原则**（用户明确）：代码自带逻辑 + **实测记录** + **踩过的坑** + git 历史。
所以 `docs/archive/` 只剩 10 份"事故复盘 / 排查记录 / 复查报告 / 规范"，
每份都在 `archive/README.md` 里有索引说明。

⚠️ 删前全部用 `git rm`（历史可找回：`git log --diff-filter=D --name-only -- tools/`）。

### 3. 顺手修掉一个真 bug：失败渲染清单里的僵尸条目

现象：`.temp/failed_renders.json` 里 11 条 `rendering` 状态的记录，
源视频**全部不存在**（用户归档了录播 + 目录迁移），却永远清不掉。

根因：`load_failed_tasks()` 把清单全量加载进内存，之后 `save_failed_tasks()`
每次都**原样写回** —— 手工清空文件也会被写回来，永远清不掉；
Web 界面的"失败渲染"列表也永远显示这堆不存在的东西。

修法：加载时逐条校验 `video.path` 是否在磁盘上，不存在就丢弃并立即落盘。
重启后实测：`丢弃 11 条源视频已不存在的失败渲染记录` → 清单归零。

### 4. 顺带修掉：通知凭据硬编码在公开仓库里

`DMR/notify.py` 里 Server酱 key 是硬编码常量，而该文件在**公开仓库**
`SylvanLee205/DanmakuRepo` 里。改为按优先级读 `.login_info/notify_key.txt`
→ 环境变量 `DMR_SCT_KEY` → `configs/global.yml` 的 `notify.sct_key`；
同时删掉同样带 key 的一次性脚本 `tools/update_bat_dynamic_notify.py`，
`doctor.py` 的通知自检改读实际端点。**旧 key 仍需去 Server酱³ 后台重置**
（历史里那份要作废）。

---

## 2026-11-04 晚 · 全链路实测验证 + 推送通道故障修复

### 1. 目录新布局：端到端验证通过（可结案）

11-02 引入 `直播回放/原视频/` + `直播回放/弹幕版/` 双目录后，代码有单测兜底，
但一直标注"**未经真实开播验证**"。11-04 晚拿到完整证据链：

```
21:04  原视频/SHU SDO温州店/…21点04分.ts 落盘 801MB，同目录 .ass 195KB
22:04  分段收尾、[正在录制] 改名为正式名
22:07  渲染完成 → 弹幕版/…21点04分（弹幕版）.mp4  969MB
       （hevc_qsv，1 小时素材渲 3 分钟）
22:15  rclone 上传完成 → 落入 24h 延迟清理队列
```

同时核对：同一主播两目录已成对（原视频 12 文件 ↔ 弹幕版 7 成品），
上传按文件走（`uploader` 收的是成品路径）、Cleaner 按 `（弹幕版）` 文件名判据
（永不删成品），两条链路都**不受目录拆分影响**。此条从待办划掉。

### 2. `push_all.py` 推送通道被 `.DS_Store` 堵死

现象：跑 `tools/push_all.py` 直接退出
```
master 工作区有未提交改动，先处理掉再推：
?? .DS_Store
?? DMR/.DS_Store
```
但 `git diff` 是**空的** —— 一度让人以为是误报。

真因两层：
1. `master_dirty()`（`tools/push_all.py:81`）判据是 `git status --porcelain`
   有没有输出。`??`（未跟踪）也算输出，所以**未跟踪的垃圾文件同样会堵推送**；
   而 `git diff` 只比已跟踪文件，看不到 `??`，这是误判的根源。
2. 根因是 `.gitignore` 里**没有 `.DS_Store`**。从 Mac 用 Finder 浏览这块
   SMB 共享，**必然**在 Windows 盘上生成 `.DS_Store`（实测 5 个：
   仓库根 / `DMR/` / `backups/` / `直播回放/` / 甚至 `.git/`）。
   等于每浏览一次目录，推送通道就更堵一次。

修法：`.gitignore` 增加"操作系统垃圾文件"段 —— `.DS_Store`、`._*`、
`Thumbs.db`、`desktop.ini`。改完 `git status` 里两个 `??` 立刻消失，
push_all 恢复。

> 注意 `.gitignore` 是 **UTF-8 带 BOM** 的文件，改它别用会破坏 BOM 的工具，
> 用 python 读写 `utf-8-sig` 并保持 LF。

### 3. `requirements.txt` 的"幽灵改动"

同一时段还出现过 `M requirements.txt`，但内容一字未改。

真因：本机 git 全局 `core.autocrlf=true`（PortableGit 自带配置），
工作区 checkout 成 CRLF、索引里存 LF，而 `txt` 没有在 `.gitattributes`
里钉 `eol`，于是来回抖。实测 `git ls-files --eol`：
`i/lf w/crlf attr/(空)`。

修法：`.gitattributes` 补 `*.txt / *.json / *.toml / *.ini → text eol=lf`。
改完 `git status` 干净。

### 4. 心跳日志的主播数是启动快照，会骗人

`main.py` 心跳每 30 分钟写一行 `[心跳] 程序运行中，监听 N 个主播。`，
这个 `N` 取自启动时算好的 `n_tasks`（`main.py:637`），**之后永不刷新**。

实测：日志一直写"监听 33 个主播"，而 `configs/DMR-*.yml` 实际 35 个 ——
SHU SDO温州店（11-03 21:47）、姜姜（11-03 22:45）是热加载进来的，
日志里能查到 `检测到新任务配置文件: …，正在添加任务...`，
但心跳那行数字不动。

这个 bug 的危害不是"数字不准"，而是**用户会拿它当判据**：
看到 33 就以为有 2 个主播没加载成功。

修法：新增 `_live_task_count()`，每次现取 `dmr.engine.task_dict` 的长度
（热加载时 `engine.add_task` / `del_task` 会实时增删这张表）；
取不到时打 `logger.warning` 并退回启动快照，**不静默**（铁律 4）。

### 5. 本轮验证

- `unit_tests`：**514 tests，OK（5 expected failure）**
- `push_all.py` 预览：仅剩本次改动的 4 个文件，不再被垃圾文件堵
- 生产进程未重启（`main.py` 改动需重启才生效 → 等下次一键重启）

### 6. 顺带挖出：`push_all.py` 的"预览"会真删文件

修上面第 2 条时跑了一次 `push_all.py`（**默认预览模式**），它打印
`删除 tools/test_upload_decoupled.py`，然后那个文件**真的从公开 worktree
里消失了** —— 预览只保证"不提交/不推送"，但 worktree 已经被改了。

代码位置 `tools/push_all.py` 的 `sync_public()`：`git checkout master -- .`
和 `git rm --cached` + `os.remove` 都写在 `if not apply: return` **之前**。
私有仓库那边的 `sync_full()` 是对的（同步函数都包在 `if apply:` 里），
只有公开仓库这条路漏了。

影响：任何人"先预览看一眼"都会把公开 worktree 弄脏，被删的文件要靠
`git checkout HEAD -- <path>` 找回。属于**预览即破坏**，违背工具契约。

修法：
- `git checkout master -- .` 与删除动作都包进 `if apply:`
- 预览改用 `git diff --name-status HEAD master` 统计待同步项，
  删除的打印成 `[预览] 将删除 xxx`，一个字节都不动

> 这个 bug 和前面 `.DS_Store` 堵推送是**互相掩护**的关系：
> 因为推送一直被垃圾文件挡住，`--apply` 从没跑到同步段，
> 所以"预览会删文件"一直没暴露。

---

## 2026-11-04 23:00 · 发布 v6.1.0

### 1. 为什么发这个版

GitHub 上此前只有 `v5.1.0`（2026-10-15）一个发行版。`v6.0.0` 的 tag 虽然早就
推上去了，但**从没建过对应的 GitHub Release** —— 等于"打了标签没人看得见"。
而 v6.0.0 之后又积累了 21 个提交（含目录新布局、一键重启、交接文档、
大量缺陷修复、226 文件变更），早就该发一版了。

### 2. 一个必须纠正的历史错误：v6.0.0 的 tag 打错分支了

`main` 和 `master` 是**两条独立历史**（无共同祖先）：

| 分支 | 角色 | 内容 |
|---|---|---|
| `master` | 生产分支（本机 `F:/DanmakuRepo` 在这里） | 只有代码，**无** `configs/` 任何东西 |
| `main` | 公开发布分支 | `master` 的树 + `PUBLIC_EXTRAS`（含 `configs/global.example.yml`） |

- `v5.1.0` 打在 **`main`** → 下载源码包能拿到示例配置 ✅
- `v6.0.0` 打在 **`master`** → 下载源码包**完全没有 configs 目录** ❌

后者是个真实缺陷：别人下载 Source code 后根本不知道怎么配 `global.yml`，
得先读文档自己造一个。tag 已推上去改不了（强推会破坏别人已 clone 的引用），
所以**约定：从 v6.1.0 起发行版 tag 一律打 `main`**，并在 HANDOVER 里写死这条。
`v6.0.0` 标记为"tag 位置不正确，已作废不用"。

### 3. 发布动作

```bash
git tag -a v6.1.0 main -m "..."      # 注意是 main，不是 master
git push origin v6.1.0
gh release create v6.1.0 \
  --title "v6.1.0 —— 无人值守稳定性大修 + 全链路可观测" \
  --notes-file docs/RELEASE-v6.1.0.md
```

发行说明正文放在 `docs/RELEASE-v6.1.0.md`（进仓库，可追溯），
而不是只填在 GitHub 的 notes 框里 —— 这样文档和代码版本绑定，
以后改仓库也能查到当时发了什么。

**发布前脱敏自检**（每次都做）：
- 发行版树里**没有** `configs/DMR-*.yml` / `rclone.conf` / `notify_key` / `.login_info/`
- `configs/global.example.yml` 里没有 `SCT*` / `FT*` 形态的 key
- 实测：`git ls-tree -r --name-only v6.1.0` 共 290 个文件，关键文件齐全

### 4. 顺带踩到的坑：`.workbuddy/` 又被推送通道挡住

写完记忆文件后 `push_all.py` 报 `?? .workbuddy/`。原因是**记忆文件写错了位置**
（写进 `F:/DanmakuRepo` 而不是 agent 的工作区），生产仓库凭空多了个目录。

不是 `.gitignore` 的问题，是操作失误：立刻把记忆挪回工作区
`C:/Users/Administrator/WorkBuddy/<会话>/.workbuddy/memory/`，生产仓库恢复干净。

> 教训：**生产仓库的干净是硬约束**。任何"顺手写个笔记"的操作都不能落在
> `F:/DanmakuRepo` 里 —— 那个目录只有代码，没有杂项。

### 5. 验证

- `gh release view v6.1.0`：`isDraft=false`、`isPrerelease=false`、已发布
- 附注标签 `v6.1.0` 对象 = `321d3007`，指向 commit `5071799f`
  （2026-11-09 全库复核校正：原记 `0daea7df` 是 tag 重打前的中间值；
  因发行说明文档更新与 Release 页面链接修复，tag 前后重打过两次）
- 发行版树 290 文件，`README.md` / `HANDOVER.md` / `AGENTS.md` / `Setup.bat` /
  `configs/global.example.yml` / `docs/RELEASE-v6.1.0.md` 全部在位
- 514 个单元测试仍全绿

---

## 2026-11-06 晚 · 「添加主播」崩溃的真正根因（10-23 归因错了）

### 现象

双击「DMR-添加主播」后：

```
[X] add_room.py exited with code -1073741819.
If nothing was printed above, the script crashed before any output.
Just try again - or paste the link manually when it asks.
Press any key to continue . . .
```

`-1073741819` = `0xC0000005` = **ACCESS_VIOLATION（访问冲突）**。
关键特征：**一行输出都没有**。如果是 Python 异常，traceback 会打出来；
没有 traceback 说明崩溃发生在 **C 层**，Python 连捕获的机会都没有。

### 10-23 当时的归因（错的）

`Add_Room.bat` 和 `read_clipboard()` 的注释里写着：

> 实测极少数情况下 ctypes 直接读剪贴板会让进程**段错误**……
> 根因是剪贴板被别的进程占用/句柄失效。

于是做了三层加固：OpenClipboard 重试、先查 `IsClipboardFormatAvailable`、
拿不到访问权才退回外部命令兜底。

**方向错了。** 所以 11-06 又崩了一次，症状一模一样 —— 这说明加固没碰到根因，
只是降低了触发概率。

### 真正的根因：ctypes 默认 restype 是 32 位

`tools/add_room.py` 原来这么写：

```python
handle = user32.GetClipboardData(CF_UNICODETEXT)   # ← 没声明 restype
kernel32.GlobalLock.restype = ctypes.c_wchar_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
text = kernel32.GlobalLock(handle)
```

ctypes 调用 Win32 API 时，**没声明 `restype` 就一律按 `c_int` 处理**。
在 64 位 Python 下 `c_int` 只有 32 位，而 `HANDLE`/`HGLOBAL` 是 64 位指针
—— **高 32 位被静默截断**。

本机实测（64 位 Python 3.11）：

```
完整句柄 0x24b64eccd10
截断后   0x64eccd10        ← 只剩低 32 位
默认返回值 -1341914016  =  0xb0040460
正确返回值 2184796439648 = 0x1fcb0040460
```

把截断值传给 `GlobalLock`，直接 `Segmentation fault`：

```
截断句柄: 0x64eccd10
完整句柄: 0x24b64eccd10
rc=139   Segmentation fault
```

**为什么是"偶发"的**：堆句柄的高 32 位有时候恰好是 0（此时截断无损，正常运行），
有时候非零（截断后指向野地址 → 崩）。所以这 bug 看起来"时好时坏"，
而且越跑越容易碰上（进程堆地址随机化 + 堆增长都会让高位变成非零）。
这也是为什么"重试一次就好了" —— 下一次进程拿到的高位可能是 0。

还有一个次要陷阱：**不要用 `c_char_p` / `c_wchar_p` 作 restype**。
它们会让 ctypes **自己解引用**返回值，指针一旦无效同样崩在 C 层。

### 修法

```python
# 所有返回/接收句柄的 API 都显式声明成 c_void_p
user32.GetClipboardData.restype = ctypes.c_void_p
user32.GetClipboardData.argtypes = [ctypes.c_uint]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.restype = ctypes.c_int
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]

handle = user32.GetClipboardData(CF_UNICODETEXT)
ptr = kernel32.GlobalLock(handle)      # 原始指针，不解引用
try:
    text = ctypes.wstring_at(ptr)      # 手动读，自己控制
finally:
    kernel32.GlobalUnlock(handle)
```

原来的三层防御（重试 / 查格式 / 外部命令兜底）全部保留 —— 它们本身是对的，
只是不足以修根因。

### 验证

- 实测读剪贴板：修复前必崩，修复后正确返回 `'https://live.douyin.com/865102911165\r\n'`
- **连跑 20 次，0 失败**（原来"偶发"崩溃，必须靠重复验证）
- 完整 `add_room.py --dry-run`：正常识别、正确跳过已存在房间、退出码 0
- 新增 `unit_tests/test_add_room.py`（15 个测试）：
  - 5 个是**静态断言**，检查句柄 API 是否声明了指针宽度类型
    （不依赖真实剪贴板，CI 也能跑）
  - 6 个测链接解析（纯房间号 / 完整 URL / 抖音分享脏文本 / 多链接 / 去重 / 空）
  - 4 个测配置模板（四开关 / dltype / 新布局输出目录 / Windows 非法字符）
- **反向验证过测试有效**：把代码临时改回旧写法，3 个测试立刻失败
- 全量：**529 tests OK**（514 + 15）

### 教训（写进 HANDOVER 坑速查）

1. **看到 `0xC0000005` + 没有任何 traceback，先怀疑指针位宽**，不要怀疑业务逻辑。
2. `ctypes` 不写 `restype` = `c_int` = 32 位。64 位下凡是句柄/指针都要显式声明。
   项目里的正确范式：`DMR/utils/instance_lock.py` 用 `wintypes.HANDLE`。
3. **归因要打到能复现的层面**。10-23 那次只观察到"段错误"就归因为"剪贴板被占用"，
   结果加固了一圈防御，根因原封不动，两个月后又崩。判断"根因是否找对"的标准
   应该是：**能不能稳定复现、修完能不能验证不再复现**。


---

# 2026-11-09 例行体检：四个真 bug（含两个数据安全险情）

用户："今天也帮我检查一下吧"。完整报告在 `docs/healthcheck-2026-11-09.md`，
这里记结论和教训。

## 机器本身没问题，问题全在边界判据

进程 PID 12724/12824 起于 11-03 05:06，**连续运行 6 天零重启**；03:41 分段
→ 03:44 渲染 → 03:44 上传 → 进 24h 队列，3 分钟走完全链路；近 24h 渲染失败 0 次、
真实 QSV 降级 0 次；今日 4 条 error 全是网络抖动重试。

但三个"判据类"bug 一直在悄悄吃数据。

## bug 1：`LiveEvents.onTaskError` 失败只改内存、不写库

状态库里 2 条 `dm_video` 永远停在 `rendering` / `attempts=0` / `last_error=None`，
而日志里它们明确失败过（产物时长异常）。全库 `state='failed'` 计数 = **0** ——
`_STATUS_TO_STATE['failed']` 这条 10-19 补的映射从来没生效过，因为**没人调同步函数**。

11 处 `['status'] = '...'` 赋值点里，只有 `onTaskError` 那处后面没有 `_sync_artifact`。

后果链条（因为进程 6 天不重启，reconciler 的启动降级一次都没跑过）：
```
失败 → 内存 failed（本次运行正确）→ 库里仍 rendering
     → 重启降成 recorded → 重渲 → 又失败 → 无限循环，attempts 恒 0
```

修：新增 `_persist_failed()`，落 FAILED + `last_error`（切 500 字）+ `attempts+1`。
不用 `_sync_artifact(state=...)` 是因为它不带 `last_error`，库里只剩个干巴巴的
failed，下次重跑还是原地踩坑。整个方法不抛异常 —— 写库失败不能连累清理判定，
否则回到 10-05"源视频永不清理"的老问题。

5 个测试 + 反向验证（回退修复 → 3 个用例立刻失败）。

## bug 2：`clean_sources.py` 只认老布局

目录模型是 11-02 改版前的（`直播回放/<主播>/` + `<主播>（弹幕版）/`）。改版后
`直播回放/` 下只有 `原视频`、`弹幕版` 两个容器，老逻辑把它俩当主播名去找
`原视频（弹幕版）`，永远找不到 → 工具静默退化成"没有可清理的文件"。

叠加上"成品名精确匹配"的缺陷：`掉了颗兔牙-...00点20分(2).ts` 对不上
`...(2)（弹幕版）.mp4`（差一个 `(2)`）。4 个已上传分段的源视频 2.2GB 躺了一周。

修：`_scan_layouts()` 双布局兼容；`_exists_rendered()` 容忍 `(1)`~`(9)`。

## bug 3：云端校验用截断前缀 → 会把"从未上传"判成"云端已有"

```python
n.startswith(stem[:len(stem) - 6])   # 6 是照 "14点19分" 猜的
```
截断后 `vvu-2026年11月07日14点19分` → `vvu-2026年11月07日`，于是当天**任意**
一场录制都能蒙对。险情恰好命中 bug 1 里那两段：
```
vvu 11-07 14:19   → 误命中 11-07 15:19
君君 11-06 18:52  → 误命中 11-06 18:03
```
而这两段是渲染失败、**从未上传**的。不修就是"一条 --apply 永久丢两段直播"。

修：严格同名（含 `(n)` 变体），绝不截断 stem；加静态断言禁止再出现截断前缀。

## bug 4：完全无视 24h 延迟清理队列

修完前两个，工具列出 79 个候选 / 44.5GB。对比队列才发现：
```
仍在队列里（刚渲染没上传）：66 个 / 40.92 GB
不在队列里（真僵尸）：      13 个 /  3.62 GB
```
判据只是"本地有渲染产物"，而正式流程是**渲染 → 上传 → 等 24h → 才删**。
顺手 `--apply` 就把没上传的全删了，还绕过保护窗口。

修：`_pending_clean_names()` 命中即跳过；**队列读不出来按"全部保护"处理**。

## 清理结果：9 个 / 2.91GB

删前逐条核实成品**已上传网盘**。其中 `掉了颗兔牙` 要特别查：本地两个成品，
`（弹幕版）.mp4`（3601 秒，已上传、云端有）和 `(2)（弹幕版）.mp4`（2995 秒，
日志判定读不出时长跳过上传）—— 内容已通过前一版上云，删源不丢内容。

> **2026-11-09 补记（用户提醒：别漏了「跳过上传」机制）**
> 上面「已上传」只是这 5 条的实情，**不是删除的通用前提**。
> `skip_upload_rule` 的语义是：横屏必选 + (码率≥2500 或 帧率≥40) 任一命中
> ＝ **三条里命中两条 → 整组不上传**（配置写 `skip_min_matches: 1` 是因为
> 横屏被排除在计数外，很容易看错成"命中一条"）。`global.yml` 原文：
> 「跳过上传**不影响本地文件**：原视频照常按 clean_args 删除，弹幕版按
> clean_args 留存（不上传也不删）」。所以命中规则的分段**云端永远查不到，
> 源视频却本来就该删**；把云端当硬门槛会让这批源文件永久滞留。
> 复核未误删：库里 21 条 `dm_video: skipped` 全是 11-05 之后的
> 断尾的人类不会喵 / 怦怦！ / 小鱼大王，与 11-02~11-03 这 5 条不重合；
> 其中 18 条源视频已按 24h 队列清掉、3 条仍在队列，链路正常。
> `掉了颗兔牙(2)` 的"跳过上传"是**渲染残缺**（日志：读不出时长、
> 产物缺索引），不是命中规则。
>
> 由此补两处：`clean_sources.py` 默认后悔期 30 分钟 → **1440 分钟（24h）**
> —— 跳过上传的分段不产生上传事件，可能压根没进 `pending_clean.json`
> （10-23 前积压过 42.9GB），队列防护兜不住，只能靠文件年龄；代码注释
> 明确「本地有弹幕版成品即独立充分、云端不是必要条件」。新增 4 个测试
> 含反向验证。**另修正本报告"磁盘涨 90G → 缩短延迟/加冷存"的判断**：
> 弹幕版永久留本地是硬性要求，增长属预期；真风险是跳过分段的成品
> **只有本地一份、无云端备份**，该做异地备份而非删本地。

清理后 `原视频` 41 → 36 个 .ts，再跑工具报"无残留"。

## 状态库脏数据

`fix_stuck_artifacts.py`（新建）：运行中状态按磁盘事实收敛 —— 同为"磁盘上没有"，
`dm_video` 回 `recorded`（合法的"待渲染"中间态），`src_video` 回 `missing`
（源真的丢了）。写成同一个值会让 reconciler 判断错。2 条已改，运行中状态现 0 残留。

`fix_state_payload.py`（新建）：53 条载荷 `Invalid \escape`，旧版拼路径一条用 `\`
一条用 `\\`，JSON 里出现 `\v`。`decode_payload` 有容错不挡启动，但会静默丢字段。
53 条**全是终态**（cleaned 33 / uploaded 14 / skipped 5 / missing 1）→ 不影响生产。
在 JSON 文本层补反斜杠，只在"修完确实能解析"时写；结构损坏返回 None 不硬改。
复核 386/386 全部可解析。

## 测试 566 个全绿

本轮累计新增 37 个（529→562 加 33：失败落库 5 / 清理判据 16 / 维护工具 12；
562→566 加 4：跳过上传语义），三组都做了反向验证。

## 教训

1. **补了映射不等于接通链路**。"状态枚举有 `failed`、映射表也有 `failed`"看起来
   万事俱备，但没人调同步函数。判断标准应该是**查数据**：全库 `failed` 记录数
   为 0，就该立刻怀疑"这条路径从没跑过"。数字比代码注释可靠。
2. **删东西的工具，判据要从"能不能删"倒推"删了会不会丢"**。
   `clean_sources.py` 三个缺陷叠在一起：先静默失效（不出事），修好后立刻
   暴露成"要删 44.5GB 没上传的东西"。**修好一个坏工具，比让它坏着更危险** ——
   必须同步把保护补齐。
3. **启动时兜底不能替代运行中收敛**。reconciler 的 `_step0_sanitize_unstable`
   设计没错，但它是"下次启动时"的兜底。长期常驻的进程没有"下次启动"，
   所以运行期必须自己把状态收敛干净。
4. 前缀匹配这类"猜长度"的模糊判断，在**文件名里含时间戳**的场景下几乎必错。
   要么严格同名，要么用能定位到唯一性的字段。
