"""Client RFC6455 stdlib, Python 3.7+, limites strictes, aucune API du jeu."""
import base64
import hashlib
import hmac
import ipaddress
import os
import socket
import struct
import threading
import time
from urllib.parse import urlsplit

# getaddrinfo importe ce codec à la première résolution ; importé depuis plusieurs threads à la fois,
# Python 3.7 lève « LookupError: unknown encoding: idna ». Chargé ici, sur le thread d'import.
import encodings.idna  # noqa: F401,E402

try:
    import ssl
except ImportError:
    # Le Python embarqué des Sims 4 n'a pas _ssl : seul ws:// (LAN) est utilisable en jeu.
    ssl = None


class TlsUnavailable(RuntimeError):
    """wss:// demandé alors que le runtime n'a pas de module ssl."""


class ProtocolError(ValueError):
    pass


def is_lan(host):
    try:
        address = ipaddress.ip_address(host)
        return address.version == 4 and any(address in ipaddress.ip_network(net)
            for net in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
    except ValueError:
        return False

def validate_endpoint(url, allow_lan=False):
    try:
        parsed = urlsplit(url)
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == 'wss' else 80)
    except (ValueError, TypeError):
        raise ValueError('Adresse serveur invalide')
    if parsed.scheme not in ('ws', 'wss') or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('Adresse serveur invalide')
    if any(c in url for c in ('\r', '\n', '\x00')) or not 1 <= port <= 65535:
        raise ValueError('Adresse serveur invalide')
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname.lower() == 'localhost'
    if parsed.scheme == 'ws' and not loopback and not (allow_lan and is_lan(parsed.hostname)):
        raise ValueError('WSS requis hors loopback')
    return parsed, port


