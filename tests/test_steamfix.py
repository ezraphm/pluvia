import asyncio
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import steamfix as sf  # noqa: E402

PAC = "http://127.0.0.1:31627/proxy.pac"


class Rec:
    """Stands in for subprocess.run: records argv, refuses shell use."""

    def __init__(self, calls, returncode=0):
        self.calls = calls
        self.returncode = returncode

    def __call__(self, cmd, **kw):
        assert isinstance(cmd, list), "argv must be a list, got %r" % (cmd,)
        assert all(isinstance(a, str) for a in cmd), "non-str argv: %r" % (cmd,)
        assert kw.get("shell") is not True, "shell=True is forbidden"
        assert not any(isinstance(a, (list, tuple, dict)) for a in cmd)
        self.calls.append(list(cmd))
        return mock.Mock(returncode=self.returncode, stdout="", stderr="")


# ------------------------------------------------------------------ ClientHello builder

def _ext(etype, data):
    return struct.pack(">HH", etype, len(data)) + data


def build_hello(host="store.steampowered.com", session=b"", with_sni=True, extra_ext=True):
    """Hand-built TLS 1.2 ClientHello record; returns (record, expected SNI offset)."""
    exts = b""
    want = None
    if extra_ext:
        exts += _ext(0x000A, b"\x00\x02\x00\x17")            # supported_groups
    if with_sni:
        name = host.encode()
        data = struct.pack(">H", 3 + len(name)) + b"\x00" + struct.pack(">H", len(name)) + name
        exts += _ext(0x0000, data)
        want = len(exts) - len(data) + 5                       # list len 2 + type 1 + name len 2
    if extra_ext:
        exts += _ext(0x002B, b"\x03\x04\x03\x03")             # supported_versions
        exts += _ext(0x0015, b"\x00")                          # padding
    body = b"\x03\x03" + bytes(range(32))
    body += bytes([len(session)]) + session
    body += struct.pack(">H", 2) + b"\x13\x01"                 # one cipher suite
    body += b"\x01\x00"                                        # null compression
    body += struct.pack(">H", len(exts)) + exts
    hs = b"\x01" + len(body).to_bytes(3, "big") + body
    rec = b"\x16\x03\x01" + len(hs).to_bytes(2, "big") + hs
    return rec, (None if want is None else 52 + len(session) + want)


class SniSpan(unittest.TestCase):
    def test_finds_name_in_hand_built_hello(self):
        rec, want = build_hello()
        self.assertEqual(sf.sni_span(rec), (want, len("store.steampowered.com")))
        off, ln = sf.sni_span(rec)
        self.assertEqual(rec[off:off + ln], b"store.steampowered.com")
        self.assertEqual(rec[off - 2:off], struct.pack(">H", ln))

    def test_session_id_shifts_the_offset(self):
        rec, want = build_hello(session=bytes(range(32)))
        self.assertEqual(want, 101)
        self.assertEqual(sf.sni_span(rec), (want, len("store.steampowered.com")))
        self.assertEqual(rec[want:want + 22], b"store.steampowered.com")

    def test_no_sni_extension(self):
        rec, want = build_hello(with_sni=False)
        self.assertIsNone(want)
        self.assertIsNone(sf.sni_span(rec))

    def test_not_a_handshake_record(self):
        rec, _ = build_hello()
        self.assertIsNone(sf.sni_span(b"\x17\x03\x03" + rec[3:]))       # application data
        self.assertIsNone(sf.sni_span(b"\x16\x03\x01\x00\x05\x02hello"))  # ServerHello

    def test_truncated_and_empty_input(self):
        rec, _ = build_hello()
        for bad in (b"", b"\x16", b"\x16\x03\x01\x00", rec[:30], rec[:45], rec[:60]):
            self.assertIsNone(sf.sni_span(bad), bad)


# ---------------------------------------------------------------------- validation

class ValidateApplyNetwork(unittest.TestCase):
    def setUp(self):
        svcs = mock.patch.object(sf, "network_services", return_value={"Wi-Fi", "Ethernet", "Tailscale"})
        svcs.start()
        self.addCleanup(svcs.stop)
        self.no_io = mock.patch.multiple(sf, sh=mock.Mock(side_effect=AssertionError("io")),
                                         subprocess=mock.Mock())
        self.no_io.start()
        self.addCleanup(self.no_io.stop)

    def rejects(self, svc="Wi-Fi", dns="google", url=PAC, mtu=None):
        with self.assertRaises(ValueError):
            sf.apply_network(svc, dns, url, mtu)

    def test_service_must_be_a_known_name(self):
        for svc in ["Wi-Fi ", "* Wi-Fi", "Wi-Fi\n", "lo0", "", None, 42, ["Wi-Fi"],
                    "Wi-Fi; rm -rf /", "Wi-Fi\0", "An asterisk (*) denotes that a network service is disabled."]:
            with self.subTest(svc=svc):
                self.rejects(svc=svc)

    def test_dns_name_must_be_a_dns_sets_key(self):
        for dns in ["Google", "fastest", "", None, ["google"], "google\0", "8.8.8.8"]:
            with self.subTest(dns=dns):
                self.rejects(dns=dns)

    def test_pac_url_must_be_our_loopback_pac(self):
        for url in ["http://127.0.0.1:31627/proxy.pac\n", "http://127.0.0.1:99999/proxy.pac",
                    "https://127.0.0.1:31627/proxy.pac", "http://localhost:31627/proxy.pac",
                    "http://127.0.0.1:31627/", "http://10.0.0.1:31627/proxy.pac",
                    "http://127.0.0.1:0/proxy.pac", "http://127.0.0.1.evil:31627/proxy.pac",
                    "", None, 31627]:
            with self.subTest(url=url):
                self.rejects(url=url)

    def test_mtu_range_and_type(self):
        for mtu in [575, 1501, -1, 0, True, 900.0, "900", [900]]:
            with self.subTest(mtu=mtu):
                self.rejects(mtu=mtu)

    def test_pac_url_helper_matches_the_contract(self):
        self.assertEqual(sf.pac_url(), PAC)
        self.assertEqual(sf.pac_url(40000), "http://127.0.0.1:40000/proxy.pac")
        for port in [0, -1, 65536, "31627", True, 3.5]:
            with self.subTest(port=port):
                with self.assertRaises(ValueError):
                    sf.pac_url(port)


