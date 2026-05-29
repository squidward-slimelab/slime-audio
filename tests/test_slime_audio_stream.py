import unittest

from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from slime_audio_stream import (
    Receiver,
    encode_audio_packet,
    parse_discovery_response,
    parse_endpoint,
    resolve_targets,
)


class SlimeAudioStreamTests(unittest.TestCase):
    def test_parse_discovery_response(self):
        payload = (
            b'{"App":"slime-audio","MachineName":"SPATULA","UserName":"slimeq",'
            b'"Version":"0.3.0","Port":47777,"UnixTimeMs":1}'
        )

        receiver = parse_discovery_response(payload, "192.168.0.163")

        self.assertIsNotNone(receiver)
        self.assertEqual(receiver.endpoint, "192.168.0.163:47777")
        self.assertEqual(receiver.machine_name, "SPATULA")

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

    def test_encoded_audio_packet_uses_protocol_magic(self):
        import uuid

        packet = encode_audio_packet(uuid.UUID("00000000-0000-0000-0000-000000000001"), 7, 1234, 48000, 2, b"abc")

        self.assertEqual(packet[:4], b"SLA1")
        self.assertEqual(packet[4], 1)
        self.assertEqual(len(packet), 43 + 3)


if __name__ == "__main__":
    unittest.main()
