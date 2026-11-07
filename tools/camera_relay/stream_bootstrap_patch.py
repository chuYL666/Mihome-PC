"""Apply our bounded in-memory GOP bootstrap to the verified pinned server source."""
from pathlib import Path


def patch_http_stream_server(path: Path):
    text = path.read_text(encoding='utf-8')
    if 'MIJIADECK_GOP_BOOTSTRAP' in text:
        return
    replacements = [
        ('    private final List<Viewer> viewers = new CopyOnWriteArrayList<>();',
         '''    private final List<Viewer> viewers = new CopyOnWriteArrayList<>();
    // MIJIADECK_GOP_BOOTSTRAP: a viewer may arrive after the first keyframe.
    // Keep only one complete GOP, bounded by both payload bytes and frames.
    private static final int BOOTSTRAP_MAX_BYTES = 16 * 1024 * 1024;
    private static final int BOOTSTRAP_MAX_FRAMES = 400;
    private final Object bootstrapLock = new Object();
    private final java.util.ArrayList<MediaPacket> bootstrap = new java.util.ArrayList<>();
    private int bootstrapBytes;'''),
        ('''        for (Viewer viewer : viewers) {
            viewer.offer(packet);
        }''',
         '''        synchronized (bootstrapLock) {
            if (packet.isKeyframe()) {
                bootstrap.clear();
                bootstrapBytes = 0;
            }
            if (packet.isKeyframe() || !bootstrap.isEmpty()) {
                if (bootstrap.size() < BOOTSTRAP_MAX_FRAMES
                        && packet.payload().length <= BOOTSTRAP_MAX_BYTES - bootstrapBytes) {
                    bootstrap.add(packet);
                    bootstrapBytes += packet.payload().length;
                } else {
                    // Never replay a partial GOP after an overflow. Wait for IDR.
                    bootstrap.clear();
                    bootstrapBytes = 0;
                }
            }
            for (Viewer viewer : viewers) viewer.offer(packet);
        }'''),
        ('''        Viewer viewer = new Viewer();
        viewers.add(viewer);''',
         '''        Viewer viewer;
        synchronized (bootstrapLock) {
            viewer = new Viewer(List.copyOf(bootstrap));
            viewers.add(viewer);
        }'''),
        ('''        server.stop(0);
        viewers.clear();''',
         '''        server.stop(0);
        synchronized (bootstrapLock) {
            viewers.clear();
            bootstrap.clear();
            bootstrapBytes = 0;
        }'''),
        ('''        private volatile long dropped;

        void offer''',
         '''        private volatile long dropped;
        private List<MediaPacket> initial;

        Viewer(List<MediaPacket> initial) {
            this.initial = initial;
            this.started = !initial.isEmpty();
        }

        void offer'''),
        ('''            TsMuxer muxer = null;
            while (true) {''',
         '''            TsMuxer muxer = null;
            for (MediaPacket packet : initial) {
                if (muxer == null) muxer = new TsMuxer(out, packet.codecId());
                muxer.write(packet);
            }
            initial = List.of();
            if (muxer != null) muxer.flush();
            while (true) {'''),
    ]
    for original, replacement in replacements:
        if text.count(original) != 1:
            raise RuntimeError('Pinned camera HTTP server source changed; review bootstrap patch')
        text = text.replace(original, replacement, 1)
    path.write_text(text, encoding='utf-8')
