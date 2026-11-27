# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""给固定版本的 micam PPPP 连接补 RDT（file-transfer）channel 1。

上游只开 channel 0（命令）和 channel 2（媒体），SD 卡录像的目录与文件
都在 channel 1 上，收包会被 onData 直接丢掉。这个补丁在构建期把
channel 1 接成第三条有序 DataChannel，并暴露 sendRDT/nextRDT；
channel 0/2 的原有行为一字不改。

与 stream_bootstrap_patch.py 一样：只改已校验 SHA256 的固定源码快照，
生成物不进 Git。
"""

from pathlib import Path

_MARKER = 'MIJIADECK_RDT_CHANNEL'


def patch_pppp_connection(path: Path) -> None:
    text = path.read_text(encoding='utf-8')
    if _MARKER in text:
        return
    replacements = [
        ("""    /** Inbound media. */
    public static final int CHANNEL_MEDIA = 2;
""",
         """    /** Inbound media. */
    public static final int CHANNEL_MEDIA = 2;
    /** MIJIADECK_RDT_CHANNEL: SD-card catalogue / file transfer. */
    public static final int CHANNEL_RDT = 1;
"""),
        ("""    private final DataChannel commandChannel = new DataChannel(CHANNEL_COMMAND);
    private final DataChannel mediaChannel = new DataChannel(CHANNEL_MEDIA);
    private final BlockingQueue<byte[]> commands = new LinkedBlockingQueue<>();
    private final BlockingQueue<byte[]> media = new LinkedBlockingQueue<>();
""",
         """    private final DataChannel commandChannel = new DataChannel(CHANNEL_COMMAND);
    private final DataChannel mediaChannel = new DataChannel(CHANNEL_MEDIA);
    private final DataChannel rdtChannel = new DataChannel(CHANNEL_RDT);
    private final BlockingQueue<byte[]> commands = new LinkedBlockingQueue<>();
    private final BlockingQueue<byte[]> media = new LinkedBlockingQueue<>();
    private final BlockingQueue<byte[]> rdt = new LinkedBlockingQueue<>();
"""),
        ("""    private int commandSeq;
""",
         """    private int commandSeq;
    private int rdtSeq;
"""),
        ("""    public void sendRaw(byte[] datagram) {
""",
         """    /** MIJIADECK_RDT_CHANNEL: one encrypted RDT message, no 0x1001 envelope. */
    public synchronized void sendRDT(byte[] body) {
        byte[] payload = new byte[8 + body.length];
        payload[0] = (byte) DRW_MAGIC;
        payload[1] = (byte) CHANNEL_RDT;
        payload[2] = (byte) (rdtSeq >>> 8);
        payload[3] = (byte) rdtSeq;
        writeInt(payload, 4, body.length);
        System.arraycopy(body, 0, payload, 8, body.length);
        rdtSeq = (rdtSeq + 1) & 0xFFFF;
        sendRaw(Frame.encode(Msg.DRW, payload));
    }

    public void sendRaw(byte[] datagram) {
"""),
        ("""    /** Next media message, or null on timeout. */
    public byte[] nextMedia(Duration timeout) {
        return take(media, timeout);
    }
""",
         """    /** Next media message, or null on timeout. */
    public byte[] nextMedia(Duration timeout) {
        return take(media, timeout);
    }

    /** Next reassembled RDT chunk, or null on timeout. */
    public byte[] nextRDT(Duration timeout) {
        return take(rdt, timeout);
    }
"""),
        ("""        DataChannel target = switch (channel) {
            case CHANNEL_COMMAND -> commandChannel;
            case CHANNEL_MEDIA -> mediaChannel;
            default -> null;
        };
        if (target == null) {
            return;
        }
        target.push(seq, data);
        BlockingQueue<byte[]> queue = channel == CHANNEL_COMMAND ? commands : media;
""",
         """        DataChannel target = switch (channel) {
            case CHANNEL_COMMAND -> commandChannel;
            case CHANNEL_MEDIA -> mediaChannel;
            case CHANNEL_RDT -> rdtChannel;
            default -> null;
        };
        if (target == null) {
            return;
        }
        target.push(seq, data);
        BlockingQueue<byte[]> queue = channel == CHANNEL_COMMAND ? commands
                : channel == CHANNEL_MEDIA ? media : rdt;
"""),
    ]
    for old, new in replacements:
        if old not in text:
            raise RuntimeError('rdt channel patch: upstream source shape changed: '
                               + old.splitlines()[0][:60])
        text = text.replace(old, new, 1)
    path.write_text(text, encoding='utf-8')
