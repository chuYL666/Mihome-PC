"""Synthetic video through the actual relay HTTP muxer and Qt player, no camera."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from zipfile import ZipFile

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PySide6.QtCore import QEventLoop, QTimer, QUrl, qInstallMessageHandler
from PySide6.QtMultimedia import QMediaPlayer, QVideoSink
from PySide6.QtWidgets import QApplication


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--jar', type=Path, default=ROOT / 'tools/camera_relay/camera-relay.jar')
    parser.add_argument('--output', type=Path)
    options = parser.parse_args()
    folder = ROOT / 'build/camera-debug'
    paths = json.loads((folder / 'paths.json').read_text())
    compiler = Path(paths['jdk']) / 'bin/javac.exe'
    java = ROOT / 'tools/camera_relay/runtime/bin/java.exe'
    jar = options.jar.resolve()
    classes = folder / 'fixture-classes'
    classes.mkdir(exist_ok=True)
    subprocess.run([str(compiler), '--release', '21', '-encoding', 'UTF-8', '-cp', str(jar),
                    '-d', str(classes), str(ROOT / 'tools/camera_relay/tests/CameraStreamFixture.java')], check=True)
    executable = next(folder.glob('ffmpeg/*/binaries/*.exe'), None)
    if executable is None:
        with ZipFile(next((folder / 'wheels').glob('imageio_ffmpeg-*.whl'))) as wheel:
            wheel.extractall(folder / 'ffmpeg')
        executable = next(folder.glob('ffmpeg/*/binaries/*.exe'))
    qInstallMessageHandler(lambda *_: None)
    app = QApplication.instance() or QApplication([])
    results = []
    for codec, encoder, args in [('h264', 'libx264', ['-x264-params', 'aud=1:repeat-headers=1:scenecut=0']),
                                  ('h265', 'libx265', ['-x265-params', 'aud=1:repeat-headers=1:scenecut=0:log-level=error'])]:
        for gop in (20, 200):
            fixture = folder / f'synthetic-{codec}-{gop}.annexb'
            if not fixture.exists():
                subprocess.run([str(executable), '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi',
                    '-i', 'testsrc2=size=320x180:rate=20', '-t', '12', '-an', '-c:v', encoder,
                    '-preset', 'ultrafast', '-bf', '0', '-g', str(gop), *args,
                    '-f', 'hevc' if codec == 'h265' else 'h264', str(fixture)], check=True)
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
            process = subprocess.Popen([str(java), '-Xmx96m', '-cp', os.pathsep.join((str(jar), str(classes))),
                'CameraStreamFixture', str(port), str(fixture), '4' if codec == 'h264' else '5'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                if not process.stdout.readline().startswith('READY '):
                    raise RuntimeError('Synthetic relay did not start')
                player, sink, loop = QMediaPlayer(), QVideoSink(), QEventLoop()
                player.setVideoOutput(sink)
                started = time.monotonic()
                result = {'codec': codec, 'gop': gop, 'frames': 0}
                def frame_received(frame):
                    if frame.isValid():
                        result['frames'] += 1
                        result.update(width=frame.width(), height=frame.height())
                        if result['frames'] >= 3:
                            loop.quit()
                def failed(error, message):
                    result.update(error=error.name, detail=message)
                    loop.quit()
                sink.videoFrameChanged.connect(frame_received)
                player.errorOccurred.connect(failed)
                timeout = QTimer()
                timeout.setSingleShot(True)
                timeout.timeout.connect(loop.quit)
                timeout.start(17000)
                player.setSource(QUrl(f'http://127.0.0.1:{port}/synthetic.ts'))
                player.play()
                loop.exec()
                result['seconds'] = round(time.monotonic() - started, 2)
                player.stop()
                player.setSource(QUrl())
                timeout.stop()
                results.append(result)
                print(json.dumps(result), flush=True)
                player.deleteLater()
                sink.deleteLater()
                app.processEvents()
            finally:
                process.terminate()
                process.communicate(timeout=5)
    output = options.output or folder / 'synthetic-results.json'
    output.write_text(json.dumps(results, indent=2), encoding='utf-8')
    return 0 if all(r['frames'] >= 3 for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
