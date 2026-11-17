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

- 实测读剪贴板：修复前必崩，修复后正确返回 `'https://live.douyin.com/<房间号>\r\n'`
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

---

# 2026-11-16 · 时长闸门误杀完整成品（A-1/A-2）+ 两个配套修复

起因：外部审查报告 `DMR审查报告-20261116` 指出「时长校验在误杀完整成品」。
本会话先独立复验，确认成立后按用户要求修复。**上一轮体检里"闸门在正常拦截"
的判断是错的**，这里一并更正。

## A-1 根因：参考值是墙钟，产物是媒体时间轴

`dmrender.py` 的闸门拿「弹幕最后时间戳 / `VideoInfo.duration` / 容器时长」当
基准，这三个都含起流等待、卡顿、断流重连的空转时间；而渲染带
`-fflags +genpts`，会把时间戳空洞压掉，产物时长对应的是**真正解码出来的画面**。

ffprobe 实测三例（都是被判失败并删除的产物）：

| 案例 | 容器跨度 | 可解码 | 产物 | 旧判定 | 事实 |
|---|---|---|---|---|---|
| vvu 11-07 14:19 | 3601s | 3060s | 3096s | 86% 删除 | 21 处缺口共 514s，产物完整 |
| 君君 11-10 19:42 | 168.3s | 168.2s | 168s | 74% 删除 | 产物与源一秒不差 |
| SFault 11-11 22:05 | 127.9s | 123.7s | 128s | 83% 删除 | 产物完整 |

**修法**：新增 `FFprobe.get_media_duration()` —— 用
`-show_entries packet=duration_time` 对视频包时长求和，得到可解码媒体时长。
闸门先用廉价参考（墙钟/容器/弹幕）粗判，只有产物短于最大候选的 98% 才扫包
（1 小时 4GB ts 实测 1.2 秒），判定式为 `缺口 > max(3s, 参考时长×10%)`。
包时长求和还加了合理性闸（须落在容器跨度 30%~120%），否则退回弹幕/最小候选，
避免 `duration_time` 大面积缺失时拿偏小数放过真截断。

10-18 断尾人类那次（源 3791s、产物 2027s、53%）依然会被拦住：缺口 1764s ≫ 379s。

## A-2 临时 ass 先删后读

XML 弹幕转出的临时 `.ass` 在渲染结束的 `finally` 里删除，而原实现等到闸门里才
读弹幕时间戳 → 永远读到"文件不存在"→ 返回 0 → 必然回退墙钟口径。
改为 `_prepare_danmaku()` 之后立刻读一次存进 `dm_last_ts`。

## P-1 `_has_danmaku` 缺 `@staticmethod`

`Maintenance._has_danmaku(src_dir, dm_base)` 定义在类里却无 `self`，调用处写
`self._has_danmaku(...)` → 必 `TypeError`。磁盘跌破 60G 时的紧急清理会 100%
失效（当前剩 700G，所以一直休眠）。补装饰器。

## A-3 `failed_renders.json` 按源视频去重

原来只按 uuid 去重，同一分段反复失败会堆多条（实测 16 条里 5 组重复）。
新增 `Render._video_key()`（`normcase + abspath`），加载时同一路径只留最新一条、
写入时先顶掉旧条目，并把合并结果立刻落盘。

配套两处收尾（同一根因：失败记录的"过期"没人清）：

- **加载时**：`_output_looks_complete()` —— 产物存在且时长 ≥ 源可解码 90%，
  说明这条"失败"是补派成功后的残留，直接丢弃（只对 output 存在的条目探测，
  不增加常规启动开销）。
- **渲染成功时**：`_gather` 清掉同源旧条目。补派走的是新 uuid，旧条目不会
  自己被顶掉，界面会一直显示"失败"，手工重试等于白渲。

实测：重启后从 10 条清到 5 条（余下 5 条产物确实不存在，属真失败）。

## 验证

- 新增 `unit_tests/test_render_duration_gate.py`（7 个用例）：三个误杀案例回放、
  53% 真残缺必须拒绝、短片段 10% 容差边界、临时 ass 先读后删、去重落盘、
  Windows 路径大小写不敏感。
- 全量 **576 tests OK**（566 + 10，expected failures=5 为既有 xfail）。
- 重启后实测：被误判的 5 个分段全部渲染成功（vvu 产物 3096s / 可解码 3060s），
  时长闸门 0 次误杀；失败清单 10 → 5 条（清掉的全是"产物已完整落盘"的过期记录）。
- `check_imports.py` / `check_undefined_names.py` 全部通过。
- 修改前已备份 `.temp/failed_renders.json`，随后优雅重启验证。

## 教训

1. **两个时长含义不同，别用同一个词**。日志把墙钟 `duration` 标成"源文件容器时长"，
   排查时极易误信 —— 这正是上一轮判断反了的直接原因。
2. **测试要锁"判据"，不只是锁"流程"**。这次三个反例（86%/74%/83%）都发生在
   闸门逻辑正确、参数不对的情况下，只有拿真实文件量出来的数字才能证伪。
3. **先删后读的顺序错误会被"返回值 0"掩盖**。凡"读不到就返回 0/-1"的兜底，
   都要问一句：这个 0 是"真的没有"，还是"文件已经没了"？

---

# 2026-11-16 · 审查报告全量修复（A/P/D/S/R/L 共 46 项）

外部审查报告（`DMR审查报告-20261116.md`）列出 A-1~A-3、P-1~P-4、D-1~D-12、
S-1~S-4、R-1~R-15、L-1~L-22 共 46 条。A-1/A-2/P-1/A-3 已于当日先行修复并验证
（见上一节）。本节记录**其余 42 条**的处理，按"为什么必须改"分组，同型缺陷合并写。

## 一、数据安全 / 红线（D 组 + P 组）

