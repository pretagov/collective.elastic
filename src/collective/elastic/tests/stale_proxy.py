"""A TCP proxy in front of elasticsearch that can break connections the way a
suspended and resumed host sees them."""

import select
import socket
import struct
import threading
import time


class StaleProxy:
    def __init__(self, target=("127.0.0.1", 9200)):
        self.target = target
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(32)
        self.url = f"http://127.0.0.1:{self.listener.getsockname()[1]}"
        self.pairs = []
        self.lock = threading.Lock()
        self.outage_until = 0
        self.connections = 0
        self.threads = [threading.Thread(target=self._accept, daemon=True)]
        self.threads[0].start()

    def _accept(self):
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            if time.time() < self.outage_until:
                self._reset(client)
                continue
            upstream = socket.create_connection(self.target)
            pair = [client, upstream, "live"]
            with self.lock:
                self.pairs.append(pair)
                self.connections += 1
            thread = threading.Thread(target=self._pump, args=(pair,), daemon=True)
            self.threads.append(thread)
            thread.start()

    @staticmethod
    def _reset(sock):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        sock.close()

    def _pump(self, pair):
        client, upstream, _ = pair
        while True:
            try:
                ready, _, _ = select.select([client, upstream], [], [], 0.1)
            except (OSError, ValueError):
                return
            if pair[2] == "reset":
                self._reset(client)
                upstream.close()
                return
            for sock in ready:
                try:
                    data = sock.recv(65536)
                except OSError:
                    data = b""
                if not data:
                    client.close()
                    upstream.close()
                    return
                if pair[2] == "blackhole":
                    # The peer is gone without a FIN or RST: nothing comes back
                    continue
                (upstream if sock is client else client).sendall(data)

    def make_existing_stale(self, mode="blackhole"):
        """Existing connections stop answering ("blackhole") or are reset."""
        with self.lock:
            for pair in self.pairs:
                if pair[2] == "live":
                    pair[2] = mode

    def outage(self, seconds):
        """Existing connections go silent and new ones are refused for a while."""
        self.make_existing_stale("blackhole")
        self.outage_until = time.time() + seconds

    def close(self):
        try:
            # Wakes up the thread blocked in accept()
            self.listener.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.listener.close()
        with self.lock:
            for client, upstream, _ in self.pairs:
                client.close()
                upstream.close()
        for thread in self.threads:
            thread.join(timeout=5)
