# MijiaDeck v1.1.1

本次更新修复摄像头预览的状态读取阻塞，并新增设备诊断报告，方便用户提供有效的兼容性反馈，帮助逐步完善不同设备型号和使用场景的支持。

## 修复摄像头预览阻塞

- 修复部分在线摄像头因无法读取开关状态，一直停留在“暂时无法读取摄像头状态，点击画面重试”，未能继续申请视频连接的问题。
- 开关状态不可用时，播放页会继续尝试视频授权与连接，并隐藏无效的开关按钮；已明确处于休眠状态的设备仍遵循原有处理。

这次修复解决的是程序进入视频连接前的阻塞，不代表所有摄像头型号已完成适配。感谢 Issue #1 和 #2 的用户反馈；此前无法观看的用户请更新后重试，并提供新的诊断报告，帮助确认实际协议和连接结果。

## 新增设备诊断报告

- 主界面右上角菜单增加“设备诊断报告”，可选择账号中的设备；摄像头和灯光控制页也提供直接入口。
- 可预览并一键复制报告到 GitHub Issue，包含设备型号、可获得的固件版本、属性／动作／通知／事件定义；摄像头额外记录本次连接阶段、协议供应商编号及错误信息。
- 设备声明的能力与实际连接结果分开记录，为后续完善摄像头、门铃、音箱、人体传感器等设备的支持提供依据，不将尚未验证的能力标记为已支持。
- 报告排除账号凭证、设备标识、密钥、设备名称、家庭和房间、媒体地址及画面；由用户自行复制和提交，不会自动上传。

**如何反馈：**先复现问题 → 打开“诊断报告” → 复制到 [Issue](https://github.com/Songmanual/MijiaDeck/issues) → 补充操作步骤、实际现象和米家 APP 中是否正常。[详细说明](https://github.com/Songmanual/MijiaDeck/blob/main/docs/DEVICE-DIAGNOSTICS.md)

## 下载与升级

下载 **MijiaDeck-v1.1.1-windows-x64.zip**，完整解压后运行 `MijiaDeck.exe`，无需安装 Python 或 Java。支持 Windows 10 1903+ / Windows 11 x64。

升级前请从托盘菜单退出旧版，再完整解压并覆盖原目录，保留 `_internal` 文件夹。登录和个人设置保存在用户数据目录中，不包含在发布包里。

## English

- Fixed camera previews getting stuck before video authorization when the camera's power state could not be read. Online cameras can now attempt video connection independently of an unavailable power property.
- Added per-device diagnostic reports with public MIoT capability definitions and observed camera connection stages/error codes. Reports can be reviewed and copied into GitHub Issues to help investigate compatibility and future device support.
- Reports exclude credentials, device IDs, cryptographic material, device/home/room names, media URLs and images. Nothing is uploaded automatically.

This fixes a connection-blocking issue; it does not claim that all camera models are compatible. Users affected by #1 or #2 are invited to retry and share diagnostic reports.

Windows 10 1903+ / Windows 11 x64 portable ZIP. Exit the running app before extracting the complete archive over an older version. Keep the `_internal` directory.

MijiaDeck is an independent, free and open-source project based on [MiHome-Windows](https://github.com/huanyuejue/MiHome-Windows), with camera transport based on [micam](https://github.com/eugeneRover/micam). It is not affiliated with Xiaomi or Mi Home.
