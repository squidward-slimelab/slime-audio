import unittest

from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
import json


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import slime_audio_stream as stream
from slime_audio_stream import (
    EFFECT_MESSAGE_PREFIX,
    OUTPUT_DEVICE_MESSAGE_PREFIX,
    RESET_AUDIO_MESSAGE,
    Receiver,
    SHARED_STREAM_START_MESSAGE,
    SHARED_STREAM_STOP_MESSAGE,
    format_diagnostics,
    parse_discovery_response,
    parse_endpoint,
    resolve_targets,
)


class SlimeAudioStreamTests(unittest.TestCase):
    def test_parse_discovery_response(self):
        payload = (
            b'{"App":"slime-audio","MachineName":"SPATULA","UserName":"slimeq",'
            b'"Version":"0.3.0","Port":47777,"UnixTimeMs":1500,"StreamMuted":true,'
            b'"Diagnostics":{"ActiveSessions":1,"ReceivedPackets":42,"MissingFrames":7,"LastPacketUnixTimeMs":1100}}'
        )

        receiver = parse_discovery_response(payload, "192.168.0.163", 1000, 1200)

        self.assertIsNotNone(receiver)
        self.assertEqual(receiver.endpoint, "192.168.0.163:47777")
        self.assertEqual(receiver.machine_name, "SPATULA")
        self.assertEqual(receiver.rtt_ms, 200)
        self.assertEqual(receiver.clock_offset_ms, 400)
        self.assertTrue(receiver.stream_muted)
        self.assertEqual(receiver.diagnostics["ReceivedPackets"], 42)

    def test_parse_discovery_response_rejects_other_apps(self):
        self.assertIsNone(parse_discovery_response(b'{"App":"nope"}', "127.0.0.1"))

    def test_resolve_targets_accepts_all_names_and_manual_endpoints(self):
        discovered = [
            Receiver("192.168.0.163:47777", "192.168.0.163", 47777, "SPATULA", "user", "0.3.0"),
            Receiver("192.168.0.200:47777", "192.168.0.200", 47777, "SPONGEBOT", "user", "0.3.0"),
        ]

        self.assertEqual(len(resolve_targets(["all"], discovered)), 2)
        self.assertEqual(resolve_targets(["spatula"], discovered)[0].host, "192.168.0.163")
        self.assertEqual(resolve_targets(["10.0.0.5:48888"], discovered)[0].port, 48888)

    def test_parse_endpoint_defaults_port(self):
        self.assertEqual(parse_endpoint("SPATULA"), ("SPATULA", 47777))
        self.assertEqual(parse_endpoint("SPATULA:48888"), ("SPATULA", 48888))

    def test_resolve_targets_deduplicates_all_and_named_target(self):
        discovered = [
            Receiver("192.168.0.163:47777", "192.168.0.163", 47777, "SPATULA", "user", "0.3.0"),
        ]

        self.assertEqual(len(resolve_targets(["all", "SPATULA"], discovered)), 1)

    def test_resolve_targets_all_skips_muted_receivers(self):
        discovered = [
            Receiver("192.168.0.163:47777", "192.168.0.163", 47777, "SPATULA", "user", "0.3.0", stream_muted=True),
            Receiver("192.168.0.123:47777", "192.168.0.123", 47777, "SPONGEBOT", "user", "0.3.0"),
        ]

        self.assertEqual([target.machine_name for target in resolve_targets(["all"], discovered)], ["SPONGEBOT"])
        self.assertEqual(len(resolve_targets(["all"], discovered, include_muted=True)), 2)

    def test_shared_stream_control_messages_match_protocol(self):
        self.assertEqual(SHARED_STREAM_START_MESSAGE, b"SLIME_AUDIO_SHARED_STREAM_START_V1")
        self.assertEqual(SHARED_STREAM_STOP_MESSAGE, b"SLIME_AUDIO_SHARED_STREAM_STOP_V1")
        self.assertEqual(RESET_AUDIO_MESSAGE, b"SLIME_AUDIO_RESET_AUDIO_V1")
        self.assertEqual(EFFECT_MESSAGE_PREFIX, b"SLIME_AUDIO_EFFECT_V1 ")

    def test_format_diagnostics(self):
        text = format_diagnostics(
            {
                "ActiveSessions": 1,
                "ReceivedPackets": 42,
                "MissingFrames": 7,
                "ReadCalls": 3,
                "MaxBufferedPackets": 12,
                "MaxBufferedPacketSpan": 13,
                "LatestSequence": 99,
                "LastPacketUnixTimeMs": 1_000,
                "ResetCount": 2,
                "DecodeFailures": 0,
                "SharedStreamServerHost": "192.168.0.122",
                "SharedStreamProcessId": 1234,
                "SharedStreamExitCount": 2,
                "SharedStreamTelemetryPath": r"C:\Users\slimeq\AppData\Local\SlimeAudio\telemetry.jsonl",
                "SharedStreamLastExitStatus": "Shared stream disconnected: -1073741819",
                "SharedStreamLastStderr": "snapclient audio error",
                "SharedStreamStartCommand": "snapclient.exe -h 192.168.0.122",
                "SharedStreamUptimeMs": 42000,
                "SharedStreamReconnectAttempts": 3,
            },
            now_ms=1_250,
            clock_offset_ms=50,
        )

        self.assertIn("diag_packets=42", text)
        self.assertIn("diag_missing_frames=7", text)
        self.assertIn("diag_last_packet_age_ms=300", text)
        self.assertIn("shared_stream_host=192.168.0.122", text)
        self.assertIn("shared_stream_pid=1234", text)
        self.assertIn("shared_stream_exits=2", text)
        self.assertIn("shared_stream_uptime_ms=42000", text)
        self.assertIn("shared_stream_reconnect_attempts=3", text)
        self.assertIn("telemetry_path=", text)
        self.assertIn("output_device=default", text)
        self.assertIn("last_exit_status=Shared stream disconnected: -1073741819", text)
        self.assertIn("last_stderr=snapclient audio error", text)
        self.assertIn("start_command=snapclient.exe -h 192.168.0.122", text)

    def test_format_diagnostics_includes_output_device(self):
        text = format_diagnostics(
            {
                "SharedStreamOutputDevice": "Speakers",
                "SharedStreamOutputDevices": ["Headphones", "Speakers"],
            },
        )

        self.assertIn("output_device=Speakers", text)
        self.assertIn("output_devices=Headphones,Speakers", text)

    def test_output_device_control_message_prefix_is_stable(self):
        self.assertEqual(OUTPUT_DEVICE_MESSAGE_PREFIX, b"SLIME_AUDIO_OUTPUT_DEVICE_V1 ")

    def test_publish_active_stream_writes_synthetic_dashboard_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            media = temp / "song.mp3"
            media.write_bytes(b"fake")
            pointer = temp / "active-set.json"
            session = temp / "active-stream-session.json"
            state = temp / "active-stream-state.json"
            receiver = Receiver("127.0.0.1:47777", "127.0.0.1", 47777, "SPONGEBOT", "user", "0.4.28")

            with patch.object(stream, "probe_duration_ms", return_value=123_000):
                stream.publish_active_stream(
                    input_path=media,
                    targets=[receiver],
                    mode="snapcast",
                    backend="ffmpeg",
                    active_pointer=pointer,
                    active_session=session,
                    active_state=state,
                    source_session=None,
                    dashboard_title="Kitchen Sink",
                    dashboard_slug="kitchen-sink",
                    start_offset_ms=12_000,
                    dry_run=False,
                )

            pointer_payload = json.loads(pointer.read_text(encoding="utf-8"))
            session_payload = json.loads(session.read_text(encoding="utf-8"))
            state_payload = json.loads(state.read_text(encoding="utf-8"))

        self.assertEqual(pointer_payload["title"], "Kitchen Sink")
        self.assertEqual(pointer_payload["playback_mode"], "direct-stream")
        self.assertEqual(pointer_payload["active_session_path"], str(session.resolve()))
        self.assertEqual(session_payload["timeline_mode"], "direct-stream")
        self.assertEqual(session_payload["clips"][0]["path"], str(media.resolve()))
        self.assertEqual(state_payload["current"], str(media.resolve()))
        self.assertEqual(state_payload["duration_ms"], 123_000)
        self.assertEqual(state_payload["window_start_ms"], 12_000)
        self.assertEqual(state_payload["receivers"][0]["machine_name"], "SPONGEBOT")

    def test_publish_active_stream_can_reference_source_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            media = temp / "render.mp3"
            media.write_bytes(b"fake")
            source_session = temp / "real-session.json"
            source_session.write_text('{"version": 1, "clips": []}', encoding="utf-8")
            pointer = temp / "active-set.json"
            generated_session = temp / "active-stream-session.json"
            state = temp / "active-stream-state.json"
            receiver = Receiver("127.0.0.1:47777", "127.0.0.1", 47777, "SPONGEBOT", "user", "0.4.28")

            with patch.object(stream, "probe_duration_ms", return_value=60_000):
                stream.publish_active_stream(
                    input_path=media,
                    targets=[receiver],
                    mode="snapcast",
                    backend="ffmpeg",
                    active_pointer=pointer,
                    active_session=generated_session,
                    active_state=state,
                    source_session=source_session,
                    dashboard_title=None,
                    dashboard_slug=None,
                    start_offset_ms=0,
                    dry_run=False,
                )

            pointer_payload = json.loads(pointer.read_text(encoding="utf-8"))

        self.assertEqual(pointer_payload["active_session_path"], str(source_session.resolve()))
        self.assertFalse(generated_session.exists())

    def test_publish_active_stream_rejects_missing_source_session(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            media = temp / "render.mp3"
            media.write_bytes(b"fake")
            receiver = Receiver("127.0.0.1:47777", "127.0.0.1", 47777, "SPONGEBOT", "user", "0.4.28")

            with self.assertRaises(FileNotFoundError):
                stream.publish_active_stream(
                    input_path=media,
                    targets=[receiver],
                    mode="snapcast",
                    backend="ffmpeg",
                    active_pointer=temp / "active-set.json",
                    active_session=temp / "active-stream-session.json",
                    active_state=temp / "active-stream-state.json",
                    source_session=temp / "missing-session.json",
                    dashboard_title=None,
                    dashboard_slug=None,
                    start_offset_ms=0,
                    dry_run=False,
                )

    def test_mark_active_stream_completed_only_for_current_process(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = Path(temp_dir) / "state.json"
            state.write_text(json.dumps({"stream_pid": stream.os.getpid(), "runner_status": "streaming"}), encoding="utf-8")

            stream.mark_active_stream_completed(state, dry_run=False)

            payload = json.loads(state.read_text(encoding="utf-8"))

        self.assertEqual(payload["runner_status"], "completed")
        self.assertIn("completed_at", payload)


if __name__ == "__main__":
    unittest.main()
