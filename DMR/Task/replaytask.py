import queue
import logging
import threading

from typing import Tuple
from .liveevents import LiveEvents
from ..utils import *


class ReplayTask():
    def __init__(self, taskname, config:dict, pipe:Tuple[queue.Queue, queue.Queue]):
        self.send_queue, self.recv_queue = pipe
        self.taskname = taskname
        self.config = config
        self.logger = logging.getLogger(__name__)
        self.event_class = LiveEvents(self.taskname, self.config)
        self._event_dict = {}
        self.stoped = True

    def add_event(self, event, trigger):
        # 2026-11-16 修（审查报告 L-6）：原来是
        #     if self._event_dict.get(event) != None:
        #         self._event_dict[event].append(trigger)
        #     self._event_dict[event] = [trigger]
        # 第二行**无条件覆盖**，append 的结果永远被丢掉，同一事件只能挂一个
        # 回调；更糟的是 start() 对 list 型 trigger 先逐个 add，最后又把 list
        # 本身 add 进来，于是监控循环里 func(msg) 会拿 list 当函数调 → TypeError。
        # 现在值全是单个方法才没暴露。
        self._event_dict.setdefault(event, []).append(trigger)

    def _pipeSend(self, msg:PipeMessage):
        if not msg.source.startswith('replay/'):
            msg.source = 'replay/' + msg.source
        self.send_queue.put(msg)

    def _pipeRecvMonitor(self):
        while self.stoped == False:
            msg:PipeMessage = self.recv_queue.get()
            try:
                # target 既可能是 `replay/<taskname>`，也可能是引擎解析后的裸任务名
                # （engine._resolve_target 两者都支持）。这里统一剥掉前缀再比较，
                # 否则裸任务名消息会入队后被**静默丢弃**（只留一行 info）。
                _target = msg.target or ''
                if _target.startswith('replay/'):
                    _target = _target[len('replay/'):]
                if _target == self.taskname:
                    event = msg.source +'/'+ msg.event
                    funcs = self._event_dict.get(event) or self._event_dict.get(msg.event)
                    if isinstance(funcs, list):
                        for func in funcs:
                            ret_msgs = func(msg)
                            if not ret_msgs:
                                continue
                            elif isinstance(ret_msgs, list):
                                for return_msg in ret_msgs:
                                    self._pipeSend(return_msg)
                            elif isinstance(ret_msgs, PipeMessage):
                                self._pipeSend(ret_msgs)
                            else:
                                self.logger.error(f'Event:{event} return an unknown type of message.')
                    else:
                        if self._event_dict.get('default') is None:
                            self.logger.info(f'Event:{event} is not registered at task:{self.taskname}.')
                        else:
                            for func in self._event_dict['default']:
                                ret_msgs = func(msg)
                                if not ret_msgs:
                                    continue
                                elif isinstance(ret_msgs, list):
                                    for return_msg in ret_msgs:
                                        self._pipeSend(return_msg)
                                elif isinstance(ret_msgs, PipeMessage):
                                    self._pipeSend(ret_msgs)
                                else:
                                    self.logger.error(f'Event:{event} return an unknown type of message.')
                    
                    if msg.event == 'exit':
                        self.logger.debug(f'Task:{self.taskname} recieved exit message.')
                        self.stop()
                        break
            except Exception as e:
                self.logger.error(f'Message:{msg} raise an error: {e}')
                self.logger.exception(e)

    def start(self):
        self.stoped = False
        # 重复 start 会把回调挂两遍（同一事件触发两次渲染/上传），先清空。
        self._event_dict.clear()
        self._piperecvprocess = threading.Thread(target=self._pipeRecvMonitor, daemon=True)
        self._piperecvprocess.start()

        for event, trigger in self.event_class.event_dict.items():
            if isinstance(trigger, (list, tuple, set)):
                for func in trigger:
                    self.add_event(event, func)
            else:
                # 2026-11-16 修（L-6）：原来这里无 else，list 型会把整个
                # list 也 add 进来（见 add_event 注释）。
                self.add_event(event, trigger)

    def stop(self):
        self.stoped = True
        self._event_dict.clear()