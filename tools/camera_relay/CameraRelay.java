/* Local CS2 transport host: one JVM serves many camera sessions.
 *
 * Protocol (stdin, one command per line, TAB separated):
 *   OPEN <id> <port> <path> <uid> <init> <clientPub> <clientPrivHex>
 *        <devicePub> <sign> <quality> <channel>
 *   CMD  <id> <AUDIO 0|1 | QUALITY 0-3 | CHANNEL 0-3 | PTZ 1-4
 *            | TALK 0|1 | CODEC 1024|1026|1027|1032
 *            | MOTOR left|right|up|down|stop <speed> | AUD <b64>
 *            | INDEX <channel> | PLAY <start> <end> [channels...]
 *            | PLAYDIRECT <start> <end> [channels...] | LIVE
 *            | FILE <timestamp> <channel>>
 *   CLOSE <id>
 *   QUIT
 *
 * stdout (one message per line): <id> <message>
 *   PHASE RELAY|AUTH|MEDIA, READY, ERROR <CODE>, NOTICE ..., INFO ...,
 *   AUDIO 8000 <b64>, AUDIO_OPUS <b64>, RDT <cmd> <b64>, PLAYBACK <b64>, EXIT
 *
 * SD-card playback: the clip catalogue travels on RDT channel 1 (command 6),
 * the playback request is MISS 0x10D and its answer 0x10E; the camera then
 * streams the recording on the same media channel 2 as live video.
 *
 * Two-way talk: TALK 1 sends MISS 0x106 (SPEAKER_START_REQ), then AUD carries
 * 40 ms audio frames on DRW channel 3; TALK 0 sends 0x108 (SPEAKER_STOP).
 * The frame is the same layout as inbound channel 2: a 32-byte little-endian
 * plaintext header followed by a ChaCha20 payload with its 8-byte nonce tail.
 */
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Base64;
import java.util.HexFormat;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicBoolean;

import micam.miss.MediaPacket;
import micam.miss.MissClient;
import micam.miss.MissCrypto;
import micam.pppp.Frame;
import micam.pppp.InitString;
import micam.pppp.Msg;
import micam.pppp.PpppConnection;
import micam.pppp.RelaySession;
import micam.pppp.Uid;
import micam.server.HttpStreamServer;

public final class CameraRelay {
    private static final Map<String, Session> sessions = new ConcurrentHashMap<>();

    private static void report(String id, String message) {
        // One writer, otherwise interleaved JVM threads corrupt stdout framing.
        synchronized (System.out) {
            System.out.println(id + " " + message);
            System.out.flush();
        }
    }

    /** One camera: relay handshake, MISS auth, local TS fan-out. */
    static final class Session implements Runnable {
        private final String id;
        private final int port;
        private final String path;
        private final String uid;
        private final String init;
        private final String clientPublic;
        private final byte[] clientPrivate;
        private final String devicePublic;
        private final String sign;
        private volatile int quality;
        private volatile int channel;
        private volatile boolean audio;
        private volatile RelaySession relay;
        private volatile PpppConnection connection;
        private volatile MissClient camera;
        private volatile HttpStreamServer server;
        private final AtomicBoolean closed = new AtomicBoolean();
        private volatile int videoCodec;

        // ---- SD-card playback ----
        private volatile boolean playbackMode;
        private int playbackId = 1;
        // RDT is a byte stream: each encrypted chunk is one piece of a
        // <u32 LE cmd><u32 LE length><payload> message.
        private int rdtCmd = -1;
        private int rdtWant = -1;
        private final java.io.ByteArrayOutputStream rdtPayload =
                new java.io.ByteArrayOutputStream();

        // ---- two-way talk / speaker uplink ----
        private final byte[] mediaKey;
        private volatile int speakerCodec = 1027;   // PCMA by default
        private volatile boolean talking;
        private int audioSeq;
        private int channel3Seq;

