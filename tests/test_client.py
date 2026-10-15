# tests/test_client.py
import unittest
from typing import ClassVar
from unittest import mock

from client import sshtunnel_client as client


class Settings:
    SSH_HOST = "example.invalid"
    SSH_PORT = "2222"
    SSH_USER = "tester"
    SSH_PKEY = ""
    SSH_PKEY_PASSPHRASE = ""
    SSH_PASSWORD = "pw"
    REMOTE_PORT = "8000"
    LOCAL_PORT = "8001"


class FakeForwarder:
    instances: ClassVar[list] = []
    fail_times: ClassVar[int] = 0

    def __init__(self, address, **kwargs):
        self.address = address
        self.kwargs = kwargs
        self.is_active = False
        self.stopped = False
        FakeForwarder.instances.append(self)

    def start(self):
        if FakeForwarder.fail_times > 0:
            FakeForwarder.fail_times -= 1
            raise RuntimeError("simulated failure")
        self.is_active = True

    def stop(self):
        self.stopped = True
        self.is_active = False


class FakeEvent:
    """Event that never blocks and is never set by itself."""

    def __init__(self):
        self._set = False

    def is_set(self):
        return self._set

    def set(self):
        self._set = True

    def wait(self, timeout=None):
        return self._set


class BackoffTests(unittest.TestCase):
    def test_sequence(self):
        self.assertEqual(client.backoff_delay(0), 1.0)
        self.assertEqual(client.backoff_delay(1), 2.0)
        self.assertEqual(client.backoff_delay(2), 4.0)

    def test_is_capped(self):
        self.assertEqual(client.backoff_delay(99), client.BACKOFF_MAX)

    def test_negative_attempt_is_clamped(self):
        self.assertEqual(client.backoff_delay(-5), 1.0)


class BuildTunnelTests(unittest.TestCase):
    def setUp(self):
        FakeForwarder.instances.clear()
        self.patch = mock.patch.object(
            client, "SSHTunnelForwarder", FakeForwarder
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_password_settings(self):
        client.build_tunnel(Settings)
        kwargs = FakeForwarder.instances[0].kwargs
        self.assertEqual(kwargs["ssh_password"], "pw")
        self.assertNotIn("ssh_pkey", kwargs)
        self.assertEqual(kwargs["local_bind_address"], ("127.0.0.1", 8001))
        self.assertEqual(kwargs["remote_bind_address"], ("127.0.0.1", 8000))
        self.assertEqual(kwargs["set_keepalive"], 30.0)
        self.assertEqual(FakeForwarder.instances[0].address, ("example.invalid", 2222))

    def test_key_settings(self):
        class KeySettings(Settings):
            SSH_PKEY = "~/.ssh/id_ed25519"
            SSH_PKEY_PASSPHRASE = "unlock"

        client.build_tunnel(KeySettings)
        kwargs = FakeForwarder.instances[0].kwargs
        self.assertNotIn("ssh_password", kwargs)
        self.assertIn("id_ed25519", kwargs["ssh_pkey"])
        self.assertNotIn("~", kwargs["ssh_pkey"])
        self.assertEqual(kwargs["ssh_private_key_password"], "unlock")

    def test_missing_driver_is_reported(self):
        with mock.patch.object(client, "SSHTunnelForwarder", None), self.assertRaises(
            RuntimeError
        ):
            client.build_tunnel(Settings)


class RunLoopTests(unittest.TestCase):
    def setUp(self):
        FakeForwarder.instances.clear()
        self.patch = mock.patch.object(
            client, "SSHTunnelForwarder", FakeForwarder
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_gives_up_after_max_attempts(self):
        FakeForwarder.fail_times = 99
        with mock.patch.object(client, "_print_banner"):
            result = client.run(Settings, stop_event=FakeEvent(), max_attempts=3)
        self.assertEqual(result, 1)
        self.assertEqual(len(FakeForwarder.instances), 3)
        self.assertTrue(all(item.stopped for item in FakeForwarder.instances))

    def test_retries_then_succeeds(self):
        FakeForwarder.fail_times = 2
        stop = FakeEvent()

        class StoppingForwarder(FakeForwarder):
            def start(self):
                super().start()
                if self.is_active:
                    stop.set()

        with mock.patch.object(
            client, "SSHTunnelForwarder", StoppingForwarder
        ), mock.patch.object(client, "_print_banner"):
            result = client.run(Settings, stop_event=stop)
        self.assertEqual(result, 0)
        self.assertEqual(len(StoppingForwarder.instances), 3)


if __name__ == "__main__":
    unittest.main()
