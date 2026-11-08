# 米家电脑版（MijiaDeck 定制版）

基于 [MijiaDeck](https://github.com/Songmanual/MijiaDeck) v1.1.2（GPL-3.0）的本地定制版，
补齐了原版在**摄像头画质、双摄切换、多路同屏、设备功能展示**上的缺口。

## 相对原版的改动

### 摄像头

| 问题 | 修复 |
| --- | --- |
| 画质最高只能到 360P | 画质是**会话级参数**，原实现只在旧会话里发命令，摄像头不会重新协商分辨率。现在切换画质会整段重连（重新授权 → 新中继 → 新会话）；启动档位白名单放开到 `0-3`（全分辨率），单摄像头页默认高清 |
| 双摄（isa.camera.hlmax）切到枪机仍是云台画面 | 写 `hl-camera-switch`（siid 6 / piid 21）后等待 1.8s、**回读校验**再重开会话；另提供「通道模式」，在 MISS 视频请求里带 `channel` 参数请求第二路视频 |
| 声音打不开 | 开启声音时中继会先停止视频流再以 `enableaudio=1` 重启（摄像头忽略运行中流上的音频开关）；UI 增加 3.5s 看门狗，没有 PCM 会明确提示「该摄像头未输出声音（可能只推 Opus 音频）」 |
| 摄像头拒收画质时界面看不出 | 中继在 `startVideo` 后读取设备应答，被拒时回报 `NOTICE QUALITY_REJECTED`，界面提示「摄像头不接受该画质」 |
| 多路同屏卡顿 | 摄像头墙每格独立 JVM + 完整会话鉴权，原先九路同时开会把 CPU/带宽打满。现在**最多 3 路并发**、错峰启动，连上一个补下一个，标题栏显示「已出画面 x / 排队 y」；JVM 参数也做了轻量化（`-Xmx80m -XX:TieredStopAtLevel=1 -XX:+UseSerialGC`） |

### 界面

- **九宫格摄像头墙**：主窗口右上角九宫格按钮，或「更多 → 摄像头墙（九宫格）」。每格独立预览，双击进单摄像头窗口。
- **设备详情面板**：任意设备卡片点击（或右键「设备功能 / 详情」）都会打开详情面板，按 MIoT 规格自动生成：
  - 可控功能：开关、枚举（下拉）、数值（步进器）
  - 设备动作：重启、PTZ 校准、格式化 SD 卡等
  - 设备信息：型号、设备 ID、家庭、固件
  摄像头窗口头部也新增「功能」按钮，查看夜视、移动侦测、SD 卡等非视频能力。
- **卡片拖拽排序**：按住卡片拖动（半透明缩略图跟随），放到目标卡片上交换位置，顺序写入 `card_order.json`，重启/刷新不丢。

## 运行

```powershell
# 1) 依赖
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .

# 2) 摄像头组件（仓库内已带 camera-relay.jar，只需补一个 Java 运行时）
#    把 Temurin JRE 21 解压到 tools/camera_relay/runtime
#    目录结构：tools/camera_relay/runtime/bin/java.exe

# 3) 启动
.\.venv\Scripts\pythonw.exe -X utf8 run.py --show-main
```

重新编译中继（可选，改过 `tools/camera_relay/CameraRelay.java` 或 `micam` 源码时）：

```powershell
javac -encoding UTF-8 -cp tools/camera_relay/camera-relay.jar `
      -sourcepath <micam 源码根> -d build/relay tools/camera_relay/CameraRelay.java
jar uf tools/camera_relay/camera-relay.jar -C build/relay .
```

## 说明

- `micam/`（CS2 传输实现）为第三方代码，未随仓库分发；中继 jar 为本地构建产物。
- 未包含 Java 运行时（`tools/camera_relay/runtime`），请按上面的步骤自备 Temurin 21。
- 本项目沿用上游 GPL-3.0 许可，详见 `LICENSE`。
