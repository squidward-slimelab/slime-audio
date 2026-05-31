import unittest

from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from slime_audio_stream import (
    EFFECT_MESSAGE_PREFIX,
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
        self.assertIn("telemetry_path=", text)


if __name__ == "__main__":
    unittest.main()