# ------------------------------------------------------------------ active_service

ROUTE = "   route to: default\ndestination: default\n       interface: %s\n"
ORDER = ("An asterisk (*) denotes that a network service is disabled.\n"
         "(1) Ethernet\n(Hardware Port: Ethernet, Device: en0)\n\n"
         "%s(2) Wi-Fi\n(Hardware Port: Wi-Fi, Device: en1)\n\n"
         "(3) Tailscale\n(Hardware Port: Tailscale, Device: utun6)\n")


class NetworkServices(unittest.TestCase):
    def test_parses_listallnetworkservices_and_skips_disabled(self):
        out = ("An asterisk (*) denotes that a network service is disabled.\n"
               "Wi-Fi\n*Thunderbolt Bridge\nUSB 10/100/1000 LAN\n")
        with mock.patch.object(sf, "sh", return_value=out):
            self.assertEqual(sf.network_services(), {"Wi-Fi", "USB 10/100/1000 LAN"})


class ActiveService(unittest.TestCase):
    def call(self, dev, star=""):
        def fake_sh(*args, **kw):
            if args[0] == "route":
                return ROUTE % dev if dev else ""
            return ORDER % star
        with mock.patch.object(sf, "sh", side_effect=fake_sh):
            return sf.active_service()

    def test_maps_device_to_service_name(self):
        self.assertEqual(self.call("en1"), "Wi-Fi")
        self.assertEqual(self.call("en0"), "Ethernet")
        self.assertEqual(self.call("utun6"), "Tailscale")

    def test_disabled_service_marker_is_stripped(self):
        self.assertEqual(self.call("en1", star="* "), "Wi-Fi")

    def test_order_output_with_crlf_is_understood(self):
        def fake_sh(*args, **kw):
            if args[0] == "route":
                return ROUTE % "en1"
            return (ORDER % "").replace("\n", "\r\n")
        with mock.patch.object(sf, "sh", side_effect=fake_sh):
            self.assertEqual(sf.active_service(), "Wi-Fi")

    def test_unknown_device_and_no_route_raise(self):
        with self.assertRaises(RuntimeError):
            self.call("en9")
        with self.assertRaises(RuntimeError):
            self.call("")


# --------------------------------------------------------------- state save/restore

