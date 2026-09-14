import asyncio
import datetime
import ipaddress
import ssl
import threading

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


class MemoryVault:
    def __init__(self):
        self.value = None

    def load(self):
        return dict(self.value) if self.value else None

    def save(self, value):
        self.value = dict(value)

    def delete(self):
        self.value = None


class FakeInjector:
    """Test-only sink. Never used by a production launcher."""
    def __init__(self):
        self.events = []
        self.permission = True
        self.lock = threading.Lock()

    def permitted(self):
        return self.permission

    def record(self, *value):
        with self.lock:
            self.events.append(value)

    def key(self, key, down):
        self.record("key", key, down)

    def button(self, button, down):
        self.record("button", button, down)

    def move(self, x, y):
        self.record("move", x, y)

    def scroll(self, x, y):
        self.record("scroll", x, y)


def tls_contexts(directory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"),
                           x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert_path, key_path)
    client = ssl.create_default_context(cafile=str(cert_path))
    return server, client


async def eventually(condition, seconds=2):
    async with asyncio.timeout(seconds):
        while not condition():
            await asyncio.sleep(.01)

