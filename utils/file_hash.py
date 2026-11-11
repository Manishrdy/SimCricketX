"""SHA-256 of an open binary stream; hashlib.file_digest needs Python 3.11, production runs 3.8."""
import hashlib


def sha256_hexdigest(stream):
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
        digest.update(chunk)
    return digest.hexdigest()