| 条目 | 问题 | 修法 |
|---|---|---|
| P-2 | dryrun 的上传闸门值为 0（等于放行），且共享生产状态库、无单实例锁 | `DRYRUN_MIN_LENGTH=10**9` + 新增 `--allow-upload` 显式开关；独立状态库 `logs/dmr_state_dryrun.db`、独立目录 `直播回放-dryrun/`、独立互斥体 `Global\DanmakuRepo_DryRun`；`dmr.maintenance=None` |
| P-3 | `cleanup_stale` 会把 `弹幕版/` 子目录里的成品删掉（红线） | 目录剪枝 `ds[:] = [d for d in ds if d != DANMAKU_SUBDIR]` + 文件名含 `DM_MARK` 直接跳过 |
| P-4 | biliuprs 只看日志里有没有 bvid 判断上传成功，不看退出码 | `call_biliuprs` 返回 `(logfile, args, rc)`，`rc != 0` 即失败；`login()` 成功即 `return`；`islogin()` 改退出码 + 行首正则 |
| D-1/D-2 | 多文件上传只保留最后一个状态；分片失败被无视后伪造 parts | 状态累积 + 失败 `break`；`as_completed` 逐个 `future.result()`，`failed or len(results) != chunks` 时 `return None` |
| D-3 | `chunked_render` 拼接到 FINAL 前先删成品 | 拼到 `FINAL.partial.mp4`，解码校验通过后 `os.replace()` 原子替换 |
| D-4 | `clean_sources` 只看"本地有 mp4"就删源 | 新增 `_state_map()` 读状态库，判据改为 `uploaded` 或 `skipped`+规则原因；队列缺失时 `return None`（不再返回空集）；`--apply` 删前重读队列 |
| D-5 | `clean_confirmed_uploaded` 完全绕过 24h 队列 | `MIN_AGE_H 1.0 → 24.0`、读 `pending_clean.json` 保护、`--apply` 前 `require_not_running()`、`.ass` 也做年龄检查 |
| D-6 | Cleaner 的弹幕版硬保护只拦 `delete`，move/copy/custom 全绕过 | 判据改成 `if is_dm_video_path(file.path)`，对所有 method 一律拒绝 |
| D-7 | `migrate_layout` 在原始 JSON 文本上正则替换，制造非法转义 | 改为 `json.loads → _migrate_payload() → json.dumps`，坏 JSON 计数跳过 |
| D-8 | `make_handover_bundle` 的 `_is_text` 恒真 → 把 git 二进制也当文本脱敏 | 去掉 `TEXT_SUFFIX` 尾部空串，新增 `_looks_text()`（读前 8KB 判 NUL）；`--with-configs` 真正生效并打警告 |
| D-9 | `emergency_clean` 删源不看上传确认，可删正在渲染的源 | 新增 `_removable_sources()`：状态库确认 `uploaded`/`skipped`+规则原因才可删，并排除 `pending_clean.json` 与 `failed_uploads.json` 涉及的 basename；库不可用时整体放弃（`return 0.0`） |
| D-10 | 非实时上传把未真正进入队列的分段标成 uploaded → 源被误删 | 按 `normcase(path)` 比对，只标记**实际进入 `up_videos`** 的槽位，并输出 `_marked`/`_dropped` 计数 |
| D-11 | `skipped` 状态语义重载，恢复流程误当"可跳过" | reconciler 三处 `SKIPPED` → `FAILED`（文件校验未通过 / 丢弃中断产物 / stale 抛弃） |
| D-12 | Config 清理兜底规则是 `delay: 0`（立即删源） | 兜底改为 `delay: 86400` |

配套：`DMR/Cleaner/move.py` 的 `move2Trash` 返回值原来被忽略（回收站失败=静默永久删除），
现在检查失败即抛 `RuntimeError`；同名目标自动加 `(n)` 后缀而不是静默覆盖。

## 二、安全 / 隐私（S 组）

| 条目 | 修法 |
|---|---|
| S-1 | WebUI 新增 `safe_config_name()`（拒绝 `/`、`..`、非 `.yml`），`config_edit`/`config_delete` 全部接入；`WebApi` 默认 `host=127.0.0.1`，默认口令 + 非 localhost 时**强制收窄**并 warning；`default.yml` 的 `webservice_kernel_args.host` 由 `0.0.0.0` 改回 `127.0.0.1` |
| S-2 | `push_all.py` 新增 `SENSITIVE_PATTERNS`（Server酱 key / Tailscale IP / 抖音与 B 站房间号 / 手机号）+ `--allow-sensitive`/`--skip-scan`，命中即中止（退出码 2）。**并做了实际脱敏**：真实房间号 17 处 → 0 处（`tools/add_room.py`、2 个测试文件、5 篇文档） |
| S-3 | `push_all.py` 的 `git push` 结果原来不检查，现在失败返回 `push_failed` 并以 1 退出 |
| S-4 | `doctor.py --export` 新增 `_mask_config_text()`，**按行**掩码（保留 558 行注释、支持 `cookies: |` 块标量），cookie/token 类键不再外发 |

## 三、无人值守可靠性（R 组）

- **R-1 编码自保护**：32 个 `tools/*.py` 统一在 import 段后插入
  `from _guard import ensure_utf8_stdout`（新建 `tools/_guard.py`，同时提供
  `dmr_running()` 精确进程判断、`sqlite_backup()` 在线备份、`require_not_running()`）。
  实测 `PYTHONIOENCODING=gbk` 下 doctor 不再崩。
- **R-2** `Run_DMR.bat` / `Start_Render.bat` 的 `pause` 分支改为写
  `logs\startup_error.log` 并 `exit /b 1`（session 0 下 `pause` = 永久挂起）。
- **R-3** `render_only.py` 4 处裸 `input()` 改为 `_ask()`（无 tty 时用默认值）；
  `check_env.py` 在非交互环境下直接抛 `EOFError` 而不是挂住。