        Session(String id, int port, String path, String uid, String init,
                String clientPublic, byte[] clientPrivate, String devicePublic,
                String sign, int quality, int channel) {
            this.id = id;
            this.port = port;
            this.path = path;
            this.uid = uid;
            this.init = init;
            this.clientPublic = clientPublic;
            this.clientPrivate = clientPrivate;
            this.devicePublic = devicePublic;
            this.sign = sign;
            this.quality = quality;
            this.channel = channel;
            this.mediaKey = MissCrypto.sessionKey(clientPrivate, devicePublic);
        }

        private void videoInfo(MediaPacket packet) {
            if (packet.codecId() != videoCodec) {
                videoCodec = packet.codecId();
                report(id, "INFO VIDEO_CODEC " + (videoCodec == 4 ? "H264" : "H265"));
            }
        }

        private void startVideo(int requested) {
            if (camera == null) return;
            camera.startVideo(requested, audio, channel);
            try {
                MissClient.Response ack = camera.awaitResponse(Duration.ofMillis(1500));
                if (ack != null && !ack.text().contains("\"result\":\"success\""))
                    report(id, "NOTICE QUALITY_REJECTED");
            } catch (Exception ignored) { }
        }

        /** Cameras ignore enableaudio/channel on a live stream, so it has to restart. */
        private void restartVideo() {
            if (camera == null) return;
            try { camera.stopVideo(); } catch (Exception ignored) { }
            try { Thread.sleep(300); } catch (InterruptedException ignored) { }
            startVideo(quality);
        }

        void command(String text) {
            try {
                if (text.matches("QUALITY [0123]")) {
                    quality = text.charAt(8) - '0';
                    restartVideo();
                } else if (text.matches("CHANNEL [0-3]")) {
                    channel = text.charAt(8) - '0';
                    restartVideo();
                    report(id, "NOTICE CHANNEL_SET");
                } else if (text.matches("AUDIO [01]")) {
                    audio = text.endsWith("1");
                    if (camera != null) {
                        restartVideo();
                        camera.sendEncrypted(audio ? 0x104 : 0x105, "");
                    }
                    report(id, audio ? "NOTICE AUDIO_ON" : "NOTICE AUDIO_OFF");
                } else if (text.matches("PTZ [1234]")) {
                    // Fixed-step motors (most Xiaomi PTZ cameras): one step per command.
                    if (camera != null) {
                        camera.sendEncrypted(0x112, "{\"operation\":" + text.charAt(4) + "}");
                        report(id, "NOTICE MOTOR_OK");
                    } else {
                        report(id, "NOTICE MOTOR_FAILED");
                    }
                } else if (text.matches("MOTOR (left|right|up|down|stop) [0-9]{1,2}")) {
                    // Continuous motors (Dafang etc.): direction + speed, plus an
                    // explicit stop when the direction button is released.
                    if (camera != null) {
                        String[] parts = text.split(" ");
                        camera.sendEncrypted(0x112,
                                "{\"direction\":\"" + parts[1] + "\",\"speed\":" + parts[2] + "}");
                        report(id, "NOTICE MOTOR_OK");
                    } else {
                        report(id, "NOTICE MOTOR_FAILED");
                    }
                } else if (text.matches("TALK [01]")) {
                    boolean on = text.endsWith("1");
                    if (camera == null) {
                        report(id, "NOTICE TALK_FAILED");
                        return;
                    }
                    talking = on;
                    camera.sendEncrypted(on ? 0x106 : 0x108, "");
                    report(id, on ? "NOTICE TALK_ON" : "NOTICE TALK_OFF");
                } else if (text.matches("CODEC (1024|1026|1027|1032)")) {
                    speakerCodec = Integer.parseInt(text.substring(6));
                    report(id, "NOTICE CODEC_SET");
                } else if (text.startsWith("AUD ")) {
                    sendSpeakerAudio(text.substring(4));
                } else if (text.matches("INDEX [0-9]{1,3}")) {
                    int channel = Integer.parseInt(text.substring(6));
                    if (connection == null) { report(id, "NOTICE INDEX_FAILED"); return; }
                    sendRdt(6, indexPayload(channel));
                    report(id, "NOTICE INDEX_SENT " + channel);
                } else if (text.matches("PLAY -?[0-9]+ -?[0-9]+( [0-9]{1,3})*")) {
                    startPlayback(text.substring(5), false);
                } else if (text.matches("PLAYDIRECT -?[0-9]+ -?[0-9]+( [0-9]{1,3})*")) {
                    // Some CS2-relay firmwares want the command id as the outer
                    // channel-0 command instead of the 0x1001 envelope.
                    startPlayback(text.substring(11), true);
                } else if (text.equals("LIVE")) {
                    playbackMode = false;
                    if (camera != null) {
                        camera.sendEncrypted(0x10D, playbackJson(playbackId++, 0, 0,
                                new int[0]));
                        restartVideo();
                    }
                    report(id, "NOTICE LIVE");
                } else if (text.matches("FILE [0-9]+ [0-9]{1,3}")) {
                    String[] parts = text.split(" ");
                    if (connection == null) { report(id, "NOTICE INDEX_FAILED"); return; }
                    sendRdt(1, filePayload(Long.parseLong(parts[1]),
                            Integer.parseInt(parts[2])));
                }
            } catch (Exception error) {
                report(id, "NOTICE CONTROL_FAILED");
            }
        }

