/* Synthetic Annex-B video only. No camera, credentials, or external network. */
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.net.HttpURLConnection;
import java.net.ServerSocket;
import java.net.SocketTimeoutException;
import java.net.URI;
import micam.miss.MediaPacket;
import micam.server.HttpStreamServer;

public final class CameraStreamFixture {
    private static int startLength(byte[] b, int p) {
        if (p + 3 < b.length && b[p] == 0 && b[p+1] == 0 && b[p+2] == 0 && b[p+3] == 1) return 4;
        if (p + 2 < b.length && b[p] == 0 && b[p+1] == 0 && b[p+2] == 1) return 3;
        return 0;
    }
    private static List<byte[]> frames(byte[] data, int codec) {
        List<Integer> offsets = new ArrayList<>();
        for (int p = 0; p < data.length - 4; p++) {
            int n = startLength(data, p);
            if (n == 0) continue;
            int type = codec == 4 ? data[p+n] & 31 : (data[p+n] >> 1) & 63;
            if (type == (codec == 4 ? 9 : 35)) offsets.add(p);
            p += n - 1;
        }
        if (offsets.isEmpty()) throw new IllegalArgumentException("AUD fixture required");
        offsets.set(0, 0);
        List<byte[]> result = new ArrayList<>();
        for (int i = 0; i < offsets.size(); i++)
            result.add(Arrays.copyOfRange(data, offsets.get(i), i+1 < offsets.size() ? offsets.get(i+1) : data.length));
        return result;
    }
    private static boolean key(byte[] frame, int codec) {
        for (int p = 0; p < frame.length - 4; p++) {
            int n = startLength(frame, p);
            if (n == 0) continue;
            int type = codec == 4 ? frame[p+n] & 31 : (frame[p+n] >> 1) & 63;
            if (codec == 4 ? type == 5 : type >= 16 && type <= 23) return true;
            p += n - 1;
        }
        return false;
    }
    public static void main(String[] args) throws Exception {
        if (args.length == 1 && args[0].equals("--check-bootstrap")) {
            checkBootstrap();
            return;
        }
        int codec = Integer.parseInt(args[2]);
        List<byte[]> frames = frames(Files.readAllBytes(Path.of(args[1])), codec);
        try (HttpStreamServer server = new HttpStreamServer(Integer.parseInt(args[0]), "/synthetic.ts", null)) {
            server.start();
            server.publish(new MediaPacket(codec, 0, 1, 0, frames.get(0)));
            System.out.println("READY " + frames.size());
            System.out.flush();
            // Publish the first IDR before the HTTP viewer subscribes, just as
            // in the real adapter. The patched server must replay a valid GOP.
            for (int i = 1; i < 800; i++) {
                byte[] frame = frames.get(i % frames.size());
                server.publish(new MediaPacket(codec, i, key(frame, codec) ? 1 : 0, i * 50L, frame));
                Thread.sleep(50);
            }
        }
    }

    private static void checkBootstrap() throws Exception {
        int port;
        try (ServerSocket probe = new ServerSocket(0)) { port = probe.getLocalPort(); }
        try (HttpStreamServer server = new HttpStreamServer(port, "/synthetic.ts", null)) {
            server.start();
            byte[] small = new byte[]{0, 0, 0, 1, 0x65, 0};
            server.publish(new MediaPacket(4, 0, 1, 0, small));
            server.publish(new MediaPacket(4, 1, 0, 50, small));
            checkRead(port, true);
            for (int i = 2; i < 402; i++) server.publish(new MediaPacket(4, i, 0, i*50, small));
            checkRead(port, false); // A frame-overflowed GOP must never be replayed.
            server.publish(new MediaPacket(4, 403, 1, 20150, small));
            checkRead(port, true);
            byte[] large = new byte[1024 * 1024];
            server.publish(new MediaPacket(4, 404, 1, 20200, large));
            for (int i = 405; i < 423; i++) server.publish(new MediaPacket(4, i, 0, i*50, large));
            checkRead(port, false); // Byte limit also invalidates the entire GOP.
            server.publish(new MediaPacket(4, 424, 1, 21200, small));
            checkRead(port, true);
        }
        System.out.println("BOOTSTRAP_REPLAY_AND_BOUNDS_OK");
    }

    private static void checkRead(int port, boolean expected) throws Exception {
        HttpURLConnection connection = (HttpURLConnection) URI.create("http://127.0.0.1:" + port + "/synthetic.ts").toURL().openConnection();
        connection.setReadTimeout(500);
        connection.setConnectTimeout(1000);
        try {
            int value = connection.getInputStream().read();
            if (!expected || value != 0x47) throw new AssertionError("Invalid bootstrap data");
        } catch (SocketTimeoutException error) {
            if (expected) throw new AssertionError("Cached IDR was lost", error);
        } finally { connection.disconnect(); }
    }
}