class StateFile(unittest.TestCase):
    def setUp(self):
        svcs = mock.patch.object(sf, "network_services", return_value={"Wi-Fi", "Ethernet", "Tailscale"})
        svcs.start()
        self.addCleanup(svcs.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(sf, "STATE", os.path.join(self.tmp.name, "saf-state.json"))
        p.start()
        self.addCleanup(p.stop)
        self.calls = []
        self.dns = "There aren't any DNS Servers set on Wi-Fi.\n"
        self.proxy_url = "URL: (null)\nEnabled: No\n"
        self.mtu = "Current Setting: 1500\n"
        p = mock.patch.object(sf, "sh", side_effect=self.fake_sh)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(sf.subprocess, "run", Rec(self.calls))
        p.start()
        self.addCleanup(p.stop)

    def fake_sh(self, *args, **kw):
        if args[:2] == ("networksetup", "-getdnsservers"):
            return self.dns
        if args[:2] == ("networksetup", "-getautoproxyurl"):
            return self.proxy_url
        if args[:2] == ("networksetup", "-getMTU"):
            return self.mtu
        raise AssertionError("unexpected read: %r" % (args,))

    def state(self):
        with open(sf.STATE, encoding="utf-8") as f:
            return json.load(f)

    def test_apply_saves_state_and_configures_service(self):
        st = sf.apply_network("Wi-Fi", "cloudflare", PAC)
        self.assertEqual(st["dns"], ["1.1.1.1", "1.0.0.1"])
        self.assertEqual(self.state(), {"svc": "Wi-Fi", "keep_dns": False, "dns": [],
                                        "pac_url": "", "pac_on": False, "mtu": 1500})
        self.assertEqual(self.calls, [
            ["networksetup", "-setdnsservers", "Wi-Fi", "1.1.1.1", "1.0.0.1"],
            ["dscacheutil", "-flushcache"],
            ["killall", "-HUP", "mDNSResponder"],
            ["networksetup", "-setautoproxyurl", "Wi-Fi", PAC],
            ["networksetup", "-setautoproxystate", "Wi-Fi", "on"],
        ])

    def test_second_apply_is_refused_and_keeps_the_original_state(self):
        sf.apply_network("Wi-Fi", "google", PAC)
        first = self.state()
        n = len(self.calls)
        with self.assertRaises(RuntimeError):
            sf.apply_network("Wi-Fi", "cloudflare", PAC)
        self.assertEqual(self.state(), first)
        self.assertEqual(len(self.calls), n)

    def test_apply_with_mtu_sets_it_before_the_proxy(self):
        sf.apply_network("Ethernet", "google", PAC, mtu=1500)
        self.assertEqual(self.calls[3], ["networksetup", "-setMTU", "Ethernet", "1500"])
        self.assertEqual(self.calls[4], ["networksetup", "-setautoproxyurl", "Ethernet", PAC])

    def test_apply_does_not_set_mtu_when_not_asked(self):
        sf.apply_network("Tailscale", "quad9", PAC)
        self.assertFalse(any("-setMTU" in c for c in self.calls))

    def test_restore_roundtrips_old_values_and_removes_state(self):
        sf.save_state("Wi-Fi")
        self.dns = "1.1.1.1\n9.9.9.9\n"
        self.proxy_url = "URL: http://old.example/proxy.pac\nEnabled: Yes\n"
        self.mtu = "Current Setting: 1400\n"
        sf.save_state("Wi-Fi")
        self.calls.clear()
        self.assertEqual(sf.restore_network(), "Wi-Fi")
        self.assertEqual(self.calls, [
            ["networksetup", "-setdnsservers", "Wi-Fi", "1.1.1.1", "9.9.9.9"],
            ["networksetup", "-setautoproxyurl", "Wi-Fi", "http://old.example/proxy.pac"],
            ["networksetup", "-setautoproxystate", "Wi-Fi", "on"],
            ["networksetup", "-setMTU", "Wi-Fi", "1400"],
            ["dscacheutil", "-flushcache"],
            ["killall", "-HUP", "mDNSResponder"],
        ])
        self.assertFalse(os.path.exists(sf.STATE))

    def test_restore_clears_dns_and_proxy_when_there_was_none(self):
        sf.apply_network("Wi-Fi", "google", PAC)
        self.calls.clear()
        sf.restore_network()
        self.assertEqual(self.calls[0], ["networksetup", "-setdnsservers", "Wi-Fi", "Empty"])
        self.assertEqual(self.calls[1], ["networksetup", "-setautoproxystate", "Wi-Fi", "off"])
        self.assertNotIn("-setautoproxyurl", [c[1] for c in self.calls])
        self.assertFalse(os.path.exists(sf.STATE))

    def test_restore_without_state_is_a_noop(self):
        self.assertIsNone(sf.restore_network())
        self.assertEqual(self.calls, [])

    def test_restore_refuses_a_state_file_with_an_unknown_service(self):
        with open(sf.STATE, "w", encoding="utf-8") as f:
            json.dump({"svc": "Wi-Fi; touch /tmp/pwned", "dns": []}, f)
        with self.assertRaises(ValueError):
            sf.restore_network()
        self.assertEqual(self.calls, [])
        self.assertTrue(os.path.exists(sf.STATE))

    def test_restore_refuses_a_saved_dns_list_that_is_not_ip_literals(self):
        with open(sf.STATE, "w", encoding="utf-8") as f:
            json.dump({"svc": "Wi-Fi", "dns": ["8.8.8.8", "-setautoproxyurl"]}, f)
        with self.assertRaises(ValueError):
            sf.restore_network()
        self.assertEqual(self.calls, [])

    def test_restore_accepts_ipv6_servers(self):
        with open(sf.STATE, "w", encoding="utf-8") as f:
            json.dump({"svc": "Wi-Fi", "dns": ["2001:4860:4860::8888"], "pac_url": "", "pac_on": False}, f)
        with mock.patch.object(sf, "sh", side_effect=self.fake_sh):
            self.assertEqual(sf.restore_network(), "Wi-Fi")
        self.assertEqual(self.calls[0], ["networksetup", "-setdnsservers", "Wi-Fi", "2001:4860:4860::8888"])

    def test_state_file_is_private_and_never_written_through_a_symlink(self):
        sf.save_state("Wi-Fi")
        self.assertEqual(os.stat(sf.STATE).st_mode & 0o777, 0o600)
        os.remove(sf.STATE)
        target = os.path.join(self.tmp.name, "target.json")
        with open(target, "w", encoding="utf-8") as f:
            f.write("untouched")
        os.symlink(target, sf.STATE)
        with self.assertRaises(OSError):
            sf.save_state("Wi-Fi")
        with open(target, encoding="utf-8") as f:
            self.assertEqual(f.read(), "untouched")

    def test_restore_refuses_a_symlinked_state_file(self):
        target = os.path.join(self.tmp.name, "target.json")
        with open(target, "w", encoding="utf-8") as f:
            json.dump({"svc": "Wi-Fi", "dns": [], "pac_url": "", "pac_on": False}, f)
        os.symlink(target, sf.STATE)
        with self.assertRaises(ValueError):
            sf.restore_network()
        self.assertEqual(self.calls, [])

    def test_failed_apply_keeps_state_for_recovery(self):
        calls = []

        def flaky(cmd, **kw):
            calls.append(list(cmd))
            rc = 9 if "-setautoproxyurl" in cmd else 0
            return mock.Mock(returncode=rc, stdout="", stderr="nope")

        with mock.patch.object(sf.subprocess, "run", side_effect=flaky):
            with self.assertRaises(RuntimeError):
                sf.apply_network("Wi-Fi", "google", PAC)
        self.assertEqual(calls[-1][:2], ["networksetup", "-setautoproxyurl"])
        self.assertEqual(self.state()["svc"], "Wi-Fi")

    def test_apply_prompts_for_admin_when_networksetup_is_denied(self):
        calls = []

        def denied_then_ok(cmd, **kw):
            calls.append(list(cmd))
            if cmd[0] == "networksetup":
                return mock.Mock(returncode=1, stdout="", stderr="Authorization denied")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(sf.subprocess, "run", side_effect=denied_then_ok):
            st = sf.apply_network("Wi-Fi", "google", PAC)
        self.assertTrue(st["active"])
        self.assertEqual([c[0] for c in calls], [
            "networksetup", "osascript",                       # -setdnsservers
            "dscacheutil", "killall",                          # flush_dns
            "networksetup", "osascript",                       # -setautoproxyurl
            "networksetup", "osascript",                       # -setautoproxystate
        ])
        self.assertIn("with administrator privileges", calls[1][2])


# ------------------------------------------------- networksetup admin fallback

def reply(returncode=0, stdout="", stderr=""):
    return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)


