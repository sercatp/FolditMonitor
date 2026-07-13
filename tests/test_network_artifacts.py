import base64
import hashlib
import json
import os
import tempfile
import unittest

from network import (
    ARTIFACT_QUERY_CAPABILITY,
    ARTIFACT_TRANSFER_CAPABILITY,
    NetworkManager,
    sanitize_artifact_filename,
    sanitize_storage_component,
)


class ImmediateWindow:
    def after(self, _delay, callback=None):
        if callback is not None:
            callback()


class DummyTree:
    def get_children(self):
        return []


def make_manager(temp_dir, received=None, errors=None, build_artifact=None, build_artifact_query=None):
    if received is None:
        received = []
    if errors is None:
        errors = []
    return NetworkManager(
        main_window=ImmediateWindow(),
        callbacks={
            "create_remote_tree": lambda *_args: None,
            "remove_remote_tree": lambda *_args: None,
            "update_remote_trees": lambda *_args: None,
            "artifact_received": received.append,
            "artifact_error": errors.append,
            "build_artifact": build_artifact,
            "build_artifact_query": build_artifact_query,
        },
        tree=DummyTree(),
        monitored_processes={},
        artifact_root=temp_dir,
        max_artifact_bytes=1024 * 1024,
        artifact_chunk_bytes=256 * 1024,
    )