- **R-4** `logintools.bili_login` 的 `while not islogin()` 死循环 → `for attempt in range(1,6)`
  + `proc.wait(timeout=180)`，失败抛 `RuntimeError`。
- **R-5** `FFprobe.run_ffprobe` 增加 `timeout=60`，区分 `TimeoutExpired`；所有裸 `except`
  改 `except Exception`。
- **R-6** 新布局（`原视频/` + `弹幕版/`）下 6 个工具静默失效：`recover_part` /
  `restore_dm_videos_from_cloud` / `audit_cloud` / `rename_orphans` / `health_check` 全部改走
  `danmaku_dir_for()` 与 `直播回放/弹幕版/<主播>/`，老布局作为兜底保留。
- **R-7** 自启锁 `LOCK_MAX_AGE 150 → 1800` 并加 30s 心跳（`os.utime`），
  `restart_dmr.py` 现在也参与互斥（否则"我重启"与"守护拉起"会撞车）。
- **R-8** 进程匹配从子串 `'main.py'` 改为正则 `(?i)(^|[\s"])main\.py([\s"]|$)` + 项目名过滤，
  `stop_dmr` / `autostart_guard` / `post_boot_check` 同步修正（原来会误杀别人的 `main.py`）。
- **R-9** `post_boot_check` 的 `DMR-2026*.log` 改 `DMR-*.log`、`netplwizard` 改 `netplwiz`、
  任务数硬编码 `< 30` 改为按 `configs/DMR-*.yml` 计数（2027 年起不再假报警）。
- **R-10** `backup_to_disk` 的 `d.upper() == proj_drive` 只比较首字母（`D` vs `D:` 永不等），
  改为 `d[:2]`；`do_backup` 检查备份结果与 `verify()`，失败返回 1。
- **R-11** `purge_task_state` 新增 `--force`，目标命中**在用任务**时默认拒绝；
  备份改走 SQLite 在线备份 API。
- **R-12** `test_hwdecode.py` 的"测试"不再直接改生产 `default.yml`，要 `--apply` 才落盘，
  并保留原文件行尾风格。
- **R-13** `gen_restart_notify.cd2_ok()` 换成 `cloud_ok()`：直接 `rclone lsd <dest>` 探真实
  上传链路（15s 超时）。⚠️ **顺带纠正审查报告的前提**：CD2 服务其实还在跑
  （`clouddrive.exe` PID 6772，19798 在监听），假告警的真正根因是 `_ps()` 在 session 0 下
  调不到系统命令（即 R-14 的 PATH 问题），`_ps` 吞掉异常后返回空串 → 被解释成
  "服务不存在"。改用 rclone 探活后不再依赖任何 shell/服务名。
  同时修掉告警滞后：计数规则改为按间隔分流（间隔 < 2 分钟视为崩溃循环**照常计数**，
  2~10 分钟按人工重启不计数），崩溃循环 30 秒内就能触发"频繁重启"提示。
- **R-14** `_env.bat` 的 `endlocal & set "DMR_PY=%DMR_PY%"` 只导出解释器路径，
  在 `setlocal` 里前缀的 System32 PATH 被一并还原 —— 于是 session 0 起的子进程
  （rclone / ffmpeg / schtasks / net use）全部拿不到系统工具。改为
  `endlocal & set "DMR_PY=%DMR_PY%" & set "PATH=%PATH%"`（同一行展开，取的是内层值）。
  实测调用后 PATH 首段已是 `C:\WINDOWS\System32;...`，`where rclone` / `where ffmpeg` 均命中。
- **R-15** `doctor.chk_tasks` 的 except 分支漏 `continue`（`r` 未绑定）。

## 四、潜伏 bug（L 组）

