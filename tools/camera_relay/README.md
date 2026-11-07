Experimental remote camera preview (local test build)

Transport reference: https://github.com/eugeneRover/micam
Pinned source commit: 2cc9d9e7607014d1d05c17bc5676544fbb0d5d24
Only miss/, pppp/ and server/ are compiled. The upstream cloud login, CLI and
credential persistence are not used. CameraRelay.java is this app's pipe adapter.

Build: python tools/install_camera_backend.py
Optional existing compiler: --jdk <JDK 21 directory>
The build verifies SHA256 for the source archive and Temurin JDK archive. A small
Java runtime is created locally; users need no system-wide Java installation.
The runtime preserves its legal/ notices. The source checkout and full JDK are
only temporary build inputs. Generated binaries/runtime are excluded from Git.

Local playback patch: stream_bootstrap_patch.py is applied to the verified pinned
HttpStreamServer source before compilation. It retains one in-memory GOP up to
16 MiB / 400 frames and replays it atomically when a local viewer connects.
Overflow invalidates the entire cached GOP until a new keyframe arrives; partial
GOPs are never replayed. CameraRelay seeds the first received keyframe before
reporting READY. This prevents local viewers from waiting for the next IDR and
timing out on long-GOP cameras. No video is written to disk.

Offline reproduction: tools/test_camera_stream.py uses generated test-pattern
H.264/H.265 streams through the actual HTTP muxer and Qt player. Compiler paths
and the synthetic-video encoder live only under build/camera-debug. The Java
fixture also supports --check-bootstrap to check replay and byte/frame limits.

This is experimental. The transport repository has no explicit license file at
this commit, and upstream redistribution permission has not been confirmed.
The maintainer has elected to include a compiled relay in the v1.0.1 full build;
this does not establish permission or relicense upstream code. Its source is
not copied into the Git tree. See THIRD_PARTY_NOTICES.md for the recorded status.

Existing MiHome cloud authentication prepares fresh X25519 video keys, the
camera UID and relay-server init string. Only these temporary values are sent to
the helper's stdin; no account login credentials are sent to it. The helper uses
CS2 UDP relay, without requiring the camera's LAN IP. Decoded video is repackaged
as MPEG-TS on 127.0.0.1 under a random 128-bit path. No media is recorded.
Only status codes and optional base64 PCM audio use the private stdout pipe;
raw server responses and keys are never logged. Audio defaults off. G.711 A-law
and mu-law frames are decoded to 8 kHz mono PCM in memory. Other audio codecs are
reported as unsupported. QUALITY/AUDIO/PTZ commands use the existing stdin pipe;
PTZ uses a single MISS 0x112 motor step per drag gesture, never a continuous move.
Closing preview sends STOP and terminates any unresponsive owned process.