class NetworkEnvelopeCases(unittest.TestCase):
    def test_deserialize_legacy_tree_payload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_manager(temp_dir)
            raw = json.dumps(
                {
                    "version": 1,
                    "items": [
                        {
                            "values": ["1234.0", "5", "f1", "DRW"],
                            "tags": ["123", "C:/Foldit/foldit1"],
                            "log_lines": [[1, "line\n"]],
                        }
                    ],
                }
            ).encode("utf-8")

            message = manager._deserialize_message(raw)

            self.assertEqual(message["type"], "tree_update")
            self.assertEqual(message["capabilities"], set())
            self.assertEqual(message["items"][0]["row_id"], "")
            self.assertEqual(message["items"][0]["values"][2], "f1")

    def test_deserialize_tree_update_with_capabilities_and_row_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_manager(temp_dir)
            raw = json.dumps(
                {
                    "type": "tree_update",
                    "version": 2,
                    "capabilities": [ARTIFACT_TRANSFER_CAPABILITY, ARTIFACT_QUERY_CAPABILITY],
                    "items": [
                        {
                            "values": ["1234.0", "5", "f1", "DRW"],
                            "tags": ["123", "C:/Foldit/foldit1"],
                            "log_lines": [],
                            "row_id": "123",
                        }
                    ],
                }
            ).encode("utf-8")

            message = manager._deserialize_message(raw)

            self.assertEqual(message["type"], "tree_update")
            self.assertEqual(message["capabilities"], {ARTIFACT_TRANSFER_CAPABILITY, ARTIFACT_QUERY_CAPABILITY})
            self.assertEqual(message["items"][0]["row_id"], "123")

    def test_deserialize_artifact_query_request_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_manager(temp_dir)

            message = manager._deserialize_message(
                b'{"type":"artifact_query_request","request_id":"req","kind":"log","query":{"client_name":"foldit1"}}'
            )

            self.assertEqual(message["type"], "artifact_query_request")
            self.assertEqual(message["query"]["client_name"], "foldit1")

    def test_send_artifact_query_request_tracks_pending_and_sends_message(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_manager(temp_dir)
            sent_messages = []
            manager._send_message_to_connection = (
                lambda _address, _connection_id, message: sent_messages.append(message) or True
            )

            request_id = manager.send_artifact_query_request(
                "host",
                "conn",
                "log",
                {
                    "client_name": "foldit1",
                    "puzzle_id": "1234",
                    "script_type": "DRW",
                    "score": "4300.9",
                },
                open_after=True,
                notify=False,
            )

            self.assertIn(request_id, manager.pending_artifact_requests)
            self.assertEqual(sent_messages[0]["type"], "artifact_query_request")
            self.assertEqual(sent_messages[0]["query"]["script_type"], "DRW")
            self.assertTrue(manager.pending_artifact_requests[request_id]["open_after"])
            self.assertFalse(manager.pending_artifact_requests[request_id]["notify"])

    def test_unknown_message_type_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_manager(temp_dir)

            message = manager._deserialize_message(b'{"type":"future_message","x":1}')

            self.assertEqual(message["type"], "future_message")
            self.assertEqual(message["x"], 1)


class ArtifactAssemblerCases(unittest.TestCase):
    def test_chunked_artifact_saves_verified_file_and_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            received = []
            errors = []
            manager = make_manager(temp_dir, received=received, errors=errors)
            request_id = "req1"
            data = b"remote log contents\n" * 8
            sha = hashlib.sha256(data).hexdigest()
            manager.pending_artifact_requests[request_id] = {
                "kind": "log",
                "open_after": True,
                "notify": False,
            }

            manager._handle_message(
                "192.168.1.7",
                "conn",
                {
                    "type": "artifact_start",
                    "request_id": request_id,
                    "transfer_id": "tx1",
                    "kind": "log",
                    "filename": "remote-log.txt",
                    "total_bytes": len(data),
                    "sha256": sha,
                },
            )
            manager._handle_message(
                "192.168.1.7",
                "conn",
                {
                    "type": "artifact_chunk",
                    "request_id": request_id,
                    "transfer_id": "tx1",
                    "seq": 0,
                    "data_b64": base64.b64encode(data).decode("ascii"),
                },
            )
            manager._handle_message(
                "192.168.1.7",
                "conn",
                {
                    "type": "artifact_end",
                    "request_id": request_id,
                    "transfer_id": "tx1",
                    "sha256": sha,
                },
            )

            self.assertEqual(errors, [])
            self.assertEqual(len(received), 1)
            saved_path = received[0]["path"]
            self.assertTrue(os.path.exists(saved_path))
            self.assertFalse(os.path.exists(saved_path + ".part"))
            with open(saved_path, "rb") as handle:
                self.assertEqual(handle.read(), data)
            manifest_path = os.path.join(os.path.dirname(os.path.dirname(saved_path)), "manifest.jsonl")
            self.assertTrue(os.path.exists(manifest_path))

    def test_out_of_order_chunk_cleans_part_file_and_reports_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            received = []
            errors = []
            manager = make_manager(temp_dir, received=received, errors=errors)
            request_id = "req2"
            manager.pending_artifact_requests[request_id] = {
                "kind": "log",
                "open_after": False,
                "notify": True,
            }

            manager._handle_artifact_start(
                "host",
                "conn",
                {
                    "request_id": request_id,
                    "transfer_id": "tx2",
                    "kind": "log",
                    "filename": "bad.txt",
                    "total_bytes": 3,
                    "sha256": hashlib.sha256(b"bad").hexdigest(),
                },
            )
            part_path = manager.incoming_artifact_transfers["tx2"]["part_path"]
            manager._handle_artifact_chunk(
                "host",
                "conn",
                {
                    "request_id": request_id,
                    "transfer_id": "tx2",
                    "seq": 1,
                    "data_b64": base64.b64encode(b"bad").decode("ascii"),
                },
            )

            self.assertEqual(received, [])
            self.assertEqual(len(errors), 1)
            self.assertIn("out of order", errors[0]["message"])
            self.assertFalse(os.path.exists(part_path))

    def test_oversize_artifact_start_reports_error_without_part_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            errors = []
            manager = make_manager(temp_dir, errors=errors)
            manager.max_artifact_bytes = 3
            request_id = "req3"
            manager.pending_artifact_requests[request_id] = {
                "kind": "log",
                "open_after": False,
                "notify": True,
            }

            manager._handle_artifact_start(
                "host",
                "conn",
                {
                    "request_id": request_id,
                    "transfer_id": "tx3",
                    "kind": "log",
                    "filename": "too-big.txt",
                    "total_bytes": 4,
                    "sha256": "",
                },
            )

            self.assertEqual(len(errors), 1)
            self.assertIn("too large", errors[0]["message"])
            self.assertEqual(manager.incoming_artifact_transfers, {})

    def test_connection_cleanup_removes_part_file_and_pending_request(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            errors = []
            manager = make_manager(temp_dir, errors=errors)
            request_id = "req-cleanup"
            manager.pending_artifact_requests[request_id] = {
                "address": "host",
                "connection_id": "conn",
                "kind": "log",
                "open_after": False,
                "notify": True,
            }
            manager._handle_artifact_start(
                "host",
                "conn",
                {
                    "request_id": request_id,
                    "transfer_id": "tx-cleanup",
                    "kind": "log",
                    "filename": "partial.txt",
                    "total_bytes": 10,
                    "sha256": "",
                },
            )
            part_path = manager.incoming_artifact_transfers["tx-cleanup"]["part_path"]

            manager._cleanup_artifacts_for_connection("host", "conn")

            self.assertFalse(os.path.exists(part_path))
            self.assertEqual(manager.incoming_artifact_transfers, {})
            self.assertNotIn(request_id, manager.pending_artifact_requests)
            self.assertEqual(len(errors), 1)
            self.assertIn("Connection closed", errors[0]["message"])


class ArtifactUtilityCases(unittest.TestCase):
    def test_sanitize_artifact_filename_removes_path_and_invalid_chars(self):
        filename = sanitize_artifact_filename('..\\bad:name?.txt')

        self.assertNotIn(":", filename)
        self.assertNotIn("?", filename)
        self.assertTrue(filename.endswith(".txt"))

    def test_sanitize_storage_component_keeps_component_safe(self):
        component = sanitize_storage_component("fe80::1%local")

        self.assertNotIn(":", component)
        self.assertNotIn("%", component)
        self.assertTrue(component)

    def test_responder_sends_artifact_error_when_builder_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_manager(
                temp_dir,
                build_artifact=lambda *_args: (_ for _ in ()).throw(FileNotFoundError("missing log")),
            )
            sent_errors = []
            manager._send_artifact_error = (
                lambda address, connection_id, request_id, message, transfer_id=None:
                sent_errors.append((address, connection_id, request_id, message))
            )

            manager._send_artifact_response("host", "conn", "req4", "log", "123")

            self.assertEqual(len(sent_errors), 1)
            self.assertEqual(sent_errors[0][2], "req4")
            self.assertIn("missing log", sent_errors[0][3])

    def test_query_responder_uses_query_builder(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = os.path.join(temp_dir, "match.txt")
            with open(log_path, "w", encoding="utf-8") as handle:
                handle.write("matched")
            manager = make_manager(
                temp_dir,
                build_artifact_query=lambda kind, query, *_args: {
                    "path": log_path,
                    "filename": f"{kind}-{query['script_type']}.txt",
                },
            )
            sent_files = []
            manager._send_artifact_file = (
                lambda address, connection_id, request_id, kind, artifact:
                sent_files.append((address, connection_id, request_id, kind, artifact))
            )

            manager._send_artifact_query_response(
                "host",
                "conn",
                "req-query",
                "log",
                {"script_type": "DRW"},
            )

            self.assertEqual(len(sent_files), 1)
            self.assertEqual(sent_files[0][3], "log")
            self.assertEqual(sent_files[0][4]["filename"], "log-DRW.txt")

    def test_query_responder_sends_error_when_builder_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = make_manager(
                temp_dir,
                build_artifact_query=lambda *_args: (_ for _ in ()).throw(FileNotFoundError("no match")),
            )
            sent_errors = []
            manager._send_artifact_error = (
                lambda address, connection_id, request_id, message, transfer_id=None:
                sent_errors.append((address, connection_id, request_id, message))
            )

            manager._send_artifact_query_response("host", "conn", "req-query", "log", {})

            self.assertEqual(len(sent_errors), 1)
            self.assertEqual(sent_errors[0][2], "req-query")
            self.assertIn("no match", sent_errors[0][3])


if __name__ == "__main__":
    unittest.main()
