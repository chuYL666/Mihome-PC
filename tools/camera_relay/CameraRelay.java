/* Local test adapter for the micam CS2 transport; no account credentials or recordings. */
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.HexFormat;
import java.util.Base64;
import java.util.concurrent.atomic.AtomicBoolean;
import micam.miss.MediaPacket;
import micam.miss.MissClient;
import micam.miss.MissCrypto;
import micam.pppp.InitString;
import micam.pppp.PpppConnection;
import micam.pppp.RelaySession;
import micam.pppp.Uid;
import micam.server.HttpStreamServer;

public final class CameraRelay {
    private static volatile RelaySession relay;
    private static volatile PpppConnection connection;
    private static volatile MissClient camera;
    private static volatile HttpStreamServer server;
    private static final AtomicBoolean stopped = new AtomicBoolean();
    private static volatile int quality = 2;
    private static volatile boolean audio;
    private static volatile int channel = 0;
    private static int videoCodec;

    private static void videoInfo(MediaPacket packet) {
        if (packet.codecId() != videoCodec) {
            videoCodec = packet.codecId();
            report("INFO VIDEO_CODEC " + (videoCodec == 4 ? "H264" : "H265"));
        }
    }

    private static void report(String message) {
        System.out.println(message);
        System.out.flush();
    }

    /** Request a video stream and surface whether the camera accepted the quality. */
    private static void startVideo(int requested) {
        if (camera == null) return;
        camera.startVideo(requested, audio, channel);
        try {
            MissClient.Response ack = camera.awaitResponse(Duration.ofMillis(1500));
            if (ack != null && !ack.text().contains("\"result\":\"success\""))
                report("NOTICE QUALITY_REJECTED");
        } catch (Exception ignored) { }
    }

    /**
     * Restart the running stream with the current quality/audio/channel.
     * Cameras ignore enableaudio and channel changes on an already-running
     * stream, so every switch has to stop the stream first.
     */
    private static void restartVideo() {
        if (camera == null) return;
        try { camera.stopVideo(); } catch (Exception ignored) { }
        try { Thread.sleep(300); } catch (InterruptedException ignored) { }
        startVideo(quality);
    }

    private static String field(BufferedReader reader) throws Exception {
        String value = reader.readLine();
        if (value == null || value.isBlank() || value.length() > 16384)
            throw new IllegalArgumentException();
        return value;
    }

    private static synchronized void stop() {
        if (stopped.getAndSet(true)) return;
        try { if (camera != null) camera.stopVideo(); } catch (Exception ignored) { }
        try { if (connection != null) connection.close(); } catch (Exception ignored) { }
        try { if (relay != null) relay.close(); } catch (Exception ignored) { }
        try { if (server != null) server.close(); } catch (Exception ignored) { }
    }