DENIED = reply(1, stderr="Authorization denied: you need admin rights for this change")
CANCELED = reply(1, stdout="", stderr='execution error: User canceled. (-128)')


class Recorder:
    """subprocess.run stand-in: replays canned replies, records argv, refuses extras."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, cmd, **kw):
        assert isinstance(cmd, list), "argv must be a list, got %r" % (cmd,)
        assert all(isinstance(a, str) for a in cmd), "non-str argv: %r" % (cmd,)
        assert kw.get("shell") is not True, "shell=True is forbidden"
        self.calls.append(list(cmd))
        if not self.replies:
            raise AssertionError("unexpected extra subprocess.run: %r" % (cmd,))
        return self.replies.pop(0)


class NeedsAdmin(unittest.TestCase):
    def test_permission_wording_is_recognised_on_any_exit_status(self):
        for p in [reply(1, stderr="Authorization denied"),
                  reply(1, stderr="you are not permitted to do that"),
                  reply(7, stderr="NOT PERMITTED"),
                  reply(1, stdout="Authorization denied"),
                  reply(1, stderr="this tool requires administrator privileges")]:
            with self.subTest(p=(p.returncode, p.stderr, p.stdout)):
                self.assertTrue(sf._needs_admin(p))

    def test_everything_else_stays_a_plain_failure(self):
        for p in [reply(0), reply(0, stderr="authorization denied"),   # success is never retried
                  reply(9, stderr="Invalid parameter"),
                  reply(1, stderr=""),
                  reply(1),
                  reply(2, stderr="requires admin"),                   # only status 1, per contract
                  reply(1, stderr="Error: The parameters were not valid.")]:
            with self.subTest(p=(p.returncode, p.stderr)):
                self.assertFalse(sf._needs_admin(p))

    def test_none_streams_are_harmless(self):
        self.assertFalse(sf._needs_admin(mock.Mock(returncode=1, stdout=None, stderr=None)))


class AppleScriptQuote(unittest.TestCase):
    def test_plain_argv_becomes_one_shell_line(self):
        self.assertEqual(
            sf._applescript_quote(["networksetup", "-setdnsservers", "Wi-Fi", "8.8.8.8"]),
            "networksetup -setdnsservers Wi-Fi 8.8.8.8")

    def test_a_service_name_with_spaces_is_one_shell_word(self):
        self.assertEqual(
            sf._applescript_quote(["networksetup", "-setdnsservers", "USB 10/100/1000 LAN", "8.8.8.8"]),
            "networksetup -setdnsservers 'USB 10/100/1000 LAN' 8.8.8.8")

    def test_quotes_and_backslashes_are_escaped_after_shell_quoting(self):
        self.assertEqual(sf._applescript_quote(['x"y']), "'x\\\"y'")
        self.assertEqual(sf._applescript_quote(["c\\d"]), "'c\\\\d'")
        # a hostile-looking name stays inside the single quotes AppleScript will pass to sh
        out = sf._applescript_quote(['Wi"; do shell script "rm -rf /'])
        self.assertNotIn('"', out.replace('\\"', ""))

    def test_control_characters_are_refused(self):
        for bad in ["Wi-Fi\n", "Wi-Fi\r", "Wi-Fi\0", "Wi-Fi\t"]:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    sf._applescript_quote(["networksetup", "-getdnsservers", bad])


class NetworksetupAdminFallback(unittest.TestCase):
    def patch_run(self, *replies):
        rec = Recorder(*replies)
        p = mock.patch.object(sf.subprocess, "run", rec)
        p.start()
        self.addCleanup(p.stop)
        return rec

    def test_success_is_used_as_is_without_a_prompt(self):
        rec = self.patch_run(reply(0))
        p = sf._networksetup("-setdnsservers", "Wi-Fi", "1.1.1.1", check=True)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(rec.calls, [["networksetup", "-setdnsservers", "Wi-Fi", "1.1.1.1"]])

    def test_permission_error_falls_back_exactly_once(self):
        rec = self.patch_run(DENIED, reply(0, stdout=""))
        p = sf._networksetup("-setautoproxystate", "Wi-Fi", "on", check=True)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(len(rec.calls), 2)
        self.assertEqual(rec.calls[1][:2], ["osascript", "-e"])
        self.assertEqual(rec.calls[1][2], 'do shell script "networksetup -setautoproxystate '
                                          'Wi-Fi on" with administrator privileges')

    def test_fallback_failure_raises_runtime_error_and_never_loops(self):
        rec = self.patch_run(DENIED, CANCELED, reply(0))
        with self.assertRaises(RuntimeError) as ctx:
            sf._networksetup("-setautoproxystate", "Wi-Fi", "on", check=True)
        self.assertIn("User canceled", str(ctx.exception))
        self.assertEqual([c[0] for c in rec.calls], ["networksetup", "osascript"])
        self.assertEqual(len(rec.replies), 1)                      # the spare reply is untouched

    def test_non_permission_failure_raises_without_a_prompt(self):
        rec = self.patch_run(reply(9, stderr="Invalid parameter"))
        with self.assertRaises(RuntimeError):
            sf._networksetup("-setdnsservers", "Wi-Fi", "bogus", check=True)
        self.assertEqual([c[0] for c in rec.calls], ["networksetup"])

    def test_service_name_with_spaces_is_quoted_inside_the_prompt(self):
        rec = self.patch_run(reply(1, stderr="not permitted"), reply(0))
        sf._networksetup("-setdnsservers", "USB 10/100/1000 LAN", "8.8.8.8", check=True)
        script = rec.calls[1][2]
        self.assertTrue(script.startswith('do shell script "'))
        self.assertTrue(script.endswith('" with administrator privileges'))
        self.assertIn("'USB 10/100/1000 LAN'", script)
        self.assertEqual(script, 'do shell script "networksetup -setdnsservers '
                                 "'USB 10/100/1000 LAN' 8.8.8.8\" with administrator privileges")

    def test_admin_prompt_that_cannot_start_reports_the_original_failure(self):
        calls = []

        def run(cmd, **kw):
            calls.append(list(cmd))
            if cmd[0] == "osascript":
                raise OSError("osascript missing")
            return DENIED

        with mock.patch.object(sf.subprocess, "run", side_effect=run):
            with self.assertRaises(RuntimeError) as ctx:
                sf._networksetup("-setautoproxystate", "Wi-Fi", "on", check=True)
        self.assertIn("Authorization denied", str(ctx.exception))
        self.assertEqual([c[0] for c in calls], ["networksetup", "osascript"])

    def test_check_false_keeps_the_result_instead_of_raising(self):
        self.patch_run(DENIED, CANCELED)
        p = sf._networksetup("-setautoproxystate", "Wi-Fi", "on")
        self.assertEqual(p.returncode, 1)


# ------------------------------------------------------------------------- status

class Status(unittest.TestCase):
    def setUp(self):
        svcs = mock.patch.object(sf, "network_services", return_value={"Wi-Fi", "Ethernet", "Tailscale"})
        svcs.start()
        self.addCleanup(svcs.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(sf, "STATE", os.path.join(self.tmp.name, "saf-state.json"))
        p.start()
        self.addCleanup(p.stop)

    def with_sh(self, route, dns="There aren't any DNS Servers set on Wi-Fi.\n"):
        def fake_sh(*args, **kw):
            if args[0] == "route":
                return route
            if args[0] == "networksetup" and args[1] == "-listnetworkserviceorder":
                return ORDER % ""
            if args[0] == "networksetup" and args[1] == "-getdnsservers":
                return dns
            raise AssertionError("unexpected read: %r" % (args,))
        return mock.patch.object(sf, "sh", side_effect=fake_sh)

    def test_inactive_reports_the_live_service(self):
        with self.with_sh(ROUTE % "en1", dns="8.8.8.8\n"):
            self.assertEqual(sf.status(), {"active": False, "svc": "Wi-Fi", "dns": ["8.8.8.8"]})

    def test_active_reads_the_state_file(self):
        with open(sf.STATE, "w", encoding="utf-8") as f:
            json.dump({"svc": "Ethernet", "dns": []}, f)
        with self.with_sh(ROUTE % "en0", dns="There aren't any DNS Servers set on Ethernet.\n"):
            self.assertEqual(sf.status(), {"active": True, "svc": "Ethernet", "dns": []})

    def test_no_default_route_reports_nothing(self):
        with self.with_sh(""):
            self.assertEqual(sf.status(), {"active": False, "svc": None, "dns": []})

    def test_corrupt_state_is_not_active(self):
        with open(sf.STATE, "w", encoding="utf-8") as f:
            f.write("{not json")
        with self.with_sh(ROUTE % "en1"):
            self.assertEqual(sf.status()["active"], False)


# ---------------------------------------------------------------- DNS over UDP

class FakeSock:
    def __init__(self, response):
        self.response = response
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def settimeout(self, t):
        self.timeout = t

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def recv(self, n):
        return self.response


def dns_response(ips, question="store.steampowered.com", ancount=None):
    out = struct.pack(">HHHHHH", 0x1234, 0x8180, 1, len(ips) if ancount is None else ancount, 0, 0)
    for label in question.split("."):
        out += bytes([len(label)]) + label.encode()
    out += b"\x00" + struct.pack(">HH", 1, 1)
    for ip in ips:
        out += b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 300, 4) + socket.inet_aton(ip)
    return out


def dns_response_aaaa(question="store.steampowered.com"):
    """A reply that answers, but only with an AAAA record: no IPv4 to use."""
    out = struct.pack(">HHHHHH", 0x1234, 0x8180, 1, 1, 0, 0)
    for label in question.split("."):
        out += bytes([len(label)]) + label.encode()
    out += b"\x00" + struct.pack(">HH", 1, 1)
    out += b"\xc0\x0c" + struct.pack(">HHIH", 28, 1, 300, 16) + b"\x00" * 16
    return out


class DnsQuery(unittest.TestCase):
    def run_query(self, response, host="store.steampowered.com", server="8.8.8.8"):
        sock = FakeSock(response)
        with mock.patch.object(sf.socket, "socket", return_value=sock):
            return sf.dns_query(host, server), sock

    def test_parses_all_a_records(self):
        ips, sock = self.run_query(dns_response(["1.2.3.4", "5.6.7.8"]))
        self.assertEqual(ips, ["1.2.3.4", "5.6.7.8"])
        self.assertEqual(sock.sent[0][1], ("8.8.8.8", 53))
        self.assertEqual(sock.timeout, 4)

    def test_query_encodes_labels_and_type(self):
        _, sock = self.run_query(dns_response(["1.2.3.4"]), host="a.bc")
        q = sock.sent[0][0]
        self.assertEqual(q[12:], b"\x01a\x02bc\x00" + struct.pack(">HH", 1, 1))

    def test_no_a_record_raises(self):
        with self.assertRaises(ValueError):
            self.run_query(dns_response([], ancount=0))
        with self.assertRaises(ValueError):
            self.run_query(dns_response_aaaa())

    def test_timeout_propagates(self):
        sock = FakeSock(dns_response(["1.2.3.4"]))

        def boom(n):
            raise socket.timeout("timed out")
        sock.recv = boom
        with mock.patch.object(sf.socket, "socket", return_value=sock):
            with self.assertRaises(socket.timeout):
                sf.dns_query("store.steampowered.com", "8.8.8.8")


class DnsSets(unittest.TestCase):
    def test_keys_are_the_documented_providers(self):
        self.assertEqual(set(sf.DNS_SETS), {"google", "cloudflare", "controld", "quad9", "opendns",
                                            "adguard", "nextdns", "alidns", "dnssb", "comodo"})
        self.assertEqual(list(sf.DNS_SETS)[0], "google")

    def test_every_set_is_two_distinct_ipv4_servers(self):
        for name, servers in sf.DNS_SETS.items():
            with self.subTest(provider=name):
                self.assertEqual(len(servers), 2)
                self.assertEqual(len(set(servers)), 2)
                for ip in servers:
                    self.assertEqual(socket.inet_aton(ip).__len__(), 4)

    def test_fastest_dns_picks_the_reachable_provider(self):
        def fake_query(host, server, timeout=4):
            if server == sf.DNS_SETS["quad9"][0]:
                return ["1.2.3.4"]
            raise OSError("unreachable")
        with mock.patch.object(sf, "dns_query", side_effect=fake_query):
            best, table = sf.fastest_dns()
        self.assertEqual(best, "quad9")
        self.assertEqual(len(table), len(sf.DNS_SETS))
        self.assertEqual(table[0][1], "quad9")
        self.assertEqual([name for _ms, name in table[1:]], sorted(n for n in sf.DNS_SETS if n != "quad9"))
        self.assertTrue(all(ms == float("inf") for ms, name in table if name != "quad9"))

    def test_fastest_dns_prefers_the_quicker_answer(self):
        def fake_query(host, server, timeout=4):
            if server == sf.DNS_SETS["google"][0]:
                return ["1.2.3.4"]
            time.sleep(0.06)
            return ["5.6.7.8"]
        with mock.patch.object(sf, "dns_query", side_effect=fake_query):
            best, _ = sf.fastest_dns()
        self.assertEqual(best, "google")

    def test_fastest_dns_raises_when_nothing_answers(self):
        with mock.patch.object(sf, "dns_query", side_effect=OSError("down")):
            with self.assertRaises(RuntimeError):
                sf.fastest_dns()


class Resolve(unittest.TestCase):
    def setUp(self):
        sf.clear_dns_cache()
        self.addCleanup(sf.clear_dns_cache)

    def test_caches_per_host_and_passes_the_server(self):
        with mock.patch.object(sf, "_resolve", return_value=["1.1.1.1", "2.2.2.2"]) as r:
            self.assertEqual(sf.resolve("cdn.example", "9.9.9.9"), ["1.1.1.1", "2.2.2.2"])
            self.assertEqual(sf.resolve("cdn.example", "9.9.9.9"), ["1.1.1.1", "2.2.2.2"])
        r.assert_called_once_with("cdn.example", "9.9.9.9")

    def test_demote_rotates_a_failed_ip_to_the_back(self):
        with mock.patch.object(sf, "_resolve", return_value=["1.1.1.1", "2.2.2.2", "3.3.3.3"]):
            sf.resolve("cdn.example")
        sf.demote_ip("cdn.example", "1.1.1.1")
        self.assertEqual(sf.resolve("cdn.example"), ["2.2.2.2", "3.3.3.3", "1.1.1.1"])

    def test_demote_keeps_the_last_ip(self):
        with mock.patch.object(sf, "_resolve", return_value=["1.1.1.1"]):
            sf.resolve("cdn.example")
        sf.demote_ip("cdn.example", "1.1.1.1")
        self.assertEqual(sf.resolve("cdn.example"), ["1.1.1.1"])


# ------------------------------------------------------------ reachability check

class CheckSteam(unittest.TestCase):
    """Every network call is mocked: no DNS query and no TLS connection leaves this machine."""

    def run_check(self, system=None, google=None, tls=True):
        """system/google: a return value or an exception instance; tls: bool or a side_effect callable."""
        def patch(name, value):
            spec = {"side_effect": value} if isinstance(value, Exception) else {"return_value": value}
            return mock.patch.object(sf, name, **spec)

        tls_mock = mock.Mock(return_value=tls) if isinstance(tls, bool) else mock.Mock(side_effect=tls)
        with patch("system_resolve", system), patch("google_resolve", google), \
             mock.patch.object(sf, "tls_handshake", tls_mock):
            res = sf.check_steam()
        self.tls = tls_mock
        return res

    def test_ok_when_the_system_address_completes_tls(self):
        res = self.run_check(system=["203.0.113.5"], google=["198.51.100.7"], tls=True)
        self.assertEqual(res["verdict"], "ok")
        self.assertEqual(res["system_ip"], "203.0.113.5")
        self.assertEqual(res["google_ip"], "198.51.100.7")
        self.assertIn("203.0.113.5", res["detail"])
        self.tls.assert_called_once_with("203.0.113.5", sf.STEAM_CHECK_HOST, sf.CHECK_TIMEOUT)

    def test_ok_when_system_and_google_agree(self):
        res = self.run_check(system=["203.0.113.5"], google=["203.0.113.5"], tls=True)
        self.assertEqual(res["verdict"], "ok")
        self.assertEqual(res["system_ip"], res["google_ip"])

    def test_dns_poisoned_when_system_ip_differs_and_fails_while_google_works(self):
        res = self.run_check(system=["10.0.0.1"], google=["203.0.113.5"],
                             tls=lambda ip, *a, **k: ip == "203.0.113.5")
        self.assertEqual(res["verdict"], "dns_poisoned")
        self.assertEqual(res["system_ip"], "10.0.0.1")
        self.assertEqual(res["google_ip"], "203.0.113.5")
        self.assertIn("10.0.0.1", res["detail"])
        self.assertEqual([c.args[0] for c in self.tls.call_args_list], ["10.0.0.1", "203.0.113.5"])

    def test_dns_poisoned_when_the_system_resolver_returns_nothing(self):
        res = self.run_check(system=OSError("no answer"), google=["203.0.113.5"], tls=True)
        self.assertEqual(res["verdict"], "dns_poisoned")
        self.assertIsNone(res["system_ip"])
        self.assertEqual(res["google_ip"], "203.0.113.5")
        self.assertIn("returned nothing", res["detail"])

    def test_dpi_blocked_when_both_handshakes_fail(self):
        res = self.run_check(system=["203.0.113.5"], google=["198.51.100.7"], tls=False)
        self.assertEqual(res["verdict"], "dpi_blocked")
        self.assertEqual(res["system_ip"], "203.0.113.5")
        self.assertEqual(res["google_ip"], "198.51.100.7")
        self.assertEqual(self.tls.call_count, 2)
        self.assertIn("203.0.113.5", res["detail"])

    def test_dpi_blocked_without_a_system_address(self):
        res = self.run_check(system=OSError("boom"), google=["198.51.100.7"], tls=False)
        self.assertEqual(res["verdict"], "dpi_blocked")
        self.assertIsNone(res["system_ip"])
        self.assertIn("none resolved", res["detail"])

    def test_offline_when_nothing_resolves(self):
        res = self.run_check(system=OSError("offline"), google=OSError("timeout"), tls=True)
        self.assertEqual(res["verdict"], "offline")
        self.assertIsNone(res["system_ip"])
        self.assertIsNone(res["google_ip"])
        self.tls.assert_not_called()

    def test_offline_when_both_resolvers_return_an_empty_list(self):
        self.assertEqual(self.run_check(system=[], google=[])["verdict"], "offline")

    def test_same_ip_that_works_on_the_retry_is_ok_not_poisoned(self):
        attempts = []

        def tls(ip, *a, **k):
            attempts.append(ip)
            return len(attempts) > 1        # first attempt dropped, second one handshakes

        res = self.run_check(system=["203.0.113.5"], google=["203.0.113.5"], tls=tls)
        self.assertEqual(res["verdict"], "ok")
        self.assertEqual(attempts, ["203.0.113.5", "203.0.113.5"])

    def test_result_shape_has_exactly_the_four_json_keys(self):
        res = self.run_check(system=["203.0.113.5"], google=["203.0.113.5"], tls=True)
        self.assertEqual(set(res), {"verdict", "system_ip", "google_ip", "detail"})
        self.assertIn(res["verdict"], sf.VERDICTS)
        json.dumps(res)  # must be printable by --json as-is

    def test_check_passes_the_timeout_to_the_dns_query_and_the_handshake(self):
        with mock.patch.object(sf, "system_resolve", return_value=["203.0.113.5"]) as sysr, \
             mock.patch.object(sf, "google_resolve", return_value=["198.51.100.7"]) as goog, \
             mock.patch.object(sf, "tls_handshake", return_value=True):
            sf.check_steam(timeout=7)
        sysr.assert_called_once_with(sf.STEAM_CHECK_HOST)
        goog.assert_called_once_with(sf.STEAM_CHECK_HOST, 7)
        self.assertEqual(sf.CHECK_TIMEOUT, 4)


class CheckNetworkHelpers(unittest.TestCase):
    def test_google_resolve_asks_8888_with_plain_dns(self):
        self.assertEqual(sf.STEAM_CHECK_DNS, "8.8.8.8")
        self.assertEqual(sf.STEAM_CHECK_DNS, sf.DNS_SETS["google"][0])
        with mock.patch.object(sf, "dns_query", return_value=["203.0.113.5"]) as q:
            self.assertEqual(sf.google_resolve("store.example", 4), ["203.0.113.5"])
        q.assert_called_once_with("store.example", "8.8.8.8", 4)

    def test_system_resolve_uses_getaddrinfo_ipv4_and_dedupes(self):
        infos = [(2, 1, 6, "", ("203.0.113.5", 443)), (2, 1, 6, "", ("203.0.113.5", 443)),
                 (2, 1, 6, "", ("198.51.100.7", 443))]
        with mock.patch.object(sf.socket, "getaddrinfo", return_value=infos) as gai:
            self.assertEqual(sf.system_resolve(), ["203.0.113.5", "198.51.100.7"])
        gai.assert_called_once_with(sf.STEAM_CHECK_HOST, 443, sf.socket.AF_INET)

    def test_system_resolve_propagates_failure(self):
        with mock.patch.object(sf.socket, "getaddrinfo", side_effect=OSError("no dns")):
            with self.assertRaises(OSError):
                sf.system_resolve()

    def test_tls_handshake_is_false_when_the_connect_fails(self):
        for exc in (OSError("refused"), sf.socket.timeout("timed out")):
            with self.subTest(exc=exc), \
                 mock.patch.object(sf.socket, "create_connection", side_effect=exc):
                self.assertFalse(sf.tls_handshake("203.0.113.5"))

    def test_tls_handshake_is_false_when_the_handshake_fails(self):
        class Ctx:
            def wrap_socket(self, raw, server_hostname=None):
                raise sf.ssl.SSLError("handshake blocked")
        with mock.patch.object(sf.socket, "create_connection", return_value=mock.MagicMock()), \
             mock.patch.object(sf.ssl, "SSLContext", return_value=Ctx()):
            self.assertFalse(sf.tls_handshake("203.0.113.5"))

    def test_tls_handshake_sends_sni_with_a_4s_budget_and_ignores_the_certificate(self):
        ctx = mock.MagicMock()
        with mock.patch.object(sf.socket, "create_connection", return_value=mock.MagicMock()) as conn, \
             mock.patch.object(sf.ssl, "SSLContext", return_value=ctx):
            self.assertTrue(sf.tls_handshake("203.0.113.5"))
        conn.assert_called_once_with(("203.0.113.5", 443), timeout=4)
        ctx.wrap_socket.assert_called_once_with(mock.ANY, server_hostname=sf.STEAM_CHECK_HOST)
        self.assertEqual(ctx.verify_mode, sf.ssl.CERT_NONE)
        self.assertFalse(ctx.check_hostname)

    def test_tls_handshake_returns_false_for_an_invalid_ip(self):
        self.assertFalse(sf.tls_handshake("not-an-ip"))


# -------------------------------------------------------------------------- proxy

class PacScript(unittest.TestCase):
    def test_only_steam_domains_are_proxied(self):
        pac = sf.pac_script(31627)
        self.assertIn('return "PROXY 127.0.0.1:31627; DIRECT"', pac)
        self.assertTrue(pac.startswith("function FindProxyForURL(url, host) {"))
        self.assertTrue(pac.endswith('return "DIRECT"; }'))
        for domain in sf.DOMAINS:
            self.assertIn('dnsDomainIs(host,"%s")' % domain, pac)

    def test_invalid_port(self):
        for port in [0, 70000, "31627", None]:
            with self.subTest(port=port):
                with self.assertRaises(ValueError):
                    sf.pac_script(port)


class ProxyServerTest(unittest.TestCase):
    def request(self, payload, start="ProxyServer"):
        async def scenario():
            proxy = sf.ProxyServer(0, "google")
            await proxy.start()
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
                writer.write(payload)
                await writer.drain()
                data = await asyncio.wait_for(reader.read(), 10)
                writer.close()
                return data.decode("latin-1"), proxy.port
            finally:
                await proxy.close()
        return asyncio.run(scenario())

    def test_serves_the_pac_file_on_an_ephemeral_port(self):
        body, port = self.request(b"GET /proxy.pac HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        self.assertIn("200 OK", body)
        self.assertIn("application/x-ns-proxy-autoconfig", body)
        self.assertIn('PROXY 127.0.0.1:%d' % port, body)
        self.assertTrue(body.endswith(sf.pac_script(port)))

    def test_rejects_non_connect_methods(self):
        body, _ = self.request(b"POST /proxy.pac HTTP/1.1\r\n\r\n")
        self.assertIn("405 Method Not Allowed", body)

    def test_rejects_a_malformed_connect_target(self):
        body, _ = self.request(b"CONNECT store.steampowered.com \r\n\r\n")
        self.assertIn("400 Bad Request", body)
        body, _ = self.request(b"CONNECT evil:abc \r\n\r\n")
        self.assertIn("400 Bad Request", body)

    def test_binds_loopback_only_and_uses_the_real_port_in_the_pac(self):
        async def scenario():
            proxy = sf.ProxyServer(0, "google")
            await proxy.start()
            try:
                hosts = {s.getsockname()[0] for s in proxy._server.sockets}
                return hosts, proxy.pac
            finally:
                await proxy.close()
        hosts, pac = asyncio.run(scenario())
        self.assertEqual(hosts, {"127.0.0.1"})
        self.assertNotIn("31627", pac)

    def test_stop_from_another_thread_shuts_the_loop_down(self):
        proxy = sf.ProxyServer(0, "google")
        thread = threading.Thread(target=lambda: asyncio.run(proxy.run_forever()), daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while proxy._server is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIsNotNone(proxy._server)
        self.assertTrue(proxy.stop())
        thread.join(10)
        self.assertFalse(thread.is_alive())

    def test_stop_before_start_is_harmless(self):
        self.assertTrue(sf.ProxyServer(0, "google").stop())


class Serve(unittest.TestCase):
    def test_refuses_to_bind_port_zero_or_the_wrong_types(self):
        for port in [0, -1, 65536, "31627", None]:
            with self.subTest(port=port):
                with self.assertRaises(ValueError):
                    sf.serve(port=port, dns="google")

    def test_refuses_an_unknown_dns_name_before_binding(self):
        with self.assertRaises(ValueError):
            sf.serve(dns="fastest")

    def test_module_stop_is_a_noop_when_nothing_runs(self):
        with mock.patch.object(sf, "_ACTIVE", None):
            self.assertFalse(sf.stop())

    def test_module_stop_delegates_to_the_running_server(self):
        fake = mock.Mock()
        fake.stop.return_value = True
        with mock.patch.object(sf, "_ACTIVE", fake):
            self.assertTrue(sf.stop())
        fake.stop.assert_called_once_with()


# --------------------------------------------------------------- import behaviour

class ImportBehaviour(unittest.TestCase):
    def test_import_has_no_side_effects_and_ignores_argv(self):
        code = "import sys; sys.argv=['steamfix','--dns','bogus','--nodns']; import steamfix; print('ok')"
        p = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(p.stdout.strip(), "ok", p.stderr)

    def test_no_cli_flag_or_state_path_is_frozen_at_import(self):
        for name in ("KEEP_DNS", "DNS_ONLY", "MTU", "DNS_NAME"):
            self.assertFalse(hasattr(sf, name), name)
        self.assertEqual(sf.STATE, "/var/tmp/saf-state.json")
        self.assertEqual(sf.PORT, 31627)

    def test_privileged_entry_points_take_no_globals_from_argv(self):
        import inspect
        for fn in (sf.apply_network, sf.restore_network, sf.status, sf.flush_dns):
            self.assertNotIn("argv", inspect.getsource(fn))


if __name__ == "__main__":
    unittest.main()
