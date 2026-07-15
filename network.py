import base64
import binascii
import hashlib
import json
import os
import re
import select
import socket
import threading
import time
import uuid
import tkinter as tk
from tkinter import ttk, messagebox
from collections import defaultdict

from row_appearance import ROW_APPEARANCE_TAG_PREFIX, parse_appearance_tag

ARTIFACT_TRANSFER_CAPABILITY = "artifact_transfer_v1"
ARTIFACT_QUERY_CAPABILITY = "artifact_query_v1"
REMOTE_LOG_CAPABILITY = "remote_log_v1"
REMOTE_PDB_CAPABILITY = "remote_pdb_v1"
NETWORK_CAPABILITIES = [
    ARTIFACT_TRANSFER_CAPABILITY,
    ARTIFACT_QUERY_CAPABILITY,
    REMOTE_LOG_CAPABILITY,
    REMOTE_PDB_CAPABILITY,
]
DEFAULT_ARTIFACT_CHUNK_BYTES = 256 * 1024
DEFAULT_MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
ARTIFACT_KIND_DIRS = {
    "log": "logs",
    "pdb": "pdb",
}

# Home directory used to strip the user name out of folder paths before they
# travel over the network (the remote side only needs a stable row key, not the
# real path). Everything below the home prefix is kept, so rows stay unique.
_HOME_DIR = os.path.expanduser("~")


def mask_user_path(path):
    """Replace the local user-profile prefix with '~' so the user name is not sent."""
    if not isinstance(path, str) or not _HOME_DIR:
        return path
    if path.lower().startswith(_HOME_DIR.lower()):
        return "~" + path[len(_HOME_DIR):]
    return path


def sanitize_artifact_filename(filename, fallback="artifact.bin", max_length=180):
    """Return a safe single path component for received artifacts."""
    raw_name = os.path.basename(str(filename or "")).strip().strip(". ")
    if not raw_name:
        raw_name = fallback
    raw_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", raw_name)
    raw_name = re.sub(r"\s+", " ", raw_name).strip().strip(". ")
    if not raw_name:
        raw_name = fallback

    if len(raw_name) <= max_length:
        return raw_name

    stem, ext = os.path.splitext(raw_name)
    ext = ext[:20]
    stem_limit = max(1, max_length - len(ext))
    return stem[:stem_limit].rstrip(". ") + ext


def sanitize_storage_component(value, fallback="unknown"):
    text = str(value or "").strip()
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._-")
    return text or fallback


