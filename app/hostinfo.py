"""Best-effort local network address, so the launcher can print a URL that
works from a phone on the same wi-fi.

    python -m app.hostinfo

Prints the address or nothing at all; never raises, because it only exists to
make a startup message friendlier.
"""
import socket


def lan_ip() -> str:
    """This machine's address on the local network, or '' if undetermined."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.settimeout(0.4)
            # A UDP "connect" sends nothing; it just picks the outbound
            # interface, which is the address a phone on the LAN would use.
            sock.connect(("10.255.255.255", 1))
            ip = sock.getsockname()[0]
        finally:
            sock.close()
    except OSError:
        return ""
    return "" if ip.startswith("127.") else ip


if __name__ == "__main__":
    print(lan_ip())