        /** One encrypted 40 ms audio frame on DRW channel 3 (outbound audio). */
        private void sendSpeakerAudio(String encoded) {
            if (!talking || connection == null) return;
            try {
                byte[] payload = Base64.getDecoder().decode(encoded);
                if (payload.length == 0 || payload.length > 4096) return;
                byte[] message = speakerMessage(mediaKey, speakerCodec, audioSeq++,
                        System.currentTimeMillis(), payload);
                connection.sendRaw(speakerDatagram(message, channel3Seq++ & 0xFFFF));
            } catch (Exception error) {
                report(id, "NOTICE TALK_FAILED");
            }
        }

        /** MISS 0x10D body; every field is mandatory or the firmware drops it. */
        static String playbackJson(int sessionId, long start, long end, int[] channels) {
            StringBuilder s = new StringBuilder("{");
            s.append("\"sessionid\":").append(sessionId);
            s.append(",\"autoswitchtolive\":1,\"avchannelmerge\":1");
            if (channels.length == 0) {
                s.append(",\"starttime\":").append(start).append(",\"endtime\":").append(end);
                s.append(",\"offset\":0,\"speed\":1");
            } else {
                s.append(",\"starttime\":").append(repeat(String.valueOf(start), channels.length));
                s.append(",\"endtime\":").append(repeat(String.valueOf(end), channels.length));
                s.append(",\"offset\":").append(repeat("0", channels.length));
                s.append(",\"speed\":").append(repeat("1", channels.length));
                s.append(",\"channel\":[");
                for (int i = 0; i < channels.length; i++) {
                    if (i > 0) s.append(',');
                    s.append(channels[i]);
                }
                s.append(']');
            }
            return s.append('}').toString();
        }

        private static String repeat(String value, int count) {
            StringBuilder s = new StringBuilder("[");
            for (int i = 0; i < count; i++) {
                if (i > 0) s.append(',');
                s.append(value);
            }
            return s.append(']').toString();
        }

        private void startPlayback(String arguments, boolean direct) {
            if (camera == null) { report(id, "NOTICE PLAYBACK_FAILED"); return; }
            String[] parts = arguments.split(" ");
            long start = Long.parseLong(parts[0]);
            long end = Long.parseLong(parts[1]);
            int[] channels = new int[parts.length - 2];
            for (int i = 2; i < parts.length; i++) channels[i - 2] = Integer.parseInt(parts[i]);
            String body = playbackJson(playbackId++, start, end, channels);
            playbackMode = true;
            if (direct) {
                byte[] inner = new byte[4 + body.length()];
                putBe32(inner, 0, 0x10D);
                System.arraycopy(body.getBytes(StandardCharsets.UTF_8), 0, inner, 4,
                        body.length());
                connection.sendCommand(0x10D, MissCrypto.encrypt(mediaKey, inner));
            } else {
                camera.sendEncrypted(0x10D, body);
            }
            report(id, "NOTICE PLAYBACK_SENT");
        }

