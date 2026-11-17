"""``LiveEvents`` 状态镜像到状态库的测试（P1 最后一项）。

P1 的目标是让进程重启后仍知道"哪些分段还没渲染/上传/清理"。``state_dict`` 仍是
运行时真相，这里验证的是它被**镜像**到状态库的结果。

用例不联网、不起线程：``LiveEvents`` 由 ``make_live_events`` 通过 ``__new__`` 构造，
状态库用内存库；需要文件时在临时目录造。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from types import SimpleNamespace

from DMR.state import (
    ArtifactKind,
    ArtifactState,
    StateRepository,
    make_artifact_key,
)
from unit_tests.support import DMRTestCase


class _SyncTestCase(DMRTestCase):
    """共享：内存状态库 + 已注入状态库的 LiveEvents。"""

    def setUp(self) -> None:
        super().setUp()
        self.repo = StateRepository(":memory:").open()
        self.addCleanup(self.repo.close)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmpdir = self._tmp.name

    def make_events(self, *, with_repo: bool = True, **kwargs):
        events = self.make_live_events(**kwargs)
        if with_repo:
            events.state_repo = self.repo
        return events

    def path(self, name: str) -> str:
        return os.path.join(self.tmpdir, name)

    def artifact(self, group_id, kind, segment_idx):
        return self.repo.get_artifact(make_artifact_key(group_id, kind, segment_idx))

    @staticmethod
    def make_video(path, *, segment_id=1, group_id="g1", duration=600.0,
                   dm_file_id=None, upload_group_id=None):
        info = SimpleNamespace(
            path=path, segment_id=segment_id, group_id=group_id, duration=duration,
            dm_file_id=dm_file_id, size=1024, resolution=(1920, 1080),
            title="标题", streamer=None, ctime=None,
        )
        if upload_group_id is not None:
            info.upload_group_id = upload_group_id
        return info


class TestSessionEnsure(_SyncTestCase):
    """场次按需补建（渲染/上传可能先于录制登记发生）。"""

    def test_session_is_created_on_demand(self):
        events = self.make_events()

        self.assertTrue(events._ensure_session("g1"))

        session = self.repo.get_session("g1")
        self.assertIsNotNone(session)
        self.assertEqual(session.taskname, events.name)

    def test_existing_session_is_not_overwritten(self):
        """录制侧已登记的元信息（主播名等）必须保留。"""
        self.repo.create_session("g1", "othertask", streamer_name="主播甲")
        events = self.make_events()

        events._ensure_session("g1")

        session = self.repo.get_session("g1")
        self.assertEqual(session.taskname, "othertask")
        self.assertEqual(session.streamer_name, "主播甲")

    def test_empty_group_id_is_rejected(self):
        events = self.make_events()
        self.assertFalse(events._ensure_session(""))

    def test_without_repo_returns_false(self):
        events = self.make_events(with_repo=False)
        self.assertFalse(events._ensure_session("g1"))


class TestSyncArtifact(_SyncTestCase):
    """产物状态镜像。"""

    def test_creates_artifact_when_missing(self):
        events = self.make_events()

        events._sync_artifact("g1", "src_video", 1, path="seg1.flv",
                              state=ArtifactState.RECORDED)

        artifact = self.artifact("g1", ArtifactKind.SOURCE, 1)
        self.assertIsNotNone(artifact)
        self.assertEqual(artifact.state, ArtifactState.RECORDED)
        self.assertEqual(artifact.path, "seg1.flv")

    def test_updates_state_without_touching_path(self):
        """只改状态时不得丢掉已记录的路径（载荷合并语义）。"""
        events = self.make_events()
        events._sync_artifact("g1", "src_video", 1, path="seg1.flv",
                              state=ArtifactState.RECORDED)

        events._sync_artifact("g1", "src_video", 1,
                              state=ArtifactState.UPLOADED)

        artifact = self.artifact("g1", ArtifactKind.SOURCE, 1)
        self.assertEqual(artifact.state, ArtifactState.UPLOADED)
        self.assertEqual(artifact.path, "seg1.flv")

    def test_none_state_does_not_write(self):
        """``state=None`` 且内存状态为 None 时不写入 —— 不能把 RECORDED 覆盖成"未处理"。"""
        events = self.make_events()
        events._sync_artifact("g1", "src_video", 1, path="seg1.flv",
                              state=ArtifactState.RECORDED)
        events.state_dict["g1"] = [self.blank_state()]   # 三个槽位状态均为 None

        events._sync_artifact("g1", "src_video", 1)

        self.assertEqual(self.artifact("g1", ArtifactKind.SOURCE, 1).state,
                         ArtifactState.RECORDED, "不得被 None 覆盖")

    def test_state_derived_from_memory(self):
        """未显式给出状态时，应能从内存状态推导。"""
        events = self.make_events()
        state = self.blank_state()
        state["src_video"].update({"status": "uploading",
                                   "file": self.make_video(self.path("seg1.flv"))})
        events.state_dict["g1"] = [state]

        events._sync_artifact("g1", "src_video", 1)

        artifact = self.artifact("g1", ArtifactKind.SOURCE, 1)
        self.assertEqual(artifact.state, ArtifactState.UPLOADING)
        self.assertEqual(artifact.path, self.path("seg1.flv"))

    def test_danmaku_attachment_is_recorded(self):
        events = self.make_events()
        video = self.make_video(self.path("seg1.flv"), dm_file_id="seg1.ass")

        events._sync_artifact("g1", "src_video", 1, path=video,
                              state=ArtifactState.RECORDED)

        artifact = self.artifact("g1", ArtifactKind.SOURCE, 1)
        self.assertEqual(artifact.path, self.path("seg1.flv"))
        self.assertEqual(artifact.danmaku_path, "seg1.ass")

    def test_unknown_vtype_is_ignored(self):
        events = self.make_events()
        events._sync_artifact("g1", "not_a_slot", 1, state=ArtifactState.RECORDED)
        self.assertEqual(self.repo.list_artifacts(), [])

    def test_without_repo_is_a_noop(self):
        events = self.make_events(with_repo=False)
        events._sync_artifact("g1", "src_video", 1, state=ArtifactState.RECORDED)

    def test_repository_error_does_not_propagate(self):
        """同步失败绝不能影响录制/上传主流程。"""
        events = self.make_events()

        class Broken:
            def create_session(self, *a, **kw):
                raise RuntimeError('db locked')

        events.state_repo = Broken()
        events._sync_artifact("g1", "src_video", 1, state=ArtifactState.RECORDED)

    def test_all_three_slots_map_to_distinct_kinds(self):
        events = self.make_events()
        for vtype, kind in (("src_video", ArtifactKind.SOURCE),
                            ("src_video_pre", ArtifactKind.SOURCE_PRE),
                            ("dm_video", ArtifactKind.DANMAKU_VIDEO)):
            events._sync_artifact("g1", vtype, 1, path=f"{vtype}.mp4",
                                  state=ArtifactState.RECORDED)

        kinds = {a.kind for a in self.repo.list_artifacts()}
        self.assertEqual(kinds, {ArtifactKind.SOURCE, ArtifactKind.SOURCE_PRE,
                                 ArtifactKind.DANMAKU_VIDEO})


class TestUploadFlowSync(_SyncTestCase):
    """上传流程：上传组登记 + 产物标为 UPLOADING/UPLOADED。"""

    def _events_with_upload(self, *, realtime=True):
        return self.make_events(
            common_event_args={"auto_render": False, "auto_upload": True,
                               "auto_clean": False, "auto_transcode": False},
            upload_args={"src_video": [{
                "engine": "biliwebapi", "target": "bilibili",
                "account": "main", "realtime": realtime, "min_length": 120,
            }]},
        )

    def test_realtime_upload_registers_group_and_marks_uploading(self):
        events = self._events_with_upload()
        state = self.blank_state()
        state["src_video"].update({
            "status": "ready",
            "file": self.make_video(self.path("seg1.flv"), segment_id=1),
        })
        events.state_dict["g1"] = [state]

        messages = events._check_for_upload("g1")

        self.assertEqual(len(messages), 1)
        group_key = messages[0].data["upload_group"]
        group = self.repo.get_upload_group(group_key)
        self.assertIsNotNone(group, "上传组应被登记，供上传器回写 bvid")
        self.assertEqual(group.target, "bilibili")
        self.assertEqual(group.account, "main")
        self.assertEqual(self.artifact("g1", ArtifactKind.SOURCE, 1).state,
                         ArtifactState.UPLOADING)

    def test_group_key_matches_message(self):
        """状态库里的上传组键必须与消息里的键完全一致。

        否则上传器回写的 bvid 会落到另一个键上，"同组进同一稿件"失效。
        """
        events = self._events_with_upload()
        state = self.blank_state()
        state["src_video"].update({
            "status": "ready",
            "file": self.make_video(self.path("seg1.flv"), upload_group_id="23333"),
        })
        events.state_dict["g1"] = [state]

        messages = events._check_for_upload("g1")
        group_key = messages[0].data["upload_group"]

        self.assertTrue(group_key.startswith("23333_"))
        self.assertIsNotNone(self.repo.get_upload_group(group_key))

    def test_non_realtime_marks_all_segments_uploading(self):
        events = self._events_with_upload(realtime=False)
        states = []
        for idx in (1, 2):
            state = self.blank_state()
            state["src_video"].update({
                "status": "ready",
                "file": self.make_video(self.path(f"seg{idx}.flv"),
                                        segment_id=idx),
            })
            states.append(state)
        events.state_dict["g1"] = states
        events.ended_dict["g1"] = 1

        messages = events._check_for_upload("g1")

        self.assertEqual(len(messages), 1)
        for idx in (1, 2):
            self.assertEqual(self.artifact("g1", ArtifactKind.SOURCE, idx).state,
                             ArtifactState.UPLOADING)

    def test_upload_end_marks_uploaded(self):
        events = self._events_with_upload()
        state = self.blank_state()
        state["src_video"].update({
            "status": "ready",
            "file": self.make_video(self.path("seg1.flv")),
        })
        events.state_dict["g1"] = [state]
        messages = events._check_for_upload("g1")
        request_id = messages[0].request_id

        from DMR.utils import PipeMessage
        events.onUploadEnd(PipeMessage(source='uploader', target='replay/testtask',
                                       event='end', request_id=request_id,
                                       data={}))

        self.assertEqual(self.artifact("g1", ArtifactKind.SOURCE, 1).state,
                         ArtifactState.UPLOADED)

    def test_upload_end_waits_for_all_requests(self):
        """同一产物有多个上传目标时，只有全部结束才算已上传。"""
        events = self.make_events(
            common_event_args={"auto_render": False, "auto_upload": True,
                               "auto_clean": False, "auto_transcode": False},
            upload_args={"src_video": [
                {"engine": "biliwebapi", "target": "bilibili", "account": "a",
                 "realtime": True, "min_length": 0},
                {"engine": "biliwebapi", "target": "bilibili", "account": "b",
                 "realtime": True, "min_length": 0},
            ]},
        )
        state = self.blank_state()
        state["src_video"].update({
            "status": "ready",
            "file": self.make_video(self.path("seg1.flv")),
        })
        events.state_dict["g1"] = [state]
        messages = events._check_for_upload("g1")
        self.assertEqual(len(messages), 2)

        from DMR.utils import PipeMessage
        events.onUploadEnd(PipeMessage(source='uploader', target='replay/testtask',
                                       event='end',
                                       request_id=messages[0].request_id, data={}))
        self.assertEqual(self.artifact("g1", ArtifactKind.SOURCE, 1).state,
                         ArtifactState.UPLOADING, "还有一个请求未结束")

        events.onUploadEnd(PipeMessage(source='uploader', target='replay/testtask',
                                       event='end',
                                       request_id=messages[1].request_id, data={}))
        self.assertEqual(self.artifact("g1", ArtifactKind.SOURCE, 1).state,
                         ArtifactState.UPLOADED)


class TestRenderAndCleanSync(_SyncTestCase):
    """渲染与清理完成后的镜像。"""

    def test_render_end_marks_rendered(self):
        events = self.make_events()
        state = self.blank_state()
        state["dm_video"].update({"status": "rendering",
                                  "file": None, "wait": ["req-1"]})
        events.state_dict["g1"] = [state]
        events._sync_artifact("g1", "dm_video", 1, state=ArtifactState.RENDERING)

        from DMR.utils import PipeMessage
        output = self.make_video(self.path("dm.mp4"))
        events.onRenderEnd(PipeMessage(source='render', target='replay/testtask',
                                       event='end', request_id="req-1",
                                       data={'output': output}))

        artifact = self.artifact("g1", ArtifactKind.DANMAKU_VIDEO, 1)
        self.assertEqual(artifact.state, ArtifactState.RENDERED)
        self.assertEqual(artifact.path, self.path("dm.mp4"))

    def test_clean_marks_cleaned_when_rule_matches(self):
        events = self.make_events(
            common_event_args={"auto_render": False, "auto_upload": True,
                               "auto_clean": True, "auto_transcode": False},
            clean_args={"src_video": [{"method": "delete", "delay": 0}]},
        )
        state = self.blank_state()
        state["src_video"].update({
            "status": "uploaded",
            "file": self.make_video(self.path("seg1.flv")),
        })
        events.state_dict["g1"] = [state]

        events._check_for_clean()

        self.assertEqual(self.artifact("g1", ArtifactKind.SOURCE, 1).state,
                         ArtifactState.CLEANED)

    def test_no_matching_rule_leaves_state_uploaded(self):
        """没有命中清理规则时，状态库里也必须保持 uploaded（B7.2）。"""
        events = self.make_events(
            common_event_args={"auto_render": False, "auto_upload": True,
                               "auto_clean": True, "auto_transcode": False},
            clean_args={"dm_video": [{"method": "delete", "delay": 0}]},
        )
        state = self.blank_state()
        state["src_video"].update({
            "status": "uploaded",
            "file": self.make_video(self.path("seg1.flv")),
        })
        events.state_dict["g1"] = [state]
        events._sync_artifact("g1", "src_video", 1, state=ArtifactState.UPLOADED)

        events._check_for_clean()

        self.assertEqual(self.artifact("g1", ArtifactKind.SOURCE, 1).state,
                         ArtifactState.UPLOADED)


class TestLiveSegmentSync(_SyncTestCase):
    """``onLiveSegment`` 也应把分段登记为 RECORDED（覆盖不经下载器的路径）。"""

    def test_segment_is_registered_on_arrival(self):
        events = self.make_events()
        video = self.make_video(self.path("seg1.flv"))

        from DMR.utils import PipeMessage
        events.onLiveSegment(PipeMessage(source='downloader',
                                        target='replay/testtask',
                                        event='livesegment', msg='ok',
                                        dtype='VideoInfo', data=video))

        artifact = self.artifact("g1", ArtifactKind.SOURCE, 1)
        self.assertIsNotNone(artifact)
        self.assertEqual(artifact.state, ArtifactState.RECORDED)
        self.assertEqual(artifact.path, self.path("seg1.flv"))


class TestTaskErrorPersistence(_SyncTestCase):
    """``onTaskError`` 必须把"失败"写库（2026-11-09 修的真 bug）。

    历史缺陷：它只把内存里的 status 改成 'failed'，**从不调用 _sync_artifact**，
    于是 ``_STATUS_TO_STATE['failed']`` 这条映射形同虚设 —— 全库 state='failed'
    记录数为 0 就是证据。实盘后果是渲染失败的产物在库里永远停在 'rendering'，
    下次重启被 reconciler 降级成 'recorded' 重跑一遍，反复循环且 attempts 恒 0。
    """

    def _running_dm_video(self, events, *, segment_id=1, group="g1"):
        """造一个"正在渲染"的分段：内存 rendering + 库里 RENDERING。"""
        output = self.path("seg1（弹幕版）.mp4")
        state = self.blank_state() if hasattr(self, "blank_state") else None
        if state is None:
            state = {"src_video": {}, "dm_video": {}}
        events.state_dict[group] = [{
            "src_video": {"status": "uploaded", "file": None, "wait": [],
                          "segment_id": segment_id},
            "dm_video": {"status": "rendering", "wait": ["rid-1"],
                         "file": SimpleNamespace(path=output),
                         "segment_id": segment_id},
        }]
        events._sync_artifact(group, "dm_video", segment_id,
                              path=output, state=ArtifactState.RENDERING)
        return output

    def _error_message(self, msg="渲染产物时长异常：源 134 秒 → 产物 95 秒"):
        from DMR.utils import PipeMessage
        return PipeMessage(source="render", target="replay/testtask",
                           event="error", msg=msg, request_id="rid-1", data=msg)

    def test_failed_state_is_written_to_db(self):
        """核心断言：失败后库里必须是 FAILED，而不是继续停在 RENDERING。"""
        events = self.make_events()
        self._running_dm_video(events)

        before = self.artifact("g1", ArtifactKind.DANMAKU_VIDEO, 1)
        self.assertEqual(before.state, ArtifactState.RENDERING)

        events.onTaskError(self._error_message())

        after = self.artifact("g1", ArtifactKind.DANMAKU_VIDEO, 1)
        self.assertEqual(after.state, ArtifactState.FAILED,
                         '失败状态未写库：重启后会被 reconciler 当成「渲染中」重跑')

    def test_error_reason_is_recorded(self):
        """last_error 必须落盘，否则下次重跑还是原地踩同一个坑。"""
        events = self.make_events()
        self._running_dm_video(events)

        events.onTaskError(self._error_message("产物仅 95 秒（71%）"))

        artifact = self.artifact("g1", ArtifactKind.DANMAKU_VIDEO, 1)
        payload = self.repo._raw_payload(artifact.artifact_id)
        self.assertIn("95 秒", payload.get("last_error") or "",
                      "last_error 没写进载荷：失败原因丢失")

    def test_attempts_are_bumped(self):
        """attempts 要能累加，否则"同一段反复失败"在库里看不出来。"""
        events = self.make_events()
        self._running_dm_video(events)

        events.onTaskError(self._error_message())
        first = self.repo._raw_payload(
            self.artifact("g1", ArtifactKind.DANMAKU_VIDEO, 1).artifact_id
        ).get("attempts")
        self.assertEqual(first, 1, "首次失败后 attempts 应为 1")

        # 再来一次（模拟重启后重新渲染又失败）
        events._sync_artifact("g1", "dm_video", 1, state=ArtifactState.RENDERING)
        events.state_dict["g1"][0]["dm_video"].update(
            {"status": "rendering", "wait": ["rid-2"]})
        from DMR.utils import PipeMessage
        events.onTaskError(PipeMessage(source="render", target="replay/testtask",
                                       event="error", msg="又失败",
                                       request_id="rid-2", data="又失败"))

        second = self.repo._raw_payload(
            self.artifact("g1", ArtifactKind.DANMAKU_VIDEO, 1).artifact_id
        ).get("attempts")
        self.assertEqual(second, 2, "attempts 未累加")

    def test_db_failure_does_not_break_clean_check(self):
        """写库出错不能让清理判定跟着崩 —— 否则回到 10-05 的活体泄漏。"""
        events = self.make_events(
            common_event_args={"auto_render": False, "auto_upload": False,
                               "auto_clean": True, "auto_transcode": False},
        )
        self._running_dm_video(events)

        class _Boom:
            def __getattr__(self, item):
                raise RuntimeError("状态库炸了")

        events.state_repo = _Boom()
        # 不抛异常即通过
        events.onTaskError(self._error_message())

    def test_no_repo_is_noop(self):
        """没注入状态库时（纯运行时模式）照样能跑，不报错。"""
        events = self.make_events(with_repo=False)
        self._running_dm_video(events)
        events.state_repo = None
        events.onTaskError(self._error_message())
        self.assertEqual(events.state_dict["g1"][0]["dm_video"]["status"],
                         "failed", "内存状态仍应被收敛为 failed")



class TestSettleExistingProduct(_SyncTestCase):
    """「成品已在磁盘、但库里没有记录」的收敛（2026-11-16 实测缺口）。

    背景：DMR 连续运行 7 天期间状态库一条 session 都没写进去，于是那几天
    渲染完、并且**已经成功上传**的成品在库里没有 dm_video 记录。原来
    ``_dispatch_pending_renders`` 见到成品已存在就 ``continue``，既不补记也不
    补传 —— 每次重启都重新判成"待恢复"，日志永远停在
    「发现 N 个待恢复产物 → 已重新派发 0 个任务」。
    """

    def _events(self, *, journal=None):
        events = self.make_events(
            common_event_args={"auto_render": True, "auto_upload": False,
                               "auto_clean": False, "auto_transcode": False},
            render_args={"dmrender": {"output_dir": self.tmpdir, "format": "mp4"}},
        )
        events.state_repo = self.repo
        events._pending_clean_path = self.path("pending_clean.json")
        if journal is not None:
            import json as _json
            with open(events._pending_clean_path, "w", encoding="utf-8") as f:
                _json.dump(journal, f, ensure_ascii=False)
        return events

    def _journal(self, src_name):
        return [{"id": "x", "due_ts": 0, "pairs": [[src_name, src_name[:-3] + ".ass"]]}]

    def test_confirmed_by_clean_journal_marks_uploaded(self):
        """待删清单里有这一场 → 上传早已确认，直接标 UPLOADED，不重传。"""
        src = self.path("主播-2026年11月15日17点18分.ts")
        product = self.path("主播-2026年11月15日17点18分（弹幕版）.mp4")
        open(product, "wb").close()
        events = self._events(journal=self._journal(os.path.basename(src)))

        events._settle_existing_product("g1", "dm_video", 0, product)

        artifact = self.artifact("g1", ArtifactKind.DANMAKU_VIDEO, 1)
        self.assertIsNotNone(artifact)
        self.assertEqual(artifact.state, ArtifactState.UPLOADED)

    def test_no_journal_entry_falls_back_to_rendered(self):
        """没有上传证据 → 只补记 RENDERED，交回上传流程（不假装已上传）。"""
        product = self.path("主播-2026年11月16日06点56分（弹幕版）.mp4")
        open(product, "wb").close()
        events = self._events(journal=[])

        events._settle_existing_product("g1", "dm_video", 0, product)

        artifact = self.artifact("g1", ArtifactKind.DANMAKU_VIDEO, 1)
        self.assertEqual(artifact.state, ArtifactState.RENDERED)

    def test_dispatch_records_existing_product_instead_of_silently_skipping(self):
        """集成：成品已存在时不得再派渲染，但必须把状态收敛掉。

        这条用例专门覆盖 2026-11-16 引入的 ``NameError: vtype`` ——
        ``_dispatch_pending_renders`` 里并没有 ``vtype`` 变量。
        """
        src = self.path("主播-2026年11月15日18点18分.ts")
        open(src, "wb").close()
        ass = src[:-3] + ".ass"
        open(ass, "wb").close()
        product = self.path("主播-2026年11月15日18点18分（弹幕版）.mp4")
        open(product, "wb").close()
        events = self._events(journal=self._journal(os.path.basename(src)))
        video = self.make_video(src, segment_id=1, group_id="g1", dm_file_id=ass)
        events.state_dict["g1"] = [{
            "src_video": {"status": "ready", "file": video, "wait": [], "segment_id": 1},
            "src_video_pre": {"status": None, "file": None, "wait": [], "segment_id": None},
            "dm_video": {"status": None, "file": None, "wait": [], "segment_id": None},
        }]

        messages = events._dispatch_pending_renders("g1")

        self.assertEqual(messages, [], "成品已存在时不应再派渲染任务")
        artifact = self.artifact("g1", ArtifactKind.DANMAKU_VIDEO, 1)
        self.assertIsNotNone(artifact, "必须补记 dm_video 产物，否则每次重启都重复'待恢复'")
        self.assertEqual(artifact.state, ArtifactState.UPLOADED)
        self.assertEqual(events.state_dict["g1"][0]["dm_video"]["status"], "uploaded")
        # 源槽位也必须收敛，否则它仍是 RECORDED，下次重启会再次被 resume 选中
        self.assertEqual(events.state_dict["g1"][0]["src_video"]["status"], "uploaded")
        self.assertEqual(self.artifact("g1", ArtifactKind.SOURCE, 1).state,
                         ArtifactState.UPLOADED)

    # ── 2026-11-16 第二轮复审：settle 的三处新问题 ──────────────────────

    def _events_upload(self, *, journal=None):
        """带 auto_upload 的实例 —— 正是当初被测试集体绕开的那条分支。"""
        events = self.make_events(
            common_event_args={"auto_render": True, "auto_upload": True,
                               "auto_clean": False, "auto_transcode": False},
            render_args={"dmrender": {"output_dir": self.tmpdir, "format": "mp4"}},
            upload_args={"dm_video": [{"engine": "biliwebapi", "realtime": True,
                                       "target": "bilibili", "account": "a",
                                       "min_length": 0}]},
        )
        events.state_repo = self.repo
        events._pending_clean_path = self.path("pending_clean.json")
        if journal is not None:
            import json as _json
            with open(events._pending_clean_path, "w", encoding="utf-8") as f:
                _json.dump(journal, f, ensure_ascii=False)
        return events

    def _blank_group(self, video):
        return [{
            "src_video": {"status": "ready", "file": video, "wait": [], "segment_id": 1},
            "src_video_pre": {"status": None, "file": None, "wait": [],
                              "segment_id": None},
            "dm_video": {"status": None, "file": None, "wait": [], "segment_id": None},
        }]

    def test_settled_slot_is_videoinfo_not_str(self):
        """★N-1：settle 写回槽位的必须是 VideoInfo，不能是路径字符串。

        原来写 str，同一轮的 ``_check_for_upload`` 会拿它去
        ``_passes_length_gate``，那里访问 ``video.path`` → AttributeError，
        异常被启动恢复循环吞掉 → **该任务整轮恢复中止**。
        """
        src = self.path("主播-2026年11月15日19点18分.ts")
        open(src, "wb").close()
        product = self.path("主播-2026年11月15日19点18分（弹幕版）.mp4")
        with open(product, "wb") as f:
            f.write(b"\0" * 2048)
        events = self._events_upload(journal=[])          # 无上传证据 → ready
        video = self.make_video(src, segment_id=1, group_id="g1")
        events.state_dict["g1"] = self._blank_group(video)

        events._settle_existing_product("g1", "dm_video", 0, product,
                                        source_video=video)

        slot = events.state_dict["g1"][0]["dm_video"]
        self.assertFalse(isinstance(slot["file"], str),
                         "槽位里塞字符串会让上传闸门 AttributeError")
        self.assertEqual(getattr(slot["file"], "path", None), product)
        self.assertEqual(slot["status"], "ready")

        # 真正的回归断言：走一遍上传检查，不许抛异常（闸门会因缺 moov 拒传）
        events._check_for_upload("g1")

    def test_skipped_artifact_is_not_faked_as_uploaded(self):
        """★N-7：库里是 SKIPPED（按规则跳过、从未上传）时不得标 uploaded。

        待删清单里既有"上传确认后排队"，也有"命中跳过规则、源照删成品留本地"。
        只凭文件名命中就判已上传，会让状态库永久显示假"已上传"，
        以后关掉跳过规则也不会补传。
        """
        src = self.path("主播-2026年11月15日20点18分.ts")
        open(src, "wb").close()
        product = self.path("主播-2026年11月15日20点18分（弹幕版）.mp4")
        open(product, "wb").close()
        key = make_artifact_key("g1", ArtifactKind.DANMAKU_VIDEO, 1)
        self.repo.create_session("g1", "test")   # 产物有外键，必须先有场次
        self.repo.upsert_artifact(key, "g1", 1, ArtifactKind.DANMAKU_VIDEO,
                                  ArtifactState.SKIPPED, path=product,
                                  last_error="skip_upload_rule: 横屏")
        events = self._events(journal=self._journal(os.path.basename(src)))
        video = self.make_video(src, segment_id=1, group_id="g1")
        events.state_dict["g1"] = self._blank_group(video)

        events._settle_existing_product("g1", "dm_video", 0, product,
                                        source_video=video)

        self.assertEqual(self.repo.get_artifact(key).state, ArtifactState.SKIPPED)
        self.assertEqual(events.state_dict["g1"][0]["dm_video"]["status"],
                         "upload_skipped")

    def test_product_ok_fails_open_when_probe_unavailable(self):
        """★N-2：ffprobe 不可用（没装/超时）绝不能判成品坏 —— 判坏会删文件。"""
        from unittest import mock

        events = self._events(journal=[])
        product = self.path("主播-2026年11月15日21点18分（弹幕版）.mp4")
        open(product, "wb").close()
        video = self.make_video(product)

        with mock.patch("DMR.Task.liveevents.FFprobe.probe_status",
                        return_value=("unavailable", "找不到 ffprobe")):
            self.assertTrue(events._dm_product_ok(video), "探测不可用必须放行")

        with mock.patch("DMR.Task.liveevents.FFprobe.probe_status",
                        return_value=("unreadable", "moov atom not found")):
            self.assertFalse(events._dm_product_ok(video),
                             "ffprobe 明确读不了才判坏")


class TestRetryJournal(_SyncTestCase):
    """复审 L-2（后半）：失败请求映射必须跨重启存活。

    背景：WebUI 的「重试失败任务」列表来自状态库/日志，进程重启后依然显示；
    插件重试时用的是**原来的 request_id**。映射只放内存的话，重启后用户点重试
    —— 状态停在 failed、成品不上传、源文件永不清理，而日志里看起来"重试成功"。
    """

    def setUp(self) -> None:
        super().setUp()
        self.journal = self.path('retry_requests.json')

    def _events(self):
        ev = self.make_events()
        ev._retry_journal = self.journal
        # 基座为避免污染生产 .temp 预置了空 dict；这里删掉，模拟"刚启动未恢复"
        try:
            del ev._retriable_requests
        except AttributeError:
            pass
        return ev

    def test_map_survives_restart(self):
        ev = self._events()
        ev._retriable_map()['rid-1'] = ('g1', 'dm_video', 2)
        ev._save_retriable()

        restarted = self._events()          # 新对象 = 新进程
        self.assertEqual(restarted._retriable_map().get('rid-1'),
                         ('g1', 'dm_video', 2))

    def test_popped_entry_is_removed_from_disk(self):
        ev = self._events()
        ev._retriable_map()['rid-2'] = ('g2', 'src_video', 0)
        ev._save_retriable()
        ev._retriable_map().pop('rid-2')
        ev._save_retriable()

        self.assertNotIn('rid-2', self._events()._retriable_map())

    def test_corrupt_journal_is_ignored(self):
        with open(self.journal, 'w', encoding='utf-8') as f:
            f.write('{ 这不是 json')
        self.assertEqual(self._events()._retriable_map(), {})

    def test_values_are_tuples_not_lists(self):
        """JSON 读回来是 list；调用方按 3 元组解包，类型要统一。"""
        ev = self._events()
        ev._retriable_map()['rid-3'] = ('g3', 'dm_video', 1)
        ev._save_retriable()
        self.assertIsInstance(self._events()._retriable_map()['rid-3'], tuple)


if __name__ == "__main__":
    unittest.main()
