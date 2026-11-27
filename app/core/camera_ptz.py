# SPDX-License-Identifier: GPL-3.0-or-later
"""云台能力判定与方向编码。

MISS 固件通过 0x112 (MOTOR_REQ) 控制电机，但不同机型接受的 JSON 不一样：

* 固定步长机型：``{"operation": 1..4}``，发一次走一小步，不需要停止命令。
  米家 App 在画面上拖动时用的就是这一套。
* 连续移动机型（大方等）：``{"direction": "left|right|up|down", "speed": 1..10}``，
  松开方向键必须再发一条停止命令，否则电机会一直转。

之前代码把云台能力写死成 ``chuangmi.camera.029a02`` 一个型号，其它云台机
（039a01 / ip029a / hlmax 等）即便固件支持电机，界面也不给任何操作入口。
这里按型号给出能力，默认对不认识的摄像头按「固定步长」开放（保守但可用），
只对明确没有电机的机型关闭。
"""
from __future__ import annotations

# 与 CameraCanvas._send_drag_step / MISS 0x112 operation 保持一致
LEFT = 1
RIGHT = 2
UP = 3
DOWN = 4

DIRECTION_NAMES = {
    LEFT: 'left',
    RIGHT: 'right',
    UP: 'up',
    DOWN: 'down',
}

STEP = 'step'
CONTINUOUS = 'continuous'
NONE = ''

# 明确使用连续移动（需要 stop）、且参数格式不同的机型
_CONTINUOUS_MODELS = {
    'isa.camera.df3',          # 大方 1080P 云台版
    'isa.camera.hlc6',         # 部分 HLC 系列
}

# 明确没有云台电机：固定枪机、门铃、猫眼、双摄里的枪机副镜头等
_NO_MOTOR_MODELS = {
    'loock.cateye.v02',
    'chuangmi.camera.046c04',   # C200（固定）
    'chuangmi.camera.72ac1',    # C300（固定）
    'isa.camera.isc5c1',        # 小方（固定镜头，部分固件带电机另说）
    'xiaomi.camera.c01a01',     # 需实机确认，先按固定处理
}

# 已知固定步长机型（列出以便诊断，行为与默认一致）
_STEP_MODELS = {
    'chuangmi.camera.029a02',
    'chuangmi.camera.026c05',
    'chuangmi.camera.039a01',   # 小米智能摄像机 2 云台版
    'chuangmi.camera.ip029a',   # 小米智能摄像机 云台版
    'isa.camera.hlmax',         # 双摄：只有云台镜头可转动
    'isa.camera.hlc8',
    'isa.camera.hlc8a',
    'isa.camera.500dh',
}


def capability(model: str | None, channel: int = 0) -> str:
    """返回该摄像头该镜头的云台能力：'step' / 'continuous' / ''。

    ``channel`` 为 1 表示双摄的枪机副镜头，固定不可转动。
    """
    model = (model or '').strip().lower()
    if not model or '.camera.' not in model:
        return NONE
    if channel and channel > 0:
        return NONE
    if model in _NO_MOTOR_MODELS:
        return NONE
    if model in _CONTINUOUS_MODELS:
        return CONTINUOUS
    return STEP


def motor_payload(direction: int, capability: str, speed: int = 5,
                  *, stop: bool = False) -> str:
    """生成 0x112 的 JSON 参数。

    固定步长机型只认 ``operation``；连续机型用 ``direction`` + ``speed``，
    松开按键时补一条 ``stop``。两种写法互不干扰，按能力择一发送。
    """
    if capability == CONTINUOUS:
        name = 'stop' if stop else DIRECTION_NAMES.get(direction, 'stop')
        return '{"direction":"%s","speed":%d}' % (name, 0 if stop else speed)
    return '{"operation":%d}' % int(direction)


def step_interval(capability: str) -> int:
    """按住方向键时的连发间隔（毫秒）。

    固定步长是「一步一停」，太快会让电机堵转；连续机型靠 2 秒租约，
    每 500ms 续发一次即可（参考米家 App / lalmax-nvr 的做法）。
    """
    return 500 if capability == CONTINUOUS else 420