| 条目 | 问题与修法 |
|---|---|
| L-1 | 热加载任务不注入 `state_repo` → 该任务状态永不落库。`check_config_update` 的 new/updated 两处补 `_with_state_repo` |
| L-2 | WebUI「重试失败任务」成功后状态机不收敛（旧 `request_id` 已不在 `wait` 里）。新增 `_retriable_requests`（失败请求 → 槽位）映射，`onRenderEnd`/`onUploadEnd` 匹配不到时按映射兜底认领 |
| L-3 | 渲染被杀留下半截 mp4 → 分段永久卡死（ready 不重渲 + 时长闸门拒传）。`resume_recovered` 新增 `_dm_product_ok()`：mp4 探测时长 ≤0 即判残缺，删掉坏成品并留空槽位交回"待渲染"。**衍生修复**：`_passes_length_gate` 对 mp4 先补一次真实探测，把"重建对象没时长字段"和"文件真缺 moov"区分开 —— 实测救回一个 426MB 被误判 3 天的成品，重启后 1.7 秒上传完成 |
| L-4 | `render_only` 的 `-1-(-1)=0<30` 让坏成品挡住重渲染。新增 `_safe_duration()`，任一 duration ≤ 0 都重渲 |
| L-5 | `engine.start()` 用 `.values()` 解包 2 个变量 + `plugin_dict['name']` 字面量。改 `.items()` + `entry['status']` |
| L-6 | `replaytask.add_event` 先 append 再无条件覆盖；list 型 trigger 会把 list 本身注册进去。改 `setdefault().append()`，`start()` 加 else 分支，重复 start 先清表 |
| L-7 | `last_error` 被下一次状态写抹掉、`attempts` 每次 upsert 清零。改"None = 不改动（清空请传空串）"，并把 `attempts` 纳入 COALESCE 保留列表 |
| L-8 | Render/Uploader 的 `stop()` 把**在跑**任务也抄进 `failed_tasks`（随后可能成功 → 人工重试=重复投稿）；Uploader 还无锁遍历 `_uploader_pool`。改为只收 `waiting` 任务 + 快照遍历 |
| L-9 | `danmaku.py` 的 except 里引用未绑定变量 → `dm_filter` 缺键时整场录不上弹幕。预初始化 + 改 `.get()` |
| L-10 | `safe_filename`/`rename_safe` 用未转义 glob 数个数，标题含 `[1]` 时返回**已存在**的名字（渲染 -y 直接覆盖成品）。新增 `_free_name()` 逐个 `exists()` 探测；pyrequests（2 处）、virtual_downloader 同型修复 |
| L-11 | `recover_ffmpeg_segments` 弹幕配对忽略 Part 序号（Part001 抢走 Part000 的 ass）+ 时间戳差 1 秒就配不上 + `recovered` 含失败项。改为"时间戳 ±300s 容差 + Part 精确优先"排序，`recovered` 只收真正改名成功的 |
| L-12 | `streamlink_sync` 用 `-reset_timestamps`（只属 segment muxer）配 `-f flv`，启动即失败；两个子进程 `stderr=PIPE` 全程不读 → 64KB 写满双向死锁。删该选项，stderr 各自重定向到独立临时文件（Windows 上父子共享 handle 会共享文件指针，必须开两份） |
| L-13 | `pyrequests` 取流无 timeout，可永久挂死。加 `(10, 30)` 连接/读超时常量 `STREAM_TIMEOUT` |
| L-14 | `virtual_downloader` 每 30 秒全量重扫，同一文件反复派发（重复渲染/上传 + `sess_videos` 无界增长）。按 `(文件名, mtime, size)` 去重，`segment_id` 改独立计数器，场次切换时重置 |
| L-15 | `yutto._get_video` 只按后缀猜（.mkv 时 getsize 抛错）；`shutil.move` 后还 `listdir` 旧路径（整批已搬走却未登记）。补 `exists()` 判断、改 `listdir(new_dir)`、`rmtree` 加 `ignore_errors` |
| L-16 | `Downloader/ffmpeg` 监控线程把 `readline().strip()` 的空结果当 EOF → 纯空白行就提前退出、15 秒后误判管道故障。改判原始 `raw == ''` |
| L-17 | `stream_downloader` 的 stop/启动竞态产生孤儿 ffmpeg（stop 时 downloader 还是 None）。两个线程 start 前检查 `self.stoped`；`stop_once` 改用 `getattr(..., None)` |
| L-18 | `biliwebapi.get_remote_data` 吞掉一切异常返回 None，"取数据异常沿用现有稿件"的三态保护成死代码 → 网络抖动被当"稿件已删除"→ 新建稿件、合集重复。改为：只有明确"不存在/不可见"（62002/640404/-404）才返回 None，其余抛异常 |
| L-19 | `retry_failed_uploads` 补传不清洗 emoji（CD2 errno -7 但 exit 0 → 静默丢失）、用 `lsf` 字节核对（有幻影条目）、无视任务引擎一律传网盘。改用 `safe_remote_name`、`lsjson --stat` 直查对象、按任务 `upload_args` 分流 |
| L-20 | `runcmd` 传字符串命令必 `UnboundLocalError`；`wait=True` 时 `stdout=PIPE` 不读 → 超缓冲死锁（Cleaner 的 `method: custom` 正走这条）。统一改走 `run_and_capture_output`（临时文件重定向） |
| L-21 | `biliuprs.stop()` 的 `warninging` 拼写错误（AttributeError 被吞，发生在 kill 循环之前 → 有在传进程时一个都杀不掉）。改 `warning` |
| L-22 | `audit_cloud.should_skip_upload` 自写一套跳过规则，与 DMR 语义不一致：`skip_require_landscape` 时把任意横屏都当"本该跳过" → 横屏真丢了也不报。改为直接 import 复用 `evaluate_upload_skip_rule` |

## 五、验证

- 全量 **578 tests OK**（含把 4 个记录 L-5/L-6 缺陷的 `@expectedFailure` 转成正常用例）。
- `check_imports.py`：静态/运行时/真实清理三层 0 问题。
- `check_undefined_names.py`：全部文件通过，6 个 `import *` 模块的运行时导入测试通过。
- `doctor.py`：Python/依赖/ffmpeg/ffprobe/rclone/biliup/目录/配置/46 任务/状态库/磁盘/进程/
  通知连通全部通过；唯一警告是沙箱禁止 `schtasks.exe` 导致计划任务查不到（权限问题，非代码问题）。
- 优雅重启两次（`tools/restart_dmr.py`）：守护 1936→15544，DMR 8912/4140→13356/15520；
  新日志对账 `错误 0 条`、`发现 7 个待恢复产物`、`已重新派发 1 个任务`，无 traceback。
- 实测救回：`掉了颗兔牙-2026年11月03日00点20分(2)（弹幕版）.mp4`（426MB）
  自 11-03 一直卡在 `rendered` 从未上传，L-3 衍生修复后重启即上传完成。

## 六、经验

1. **假告警要先证伪它的前提**。R-13 写着"CD2 已退役"，但 `clouddrive.exe` 其实在跑；
   症状是真的（每次启动都报"服务不存在"），原因却在别处（`_ps()` 拿不到系统命令）。
   改"判断依据"的优先级高于改"文案"。
2. **同一个状态位承担两种语义，迟早在某条分支上出事**（`skipped` 既当"规则跳过"又当
   "处理失败"）。发现这类重载，宁可新增状态也不要复用。
3. **"读不到"必须区分"没有"和"失败"**。`glob` 数个数、`get_duration()` 返回 -1、
   `_ps()` 返回空串 —— 这些"安静的兜底"是本次 42 条里出现频率最高的共同根因。

---

# 2026-11-16 晚 · 重启日志复核发现的两个真问题

用户复查 17:10 那次重启的日志，问「每次重启之后的输出有没有问题」。复核后发现
两处：一处是状态库早就停摆（不是重启引起的，但重启日志把症状暴露得很清楚），
一处是重启日志里那句「7 个待恢复 → 已重新派发 1 个」背后的静默黑洞。

## 一、状态库从 11-04 起就是空的