        private void sendRdt(int cmd, byte[] payload) {
            byte[] plain = new byte[8 + payload.length];
            putLe32(plain, 0, cmd);
            putLe32(plain, 4, payload.length);
            System.arraycopy(payload, 0, plain, 8, payload.length);
            connection.sendRDT(MissCrypto.encrypt(mediaKey, plain));
        }

        static byte[] indexPayload(int channel) {
            byte[] payload = new byte[24];
            putLe32(payload, 8, channel);
            return payload;
        }

        static byte[] filePayload(long timestamp, int channel) {
            byte[] payload = new byte[12];
            putLe32(payload, 0, (int) timestamp);
            putLe32(payload, 8, channel);
            return payload;
        }

        /** Reassemble the encrypted RDT byte stream and hand whole messages out. */
        private void pumpRdt() {
            while (!closed.get()) {
                byte[] chunk = connection.nextRDT(Duration.ofMillis(250));
                if (chunk == null) continue;
                byte[] plain;
                try { plain = MissCrypto.decrypt(mediaKey, chunk); }
                catch (Exception ignored) { continue; }
                if (rdtWant < 0) {
                    if (plain.length < 8) continue;
                    long want = le32(plain, 4);
                    if (want < 0 || want > 16L * 1024 * 1024) { rdtPayload.reset(); continue; }
                    rdtCmd = (int) le32(plain, 0);
                    rdtWant = (int) want;
                    rdtPayload.reset();
                    rdtPayload.write(plain, 8, plain.length - 8);
                } else {
                    rdtPayload.write(plain, 0, plain.length);
                }
                if (rdtWant >= 0 && rdtPayload.size() >= rdtWant) {
                    byte[] body = java.util.Arrays.copyOf(rdtPayload.toByteArray(), rdtWant);
                    report(id, "RDT " + rdtCmd + " "
                            + Base64.getEncoder().encodeToString(body));
                    rdtCmd = rdtWant = -1;
                    rdtPayload.reset();
                }
            }
        }

        private static long le32(byte[] data, int offset) {
            return (data[offset] & 255L) | ((data[offset + 1] & 255L) << 8)
                    | ((data[offset + 2] & 255L) << 16) | ((data[offset + 3] & 255L) << 24);
        }

        void close() {
            if (!closed.getAndSet(true)) {
                // Tell the camera to close its speaker before tearing the P2P
                // link down; otherwise some firmwares keep the talk session open
                // and refuse the next connection for a while.
                if (talking && camera != null)
                    try { camera.sendEncrypted(0x108, ""); } catch (Exception ignored) { }
                if (playbackMode && camera != null)
                    try { camera.sendEncrypted(0x10D, playbackJson(playbackId++, 0, 0,
                            new int[0])); }
                    catch (Exception ignored) { }
                talking = false;
                try { if (camera != null) camera.stopVideo(); } catch (Exception ignored) { }
                try { if (connection != null) connection.close(); } catch (Exception ignored) { }
                try { if (relay != null) relay.close(); } catch (Exception ignored) { }
                try { if (server != null) server.close(); } catch (Exception ignored) { }
            }
        }

