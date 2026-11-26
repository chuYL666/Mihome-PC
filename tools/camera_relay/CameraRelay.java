/* Local CS2 transport host: one JVM serves many camera sessions.
 *
 * Protocol (stdin, one command per line, TAB separated):
 *   OPEN <id> <port> <path> <uid> <init> <clientPub> <clientPrivHex>
 *        <devicePub> <sign> <quality> <channel>
 *   CMD  <id> <AUDIO 0|1 | QUALITY 0-3 | CHANNEL 0-3 | PTZ 1-4>
 *   CLOSE <id>
 *   QUIT
 *
 * stdout (one message per line): <id> <message>
 *   PHASE RELAY|AUTH|MEDIA, READY, ERROR <CODE>, NOTICE ..., INFO ...,
 *   AUDIO 8000 <b64>, AUDIO_OPUS <b64>, EXIT
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
import micam.pppp.InitString;
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
                    if (camera != null)
                        camera.sendEncrypted(0x112, "{\"operation\":" + text.charAt(4) + "}");
                }
            } catch (Exception error) {
                report(id, "NOTICE CONTROL_FAILED");
            }
        }

        void close() {
            if (!closed.getAndSet(true)) {
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
                camera.useSessionKey(MissCrypto.sessionKey(clientPrivate, devicePublic));
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
                long lastFrame = System.nanoTime();
                while (!closed.get()) {
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