`sessions` 表总共只有 30 条，时间分布是 11-01~11-03（26 条）+ 11-15~11-16（3 条），
**11-04 到 11-15 完全空白**。而磁盘上有 449 个成品、云端一切正常。

对照日志文件时间戳才看明白：`logs/DMR-20261103(2).log` 的 mtime 是 11-09 12:20，
`logs/DMR-20261109.log` 的 mtime 是 11-16 09:21 —— DMR 这 13 天里**只重启过两次**，
日志按启动日期命名，所以文件名看起来像"11-03 的日志"，实际一直写到 11-09。

也就是说：状态库不是被谁清空的，是那段时间根本没写进去。这正是审查报告 L-1
（热加载任务不注入 `state_repo`）的实证 —— 任务在内存里跑得好好的，录像、渲染、
上传、清理全部正常，唯独"进度"这条线整体哑火。11-16 修掉 L-1 之后才开始恢复。

危害：**没有任何数据丢失**（云端上传一直是成功的），但库里看到的进度与磁盘完全脱节，
所有依赖状态库的判断（清理准入、恢复续跑、健康检查）在那 13 天里都是"看不见"的。

## 二、「已重新派发 1 个」背后的静默黑洞

原日志：

```
发现 7 个待恢复产物（已录制/已渲染但未完成处理）。
恢复 3 个分段待处理（1cef1f31）。
恢复 3 个分段待处理（24bc05fb）。
已重新派发 1 个处理任务（渲染/上传）。
```

看着像"7 个里只有 1 个需要处理"，实际是 `_dispatch_pending_renders` 里这一行：

```python
if os.path.exists(output):
    continue   # 已有成品，不重渲染
```

成品存在就 `continue`，**既不补记状态、也不补上传**。于是那 6 个（uu萌 11-15 ×3、
忧郁敏 11-16 ×3，共 6.6GB）每次重启都被判成"待恢复"，每次又什么都不做，
永远停在库里 `src_video=recorded`、`dm_video` 无记录。实测云端其实早就传上去了。

修法（`_settle_existing_product`）：按「磁盘上确实有成品」这个事实把状态收敛掉，
判据用 `logs/pending_clean.json` —— cleaner 只在上传确认之后才把源文件排进队列，
所以它是"这一场确实传上去了"的持久化证据：

- 队列里有这一场 → 直接标 `UPLOADED`（dm_video 与 src_video 一起），不重传；
- 否则 → 标 `RENDERED`，交回同一轮的上传流程（rclone `copyto` 按 size+mtime 跳过相同文件）。

源槽位也必须一起收敛，否则它一直是 `RECORDED`，下次重启仍被 resume 选中 ——
只修 dm_video 等于没修（第一版就漏了这点，重启后日志原样复现，第二次才修对）。

## 三、过程中自己引入并修掉的一个 bug

第一次改完直接重启，日志立刻报：

```
恢复任务 uu萌 的产物失败：name 'vtype' is not defined
  File "DMR/Task/liveevents.py", line 362, in _dispatch_pending_renders
```

`_dispatch_pending_renders` 的循环变量是 `sv`/`dv`，**没有 `vtype`**（它专判
dm_video 槽位）。578 个测试当时全绿，因为没有任何用例走过"成品已存在"这条分支。
现已补 3 个用例（`TestSettleExistingProduct`），其中一条专门断言"成品存在时不再派渲染、
但必须补记产物"，正是为了挡住这类"测试覆盖不到的分支里手滑"。

## 四、验证

- 收敛前：每次重启「发现 6 个待恢复产物 → 已重新派发 0 个任务」
- 收敛后：`状态已对齐：…（弹幕版）.mp4 早前已上传（待删清单可证），补记为 uploaded` ×6
- 再重启一次：日志里**不再出现「待恢复」**，`对账完成 … 待恢复 0 项，错误 0 条`，
  全程 0 error / 0 warning
- 库内核对：`1cef1f31` / `24bc05fb` 两个场次的 12 条产物全部 `uploaded`
- 全量 **581 tests OK**（较上一轮 +3）

## 五、经验

**"每次重启都打印同一句话"本身就是缺陷信号。** 恢复流程是幂等的才对——真收敛了，
第二次重启就不该再看见它。看到「发现 N 个待恢复」重复出现，先怀疑状态没写回去，
而不是怀疑那几个文件还有问题。

---

# 2026-11-16 晚 · 「Loaded N failed render tasks」到底是队列还是报表

## 现象

用户看重启日志：

```
[2026-11-16 17:32:58,413][info]: Loaded 5 failed render tasks.
```

每次都这一句，之后再无动作。看着像"5 个渲染任务排队等着重跑"，实际什么都没发生。

## 一、这句话从哪来，它是什么

`DMR/Render/__init__.py::load_failed_tasks()` 读的是 `.temp/failed_renders.json`。
这份文件有两个**写入方**：

1. `_gather(task, 'error')` —— 真渲染失败；
2. `stop()` —— 进程退出时的收尾。**旧实现在这里把 `render_tasks` 里的所有任务
   都抄进清单**（含正在渲染的），L-8 已于今天上午改成只收 `waiting`。

读取方只有两个：Web 界面「失败渲染」列表（`/api/...` + `tasks.html`），和人工点
"重试"按钮。**启动流程不会重跑它们。**

自动补派走的是另一条路：`DMR/state/reconciler.py::_dispatch_pending_renders`，
它**只遍历状态库里的产物**。所以这两套东西的口径一旦分裂，条目就永远没人管。

## 二、为什么这 5 条永远不会有动作

逐条核对（`uuid1` 里的时间戳 + 文件系统 + 状态库）：

| 条目（源分段） | 时长 | 产物 | 状态库记录 |
|---|---|---|---|
| 君君.-2026年11月06日18点52分 | 95.1s | 无 | **无** |
| 我真不吃土豆-2026年11月10日22点19分 | 13.6s | 无 | **无** |
| 哭哭不嘻嘻-2026年11月11日21点18分 | 76.4s | 无 | **无** |
| 小鱼大王-2026年11月11日22点15分 | 16.1s | 无 | **无** |
| Nyssa-2026年11月14日01点18分 | 12.3s | 无 | **无** |

