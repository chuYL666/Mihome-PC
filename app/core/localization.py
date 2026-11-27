# SPDX-License-Identifier: GPL-3.0-or-later
# MijiaDesk: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MijiaDesk contributors
"""设备中文名本地化：型号 / 品类 → 中文显示名。

米家设备列表接口对「用户没改过名」的设备返回的是**英文产品名**
（"Mijia DD Combination Washer Dryer 10kg"、"Mi Smart Clock 4inch"），
而米家 APP 显示的是产品库里的中文商品名。

旧版靠抓 home.miot-spec.com 产品页补中文名，该站早已不可用：请求经常
拖满 10 秒超时，长期占着网络线程的 TLS 连接，是「点开设备卡片崩溃」
的放大器之一，因此已停用（见 service.product_page_name）。

这里改为**完全本地**的两级兜底，零网络、零阻塞：
1. MODEL_NAMES_ZH —— 常见型号的精确中文商品名，最准；
2. 英文品类名 → 中文品类（"Air Conditioner" → 空调），保底不出英文。

判定很保守：只替换看起来是「产品默认英文名」的，用户自己起的名字
（哪怕纯字母，如 "chu"）一律不动。
"""

from __future__ import annotations

# 型号（小写）→ 中文商品名。以国内在售机型为主，新机型靠品类兜底。
MODEL_NAMES_ZH: dict[str, str] = {
    # 空调 / 温控
    "xiaomi.airc.h64h00": "米家空调 1.5 匹",
    "xiaomi.airc.r34h00": "米家空调 1.5 匹",
    "lumi.acpartner.mcn02": "米家空调伴侣",
    "miir.aircondition.ir02": "米家空调伴侣",
    # 音箱
    "xiaomi.wifispeaker.lx04": "小爱触屏音箱",
    "xiaomi.wifispeaker.lx06": "小爱音箱 Pro",
    "xiaomi.wifispeaker.m01a": "小米蓝牙音箱 mini",
    "xiaomi.wifispeaker.lx05": "小爱音箱 Play",
    # 摄像头
    "chuangmi.camera.039a01": "小米智能摄像机 2 云台版",
    "chuangmi.camera.ip029a": "小米智能摄像机 云台版",
    "isa.camera.hlmax": "小米智能摄像机 双摄版",
    "chuangmi.camera.021a04": "小米智能摄像机 标准版",
    # 洗护
    "mibx5.washer.v7": "米家直驱洗烘一体机 10kg",
    "mibx2.washer.t20": "米家洗衣机 Pro 10kg",
    "mibx4.washer.v1": "米家洗衣机 10kg",
    # 环境电器
    "zhimi.heater.za1": "米家电暖器",
    "dmaker.fan.p28": "米家循环扇",
    "dmaker.fan.p33": "米家直流变频落地扇",
    "zhimi.airpurifier.mb4": "米家空气净化器 4",
    "zhimi.humidifier.ca4": "米家智能加湿器 4",
    # 厨房
    "chunmi.cooker.cmwy3": "米家电饭煲",
    "chunmi.induction.cmwy1": "米家电磁炉",
    "viomi.waterheater.u12": "米家热水器",
    # 健康
    "yunmai.scales.ms103": "米家体脂秤 S400",
    "soocare.toothbrush.m1s": "素士声波电动牙刷",
    "zdeer.ajh.a9": "左点艾灸器",
    "janshi.magic_touch.g2": "按摩器",
    "miaomiaoce.sensor_ht.t2": "米家温湿度计 2",
    # 插座 / 开关
    "cuco.plug.v3": "米家智能插座 3",
    "chuangmi.plug.m3": "米家智能插座",
    "chuangmi.plug.212a01": "米家智能插座",
    "yutai.plug.socn1": "米家智能插座",
    "lumi.plug.mmeu01": "米家智能插座",
    # 路由 / 网络
    "xiaomi.router.ra50": "Redmi 路由器 AX5",
    "xiaomi.router.r3p": "小米路由器 Pro",
    "xiaomi.router.ra54": "Redmi 路由器 AX6000",
    # 灯
    "yeelink.light.bslamp2": "米家床头灯",
    "yeelink.light.mono1": "米家 LED 灯泡",
    "yeelink.light.ceiling1": "米家 LED 吸顶灯",
    # 其他
    "xiaomi.waterpuri.lx20": "米家净水器 1200G Pro",
    "xiaomi.waterpuri.lx41": "米家净水器 1200G",
    "jiqid.mistory.v1": "米兔故事机",
}