        @Override
        public void run() {
            String phase = "CONFIG";
            try {
                phase = "RELAY";
                report(id, "PHASE RELAY");
                relay = new RelaySession(Uid.parse(uid), InitString.servers(init), null);
                RelaySession.Established established = relay.connect(Duration.ofSeconds(45));
                if (closed.get()) return;
                phase = "AUTH";
                report(id, "PHASE AUTH");
                connection = new PpppConnection(relay.socket(), established.relay(), null);
                connection.start();
                camera = new MissClient(connection);
                camera.authenticate(clientPublic, sign, Duration.ofSeconds(15));
                camera.useSessionKey(mediaKey);
                startVideo(quality);
                phase = "MEDIA";
                report(id, "PHASE MEDIA");
                long deadline = System.nanoTime() + Duration.ofSeconds(20).toNanos();
                MediaPacket first = null;
                while (!closed.get() && System.nanoTime() < deadline) {
                    MediaPacket packet = camera.nextMedia(Duration.ofSeconds(1));
                    if (packet != null && packet.isVideo() && packet.isKeyframe()) {
                        first = packet;
                        break;
                    }
                }
                if (first == null) throw new IllegalStateException();
                if (closed.get()) return;
                server = new HttpStreamServer(port, path, null);
                server.start();
                // Seed the bounded cache before inviting the local player: dropping
                // the first IDR makes long-GOP cameras time out on open.
                server.publish(first);
                videoInfo(first);
                report(id, "INFO GOP_BOOTSTRAP");
                report(id, "READY");
                phase = "STREAM";
                Thread rdtThread = new Thread(this::pumpRdt, "camera-rdt-" + id);
                rdtThread.setDaemon(true);
                rdtThread.start();
                long lastFrame = System.nanoTime();
                while (!closed.get()) {
                    // 0x10E (playback status) shares the command channel with
                    // the auth/quality acks; drain it without a second consumer.
                    byte[] command = connection.nextCommand(Duration.ofMillis(1));
                    if (command != null && command.length >= 4
                            && ((command[0] & 255) << 24 | (command[1] & 255) << 16
                                | (command[2] & 255) << 8 | (command[3] & 255)) == 0x10E) {
                        report(id, "PLAYBACK " + Base64.getEncoder().encodeToString(
                                java.util.Arrays.copyOfRange(command, 4, command.length)));
                    }
                    MediaPacket packet = camera.nextMedia(Duration.ofSeconds(1));
                    if (packet != null && packet.isVideo()) {
                        videoInfo(packet);
                        lastFrame = System.nanoTime();
                        server.publish(packet);
                    } else if (packet != null && audio && (packet.codecId() == 1027 || packet.codecId() == 1026)) {
                        report(id, "AUDIO 8000 " + Base64.getEncoder().encodeToString(pcm(packet)));
                    } else if (packet != null && audio && packet.codecId() == 1032) {
                        // 1032 = Opus: the JVM has no decoder, pass the raw packet
                        // through and let the desktop decode it with libopus.
                        report(id, "AUDIO_OPUS " + Base64.getEncoder().encodeToString(packet.payload()));
                    } else if (System.nanoTime() - lastFrame > Duration.ofSeconds(20).toNanos()) {
                        throw new IllegalStateException();
                    }
                }
            } catch (Exception error) {
                // Never print server responses, URLs, session tokens or key material.
                if (!closed.get()) report(id, "ERROR " + phase);
            } finally {
                close();
                sessions.remove(id, this);
                report(id, "EXIT");
            }
        }
    }

    private static String field(String value) {
        if (value == null || value.isBlank() || value.length() > 16384)
            throw new IllegalArgumentException();
        return value;
    }

    private static void open(String[] parts) {
        if (parts.length < 12) throw new IllegalArgumentException();
        String id = parts[1];
        if (!id.matches("[a-f0-9]{8,64}")) throw new IllegalArgumentException();
        int port = Integer.parseInt(parts[2]);
        String path = parts[3];
        if (!path.matches("/camera-[a-f0-9]{32}\\.ts") || port < 1 || port > 65535)
            throw new IllegalArgumentException();
        int quality = Integer.parseInt(parts[10]);
        if (quality < 0 || quality > 3) throw new IllegalArgumentException();
        int channel = Integer.parseInt(parts[11]);
        if (channel < 0 || channel > 3) channel = 0;
        Session session = new Session(
            id, port, path,
            field(parts[4]), field(parts[5]), field(parts[6]),
            HexFormat.of().parseHex(field(parts[7])),
            field(parts[8]), field(parts[9]), quality, channel);
        if (sessions.putIfAbsent(id, session) != null) return;
        Thread worker = new Thread(session, "camera-" + id);
        worker.setDaemon(true);
        worker.start();
    }