全部 `status: rendering`，uuid1 解出的时间戳都在各自源分段之后约 2 分钟 ——
是"提交渲染后进程被杀"的残留，且都落在 **11-04 ~ 11-15 状态库冻结**（L-1）那段
窗口里，所以库里一条记录都没有。对账看不见 → 不补派；清单不是队列 → 不重跑。
两头都不管，于是每次启动都原样打印。

## 三、改了什么

1. `_log_disposition()`：把这句日志说完整 ——
   `Loaded N failed render tasks（仅用于 Web 界面展示与人工重试，启动时不会自动重跑；分段的自动补派由状态库对账负责）。`
   条数 >0 时再按 `debug` 逐条列出源文件名与产物是否缺失。空清单也打
   `Loaded 0 failed render tasks.`（"没日志"和"没检查"要能区分开）。
2. 清掉这 5 条历史残留（原文件备份在 `.temp/quarantine/failed_renders-20261116-174219.json`）。
   清理必须在**停机状态下**改：运行中的进程内存里还握着这 5 条，任何一次
   `save_failed_tasks()` 都会把它们写回文件。
3. 测试 +2（`TestFailedRenderDedup`）：一条断言日志里必须出现"不会自动重跑""状态库对账"，
   一条断言空清单打印 0。全量 **583 tests OK**。

## 四、同一个窗口留下的 5 个孤儿源分段（用户确认后已删除）

同一个窗口里还留下 5 个孤儿源分段（合计 56.3 MB，12~95 秒，弹幕 0~3 行，
无产物、无库记录）。它们不会被渲染、不会被上传、也不会被 24h 清理碰到 ——
和「直播回放/弹幕版」下那 449 个 pre-state-DB 成品是同一类遗留物。

用户确认后已连同各自的 `.ass` 一起删除（10 个文件，56.3 MB），
两个因此变空的目录（我真的不吃土豆、哭哭不嘻嘻）也用 `rmdir` 清掉了。
删除前先落了一份审计清单 `.temp/quarantine/deleted-orphans-20261116.json`
（绝对路径 / 字节数 / mtime / 时长），删完再核对"仍存在 = 无"。

**这一步不需要重启**：删的是磁盘上已经没人引用的静态文件，状态库里没有它们的
记录，`pending_clean.json` 里也没有，运行中的进程既没有句柄也没有内存引用，
`原视频/` 也不会被定期重扫（对账只遍历库里的产物）。

## 五、经验

**日志里的"N 条待办"如果没有对应的执行者，就是假队列。** 下次看到"加载了 N 个
某某任务"这类话，先问两件事：谁会消费这份清单？它和状态库是不是同一套口径？
口径分裂时，条目会同时被两套机制无视，而且**永远**不消失。

---

# 2026-11-16 深夜 · 第二轮复审：4 个高危新 bug、1 个修错、1 次历史库事故

外部复审报告（`DMR修复复审-20261116.md`）对 97 个文件、+4027/-765 的修复批次
做了全量核读。结论：约 40 项修对，但**修复本身引入了会删好成品/唯一源的新 bug**。
逐条复核后确认并修复如下（每条都写了会失败的新用例）。

## 一、会删好文件的三个（红线）

### N-2 探测失败被当成坏文件 → 删成品
`_dm_product_ok` 用 `FFprobe.get_media_duration()` 判 `> 0`，而它**把所有异常吞成 -1**
（ffprobe 没装、300 秒超时、权限错误）。于是"探测本身失效"= "文件是坏的" →
调用方 `os.remove`。docstring 写着"不在探测本身出错时判坏"，实现正好相反。

修法：新增 `FFprobe.probe_status()` **三态**探测
（`ok` / `unreadable` / `unavailable`）：只有 ffprobe 正常跑起来、退出码非 0
且 stderr 明确拒绝这个文件时才判坏；探测不可用一律 fail-open。

### N-3 扫包失败回退墙钟 → A-1 误删事故复现
`dmrender` 的时长闸门在"包时长扫不出来"或"合理性闸拒绝"时，回退到
`弹幕最后时间戳 / 最小候选` —— 两者都是**墙钟时间轴**。A-1 那三起误删
（产物 101%/100%/100% 被判 86%/74%/83%）在扫包失败或容器时长虚低（FLV 虚报
5.7 倍）时**原样复现**，照样 `os.remove`。

修法：fail-open —— 拿不到可信的可解码时长就不判、保留产物、记 warning。
真截断仍由"media_dur 与容器跨度自洽"那条路径拦住。
顺带更正注释里的性能数据：不是"4GB/1h ≈ 1.2 秒"，实测 **2.04GB ts 扫包 15.4 秒**。

### N-6 SKIPPED 关键词闸门双向失配
清理闸门（`maintenance.clean_confirmed_uploaded` 与 `tools/clean_sources.py`）靠
关键词白名单（`'低于' / '阈值' / '时长' …`）判断"这段是按规则跳过上传、源可以删"。
而 `_handle_unusable` 给"文件太小/大小未知"写的 SKIPPED 原因**恰好带这些词**
（生产库里实有 12 条 `'文件仅 15.6MB，低于 30MB 阈值'`）→ 残缺小成品也能让闸门
认定"处理完了"，删掉唯一完好源。反方向也坏：真正按规则跳过的行当年**不写原因**
（`d993c2b2` 两条 `last_error=None`）→ 永远清不掉。

修法：引入专用标记 `DMR/state/enums.py:SKIP_RULE_MARK = 'skip_upload_rule'`，
按规则跳过时写入 `last_error='skip_upload_rule: <判定细节>'`，闸门**只认这个前缀**；
`_handle_unusable` 改落 `FAILED`（同为终态、不在清理白名单）；两个清理入口
再各自加一道"成品必须真的能播"（`FFprobe.is_playable`：非 0 字节 + mp4 能解析
容器时长）。历史行没有前缀 → 不再被误删（保守方向），要清走人工。