# spec_type 里的品类（urn:miot-spec-v2:device:<category>:...）→ 中文
CATEGORY_NAMES_ZH: dict[str, str] = {
    "air-conditioner": "空调",
    "air-fryer": "空气炸锅",
    "air-purifier": "空气净化器",
    "bath-heater": "浴霸",
    "camera": "智能摄像机",
    "ceiling-fan": "吊扇",
    "ceiling-light": "吸顶灯",
    "coffee-machine": "咖啡机",
    "cooker": "电饭煲",
    "curtain": "窗帘",
    "dishwasher": "洗碗机",
    "door-lock": "智能门锁",
    "door-sensor": "门窗传感器",
    "electric-kettle": "电水壶",
    "fan": "电风扇",
    "gateway": "网关",
    "heater": "电暖器",
    "humidifier": "加湿器",
    "humidity-sensor": "湿度传感器",
    "induction-cooker": "电磁炉",
    "kettle": "电水壶",
    "light": "灯",
    "microwave-oven": "微波炉",
    "motion-sensor": "人体传感器",
    "moxibustion": "艾灸器",
    "outlet": "智能插座",
    "oven": "烤箱",
    "pet-feeder": "宠物喂食器",
    "plug": "智能插座",
    "range-hood": "抽油烟机",
    "refrigerator": "冰箱",
    "robot-cleaner": "扫地机器人",
    "router": "路由器",
    "scale": "体脂秤",
    "sleep-sensor": "睡眠传感器",
    "smoke-sensor": "烟雾传感器",
    "speaker": "智能音箱",
    "story-machine": "故事机",
    "switch": "开关",
    "television": "电视",
    "temperature-humidity-sensor": "温湿度计",
    "thermostat": "温控器",
    "toothbrush": "电动牙刷",
    "vacuum": "扫地机器人",
    "washer": "洗衣机",
    "washer-dryer": "洗烘一体机",
    "water-heater": "热水器",
    "water-purifier": "净水器",
}

# 出现在产品默认英文名里的品牌 / 产品词：命中就说明这个名字是出厂默认名
_BRAND_HINTS = (
    "mijia", "xiaomi", "redmi", "mi smart", "mi ", " aqara", "yeelink",
    "chuangmi", "zhimi", "dmaker", "lumi", "cuco", "isa ", "yunmai",
    "miaomiaoce", "mibx", "chunmi", "jiqid", "soocare", "zdeer", "yutai",
    "janshi", "miir", "roborock", "scishare", "viomi", "deerma", "morphy",
)


def has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in str(text or ""))


def spec_category(spec_type: str) -> str:
    """urn:miot-spec-v2:device:air-conditioner:0000A004:xxx:2 → air-conditioner"""
    parts = str(spec_type or "").split(":")
    if len(parts) > 3 and parts[2] == "device":
        return parts[3]
    return ""


def looks_like_default_name(name: str) -> bool:
    """像不像出厂默认英文名。

    多词名（含空格）或带品牌词才认为可以替换；单词短名（用户自己起的
    "chu"、"NAS" 等）保持原样，避免把用户命名覆盖掉。
    """
    text = str(name or "").strip()
    if not text:
        return False
    if " " in text:
        return True
    lowered = text.lower().replace("_", " ")
    return any(hint.strip() and hint.strip() in lowered for hint in _BRAND_HINTS)


def localize_name(name: str, model: str, spec_type: str) -> str | None:
    """给出中文显示名；无需替换时返回 None。

    已是中文（用户改名或国内默认名）不动；纯英文时先确认它看起来像
    出厂默认名（多词 / 带品牌词），再查精确型号表、最后按品类兜底。
    中间的「像默认名」判断很关键：用户把路由器改名成 "chu" 这种单词
    短名时，型号虽然能在表里查到，也不能覆盖用户自己的命名。
    """
    text = str(name or "").strip()
    if not text or has_cjk(text):
        return None
    if not looks_like_default_name(text):
        return None
    exact = MODEL_NAMES_ZH.get(str(model or "").strip().lower())
    if exact and exact != text:
        return exact
    category = CATEGORY_NAMES_ZH.get(spec_category(spec_type))
    return category if category and category != text else None