class WebSocket:
    def __init__(self, sock, initial=b'', max_message=65536):
        self.socket = sock
        self.buffer = bytearray(initial)
        self.max_message = max_message
        self.send_lock = threading.Lock()
        self.closed = False
        self.receive_deadline = None

    @classmethod
    def connect(cls, url, timeout=5.0, allow_lan=False):
        parsed, port = validate_endpoint(url, allow_lan=allow_lan)
        sock = socket.create_connection((parsed.hostname, port), timeout)
        try:
            if parsed.scheme == 'ws':
                # Même localhost doit effectivement aboutir à une adresse locale.
                peer = sock.getpeername()[0]
                if not ipaddress.ip_address(peer).is_loopback and not (allow_lan and is_lan(peer)):
                    raise ValueError('Connexion non locale refusée')
            else:
                if ssl is None:
                    raise TlsUnavailable('wss indisponible dans ce runtime Python')
                context = ssl.create_default_context()
                sock = context.wrap_socket(sock, server_hostname=parsed.hostname)
            sock.settimeout(timeout)
            key = base64.b64encode(os.urandom(16)).decode('ascii')
            host = parsed.hostname
            if ':' in host:
                host = '[' + host + ']'
            path = parsed.path or '/'
            if parsed.query:
                path += '?' + parsed.query
            if any(ord(c) < 33 or ord(c) > 126 for c in path):
                raise ValueError('Chemin serveur invalide')
            request = ('GET {} HTTP/1.1\r\nHost: {}:{}\r\nUpgrade: websocket\r\n'
                       'Connection: Upgrade\r\nSec-WebSocket-Key: {}\r\n'
                       'Sec-WebSocket-Version: 13\r\n\r\n').format(path, host, port, key)
            sock.sendall(request.encode('ascii'))
            received = bytearray()
            deadline = time.monotonic() + timeout
            while b'\r\n\r\n' not in received:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Upgrade dépassé')
                chunk = sock.recv(4096)
                if not chunk:
                    raise EOFError('Connexion interrompue')
                received.extend(chunk)
                if len(received) > 16384:
                    raise ProtocolError('En-tête serveur trop grand')
            raw, extra = bytes(received).split(b'\r\n\r\n', 1)
            lines = raw.decode('ascii').split('\r\n')
            if lines[0].split(' ')[:2] != ['HTTP/1.1', '101']:
                raise ProtocolError('Upgrade WebSocket refusé')
            headers = {}
            for line in lines[1:]:
                name, value = line.split(':', 1)
                name = name.strip().lower()
                if name in headers:
                    raise ProtocolError('En-tête serveur dupliqué')
                headers[name] = value.strip()
            expected = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode('ascii')).digest()).decode('ascii')
            if (headers.get('upgrade', '').lower() != 'websocket' or
                    'upgrade' not in [x.strip().lower() for x in headers.get('connection', '').split(',')] or
                    not hmac.compare_digest(headers.get('sec-websocket-accept', ''), expected) or
                    'sec-websocket-extensions' in headers or 'sec-websocket-protocol' in headers):
                raise ProtocolError('Validation WebSocket échouée')
            return cls(sock, initial=extra)
        except Exception:
            sock.close()
            raise

    def _exact(self, count):
        while len(self.buffer) < count:
            if self.receive_deadline is not None and time.monotonic() >= self.receive_deadline:
                raise TimeoutError('Réception dépassée')
            data = self.socket.recv(min(8192, count - len(self.buffer)))
            if not data:
                raise EOFError('Connexion interrompue')
            self.buffer.extend(data)
        data = bytes(self.buffer[:count])
        del self.buffer[:count]
        return data

    def _send(self, opcode, payload):
        if len(payload) > self.max_message:
            raise ProtocolError('Message sortant trop grand')
        if opcode >= 8 and len(payload) > 125:
            raise ProtocolError('Contrôle trop grand')
        n = len(payload)
        prefix = bytes([128 | opcode])
        if n < 126:
            prefix += bytes([128 | n])
        elif n < 65536:
            prefix += b'\xfe' + struct.pack('!H', n)
        else:
            prefix += b'\xff' + struct.pack('!Q', n)
        mask = os.urandom(4)
        data = prefix + mask + bytes(x ^ mask[i % 4] for i, x in enumerate(payload))
        with self.send_lock:
            if self.closed:
                raise EOFError('Connexion fermée')
            self.socket.sendall(data)

    def send_text(self, text):
        self._send(1, text.encode('utf-8'))

    def receive_text(self):
        fragments = bytearray()
        active = False
        self.receive_deadline = time.monotonic() + 5.0
        frame_count = 0
        while True:
            frame_count += 1
            if frame_count > 128:
                raise ProtocolError('Trop de fragments ou contrôles')
            first, second = self._exact(2)
            fin, opcode = bool(first & 128), first & 15
            if first & 112 or second & 128:
                raise ProtocolError('Frame serveur invalide')
            length = second & 127
            marker = length
            if length == 126:
                length = struct.unpack('!H', self._exact(2))[0]
                if length < 126:
                    raise ProtocolError('Longueur non canonique')
            elif length == 127:
                length = struct.unpack('!Q', self._exact(8))[0]
                if length < 65536 or length & (1 << 63):
                    raise ProtocolError('Longueur non canonique')
            if opcode >= 8 and (not fin or length > 125 or marker >= 126):
                raise ProtocolError('Contrôle invalide')
            if length > self.max_message or len(fragments) + length > self.max_message:
                raise ProtocolError('Message entrant trop grand')
            payload = self._exact(length)
            if opcode == 8:
                if length == 1:
                    raise ProtocolError('Fermeture invalide')
                if length >= 2:
                    code = struct.unpack('!H', payload[:2])[0]
                    if code < 1000 or code >= 5000 or code in (1004, 1005, 1006, 1015) or 1016 <= code < 3000:
                        raise ProtocolError('Code fermeture invalide')
                    try:
                        payload[2:].decode('utf-8')
                    except UnicodeDecodeError:
                        raise ProtocolError('Motif fermeture invalide')
                self._send(8, payload)
                self.close()
                raise EOFError('Serveur déconnecté')
            if opcode == 9:
                self._send(10, payload)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 1) or (opcode == 0 and not active) or (opcode == 1 and active):
                raise ProtocolError('Fragmentation invalide')
            active = True
            fragments.extend(payload)
            if fin:
                try:
                    return fragments.decode('utf-8')
                except UnicodeDecodeError:
                    raise ProtocolError('UTF-8 invalide')

    def close(self):
        self.closed = True
        self.socket.close()