    public static void main(String[] args) {
        if (args.length == 1 && args[0].equals("--check")) {
            MissCrypto.Keys keys = MissCrypto.generateKeys();
            MissCrypto.sessionKey(keys.privateScalar(), keys.publicHex());
            report("CAMERA_RELAY_2");
            return;
        }
        String phase = "CONFIG";
        Runtime.getRuntime().addShutdownHook(new Thread(CameraRelay::stop));
        try {
            BufferedReader input = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
            int port = Integer.parseInt(field(input));
            String path = field(input);
            String uid = field(input);
            String init = field(input);
            String clientPublic = field(input);
            byte[] clientPrivate = HexFormat.of().parseHex(field(input));
            String devicePublic = field(input);
            String sign = field(input);
            quality = Integer.parseInt(field(input));
            if (quality < 0 || quality > 3) throw new IllegalArgumentException();
            channel = Integer.parseInt(field(input));
            if (channel < 0 || channel > 3) channel = 0;
            if (!path.matches("/camera-[a-f0-9]{32}\\.ts") || port < 1 || port > 65535)
                throw new IllegalArgumentException();
            Thread cancellation = new Thread(() -> {
                try {
                    String command;
                    while ((command = input.readLine()) != null && !command.equals("STOP")) {
                        try {
                            if (command.matches("QUALITY [0123]")) {
                                quality = command.charAt(8) - '0';
                                restartVideo();
                            } else if (command.matches("CHANNEL [0-3]")) {
                                channel = command.charAt(8) - '0';
                                restartVideo();
                                report("NOTICE CHANNEL_SET");
                            } else if (command.matches("AUDIO [01]")) {
                                audio = command.endsWith("1");
                                if (camera != null) {
                                    restartVideo();
                                    camera.sendEncrypted(audio ? 0x104 : 0x105, "");
                                }
                                report(audio ? "NOTICE AUDIO_ON" : "NOTICE AUDIO_OFF");
                            } else if (command.matches("PTZ [1234]")) {
                                if (camera != null) camera.sendEncrypted(0x112,
                                    "{\"operation\":" + command.charAt(4) + "}");
                            }
                        } catch (Exception error) { report("NOTICE CONTROL_FAILED"); }
                    }
                } catch (Exception ignored) { }
                stop();
                System.exit(0);
            }, "camera-parent-lifetime");
            cancellation.setDaemon(true);
            cancellation.start();
            phase = "RELAY";
            report("PHASE RELAY");
            relay = new RelaySession(Uid.parse(uid), InitString.servers(init), null);
            RelaySession.Established session = relay.connect(Duration.ofSeconds(45));
            if (stopped.get()) return;
            phase = "AUTH";
            report("PHASE AUTH");
            connection = new PpppConnection(relay.socket(), session.relay(), null);
            connection.start();
            camera = new MissClient(connection);
            camera.authenticate(clientPublic, sign, Duration.ofSeconds(15));
            camera.useSessionKey(MissCrypto.sessionKey(clientPrivate, devicePublic));
            startVideo(quality);
            phase = "MEDIA";
            report("PHASE MEDIA");
            long deadline = System.nanoTime() + Duration.ofSeconds(20).toNanos();
            MediaPacket first = null;
            while (!stopped.get() && System.nanoTime() < deadline) {
                MediaPacket packet = camera.nextMedia(Duration.ofSeconds(1));
                if (packet != null && packet.isVideo() && packet.isKeyframe()) {
                    first = packet;
                    break;
                }
            }
            if (first == null) throw new IllegalStateException();
            server = new HttpStreamServer(port, path, null);
            server.start();
            // Seed the bounded server cache before inviting the local player.
            // Dropping this first IDR made long-GOP cameras time out at open.
            server.publish(first);
            videoInfo(first);
            report("INFO GOP_BOOTSTRAP");
            report("READY");
            phase = "STREAM";
            long lastFrame = System.nanoTime();
            while (!stopped.get()) {
                MediaPacket packet = camera.nextMedia(Duration.ofSeconds(1));
                if (packet != null && packet.isVideo()) {
                    videoInfo(packet);
                    lastFrame = System.nanoTime();
                    server.publish(packet);
                } else if (packet != null && audio && (packet.codecId() == 1027 || packet.codecId() == 1026)) {
                    report("AUDIO 8000 " + Base64.getEncoder().encodeToString(pcm(packet)));
                } else if (packet != null && audio && packet.codecId() == 1032) {
                    // 1032 = Opus。JVM 里没有 Opus 解码器，原样透传给桌面端
                    // 用 libopus 解；此前直接判成「不支持」关掉音频，导致
                    // chuangmi.camera.039a01 这类机型永远没有声音。
                    report("AUDIO_OPUS " + Base64.getEncoder().encodeToString(packet.payload()));
                } else if (System.nanoTime() - lastFrame > Duration.ofSeconds(20).toNanos()) {
                    throw new IllegalStateException();
                }
            }
        } catch (Exception error) {
            // Never print server responses, URLs, session tokens or key material.
            if (!stopped.get()) report("ERROR " + phase);
        } finally {
            stop();
        }
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