## 二、恢复链的崩溃与假状态

### N-1 settle 塞字符串进槽位 → 整轮恢复崩溃
`_settle_existing_product` 写 `slot['file'] = output`（str）。同轮
`_check_for_upload` → `_passes_length_gate` 里访问 `video.path` →
`AttributeError: 'str' object has no attribute 'path'`，异常被启动恢复循环
"warning+continue" 吞掉 → **该任务整轮恢复中止**（其它分段的补派/补传全丢）。
正好打在"成品做完还没传"这条本来要修好的路径上；新增的 settle 测试全用
`auto_upload=False`，恰好绕开。

修法：`_product_video_info()` 按 `dmrender.render_one` 的口径构造真 `VideoInfo`
（deepcopy 源 → path/dtype/file_id/size/dm_file_id/src_video_id），槽位里不再有 str。

### N-4 弹幕配对正则打反
`tools/recover_ffmpeg_segments.py` 的 `_token14_of` 用 `re.findall(r'\d{14}')`
要求**连续 14 位数字**，而真实命名是 `YYYYMMDD-HHMMSS`（带连字符，工具自己的
分段正则就是 `\d{8}-\d{6}`）→ 所有候选被跳过 → `find_danmaku` 恒 None →
孤儿分段恢复时"没找到配对弹幕"，渲染必失败。修法：先剥非数字再滑动取 14 位。

### N-7 跳过品被假标"已上传"
待删清单里既有"上传确认后排队"，也有"命中跳过规则、源照删成品留本地"。
settle 只凭文件名命中就标 UPLOADED → 状态库与界面永久显示假"已上传"，
以后关掉跳过规则也不补传。修法：以库里既有状态为准，SKIPPED 保持 SKIPPED。

### L-3 删了成品却落终态
`_discard_broken_render` 原来写 `FAILED`（终态、不可恢复）→ "文件删了 + 状态终态"
= 永不重渲；源若已被清理则彻底丢。改为**删掉该产物记录**，语义即"弹幕版还不存在"，
src_video 仍可恢复 → 下轮对账自动重派。

## 三、配置与守护

- **N-5 dryrun 连生产库**：代码读 `get_config('state')`，而 default.yml 与 dryrun
  都写 `state_args` —— 键名对不上，用户的 `db_path` 被静默忽略，dryrun 实际打开
  **生产**库并执行写操作。统一读 `state_args`（`DMR/__init__.py` 两处）。
- **N-8 守护认不出真身**：`DMR_Q` 要求命令行含项目名，但真身是
  `C:/Python311/python.exe main.py --quiet`（无项目名），venv 的
  `python.exe` 只是启动器。启动器一没就判"没在跑"→ 反复拉起 → 撞 Global 互斥体
  exit 3 → 10 秒重拉。`autostart_launch.dmr_processes()` 改为 psutil 归属判定
  （exe 在项目下 / cwd == 项目根 / 父进程链 / 命令行兜底），
  `post_boot_check` 复用同一判据。
- **漏修 selfcheck youtube**：`DMR/Uploader/youtubev3.py` 早已删除，但注册表仍挂着它、
  `default.yml` 模板里还有整段 youtube 配置 —— 用默认配置新建任务就会被带进坑。
  删掉 default.yml 的 youtube 段（57 行）与注册表项；selfcheck 补 GBK 护栏
  （它打印 ✓ 但没接 `ensure_utf8_stdout`，是 R-1 的唯一漏网）。
- **A-3 去重键不含输出**：`_video_key` 只用源路径，transcode 与 dmrender 同源
  会互相顶掉（生产 auto_transcode 全 False，潜伏）。键加上输出路径。
- **D-9 failed_uploads 保护死代码**：真实结构是 `{'files': [...]}`，原来只找
  `('video','src_video','file')` → 永不命中。补上 files 列表。

## 四、一次事故：单元测试把生产历史库洗了

`unit_tests/test_state_init.py` 的配置模板写 `state:`，而 N-5 修好后代码读
`state_args` —— 于是用例退回默认相对路径 `logs/dmr_state.db`，**直接连上生产库**。
更糟的是启动时的"孤儿任务清理"看到测试配置里只有一个 `test` 任务，就按
"用户换了配置目录"把 22 个历史场次（10-15 ~ 11-03）当成孤儿**全部删除**
（sessions 29 → 7，artifacts 143 → 22）。

处置：
1. 停机 → 备份三件套到 `.temp/quarantine/db-restore-20261116/`；
2. 从私有仓库的库快照（`F:/DanmakuRepo-full`，17:52）`ATTACH` + `INSERT OR IGNORE`
   合并回 26 个场次 / 129 条产物 / 105 个上传组，保留 17:52 之后新增的 3 个场次；
3. 删掉测试留下的 `sess-resume/test` 场次；
4. 校验：32 个场次、150 条产物、`integrity_check=ok`；重启后 0 错误、无再次净化。

两条防线（都加了）：
- `unit_tests/support.py` 里硬闸门：任何用例试图打开**生产** `logs/dmr_state.db`
  直接抛异常（宁可测试红，也不能写生产库）。
- 孤儿清理改为**显式开启**（`state_args.purge_orphan_tasks`，默认 false）：
  只隔离保留。避免崩溃循环靠"隔离放行"已足够，自动删历史的收益远小于误删代价。

## 五、验证

- 全量 **600 tests OK**（583 → 600，新增 17 条，其中 N-1/N-2/N-4/N-7/N-8 各有
  "改回旧代码就红"的回归用例）
- 启动日志：`对账完成 … 待恢复 0 项，错误 0 条`、`Loaded 0 failed render tasks.`、
  **0 error / 0 warning**；库内 sessions 34 / artifacts 155（历史完整，无二次净化）
