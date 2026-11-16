from tools import check_pypi
check_pypi()

import time
import argparse
from datetime import datetime
import os
import sys
import logging
import logging.handlers
from os.path import exists, splitext
from glob import glob

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.append('./tools')

from DMR.utils import *
from DMR import DanmakuRepo
from DMR.Config import Config

#: 测试期间使用的分段时间（秒）。必须小于下方的运行时长（180 秒），
#: 否则一个分段都录不完。
DRYRUN_SEGMENT = 30

#: 做法是把各上传目标的 ``min_length`` 抬到测试分段永远达不到的高度：上传流程本身
#: 仍会完整走一遍（状态登记、上传组归属），但不会有视频被提交到真实账号。
#:
#: 为什么需要这道闸门：dryrun 用的是**你当前的配置文件**，其中的账号真实可用。
#: 早期版本把 ``min_length`` 设为 ``0``，于是测试片段被真实上传到了 B 站账号。
#:
#: 2026-11-16：这个常量曾被改回 0（注释与代码互相矛盾），等于闸门全开。
#: 现在固定为 10 亿秒，并且**只**允许用 ``--allow-upload`` 显式放行，避免
#: "临时改一行测上传"变成长期状态。
DRYRUN_MIN_LENGTH = 10 ** 9

#: dryrun 独占的状态库 / 录制目录。绝不能与生产共用：
#: 两边同时写同一份状态库会互相覆盖产物状态，写同一个录制目录会生成
#: ``[正在录制]`` 重名分段。
DRYRUN_STATE_DB = 'logs/dmr_state_dryrun.db'
DRYRUN_RECORD_DIR = './直播回放-dryrun'

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='configs/global.yml',
                        help='全局配置；dryrun 会按它加载当前的全部任务配置')
    parser.add_argument('--debug', action='store_true')
    parser.add_argument('--allow-upload', action='store_true',
                        help='⚠️ 关闭上传闸门，测试片段会被真实上传到账号（默认禁止）')
    args = parser.parse_args()

    # ── 单实例：dryrun 用独立互斥体 ────────────────────────────────
    # 不能用生产那把锁（会直接判 busy 起不来），但也不能不加锁：
    # 两个 dryrun 同时跑会互相抢同一个测试状态库。
    try:
        from DMR.utils.instance_lock import acquire as _acquire
        if _acquire(r'Global\DanmakuRepo_DryRun') == 'busy':
            print('已有 dryrun 在运行，退出。')
            sys.exit(1)
    except Exception as _e:                                      # noqa: BLE001
        print(f'单实例锁不可用（继续启动）: {_e}')

    # 按当前配置启动：加载 --config 里 config_path 所指的**全部任务**，
    # 以便在真实配置下做冒烟验证（而不是另造一份测试配置）。
    config = Config(args.config)

    # ── 与生产隔离 ──────────────────────────────────────────────────
    # 状态库：独立的库文件，绝不碰 logs/dmr_state.db。
    config.global_config.setdefault('state_args', {})
    config.global_config['state_args']['db_path'] = DRYRUN_STATE_DB

    min_length = 0 if args.allow_upload else DRYRUN_MIN_LENGTH
    if args.allow_upload:
        print('⚠️ --allow-upload 已开启：测试片段会被真实提交到上传目标！')

    for name, rep_conf in config.replay_config.items():
        config.replay_config[name]['download_args']['segment'] = DRYRUN_SEGMENT
        # 录制目录：改到 直播回放-dryrun/原视频/<主播>，与生产完全分开。
        # 成品目录不用单独改：dmrender 走 paths.danmaku_dir_for()，
        # 会按源路径的 "原视频" 父目录推导出 直播回放-dryrun/弹幕版/<主播>。
        old_dir = config.replay_config[name]['download_args'].get('output_dir')
        streamer = os.path.basename(os.path.normpath(old_dir)) if old_dir else name
        config.replay_config[name]['download_args']['output_dir'] = \
            os.path.join(DRYRUN_RECORD_DIR, '原视频', streamer)
        for upd_type, upd_configs in rep_conf.get('upload_args', {}).items():
            for upid, upd_conf in enumerate(upd_configs):
                # 延迟发布：即使真的触发了上传，稿件也不会立即公开
                config.replay_config[name]['upload_args'][upd_type][upid]['dtime'] = 86400
                # 见 DRYRUN_MIN_LENGTH：阻止测试片段被真实上传
                config.replay_config[name]['upload_args'][upd_type][upid]['min_length'] = min_length

    logger = logging.getLogger('DMR')
    logger.setLevel(logging.DEBUG)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO) 
    console_handler.setFormatter(logging.Formatter("[%(asctime)s][%(levelname)s]: %(message)s"))
    
    os.makedirs('logs', exist_ok=True)
    log_file = f'logs/DMR-dryrun-{datetime.now().strftime("%Y%m%d")}.log'
    if exists(log_file):
        _cnt = len(glob(splitext(log_file)[0] + '*'))
        log_file = splitext(log_file)[0] + f'({_cnt})' + splitext(log_file)[1]
    file_handler = logging.handlers.TimedRotatingFileHandler(log_file, when='D', interval=1, backupCount=0, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("[%(asctime)s][%(module)s][%(levelname)s]: %(message)s"))
    
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)

    dmr = DanmakuRepo(config, debug=args.debug)
    # dryrun 不跑维护任务：maintenance 会扫真实录制目录做磁盘水位/紧急清理、
    # 重建首页索引、轮转生产日志，这些都与冒烟测试无关，且会动生产数据。
    dmr.maintenance = None

    logger.info('正在启动测试...')
    dmr.start()

    time.sleep(180)
    for taskname, task in dmr.engine.task_dict.items():
        msg = PipeMessage(
            source='dryrun',
            target='downloader',
            event='stoptask',
            dtype='str',
            data=taskname,
        )
        dmr.engine.pipeSend(msg)
    
    # logging.info('录制完成，请检查录制文件')
    try:
        while 1:
            time.sleep(60)
    except KeyboardInterrupt:
        dmr.stop()
        exit(0)