    public static void main(String[] args) {
        if (args.length == 1 && args[0].equals("--check")) {
            MissCrypto.Keys keys = MissCrypto.generateKeys();
            MissCrypto.sessionKey(keys.privateScalar(), keys.publicHex());
            report("check", "CAMERA_RELAY_2");
            return;
        }
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            for (Session session : sessions.values()) session.close();
        }));
        try {
            BufferedReader input = new BufferedReader(
                new InputStreamReader(System.in, StandardCharsets.UTF_8));
            String line;
            while ((line = input.readLine()) != null) {
                try {
                    String[] parts = line.split("\t", -1);
                    if (parts.length == 0) continue;
                    if (parts[0].equals("OPEN")) {
                        open(parts);
                    } else if (parts[0].equals("CMD") && parts.length >= 3) {
                        Session session = sessions.get(parts[1]);
                        if (session != null) session.command(parts[2]);
                    } else if (parts[0].equals("CLOSE") && parts.length >= 2) {
                        Session session = sessions.get(parts[1]);
                        if (session != null) session.close();
                    } else if (parts[0].equals("QUIT")) {
                        break;
                    }
                } catch (Exception error) {
                    String id = line.length() > 1 && line.contains("\t")
                        ? line.substring(line.indexOf('\t') + 1).split("\t", -1)[0] : "host";
                    report(id, "ERROR CONFIG");
                }
            }
        } catch (Exception ignored) {
        } finally {
            for (Session session : sessions.values()) session.close();
        }
    }

    /** 32-byte little-endian media header + ChaCha20 payload (8-byte nonce tail). */
    static byte[] speakerMessage(byte[] key, int codec, int seq, long timestampMs,
                                 byte[] payload) {
        byte[] encrypted = MissCrypto.encrypt(key, payload);
        byte[] header = new byte[32];
        putLe32(header, 0, encrypted.length);
        putLe32(header, 4, codec);
        putLe32(header, 8, seq);
        putLe32(header, 12, 0);                 // flags: not a keyframe
        putLe64(header, 16, timestampMs);
        byte[] message = new byte[32 + encrypted.length];
        System.arraycopy(header, 0, message, 0, 32);
        System.arraycopy(encrypted, 0, message, 32, encrypted.length);
        return message;
    }

    /** F1 D0 <len16> | D1 03 <seq16> <u32 BE message length> <message> */
    static byte[] speakerDatagram(byte[] message, int seq) {
        byte[] drw = new byte[8 + message.length];
        drw[0] = (byte) 0xD1;
        drw[1] = 3;                            // CHANNEL_OUTBOUND_AUDIO
        drw[2] = (byte) (seq >>> 8);
        drw[3] = (byte) seq;
        putBe32(drw, 4, message.length);
        System.arraycopy(message, 0, drw, 8, message.length);
        return Frame.encode(Msg.DRW, drw);
    }

    private static void putLe32(byte[] out, int offset, int value) {
        for (int i = 0; i < 4; i++) out[offset + i] = (byte) (value >>> (8 * i));
    }

    private static void putLe64(byte[] out, int offset, long value) {
        for (int i = 0; i < 8; i++) out[offset + i] = (byte) (value >>> (8 * i));
    }

    private static void putBe32(byte[] out, int offset, int value) {
        for (int i = 0; i < 4; i++) out[offset + i] = (byte) (value >>> (24 - 8 * i));
    }

    /** G.711 A-law/mu-law to mono signed 16-bit PCM, for the desktop audio sink. */
    static byte[] pcm(MediaPacket packet) {
        byte[] result = new byte[packet.payload().length * 2];
        for (int i = 0; i < packet.payload().length; i++) {
            int value = packet.payload()[i] & 255;
            int sample;
            if (packet.codecId() == 1027) {
                value ^= 0x55;
                int segment = (value >> 4) & 7;
                int magnitude = (value & 15) << 4;
                magnitude += segment == 0 ? 8 : 0x108;
                if (segment > 1) magnitude <<= segment - 1;
                sample = (value & 0x80) != 0 ? magnitude : -magnitude;
            } else {
                value = (~value) & 255;
                int magnitude = (((value & 15) << 3) + 0x84) << ((value >> 4) & 7);
                sample = (value & 0x80) != 0 ? 0x84 - magnitude : magnitude - 0x84;
            }
            result[i * 2] = (byte)sample;
            result[i * 2 + 1] = (byte)(sample >> 8);
        }
        return result;
    }
}