class ConnectDialog:
    def __init__(self, parent, default_address, default_port, default_auto_reconnect=True):
        self.dialog = tk.Toplevel(parent)
        self.dialog.title("Connect to Remote Host")
        self.dialog.transient(parent)

        # Center the window
        self.dialog.withdraw() # Hide window while setting up

        tk.Label(self.dialog, text="Address:").grid(row=0, column=0, padx=5, pady=5)
        self.address_entry = tk.Entry(self.dialog)
        self.address_entry.grid(row=0, column=1, padx=5, pady=5)
        self.address_entry.insert(0, default_address)

        tk.Label(self.dialog, text="Port:").grid(row=1, column=0, padx=5, pady=5)
        self.port_entry = tk.Entry(self.dialog)
        self.port_entry.grid(row=1, column=1, padx=5, pady=5)
        self.port_entry.insert(0, str(default_port))

        self.auto_reconnect_var = tk.BooleanVar(value=bool(default_auto_reconnect))
        tk.Checkbutton(self.dialog, text="Auto-reconnect", variable=self.auto_reconnect_var).grid(
            row=2, column=0, columnspan=2, padx=5, sticky="w")

        btn_frame = tk.Frame(self.dialog)
        btn_frame.grid(row=3, column=0, columnspan=2, pady=10)
        
        tk.Button(btn_frame, text="Connect", 
                 command=self.connect).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="Cancel", 
                 command=self.dialog.destroy).pack(side=tk.LEFT, padx=5)
        
        # Bind Enter to connect function
        self.dialog.bind('<Return>', lambda e: self.connect())
        
        # Center the window
        self.dialog.update_idletasks()
        width = self.dialog.winfo_width()
        height = self.dialog.winfo_height()
        x = (self.dialog.winfo_screenwidth() // 2) - (width // 2)
        y = (self.dialog.winfo_screenheight() // 2) - (height // 2)
        self.dialog.geometry(f'+{x}+{y}')
        
        # Show window and set focus to address field
        self.dialog.deiconify()
        self.address_entry.focus_set()
        self.address_entry.select_range(0, tk.END)
        
        self.result = None
        
    def connect(self):
        address = self.address_entry.get().strip()
        port_text = self.port_entry.get().strip()

        if not address:
            messagebox.showerror("Error", "Address is required")
            return

        try:
            port = int(port_text)
        except ValueError:
            messagebox.showerror("Error", "Port must be a number")
            return

        if not 1 <= port <= 65535:
            messagebox.showerror("Error", "Port must be between 1 and 65535")
            return

        self.result = (address, port, self.auto_reconnect_var.get())
        self.dialog.destroy()

class RemoteTreeView:
    def __init__(self, parent, address, connection_id, fonts, show_puzzle_column=True):
        self.fonts = fonts
        self.address = address
        self.connection_id = connection_id
        self.port = None
        self.dead = False
        self.frame = ttk.Frame(parent)
        self.frame._tree_id = f'remote_tree_{address}_{connection_id}'
        self.frame.pack(fill="both", expand=True, pady=(5,0))

        # Create header label
        self.header = ttk.Label(self.frame, text=f"Connected: {address} {connection_id}")
        self.header.pack(fill="x")

        # Calculate row height
        font_height = max(
            fonts['normal'].metrics()['linespace'],
            fonts['bold'].metrics()['linespace'],
            fonts['italic'].metrics()['linespace'],
            fonts['bold_italic'].metrics()['linespace'],
        )
        row_height = font_height + 6
        
        # Configure style with row height
        style = ttk.Style()
        style.configure('Treeview', rowheight=row_height)
        
        # Create treeview with style
        columns = ("Score", "CPU", "Folder", "Type", "Puzzle") if show_puzzle_column else ("Score", "CPU", "Folder", "Type")
        self.tree = ttk.Treeview(self.frame, 
            columns=columns,
            show="", 
            selectmode='none', 
            style='Treeview')
        
        # Set initial column widths
        for col in self.tree["columns"]:
            self.tree.column(col, width=100, stretch=True)  # Default width
        self.tree.column("#0", width=0, stretch=False)
        
        self.tree.pack(fill="both", expand=True)
        
        # Add dictionary for storing logs and items
        self.log_data = {}
        self.item_payloads = {}
        self.items = {}  # Mapping between data and item IDs
        self.capabilities = set()
        self.dynamic_style_tags = set()

    def mark_dead(self):
        """Freeze the last data and show that the peer dropped (red header, greyed rows)."""
        if self.dead:
            return
        self.dead = True
        self.header.config(
            text=f"Disconnected: {self.address} {self.connection_id}",
            foreground="#b91c1c",
        )
        self.tree.tag_configure("dead_row", foreground="#9ca3af")
        for item_id in self.tree.get_children():
            self.tree.item(item_id, tags=("dead_row",))

    def _ensure_dynamic_appearance_tag(self, tag_name):
        if not isinstance(tag_name, str):
            return
        if not tag_name.startswith(ROW_APPEARANCE_TAG_PREFIX) or tag_name in self.dynamic_style_tags:
            return

        appearance = parse_appearance_tag(tag_name)
        if appearance is None:
            return

        font_key, foreground, background = appearance
        tag_options = {
            "font": self.fonts.get(font_key, self.fonts["normal"])
        }
        if foreground:
            tag_options["foreground"] = foreground
        if background:
            tag_options["background"] = background
        self.tree.tag_configure(tag_name, **tag_options)
        self.dynamic_style_tags.add(tag_name)

    def _ensure_dynamic_tag(self, tag_name):
        self._ensure_dynamic_appearance_tag(tag_name)
        
    def supports_capability(self, capability):
        return capability in self.capabilities

    def update_items(self, new_data, capabilities=None):
        """Update tree items while preserving their existing IDs."""
        if capabilities is not None:
            self.capabilities = {
                str(capability)
                for capability in capabilities
                if str(capability).strip()
            }

        existing_items = set(self.tree.get_children())
        current_items = set()
        
        # Create keys for mapping items
        for item_data in new_data:
            values = item_data.get('values', [])
            tags = item_data.get('tags', [])
            for tag_name in tags:
                self._ensure_dynamic_tag(tag_name)

            # Use the full folder path as the stable row key.
            item_key = next(
                (tag for tag in tags if isinstance(tag, str) and ('\\' in tag or '/' in tag)),
                values[2] if len(values) > 2 else repr(values)
            )
            
            if item_key in self.items:
                # Update the existing item
                item_id = self.items[item_key]
                self.tree.item(item_id, values=values, tags=tags)
                self.log_data[item_id] = item_data.get('log_lines', [])
                self.item_payloads[item_id] = dict(item_data)
                current_items.add(item_id)
            else:
                # Create a new item
                item_id = self.tree.insert('', 'end', values=values, tags=tags)
                self.items[item_key] = item_id
                self.log_data[item_id] = item_data.get('log_lines', [])
                self.item_payloads[item_id] = dict(item_data)
                current_items.add(item_id)
        
        # Remove items that are no longer present in the data
        items_to_remove = existing_items - current_items
        for item_id in items_to_remove:
            self.tree.delete(item_id)
            # Remove from log_data and items
            self.log_data.pop(item_id, None)
            self.item_payloads.pop(item_id, None)
            # Remove from self.items (reverse lookup)
            keys_to_remove = [k for k, v in self.items.items() if v == item_id]
            for k in keys_to_remove:
                self.items.pop(k, None)

class NetworkManager:
    def __init__(
        self,
        main_window,
        callbacks,
        tree,
        monitored_processes,
        password="",
        artifact_root=None,
        max_artifact_bytes=DEFAULT_MAX_ARTIFACT_BYTES,
        artifact_chunk_bytes=DEFAULT_ARTIFACT_CHUNK_BYTES,
    ):
        self.main_window = main_window
        self.password = password or ""
        self.artifact_root = artifact_root or os.path.join(os.getcwd(), "puzzle_logs", "_remote")
        self.max_artifact_bytes = int(max_artifact_bytes or DEFAULT_MAX_ARTIFACT_BYTES)
        self.artifact_chunk_bytes = int(artifact_chunk_bytes or DEFAULT_ARTIFACT_CHUNK_BYTES)
        self.server_socket = None
        # Change the structure of clients to support multiple connections
        self.clients = defaultdict(dict)  # {address: {connection_id: {socket, data}}}
        self.clients_lock = threading.RLock()
        self.is_server_running = False
        self.initiated_connections = set()  # {(address, connection_id)}
        self.connection_counter = defaultdict(int)  # Connection counter for each address
        self.disconnection_callbacks = {}
        self.active_sockets = set()  # Add tracking of active sockets
        self.current_port = None
        self.connection_timeout = 5.0
        self.send_timeout = 5.0  # max seconds to wait for a socket to accept a send
        self.max_message_bytes = 1024 * 1024
        self.artifact_chunk_bytes = max(1, min(self.artifact_chunk_bytes, self.max_message_bytes // 3))
        self.max_password_bytes = 256
        self.send_lock = threading.Lock()
        self.socket_send_lock = threading.Lock()
        self.send_in_progress = False
        self.is_shutting_down = False
        self.pending_artifact_requests = {}
        self.incoming_artifact_transfers = {}
        
        # Store callbacks
        self.create_remote_tree = callbacks['create_remote_tree']
        self.remove_remote_tree = callbacks['remove_remote_tree']
        self.update_remote_trees = callbacks['update_remote_trees']
        self.build_artifact = callbacks.get('build_artifact')
        self.build_artifact_query = callbacks.get('build_artifact_query')
        self.artifact_received = callbacks.get('artifact_received')
        self.artifact_error = callbacks.get('artifact_error')
        
        # Store objects
        self.process_tree = tree
        self.monitored_processes = monitored_processes
    
    def start_server(self, port, server_timeout):
        if self.server_socket:
            return True
            
        max_port_attempts = 10
        current_port = port
        
        for port_attempt in range(max_port_attempts):
            server_socket = None
            try:
                server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server_socket.settimeout(server_timeout)
                server_socket.bind(('', current_port))
                server_socket.listen(5)
                self.server_socket = server_socket
                self.is_server_running = True
                self.current_port = current_port
                
                # adjust_column_widths(process_tree)  # Add column width correction
                if current_port != port:
                    # messagebox.showinfo("Port Changed", f"Default port {port} was busy.\nListening on port {current_port}")
                    print(f"Default port {port} was busy. Listening on port {current_port}")

                threading.Thread(target=self._accept_connections, daemon=True).start()
                return True
                
            except socket.error as e:
                if server_socket is not None:
                    try:
                        server_socket.close()
                    except OSError:
                        pass
                self.server_socket = None
                if e.errno == 98 or e.errno == 10048:  # Port already in use
                    current_port += 1
                    continue
                else:
                    messagebox.showerror("Error", f"Failed to start server: {str(e)}")
                    return False
                    
        messagebox.showerror("Error", 
            f"Could not find available port in range {port}-{current_port}")
        return False

    def connect_to_server(self, address, port):
        try:
            client_socket = socket.create_connection((address, port), timeout=self.connection_timeout)
            try:
                client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except OSError:
                pass

            # Shared-password handshake: prove we know the password before the server keeps us.
            if not self._send_password_and_wait_ack(client_socket):
                client_socket.close()
                print(f"Connection rejected by {address}:{port} (password mismatch)")
                return False

            client_socket.settimeout(None)

            connection_id = f"/{self.connection_counter[address]}"
            self.connection_counter[address] += 1
            
            with self.clients_lock:
                self.clients[address][connection_id] = {
                    'socket': client_socket,
                    'data': None,
                    'port': port,
                    'capabilities': set()
                }
                self.initiated_connections.add((address, connection_id))
            
            threading.Thread(target=self._receive_data, 
                           args=(address, connection_id), 
                           daemon=True).start()
            
            # Use the stored callback
            self.main_window.after(100, lambda: self.create_remote_tree(address, connection_id, port))
            return True
        except Exception as e:
            print(f"Connection error: {e}")
            return False
            
    def send_tree_data(self):
        tree_data = self._get_tree_data()

        try:
            payload = self._serialize_tree_data(tree_data)
        except Exception as e:
            print(f"Serialize error: {e}")
            return

        with self.send_lock:
            if self.send_in_progress:
                return
            self.send_in_progress = True

        threading.Thread(
            target=self._send_payload_to_clients,
            args=(payload,),
            daemon=True
        ).start()

    def send_artifact_request(self, address, connection_id, kind, row_id, open_after=False, notify=True):
        clean_kind = str(kind or "").strip().lower()
        clean_row_id = str(row_id or "").strip()
        if clean_kind not in ARTIFACT_KIND_DIRS:
            raise ValueError(f"Unsupported artifact kind: {kind}")
        if not clean_row_id:
            raise ValueError("row_id is required")

        request_id = uuid.uuid4().hex
        self.pending_artifact_requests[request_id] = {
            "address": address,
            "connection_id": connection_id,
            "kind": clean_kind,
            "row_id": clean_row_id,
            "open_after": bool(open_after),
            "notify": bool(notify),
            "created_at": time.time(),
        }

        message = {
            "type": "artifact_request",
            "version": 1,
            "request_id": request_id,
            "kind": clean_kind,
            "row_id": clean_row_id,
        }
        if not self._send_message_to_connection(address, connection_id, message):
            self.pending_artifact_requests.pop(request_id, None)
            return None
        return request_id

    def send_artifact_query_request(self, address, connection_id, kind, query, open_after=False, notify=True):
        clean_kind = str(kind or "").strip().lower()
        if clean_kind not in ARTIFACT_KIND_DIRS:
            raise ValueError(f"Unsupported artifact kind: {kind}")
        if not isinstance(query, dict):
            raise ValueError("query is required")

        clean_query = {
            str(key): str(value).strip()
            for key, value in query.items()
            if str(key).strip() and str(value).strip()
        }
        if not clean_query:
            raise ValueError("query is required")

        request_id = uuid.uuid4().hex
        self.pending_artifact_requests[request_id] = {
            "address": address,
            "connection_id": connection_id,
            "kind": clean_kind,
            "query": dict(clean_query),
            "open_after": bool(open_after),
            "notify": bool(notify),
            "created_at": time.time(),
        }

        message = {
            "type": "artifact_query_request",
            "version": 1,
            "request_id": request_id,
            "kind": clean_kind,
            "query": clean_query,
        }
        if not self._send_message_to_connection(address, connection_id, message):
            self.pending_artifact_requests.pop(request_id, None)
            return None
        return request_id

    def disconnect_client(self, address, connection_id, user_initiated=False):
        key = f"{address}_{connection_id}"
        socket_obj = None

        with self.clients_lock:
            if address not in self.clients or connection_id not in self.clients[address]:
                return

            socket_obj = self.clients[address][connection_id]['socket']
            self.active_sockets.discard(socket_obj)
            del self.clients[address][connection_id]
            if not self.clients[address]:
                del self.clients[address]
            self.initiated_connections.discard((address, connection_id))
            disconnect_callback = self.disconnection_callbacks.pop(key, None)

        try:
            if socket_obj is not None:
                try:
                    socket_obj.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                socket_obj.close()
        except Exception as e:
            if not self.is_shutting_down:
                print(f"Error closing socket: {e}")

        self._cleanup_artifacts_for_connection(address, connection_id)

        # Invoke the removal callback on the main GUI thread
        if disconnect_callback is not None:
            self.main_window.after(0, disconnect_callback)

        # Use the stored callback
        self.main_window.after(100, lambda: self.remove_remote_tree(address, connection_id, user_initiated))

    def register_disconnect_callback(self, address, connection_id, callback):
        """Register callback function for handling disconnection"""
        with self.clients_lock:
            self.disconnection_callbacks[f"{address}_{connection_id}"] = callback

    def has_clients(self):
        with self.clients_lock:
            return bool(self.clients)

    def has_connection(self, address, connection_id):
        with self.clients_lock:
            return address in self.clients and connection_id in self.clients[address]

    def get_connections_snapshot(self):
        with self.clients_lock:
            return [
                {
                    'address': address,
                    'connection_id': connection_id,
                    'port': client.get('port'),
                    'data': client.get('data'),
                    'capabilities': set(client.get('capabilities') or set()),
                    'initiated': (address, connection_id) in self.initiated_connections
                }
                for address, connections in self.clients.items()
                for connection_id, client in connections.items()
            ]

    def shutdown(self):
        self.is_shutting_down = True
        self.is_server_running = False

        connections = [
            (connection['address'], connection['connection_id'])
            for connection in self.get_connections_snapshot()
        ]
        for address, connection_id in connections:
            self.disconnect_client(address, connection_id, user_initiated=True)

        if self.server_socket is not None:
            try:
                self.server_socket.close()
            except OSError:
                pass
            finally:
                self.server_socket = None

    def _accept_connections(self):
        """Accept incoming connections in a loop"""
        while self.is_server_running:
            try:
                client_socket, addr = self.server_socket.accept()
                address = addr[0]

                connection_id = f"connection_{self.connection_counter[address]}"
                self.connection_counter[address] += 1

                threading.Thread(target=self._handle_client,
                              args=(client_socket, address, connection_id),
                              daemon=True).start()
            except socket.timeout:
                continue
            except OSError as e:
                if self.is_server_running and not self.is_shutting_down:
                    print(f"Error accepting connection: {e}")
                break
            except Exception as e:
                if not self.is_shutting_down:
                    print(f"Error accepting connection: {e}")
                continue

    def _handle_client(self, client_socket, address, connection_id):
        try:
            client_socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass

        # Reject the peer up front if it does not present the shared password.
        if not self._check_password(client_socket):
            try:
                client_socket.sendall(b'\x00')
            except OSError:
                pass
            try:
                client_socket.close()
            except OSError:
                pass
            return

        try:
            client_socket.sendall(b'\x01')
        except OSError:
            try:
                client_socket.close()
            except OSError:
                pass
            return

        self._handle_connection(client_socket, address, connection_id, is_client=True)

    def _frame_send(self, sock, payload_bytes):
        sock.sendall(len(payload_bytes).to_bytes(8, byteorder='big'))
        if payload_bytes:
            sock.sendall(payload_bytes)

    def _send_password_and_wait_ack(self, sock):
        """Client side: send our password, return True only if the server acknowledges it."""
        sock.settimeout(self.connection_timeout)
        try:
            self._frame_send(sock, self.password.encode('utf-8'))
            return sock.recv(1) == b'\x01'
        except OSError:
            return False

    def _check_password(self, sock):
        """Server side: read the peer's password (bounded, with timeout) and compare with ours."""
        sock.settimeout(self.connection_timeout)
        try:
            size_bytes = self._recv_exact(sock, 8)
            if not size_bytes:
                return False
            size = int.from_bytes(size_bytes, byteorder='big')
            if size < 0 or size > self.max_password_bytes:
                return False
            received = self._recv_exact(sock, size) if size else b""
            if received is None:
                return False
            return received.decode('utf-8', 'replace') == self.password
        except OSError:
            return False
        finally:
            try:
                sock.settimeout(None)
            except OSError:
                pass

    def _receive_data(self, address, connection_id):
        socket_obj = self._get_socket(address, connection_id)
        if socket_obj is not None:
            self._handle_connection(
                socket_obj,
                address, 
                connection_id, 
                is_client=False
            )

    def _handle_connection(self, socket_obj, address, connection_id, is_client=True):
        try:
            if is_client:
                with self.clients_lock:
                    self.clients[address][connection_id] = {
                        'socket': socket_obj,
                        'data': None,
                        'port': None,
                        'capabilities': set()
                    }
            with self.clients_lock:
                self.active_sockets.add(socket_obj)

            while self.has_connection(address, connection_id):
                try:
                    size_bytes = self._recv_exact(socket_obj, 8)
                    if not size_bytes:
                        break

                    size = int.from_bytes(size_bytes, byteorder='big')
                    if size <= 0 or size > self.max_message_bytes:
                        raise ValueError(f"Invalid payload size: {size}")

                    data = self._recv_exact(socket_obj, size)
                    if data is None:
                        raise ConnectionError("Connection closed while receiving payload")

                    handled_type = None
                    if self.has_connection(address, connection_id):
                        message = self._deserialize_message(data)
                        handled_type = self._handle_message(address, connection_id, message)

                    if handled_type == "tree_update" and (address, connection_id) in self.initiated_connections:
                        self.main_window.after(0, self.update_remote_trees)
                    
                except (socket.error, EOFError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as e:
                    if not self.is_shutting_down:
                        print(f"Socket error for {address} ({connection_id}): {e}")
                    break
                except Exception as e:
                    print(f"Error processing data from {address} ({connection_id}): {e}")
                    break
        except Exception as e:
            if not self.is_shutting_down:
                print(f"Handle connection error for {address} ({connection_id}): {e}")
        finally:
            self.disconnect_client(address, connection_id)

    def _get_tree_data(self):
        data = []
        try:
            for item in self.process_tree.get_children():
                values = list(self.process_tree.item(item)['values'])
                tags = self.process_tree.item(item)['tags']

                pid = next((tag for tag in tags if isinstance(tag, (int, str)) and str(tag).isdigit()), None)

                log_lines = []
                if pid and int(pid) in self.monitored_processes:
                    log_lines = list(self.monitored_processes[int(pid)]['last_log_lines'])

                # Strip the local user name out of the folder path before sending; the
                # remote side only uses this tag as a stable per-row key.
                masked_tags = [
                    mask_user_path(tag) if (isinstance(tag, str) and ('\\' in tag or '/' in tag)) else tag
                    for tag in tags
                ]

                data.append({
                    'values': values,
                    'tags': masked_tags,
                    'log_lines': log_lines,
                    'row_id': str(pid).strip() if pid is not None else "",
                })
                
            return data

        except Exception as e:
            print(f"Error in _get_tree_data: {str(e)}")
            return []

    def _get_socket(self, address, connection_id):
        with self.clients_lock:
            client = self.clients.get(address, {}).get(connection_id)
            if client is None:
                return None
            return client['socket']

    def _get_socket_snapshot(self):
        with self.clients_lock:
            return [
                (address, connection_id, client['socket'])
                for address, connections in self.clients.items()
                for connection_id, client in connections.items()
            ]

    def _send_payload_to_socket(self, address, connection_id, socket_obj, payload):
        size = len(payload)
        if size <= 0 or size > self.max_message_bytes:
            raise ValueError(f"Invalid payload size: {size}")

        size_bytes = size.to_bytes(8, byteorder='big')
        with self.socket_send_lock:
            # Keep each size+payload frame atomic relative to other sender threads.
            if not select.select([], [socket_obj], [], self.send_timeout)[1]:
                raise socket.timeout("send timed out (slow or stuck peer)")
            socket_obj.sendall(size_bytes)
            socket_obj.sendall(payload)

    def _send_message_to_connection(self, address, connection_id, message):
        socket_obj = self._get_socket(address, connection_id)
        if socket_obj is None:
            return False

        payload = self._serialize_message(message)
        try:
            self._send_payload_to_socket(address, connection_id, socket_obj, payload)
            return True
        except Exception as e:
            if not self.is_shutting_down:
                print(f"Send error to {address} ({connection_id}): {e}")
            self.disconnect_client(address, connection_id)
            return False

    def _send_payload_to_clients(self, payload):
        try:
            for address, connection_id, socket_obj in self._get_socket_snapshot():
                try:
                    self._send_payload_to_socket(address, connection_id, socket_obj, payload)
                except Exception as e:
                    if not self.is_shutting_down:
                        print(f"Send error to {address} ({connection_id}): {e}")
                    self.disconnect_client(address, connection_id)
        finally:
            with self.send_lock:
                self.send_in_progress = False

    def _serialize_message(self, message):
        payload = json.dumps(
            message,
            ensure_ascii=False,
            separators=(',', ':')
        ).encode('utf-8')
        if len(payload) > self.max_message_bytes:
            raise ValueError(f"Payload too large: {len(payload)} bytes")
        return payload

    def _serialize_tree_data(self, tree_data):
        return self._serialize_message({
            'type': 'tree_update',
            'version': 2,
            'capabilities': NETWORK_CAPABILITIES,
            'items': tree_data,
        })

    def _deserialize_message(self, raw_data):
        payload = json.loads(raw_data.decode('utf-8'))
        if not isinstance(payload, dict):
            raise ValueError("Invalid payload structure")

        message_type = payload.get("type")
        if not message_type and "items" in payload:
            message_type = "tree_update"

        if message_type == "tree_update":
            return {
                "type": "tree_update",
                "items": self._normalize_tree_items(payload),
                "capabilities": self._normalize_capabilities(payload.get("capabilities", [])),
            }

        payload["type"] = str(message_type or "unknown")
        return payload

    def _deserialize_tree_data(self, raw_data):
        message = self._deserialize_message(raw_data)
        if message.get("type") != "tree_update":
            raise ValueError("Expected tree update payload")
        return message["items"]

    @staticmethod
    def _normalize_capabilities(capabilities):
        if not isinstance(capabilities, list):
            return set()
        return {
            str(capability)
            for capability in capabilities
            if str(capability).strip()
        }

    def _normalize_tree_items(self, payload):
        items = payload.get('items')
        if not isinstance(items, list):
            raise ValueError("Invalid payload structure")

        normalized_items = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Invalid item structure")

            values = item.get('values', [])
            tags = item.get('tags', [])
            log_lines = item.get('log_lines', [])
            row_id = item.get('row_id')

            if not isinstance(values, list) or not isinstance(tags, list) or not isinstance(log_lines, list):
                raise ValueError("Invalid tree item payload")

            normalized_items.append({
                'values': values,
                'tags': tags,
                'log_lines': log_lines,
                'row_id': str(row_id).strip() if row_id is not None else "",
            })

        return normalized_items

    def _handle_message(self, address, connection_id, message):
        message_type = message.get("type")
        if message_type == "tree_update":
            with self.clients_lock:
                if address in self.clients and connection_id in self.clients[address]:
                    self.clients[address][connection_id]['data'] = message["items"]
                    self.clients[address][connection_id]['capabilities'] = set(message.get("capabilities") or set())
            return "tree_update"

        if message_type == "artifact_request":
            self._handle_artifact_request(address, connection_id, message)
            return message_type

        if message_type == "artifact_query_request":
            self._handle_artifact_query_request(address, connection_id, message)
            return message_type

        if message_type in {"artifact_start", "artifact_chunk", "artifact_end", "artifact_error"}:
            self._handle_artifact_transfer_message(address, connection_id, message)
            return message_type

        if not self.is_shutting_down:
            print(f"Unknown network message from {address} ({connection_id}): {message_type}")
        return message_type

    def _handle_artifact_request(self, address, connection_id, message):
        request_id = str(message.get("request_id") or "").strip()
        kind = str(message.get("kind") or "").strip().lower()
        row_id = str(message.get("row_id") or "").strip()

        if not request_id:
            return
        if kind not in ARTIFACT_KIND_DIRS:
            self._send_artifact_error(address, connection_id, request_id, f"Unsupported artifact kind: {kind}")
            return
        if not row_id:
            self._send_artifact_error(address, connection_id, request_id, "row_id is required")
            return
        if self.build_artifact is None:
            self._send_artifact_error(address, connection_id, request_id, "Remote artifacts are not available")
            return

        threading.Thread(
            target=self._send_artifact_response,
            args=(address, connection_id, request_id, kind, row_id),
            daemon=True,
        ).start()

    def _handle_artifact_query_request(self, address, connection_id, message):
        request_id = str(message.get("request_id") or "").strip()
        kind = str(message.get("kind") or "").strip().lower()
        query = message.get("query")

        if not request_id:
            return
        if kind not in ARTIFACT_KIND_DIRS:
            self._send_artifact_error(address, connection_id, request_id, f"Unsupported artifact kind: {kind}")
            return
        if not isinstance(query, dict) or not query:
            self._send_artifact_error(address, connection_id, request_id, "query is required")
            return
        if self.build_artifact_query is None:
            self._send_artifact_error(address, connection_id, request_id, "Remote artifact queries are not available")
            return

        clean_query = {
            str(key): str(value).strip()
            for key, value in query.items()
            if str(key).strip() and str(value).strip()
        }
        threading.Thread(
            target=self._send_artifact_query_response,
            args=(address, connection_id, request_id, kind, clean_query),
            daemon=True,
        ).start()

    def _send_artifact_file(self, address, connection_id, request_id, kind, artifact):
        if isinstance(artifact, str):
            artifact = {"path": artifact}
        if not isinstance(artifact, dict):
            raise RuntimeError("Artifact builder returned invalid data")

        path = artifact.get("path")
        filename = artifact.get("filename") or os.path.basename(str(path or ""))
        if not path or not os.path.exists(path):
            raise FileNotFoundError("Artifact file was not created")

        total_bytes = os.path.getsize(path)
        if total_bytes > self.max_artifact_bytes:
            raise RuntimeError(f"Artifact is too large: {total_bytes} bytes")

        sha256 = self._hash_file(path)
        transfer_id = uuid.uuid4().hex
        start_message = {
            "type": "artifact_start",
            "version": 1,
            "request_id": request_id,
            "transfer_id": transfer_id,
            "kind": kind,
            "filename": filename,
            "total_bytes": total_bytes,
            "sha256": sha256,
        }
        if not self._send_message_to_connection(address, connection_id, start_message):
            raise ConnectionError("Unable to send artifact_start")

        seq = 0
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(self.artifact_chunk_bytes)
                if not chunk:
                    break
                chunk_message = {
                    "type": "artifact_chunk",
                    "version": 1,
                    "request_id": request_id,
                    "transfer_id": transfer_id,
                    "seq": seq,
                    "data_b64": base64.b64encode(chunk).decode("ascii"),
                }
                if not self._send_message_to_connection(address, connection_id, chunk_message):
                    raise ConnectionError("Unable to send artifact_chunk")
                seq += 1

        end_message = {
            "type": "artifact_end",
            "version": 1,
            "request_id": request_id,
            "transfer_id": transfer_id,
            "sha256": sha256,
        }
        if not self._send_message_to_connection(address, connection_id, end_message):
            raise ConnectionError("Unable to send artifact_end")

    def _send_artifact_response(self, address, connection_id, request_id, kind, row_id):
        artifact = None
        cleanup_path = None
        try:
            artifact = self.build_artifact(kind, row_id, address, connection_id)
            cleanup_path = artifact.get("cleanup_path") if isinstance(artifact, dict) else None
            self._send_artifact_file(address, connection_id, request_id, kind, artifact)
        except Exception as e:
            self._send_artifact_error(address, connection_id, request_id, str(e))
        finally:
            path_to_remove = cleanup_path or (artifact.get("cleanup_path") if isinstance(artifact, dict) else None)
            if path_to_remove:
                try:
                    os.remove(path_to_remove)
                except OSError:
                    pass

    def _send_artifact_query_response(self, address, connection_id, request_id, kind, query):
        artifact = None
        cleanup_path = None
        try:
            artifact = self.build_artifact_query(kind, query, address, connection_id)
            cleanup_path = artifact.get("cleanup_path") if isinstance(artifact, dict) else None
            self._send_artifact_file(address, connection_id, request_id, kind, artifact)
        except Exception as e:
            self._send_artifact_error(address, connection_id, request_id, str(e))
        finally:
            path_to_remove = cleanup_path or (artifact.get("cleanup_path") if isinstance(artifact, dict) else None)
            if path_to_remove:
                try:
                    os.remove(path_to_remove)
                except OSError:
                    pass

    @staticmethod
    def _hash_file(path):
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    def _send_artifact_error(self, address, connection_id, request_id, message, transfer_id=None):
        error_message = {
            "type": "artifact_error",
            "version": 1,
            "request_id": request_id,
            "message": str(message or "Artifact transfer failed"),
        }
        if transfer_id:
            error_message["transfer_id"] = transfer_id
        self._send_message_to_connection(address, connection_id, error_message)

    def _handle_artifact_transfer_message(self, address, connection_id, message):
        message_type = message.get("type")
        if message_type == "artifact_start":
            self._handle_artifact_start(address, connection_id, message)
        elif message_type == "artifact_chunk":
            self._handle_artifact_chunk(address, connection_id, message)
        elif message_type == "artifact_end":
            self._handle_artifact_end(address, connection_id, message)
        elif message_type == "artifact_error":
            self._handle_artifact_error_message(address, connection_id, message)

    def _handle_artifact_start(self, address, connection_id, message):
        transfer_id = str(message.get("transfer_id") or "").strip()
        request_id = str(message.get("request_id") or "").strip()
        if not transfer_id:
            return

        if transfer_id in self.incoming_artifact_transfers:
            self._cleanup_incoming_artifact(transfer_id)

        pending = self.pending_artifact_requests.get(request_id, {})
        kind = str(message.get("kind") or pending.get("kind") or "file").strip().lower()
        filename = sanitize_artifact_filename(
            message.get("filename"),
            fallback=f"{kind or 'artifact'}_{time.strftime('%Y%m%d-%H%M%S')}.bin",
        )

        try:
            total_bytes = int(message.get("total_bytes", 0))
        except (TypeError, ValueError):
            total_bytes = 0
        if total_bytes < 0 or total_bytes > self.max_artifact_bytes:
            self._notify_artifact_error(
                request_id,
                f"Remote artifact is too large: {total_bytes} bytes",
                address,
                connection_id,
            )
            return

        storage_dir = self._get_artifact_storage_dir(address, connection_id, kind)
        os.makedirs(storage_dir, exist_ok=True)
        final_path = self._get_unique_artifact_path(storage_dir, filename)
        part_path = final_path + ".part"
        try:
            with open(part_path, "wb"):
                pass
        except OSError as e:
            self._notify_artifact_error(request_id, f"Cannot create artifact file: {e}", address, connection_id)
            return

        self.incoming_artifact_transfers[transfer_id] = {
            "request_id": request_id,
            "kind": kind,
            "filename": filename,
            "total_bytes": total_bytes,
            "expected_sha256": str(message.get("sha256") or "").strip().lower(),
            "part_path": part_path,
            "final_path": final_path,
            "bytes_received": 0,
            "next_seq": 0,
            "hasher": hashlib.sha256(),
            "address": address,
            "connection_id": connection_id,
        }

    def _handle_artifact_chunk(self, address, connection_id, message):
        transfer_id = str(message.get("transfer_id") or "").strip()
        state = self.incoming_artifact_transfers.get(transfer_id)
        if state is None:
            return

        try:
            seq = int(message.get("seq"))
        except (TypeError, ValueError):
            self._fail_incoming_artifact(transfer_id, "Invalid artifact chunk sequence")
            return
        if seq != state["next_seq"]:
            self._fail_incoming_artifact(transfer_id, "Artifact chunks arrived out of order")
            return

        try:
            chunk = base64.b64decode(str(message.get("data_b64") or ""), validate=True)
        except (binascii.Error, ValueError) as e:
            self._fail_incoming_artifact(transfer_id, f"Invalid artifact chunk data: {e}")
            return

        new_size = state["bytes_received"] + len(chunk)
        if new_size > self.max_artifact_bytes or (
            state["total_bytes"] and new_size > state["total_bytes"]
        ):
            self._fail_incoming_artifact(transfer_id, "Artifact transfer exceeded expected size")
            return

        try:
            with open(state["part_path"], "ab") as handle:
                handle.write(chunk)
        except OSError as e:
            self._fail_incoming_artifact(transfer_id, f"Cannot write artifact chunk: {e}")
            return

        state["hasher"].update(chunk)
        state["bytes_received"] = new_size
        state["next_seq"] += 1

    def _handle_artifact_end(self, address, connection_id, message):
        transfer_id = str(message.get("transfer_id") or "").strip()
        state = self.incoming_artifact_transfers.get(transfer_id)
        if state is None:
            return

        if state["total_bytes"] and state["bytes_received"] != state["total_bytes"]:
            self._fail_incoming_artifact(transfer_id, "Artifact transfer ended before all bytes arrived")
            return

        expected_sha = str(message.get("sha256") or state["expected_sha256"] or "").strip().lower()
        actual_sha = state["hasher"].hexdigest()
        if expected_sha and expected_sha != actual_sha:
            self._fail_incoming_artifact(transfer_id, "Artifact sha256 mismatch")
            return

        try:
            os.replace(state["part_path"], state["final_path"])
        except OSError as e:
            self._fail_incoming_artifact(transfer_id, f"Cannot finalize artifact file: {e}")
            return

        self.incoming_artifact_transfers.pop(transfer_id, None)
        pending = self.pending_artifact_requests.pop(state["request_id"], {})
        metadata = {
            "request_id": state["request_id"],
            "transfer_id": transfer_id,
            "kind": state["kind"],
            "filename": state["filename"],
            "path": state["final_path"],
            "bytes": state["bytes_received"],
            "sha256": actual_sha,
            "address": state["address"],
            "connection_id": state["connection_id"],
            "open_after": bool(pending.get("open_after")),
            "notify": bool(pending.get("notify", True)),
        }
        self._append_artifact_manifest(metadata)
        if self.artifact_received is not None:
            self.main_window.after(0, lambda m=metadata: self.artifact_received(m))

    def _handle_artifact_error_message(self, address, connection_id, message):
        request_id = str(message.get("request_id") or "").strip()
        transfer_id = str(message.get("transfer_id") or "").strip()
        if transfer_id:
            self._cleanup_incoming_artifact(transfer_id)
        self._notify_artifact_error(
            request_id,
            str(message.get("message") or "Remote artifact transfer failed"),
            address,
            connection_id,
        )

    def _fail_incoming_artifact(self, transfer_id, message):
        state = self.incoming_artifact_transfers.get(transfer_id)
        request_id = state.get("request_id") if state else ""
        address = state.get("address") if state else ""
        connection_id = state.get("connection_id") if state else ""
        self._cleanup_incoming_artifact(transfer_id)
        self._notify_artifact_error(request_id, message, address, connection_id)

    def _cleanup_incoming_artifact(self, transfer_id):
        state = self.incoming_artifact_transfers.pop(transfer_id, None)
        if not state:
            return
        try:
            os.remove(state["part_path"])
        except OSError:
            pass

    def _cleanup_artifacts_for_connection(self, address, connection_id):
        for transfer_id, state in list(self.incoming_artifact_transfers.items()):
            if state.get("address") == address and state.get("connection_id") == connection_id:
                self._fail_incoming_artifact(transfer_id, "Connection closed during artifact transfer")

        for request_id, pending in list(self.pending_artifact_requests.items()):
            if pending.get("address") == address and pending.get("connection_id") == connection_id:
                self._notify_artifact_error(
                    request_id,
                    "Connection closed during artifact transfer",
                    address,
                    connection_id,
                )

    def _notify_artifact_error(self, request_id, message, address, connection_id):
        pending = self.pending_artifact_requests.pop(request_id, {}) if request_id else {}
        metadata = {
            "request_id": request_id,
            "message": str(message or "Artifact transfer failed"),
            "address": address,
            "connection_id": connection_id,
            "kind": pending.get("kind", ""),
            "notify": bool(pending.get("notify", True)),
        }
        if self.is_shutting_down:
            return
        if self.artifact_error is not None:
            self.main_window.after(0, lambda m=metadata: self.artifact_error(m))
        elif metadata["notify"] and not self.is_shutting_down:
            print(f"Artifact error: {metadata['message']}")

    def _get_connection_port(self, address, connection_id):
        with self.clients_lock:
            client = self.clients.get(address, {}).get(connection_id)
            if client is not None:
                return client.get("port")
        return None

    def _get_artifact_storage_dir(self, address, connection_id, kind):
        port = self._get_connection_port(address, connection_id)
        host_component = sanitize_storage_component(address)
        port_component = sanitize_storage_component(port or self.current_port or "unknown")
        host_dir = f"{host_component}_{port_component}"
        kind_dir = ARTIFACT_KIND_DIRS.get(kind, "files")
        return os.path.join(self.artifact_root, host_dir, kind_dir)

    @staticmethod
    def _get_unique_artifact_path(folder, filename):
        base_path = os.path.join(folder, filename)
        if not os.path.exists(base_path) and not os.path.exists(base_path + ".part"):
            return base_path

        stem, ext = os.path.splitext(filename)
        for idx in range(2, 10000):
            candidate_name = f"{stem} ({idx}){ext}"
            candidate_path = os.path.join(folder, candidate_name)
            if not os.path.exists(candidate_path) and not os.path.exists(candidate_path + ".part"):
                return candidate_path
        raise RuntimeError("Cannot allocate artifact filename")

    def _append_artifact_manifest(self, metadata):
        manifest_dir = os.path.dirname(os.path.dirname(metadata["path"]))
        manifest_path = os.path.join(manifest_dir, "manifest.jsonl")
        record = dict(metadata)
        record["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(manifest_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError as e:
            if not self.is_shutting_down:
                print(f"Error writing artifact manifest: {e}")

    def _recv_exact(self, socket_obj, size):
        data = bytearray()
        while len(data) < size:
            chunk = socket_obj.recv(size - len(data))
            if not chunk:
                if not data:
                    return None
                raise ConnectionError("Connection closed before payload was fully received")
            data.extend(chunk)
        return bytes(data)