- `tools/selfcheck.py` 通过（youtube 断言不再失败）

## 六、没修的（复审 N-9 / 第四节剩余）

`D-1`（biliwebapi 部分成功后整组重传、按 filename 去重）、`S-4`（doctor 诊断包日志
未脱敏）、`D-8`（build_dir --out-dir 不脱敏）、`D-7`（迁移误改 title 等非路径字符串）、
`L-2`（跨重启重试仍失效，映射纯内存）、`S-1`（force_login=False 时不受收窄约束）、
`S-2` 剩余（PUBLIC_EXTRAS 里的脱敏工具自身含真实房间号；**git 历史未重写**）、
`R-3`（无人设 DMR_NONINTERACTIVE）、`R-7`（mode_supervise 不拿锁）。
其中 S-2 的 git 历史重写属破坏性操作，必须由你确认后再做。

---

# 2026-11-17 · 复审尾单：重试映射落盘、守护拿锁、免登录收窄、诊断包日志脱敏

承接 11-16 深夜那轮。剩下 8 项里 4 项动手了，2 项判定「不能盲改」，2 项失效。

## 一、L-2 的后半：映射只活在内存里，等于没修

11-16 只做了「重试成功时兜底认领」，映射 `_retriable_requests` 仍是纯内存。
而 WebUI 的「重试失败任务」列表来自状态库/日志，**重启后照样显示**，插件重试时
用的还是原来的 `request_id` —— 于是重启一次，兜底就失效：重试成功、状态停在
`failed`、成品不上传、源文件永不清理，日志里还写着"重试成功"。

改法：映射落盘到 `.temp/retry_requests.json`（tmp + `os.replace` 原子写），
首次访问时读回，写失败只降级为内存兜底（不阻断重试）。
三个变更点（`onTaskError` 记录、`onRenderEnd`/`onUploadEnd` 认领后 pop）都补了落盘。

## 二、R-7 守护模式不拿锁 → 两个守护各拉一个 DMR

`mode_supervise()` 有"拉起 DMR"的副作用，却从不加锁。计划任务重复创建、或有人
再双击一次 `Run_DMR.bat`，两个守护会同时判定"DMR 没在跑"，各拉一个实例：后起的
撞 `main.py` 的 Global 互斥体退 3，日志一片"启动失败"；更坏的情况是两边都看到
对方拉起的实例而各自 sleep，看起来正常，实际兜底没了。

改法：
- 新增独立锁 `logs/supervise.lock`。**不复用 `autostart.lock`** —— 那把锁的语义是
  "短动作互斥"，守护长期持有会挡住登录自启的 stop/start（"窗口跟着人走"就废了）。
- `acquire_lock()` / `release_lock()` / 心跳都支持指定路径（原来写死 `LOCK`）。
- 陈旧判定加一层 **pid 探活**：守护崩了不必等满 `LOCK_MAX_AGE`(1800s) 才有人接班。
  ⚠️ 只用 psutil 判 `pid_exists`，**不用 `os.kill(pid, 0)`** —— Windows 上
  `os.kill` 对非 CTRL_* 信号走 `TerminateProcess`，探活会变成杀人。
- `--dry-run` 不加锁（模块文档本来就这么约定），人工排查/测试随时可跑。

## 三、S-1 免登录 + 对外监听 = 局域网无鉴权

`force_login=False` 时 `login_required` 直接放行，配 `host: 0.0.0.0` 就是
"局域网内任何人可以停止录制、删任务、改配置、翻历史"。单机免登录是合理需求，
但"免登录 + 对外"必须是显式选择：现在这种组合会被强制收窄到 `127.0.0.1`，
要放开得在配置里写 `allow_insecure_remote: true`。

## 四、S-4 诊断包只脱敏了配置，日志原文进包

`--export` 的用途就是"发给别人 / 贴给 AI 看"。原实现配置做了掩码，`logs/` 里
三个日志尾部**原文照抄** —— 里面有完整直播地址（房间号）、出错时夹带的 cookie/key。
做了半套脱敏比不做更危险：它让人以为包是安全的。

改法：`_mask_log_text()` 按模式抹 key / 直播房间号 / Cookie 头 / 手机号，
打包时对每个日志尾部调用。注意那个正则分组的坑：域名要 `(douyin|bilibili)` 捕获、
房间号是 `\2`，写反会把房间号替换成域名（`live.990638173877.com`），第一次跑测试
就抓到了。

## 五、R-3 非交互标记从来没人设置

`check_env` 里读 `DMR_NONINTERACTIVE`，全仓却没有任何地方设置它 —— 那道"非交互
就别等回车"的护栏从未生效。现在 `run_foreground()`（session 0 兜底启动，
确定没人能按键）显式置位；登录自启那条路是可见控制台，保留交互提示。

## 六、判定不动的两项

- **D-1**（biliwebapi 部分成功后整组重传、按 filename 去重）：改的是上传主链路，
  没有真实开播就无法验证"部分成功"的分支；现有逻辑虽然浪费流量，但**不会丢片**。
  盲改的风险是重复投稿或漏传，收益不匹配。
- **D-7**（迁移误改 title 等非路径字符串）：只在跑路径迁移时触发，生产当前不需要
  迁移；改错了会污染历史文件名。

## 七、失效的项

- **D-8**（`build_dir --out-dir` 不脱敏）：`tools/build_dir.py` 已不存在。

## 八、验证

- 全量 **620 tests OK**（600 → 620，新增 20 条；L-2/R-7/S-1/S-4 各有"改回旧代码
  就红"的用例）
- 实跑 `python tools/doctor.py --export`：包内 3 个日志尾部，`live.*.com/<房间号>`
  扫描命中 **0**，替换标记确认生效
- `_retriable_requests` 落盘路径在单测里被基座指向临时目录（`unit_tests/support.py`），
  不读也不写生产 `.temp`
